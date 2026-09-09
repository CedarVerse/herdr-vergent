"""Shim arg-surface pins (bin/vergent.sh).

The shim's own contract is bash, not Python: argument errors must exit 2
with usage on stderr BEFORE any stateful machinery runs (path
canonicalization, flock, the systemd no-lazy-spawn guard, the interpreter
probe). These tests subprocess only that early window, so they are
hermetic on any machine -- no herdr, no systemctl, no flock involvement.

Deliberately NOT tested here (environment-dependent, would need bats-level
fixture work): the lock-busy exit 3, the no-lazy-spawn guard, the
interpreter-probe fallback chain, and the exec handoff to the seeder --
the seeder side of that handoff is covered end to end by test_main.py.
"""

import pathlib
import subprocess
import unittest

SHIM = pathlib.Path(__file__).resolve().parents[1] / "bin" / "vergent.sh"
STARTER = pathlib.Path(__file__).resolve().parents[1] / "projects.starter.toml"


class ShimCase(unittest.TestCase):
    def run_shim(self, *args):
        return subprocess.run(
            ["bash", str(SHIM), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )  # return code is the contract under test

    def test_unknown_arg_exits_2_with_usage(self):
        """An unrecognized flag is a usage error (exit 2, like the seeder's
        validation exit), not a silent fallthrough into a partial run."""
        r = self.run_shim("--bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)

    def test_toml_without_value_exits_2_with_usage(self):
        """--toml as the last argument has no value to take: usage error,
        exit 2 -- and crucially BEFORE readlink -f would run (set -e would
        otherwise kill the run with a bare exit 1 and no message)."""
        r = self.run_shim("--toml")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)

    def test_missing_toml_parent_exits_2_with_message(self):
        """readlink -f fails SILENTLY (exit 1, no output) when the --toml
        path's parent directory does not exist -- before the guard, a set -e
        run died with a bare exit 1 that looked like a crash. The guard
        maps it onto the validation contract: exit 2 with a message naming
        the unresolved path. Still hermetic: this fires before flock, the
        systemd guard, and the interpreter probe."""
        r = self.run_shim("--toml", "/nonexistent-parent/dir/projects.toml")
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot resolve TOML path:", r.stderr)
        self.assertIn("/nonexistent-parent/dir/projects.toml", r.stderr)

    # --- first-run starter (2026-09-09): absent config -> starter+welcome ----

    def setUp(self):
        import tempfile

        self.tmp = tempfile.mkdtemp()

    def _dead_socket_env(self):
        """The shim's later stages (guard, probe, exec) may run in these
        tests; HERDR_SOCKET_PATH pointed at nothing makes the exec'd python
        fail harmlessly at connect -- AFTER the starter logic under test."""
        import os

        env = dict(os.environ)
        env["HERDR_SOCKET_PATH"] = str(pathlib.Path(self.tmp) / "no-such.sock")
        return env

    def test_first_run_writes_starter_with_absolute_path(self):
        """Absent DEFAULT config (no --toml; HERDR_PLUGIN_CONFIG_DIR set, the
        plugin-runtime shape): the shim writes the starter under the config
        dir with __VERGENT_CONFIG_PATH__ substituted to the RESOLVED
        absolute path -- the welcome pane's message must name the real
        file. The run then continues (exec fails at the dead socket; that
        is expected and asserted to be the only failure)."""
        cfgdir = pathlib.Path(self.tmp) / "cfg"
        env = self._dead_socket_env()
        env["HERDR_PLUGIN_CONFIG_DIR"] = str(cfgdir)
        toml = cfgdir / "projects.toml"
        r = subprocess.run(
            ["bash", str(SHIM)],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=False,
        )  # return code is the contract under test
        self.assertTrue(toml.exists(), "starter was not written")
        self.assertIn("starter config written to", r.stdout)
        body = toml.read_text()
        self.assertNotIn("__VERGENT_CONFIG_PATH__", body)  # substituted
        self.assertIn(str(toml), body)  # absolute path present
        self.assertEqual(body.count(str(toml)), 3)  # comment x2 + pane command
        self.assertIn("vergent-start-here", body)  # the welcome Space

    def test_existing_config_is_never_overwritten(self):
        """The starter is gated on absence: an existing file (even a
        sentinel one-liner) must survive byte-for-byte -- first-run logic
        that clobbers user data would be worse than no onboarding."""
        toml = pathlib.Path(self.tmp) / "projects.toml"
        toml.write_text('sentinel = "mine"\n')
        r = subprocess.run(
            ["bash", str(SHIM), "--toml", str(toml)],
            capture_output=True,
            text=True,
            timeout=60,
            env=self._dead_socket_env(),
            check=False,
        )  # return code is the contract under test
        self.assertEqual(toml.read_text(), 'sentinel = "mine"\n')
        self.assertNotIn("starter config written", r.stdout)

    def test_starter_template_loads_through_real_loader(self):
        """The starter must be valid schema v3 the moment it is written:
        one workspace, one tab, one pane carrying the welcome command (the
        command one-shot delivers it exactly once -- no new machinery)."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "v_starter_test", str(SHIM.parents[1] / "bin" / "vergent.py")
        )
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        body = STARTER.read_text().replace(
            "__VERGENT_CONFIG_PATH__", str(pathlib.Path(self.tmp) / "p.toml")
        )
        target = pathlib.Path(self.tmp) / "resolved-starter.toml"
        target.write_text(body)
        model = m.load_model(str(target))
        (ws,) = model["workspaces"]
        (tab,) = ws["tabs"]
        (pane,) = tab["panes"]
        self.assertEqual(
            (ws["label"], tab["label"], pane["name"]),
            ("vergent-start-here", "read me", "welcome"),
        )
        # the welcome names the config file it cats (substitution applied)
        self.assertIn(str(pathlib.Path(self.tmp) / "p.toml"), pane["command"])


if __name__ == "__main__":
    unittest.main()
