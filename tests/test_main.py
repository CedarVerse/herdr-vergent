"""End-to-end main(): loader -> socket -> reconcile -> summary -> exit code.

Drives the REAL main() against the FAKE server via HERDR_SOCKET_PATH (the
same env var a non-default install uses), so the whole pipe is exercised --
TOML file on disk included -- and exit codes are asserted as the CLI
contract states: 0 iff failed=0 AND skipped=0 (a skip means herdr does not
match the file: that must show up in shell status, not just the log),
1 otherwise, 2 for TOML validation before any socket traffic.

The dry-run tests are the load-bearing ones: they pin that ONLY the three
read-only list calls reach the server while every mutation is recorded on
the DryRunSocket wrapper -- checked both through main()'s printed plan
count and through a direct DryRunSocket run that can assert on .planned.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import harness


class MainCase(unittest.TestCase):
    def setUp(self):
        self.m, self.fake, self.client = harness.make_env(self)
        # Route main()'s own HerdrSocket() at the fake, the same way a real
        # non-default install would -- no monkeypatching of the class needed.
        self._saved = os.environ.get("HERDR_SOCKET_PATH")
        os.environ["HERDR_SOCKET_PATH"] = self.fake.path
        self.addCleanup(self._restore_env)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.real = os.path.realpath(self.tmp)  # symlink-safe for TOML paths
        os.makedirs(os.path.join(self.real, "repo"))
        self.mark = 0

    def _restore_env(self):
        if self._saved is None:
            os.environ.pop("HERDR_SOCKET_PATH", None)
        else:
            os.environ["HERDR_SOCKET_PATH"] = self._saved

    # --- fixtures ------------------------------------------------------------

    def write_toml(self, body):
        path = os.path.join(self.real, "projects.toml")
        with open(path, "w") as f:
            f.write(body)
        return path

    def fresh_toml(self):
        """One fresh workspace/tab, pane a (agent) + pane b (command): the
        everything-to-create shape, worth created=2 and 4 planned mutations."""
        return self.write_toml(
            f'[[workspace]]\nname = "W"\npath = "{self.real}"\n\n'
            '[[workspace.tab]]\nname = "T"\npath = "repo"\n'
            '[[workspace.tab.pane]]\nname = "a"\nagent = "claude"\n'
            '[[workspace.tab.pane]]\nname = "b"\ndirection = "right"\ncommand = "htop"\n'
        )

    def run_main(self, argv):
        """main() with captured streams; returns (exit_code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.m.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def methods(self):
        return [m for m, _ in self.fake.calls]

    # --- dry-run containment ---------------------------------------------------

    def test_dry_run_plans_without_forwarding(self):
        """Fresh-state dry-run: exit 0, the server sees ONLY the read-only
        snapshot list, its state is untouched, and the plan is announced."""
        rc, out, err = self.run_main(["--toml", self.fresh_toml(), "--dry-run"])
        self.assertEqual(rc, self.m.EXIT_OK)
        self.assertIn("created=2 adopted=0 matched=0 skipped=0 failed=0", out)
        self.assertRegex(out, r"\[dry-run\] [1-9]\d* mutations planned")
        self.assertEqual(self.methods(), ["workspace.list"])
        self.assertEqual(
            (self.fake.workspaces, self.fake.tabs, self.fake.panes), ({}, {}, {})
        )
        self.assertEqual(err, "")

    def test_dry_run_socket_records_plan_and_overlays(self):
        """The DryRunSocket itself: reconcile over it records exactly the
        expected mutation sequence in .planned, forwards only the snapshot
        list, and -- because create_tab re-resolves the replacement tab by
        label and one_shots re-list panes -- completing the walk at all
        proves the overlay answers are coherent."""
        dry = self.m.DryRunSocket(self.client)
        lines = []
        counters = self.m.reconcile(
            dry, self.m.load_model(self.fresh_toml()), lines.append
        )
        self.assertEqual(
            counters,
            {"created": 2, "adopted": 0, "matched": 0, "skipped": 0, "failed": 0},
        )
        self.assertEqual(
            [m for m, _ in dry.planned],
            [
                "workspace.create",
                "layout.apply",
                "agent.start",
                "pane.send_text",
                "pane.send_keys",
            ],
        )
        self.assertEqual(self.methods(), ["workspace.list"])

    def test_dry_run_overlays_planned_tab_into_existing_workspace(self):
        """The overlay's trickiest half: a planned tab inside a REAL
        workspace must appear in scoped tab.list answers (pass-through plus
        planned), or the walk could never see its own creations there.
        Driven through an explicit DryRunSocket so the overlay is probeable
        after the walk (main()'s wrapper is internal)."""
        r = self.client.call("workspace.create", {"label": "W", "cwd": self.real})
        ws_id, root_tab = r["workspace"]["workspace_id"], r["tab"]["tab_id"]
        self.client.call("tab.rename", {"tab_id": root_tab, "label": "T"})
        self.mark = len(self.fake.calls)
        toml = self.write_toml(
            f'[[workspace]]\nname = "W"\npath = "{self.real}"\n\n'
            '[[workspace.tab]]\nname = "extra"\npath = "repo"\n'
        )
        dry = self.m.DryRunSocket(self.client)
        counters = self.m.reconcile(dry, self.m.load_model(toml), lambda _m: None)
        self.assertEqual(
            counters,
            {"created": 1, "adopted": 0, "matched": 0, "skipped": 0, "failed": 0},
        )
        # pane-less tab -> tab.create is PLANNED (not forwarded); the walk
        # still re-lists panes afterwards, so the server sees lists only
        self.assertEqual(
            self.methods()[self.mark :],
            ["workspace.list", "tab.list", "pane.list", "pane.list"],
        )
        # scoped list = live tabs + the planned one, pass-through preserved
        tabs = dry.call("tab.list", {"workspace_id": ws_id})["tabs"]
        self.assertEqual([t["label"] for t in tabs], ["T", "extra"])

    # --- live mode -------------------------------------------------------------

    def test_live_run_forwards_mutations_and_prints_summary(self):
        """No --dry-run: the same TOML actually lands (workspace + applied
        tab + one-shots server-side), the summary is printed, and no
        dry-run banner appears."""
        rc, out, err = self.run_main(["--toml", self.fresh_toml()])
        self.assertEqual(rc, self.m.EXIT_OK)
        self.assertIn("created=2 adopted=0 matched=0 skipped=0 failed=0", out)
        self.assertNotIn("[dry-run]", out)
        self.assertEqual(err, "")
        self.assertEqual(
            self.methods()[:4],
            ["workspace.list", "workspace.create", "layout.apply", "tab.list"],
        )
        panes = {
            p["label"]: p for p in self.client.call("pane.list", {}).get("panes", [])
        }
        self.assertEqual(panes["a"]["agent"], "claude")
        self.assertEqual(
            self.fake.panes[panes["b"]["pane_id"]].get("typed"), ["htop", "Enter"]
        )

    # --- exit codes ------------------------------------------------------------

    def test_validation_failure_exits_2_before_any_socket_traffic(self):
        toml = self.write_toml('[[workspace]]\nname = ""\n')
        rc, _out, err = self.run_main(["--toml", toml])
        self.assertEqual(rc, self.m.EXIT_VALIDATION)
        self.assertIn("validation error:", err)
        self.assertEqual(self.fake.calls, [])  # aborted before the socket

    def test_transport_error_exits_1(self):
        """An unreachable socket aborts the run with a shaped error: exit 1,
        "error:" on stderr, and NO summary line -- a run that never walked
        must not print counters that could be read as a result."""
        os.environ["HERDR_SOCKET_PATH"] = os.path.join(self.real, "absent.sock")
        rc, out, err = self.run_main(["--toml", self.fresh_toml(), "--dry-run"])
        self.assertEqual(rc, self.m.EXIT_FAILED)
        self.assertIn("error:", err)
        self.assertNotIn("created=", out)

    def test_matched_only_run_exits_0(self):
        """herdr already matches the file exactly: exit 0, matched=1, and
        the server sees read-only lists only."""
        repo = os.path.join(self.real, "repo")
        r = self.client.call("workspace.create", {"label": "W", "cwd": repo})
        self.client.call("tab.rename", {"tab_id": r["tab"]["tab_id"], "label": "T"})
        self.client.call(
            "pane.rename", {"pane_id": r["root_pane"]["pane_id"], "label": "shell"}
        )
        self.mark = len(self.fake.calls)
        toml = self.write_toml(
            f'[[workspace]]\nname = "W"\npath = "{self.real}"\n\n'
            '[[workspace.tab]]\nname = "T"\npath = "repo"\n'
            '[[workspace.tab.pane]]\nname = "shell"\n'
        )
        rc, out, err = self.run_main(["--toml", toml])
        self.assertEqual(rc, self.m.EXIT_OK)
        self.assertIn("created=0 adopted=0 matched=1 skipped=0 failed=0", out)
        self.assertEqual(err, "")
        # snapshot lists + the one panes_of the pre-existing walk costs
        self.assertEqual(
            self.methods()[self.mark :],
            ["workspace.list", "tab.list", "pane.list", "pane.list"],
        )

    def test_skipped_tab_exits_1(self):
        """skipped=0 AND failed=0 is the only clean exit: a tab whose path
        does not exist is skipped -> exit 1 even though nothing failed."""
        toml = self.write_toml(
            f'[[workspace]]\nname = "W"\npath = "{self.real}"\n\n'
            '[[workspace.tab]]\nname = "Ghost"\npath = "repo/missing"\n'
            '[[workspace.tab.pane]]\nname = "a"\n'
        )
        rc, out, _err = self.run_main(["--toml", toml])
        self.assertEqual(rc, self.m.EXIT_FAILED)
        self.assertIn("created=1 adopted=0 matched=0 skipped=1 failed=0", out)
        self.assertIn("skipped (resolved path does not exist", out)

    def test_missing_toml_exits_2_before_socket(self):
        """A --toml path that cannot be opened is a VALIDATION exit (2)
        before any socket traffic -- the OSError branch of load_model feeds
        the same contract as a bad document (main's mapping, not just the
        loader's exception type)."""
        rc, _out, err = self.run_main(
            ["--toml", os.path.join(self.real, "absent.toml")]
        )
        self.assertEqual(rc, self.m.EXIT_VALIDATION)
        self.assertIn("validation error:", err)
        self.assertIn("cannot read TOML", err)
        self.assertEqual(self.fake.calls, [])

    def test_workspace_without_path_creates_without_cwd(self):
        """A workspace with no declared path creates WITHOUT a cwd key --
        sending cwd: null is unprobed against the live server, so omission
        is the schema-clean 'no path'. The tab still carries its own
        absolute path and the run lands clean."""
        toml = self.write_toml(
            '[[workspace]]\nname = "W"\n\n'
            f'[[workspace.tab]]\nname = "T"\npath = "{self.real}"\n'
            '[[workspace.tab.pane]]\nname = "a"\n'
        )
        rc, _out, _err = self.run_main(["--toml", toml])
        self.assertEqual(rc, self.m.EXIT_OK)
        create = next(p for m, p in self.fake.calls if m == "workspace.create")
        self.assertNotIn("cwd", create)
        self.assertEqual(create["label"], "W")

    # --- dry-run overlay: the branches the fresh-TOML tests cannot reach -----

    def test_dry_run_paneless_first_tab_plans_fresh_create(self):
        """Dry-run, tab #1 pane-less on a FRESH workspace: the planned
        workspace's implicit root pane is INVISIBLE to create_tab's cwd
        probe (it exists only in the overlay -- pane.list for a planned
        workspace never reaches the server), so the plan conservatively
        shows tab.create at the DECLARED path. The live run re-probes the
        real root pane and rename-adopts when the cwds match; the plan's
        mutation count is identical either way, and the planned cwd is the
        honest one (what the tab will really get).
        """
        toml = self.write_toml(
            f'[[workspace]]\nname = "W"\npath = "{self.real}"\n\n'
            '[[workspace.tab]]\nname = "T"\npath = "repo"\n'
        )
        dry = self.m.DryRunSocket(self.client)
        counters = self.m.reconcile(dry, self.m.load_model(toml), lambda _m: None)
        # fresh flow counts both the workspace and its tab as created
        self.assertEqual(
            counters,
            {"created": 2, "adopted": 0, "matched": 0, "skipped": 0, "failed": 0},
        )
        self.assertEqual(
            [m for m, _ in dry.planned], ["workspace.create", "tab.create"]
        )
        planned = dict(dry.planned)["tab.create"]
        self.assertEqual(planned["cwd"], os.path.join(self.real, "repo"))
        # overlay coherence: the planned tab is probeable under its label
        # (beside the label-less implicit twin the planned workspace
        # carries), and the server saw read-only traffic only
        self.assertEqual(
            [t["label"] for t in dry.dry_tabs.values() if t["label"]], ["T"]
        )
        self.assertEqual(self.methods(), ["workspace.list"])

    def test_dry_run_records_unknown_method_without_forwarding(self):
        """Fail-safe: a mutation the overlay does not know is still
        RECORDED on the plan (never silently forwarded to the live server)
        and answered from the generic fresh-pane shape."""
        dry = self.m.DryRunSocket(self.client)
        r = dry.call("future.method", {"x": 1})
        self.assertIn(("future.method", {"x": 1}), dry.planned)
        self.assertEqual(self.fake.calls, [])  # nothing reached the server
        self.assertIn("pane_id", r["pane"])

    def test_dry_run_apply_onto_real_tab_hides_it_from_lists(self):
        """layout.apply aimed at a REAL tab id: the overlay marks it
        replaced (server-side it is closed the moment the plan executes),
        so later list answers hide the old tab AND its panes while showing
        the planned replacement with the real workspace inherited."""
        r = self.client.call("workspace.create", {"label": "W", "cwd": self.real})
        ws_id, root_tab = r["workspace"]["workspace_id"], r["tab"]["tab_id"]
        self.client.call(
            "pane.rename", {"pane_id": r["root_pane"]["pane_id"], "label": "old"}
        )
        dry = self.m.DryRunSocket(self.client)
        dry.call(
            "layout.apply",
            {
                "tab_id": root_tab,
                "tab_label": "new",
                "root": {"type": "pane", "label": "fresh", "cwd": self.real},
            },
        )
        self.assertIn(root_tab, dry.replaced_real)
        tabs = dry.call("tab.list", {"workspace_id": ws_id})["tabs"]
        self.assertEqual([t["label"] for t in tabs], ["new"])
        panes = dry.call("pane.list", {"workspace_id": ws_id})["panes"]
        self.assertEqual([p["label"] for p in panes], ["fresh"])


class DirectExecCase(unittest.TestCase):
    """Codex (2026-09-08): on python >= 3.11 the tomllib guard at the top of
    the script falls through, so direct `python vergent.py` used to
    run the reconciler with no single-flight flock and no no-lazy-spawn
    guard. The __main__ guard now refuses unless the shim vouches via
    HERDR_VERGENT_VIA_SHIM. These tests subprocess the script the way a
    user would -- and, in the vouching case, the way the shim does.
    """

    @classmethod
    def setUpClass(cls):
        cls.script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "bin",
            "vergent.py",
        )

    def test_direct_execution_refused(self):
        env = {k: v for k, v in os.environ.items() if k != "HERDR_VERGENT_VIA_SHIM"}
        r = subprocess.run(
            [sys.executable, self.script],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )  # return code is the contract under test
        self.assertEqual(r.returncode, 2)
        self.assertIn("direct execution refused", r.stderr)

    def test_shim_vouching_lets_execution_proceed(self):
        # Malformed TOML: the guard let execution reach the loader, whose
        # validation abort is the exit-2 shape pinnable WITHOUT a socket --
        # distinguished by stderr content, since both paths exit 2.
        with tempfile.TemporaryDirectory() as td:
            bad = os.path.join(td, "bad.toml")
            with open(bad, "w") as f:
                f.write("this is not = = toml")
            r = subprocess.run(
                [sys.executable, self.script, "--toml", bad, "--dry-run"],
                env={**os.environ, "HERDR_VERGENT_VIA_SHIM": "1"},
                capture_output=True,
                text=True,
                check=False,
            )  # return code is the contract under test
        self.assertNotIn("direct execution refused", r.stderr)
        self.assertIn("validation error", r.stderr)


if __name__ == "__main__":
    unittest.main()
