"""Reconcile walk + one-shot effects (bin/vergent.reconcile).

Fake-based, one scenario per test -- the 9 from the plan's Task 6, each
pinning counters AND fake state. Seeding happens through the same client the
seeder uses in production, so every test asserts on the CALL WINDOW after
seeding (run_reconcile() re-marks fake.calls before reconcile starts):
fixture traffic and reconcile traffic never mix.

Scenario numbering: the "--- scenario N ---" markers below are FILE-LOCAL
positions; the plan's Task 6 Step 1 list maps onto tests as
1->test_matched_only_tab_is_untouched, 2->test_add_pane_adopts_root_then_splits
(which also carries plan item 9's typed-keystrokes check), 3->orphan_reclaim,
4->anchor_failure, 5->pos1_cwd_mismatch, 6->multi_pane_no_match,
7->occupied_agent, 8->agent_start_failure_confirmed_by_postcheck.
test_fresh_workspace_tab_and_one_shots pins the fresh-creation half of the
walk instead of one of the nine.

Fidelity: the fake mirrors live-probed herdr semantics (design spec,
Evidence appendix) -- implicit root tab labeled "1", layout.apply whole-tab
replacement, pane.split leaves the new pane unnamed (the split->rename
window that orphan adoption reclaims on the next run), agent.start reports
onto PaneInfo.agent.

Scenarios 10-12 are branch pins beyond the required nine: the split->rename
recovery window, the loader's skip_reason passthrough, and the matched-pane
agent-degradation check (the spec's convergence-oracle clause).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import fake_herdr
import harness


def pane(name, direction=None, ratio=None, cwd="/tmp", agent=None, command=None):
    """One loader-model pane entry (same shape _load_pane returns)."""
    return {"name": name, "cwd": cwd, "direction": direction,
            "ratio": ratio, "agent": agent, "command": command}


def tab(label="T", path="/tmp", panes=(), skip_reason=None):
    """One loader-model tab entry."""
    t = {"label": label, "path": path, "panes": list(panes)}
    if skip_reason:
        t["skip_reason"] = skip_reason
    return t


def model(tabs, ws_label="W", ws_path="/tmp"):
    """A whole loader-model: one workspace holding `tabs`."""
    return {"workspaces": [{"label": ws_label, "path": ws_path,
                            "tabs": list(tabs)}]}


class AutoAgent(fake_herdr.FakeHerdr):
    """Created panes come up with an agent already attached (a concurrent
    `herdr agent start` racing the seeder, or whatever put it there): the
    one-shot pre-check must see it via panes_of and refuse a double start.
    Scoped to the tab the apply created -- a global label sweep would also
    hit unrelated fixtures that happen to carry the same pane label."""

    def m_layout_apply(self, p):
        r = super().m_layout_apply(p)
        for pi in self.tabs[r["tab"]["tab_id"]]["pane_ids"]:
            if self.panes[pi].get("label") == "a":
                self.panes[pi]["agent"] = "claude"
        return r


class StartedButFailed(fake_herdr.FakeHerdr):
    """agent.start lands server-side, THEN the reply reports failure -- the
    timeout shape: the effect happened, the answer did not arrive. Plain
    fail_next cannot express this (it short-circuits before the fake's own
    m_ method, so the agent would never be set and the post-check could not
    confirm anything)."""

    def m_agent_start(self, p):
        super().m_agent_start(p)
        raise RuntimeError("timed out")


class ReconcileCase(unittest.TestCase):
    def env(self, fake=None):
        self.m, self.fake, self.client = harness.make_env(self, fake=fake)

    def setUp(self):
        self.env()

    # --- run helpers ---------------------------------------------------------

    def run_reconcile(self, mdl):
        """One reconcile over `mdl`; returns (counters, log lines). Marks the
        call window first, so calls_after() sees reconcile traffic only.

        Named run_reconcile, NOT run: TestCase.run(result) is the
        framework's own entrypoint -- overriding it silently bypasses
        setUp/tearDown (first failure mode found the hard way: setUp never
        ran, self.fake did not exist).
        """
        self.mark = len(self.fake.calls)
        lines = []
        counters = self.m.reconcile(self.client, mdl, lines.append)
        return counters, lines

    def calls_after(self):
        return [meth for meth, _ in self.fake.calls[self.mark:]]

    def seed_preexisting(self, tab_label="T", root_label=None, orphan_cwd=None):
        """Workspace 'W' with one pre-existing tab `tab_label`.

        root_label: rename the tab's single root pane (None keeps it
        unnamed -- the adoptable shape). orphan_cwd: split an unnamed pane
        off the root at that cwd (the fake sets foreground_cwd == cwd, like
        live herdr). Returns the ids the assertions pin against.
        """
        r = self.client.call("workspace.create", {"label": "W", "cwd": "/tmp"})
        ws_id, root_tab = r["workspace"]["workspace_id"], r["tab"]["tab_id"]
        t = self.client.call("tab.create", {"workspace_id": ws_id,
                                            "label": tab_label, "cwd": "/tmp"})
        tab_id, pane_id = t["tab"]["tab_id"], t["root_pane"]["pane_id"]
        if root_label is not None:
            self.client.call("pane.rename", {"pane_id": pane_id,
                                             "label": root_label})
        orphan = None
        if orphan_cwd is not None:
            s = self.client.call("pane.split", {"target_pane_id": pane_id,
                                                "direction": "right",
                                                "cwd": orphan_cwd})
            orphan = s["pane"]["pane_id"]
        return {"ws_id": ws_id, "root_tab": root_tab, "tab_id": tab_id,
                "root_pane": pane_id, "orphan": orphan}

    def only_ws_id(self):
        return self.client.call("workspace.list")["workspaces"][0]["workspace_id"]

    def panes_by_label(self, ws_id, tab_id):
        return {p["label"]: p for p in self.m.panes_of(self.client, ws_id, tab_id)}

    def typed_of(self, wire_pane):
        """Keystrokes a pane received. Read from the FAKE's internal state:
        `typed` is fake bookkeeping, deliberately not part of the pane.list
        wire shape m_pane_list mirrors."""
        return self.fake.panes[wire_pane["pane_id"]].get("typed", [])

    def tab_labels(self, ws_id):
        return [t["label"] for t in
                self.client.call("tab.list", {"workspace_id": ws_id})["tabs"]]

    def tab_id_of(self, ws_id, label):
        return next(t["tab_id"] for t in
                    self.client.call("tab.list", {"workspace_id": ws_id})["tabs"]
                    if t["label"] == label)

    # --- scenario 1 -----------------------------------------------------------

    def test_matched_only_tab_is_untouched(self):
        """Declared pane matches by name -> zero mutating calls; the walk
        costs exactly the snapshot lists plus one panes_of, and matched=1.
        """
        ids = self.seed_preexisting(root_label="shell")
        counters, _ = self.run_reconcile(model([tab("T", "/tmp", [pane("shell")])]))
        self.assertEqual(counters, {"created": 0, "adopted": 0, "matched": 1,
                                    "skipped": 0, "failed": 0})
        # snapshot (workspace.list + per-ws tab.list/pane.list) + panes_of
        self.assertEqual(self.calls_after(),
                         ["workspace.list", "tab.list", "pane.list", "pane.list"])
        self.assertEqual(self.panes_by_label(ids["ws_id"], ids["tab_id"])
                         ["shell"]["pane_id"], ids["root_pane"])

    # --- scenario 2 -----------------------------------------------------------

    def test_fresh_workspace_tab_and_one_shots(self):
        """Fresh everything: workspace.create, then layout.apply as the
        tab_id replacement ONTO the implicit root tab (no second tab, no
        leftover "1"), then one-shots fire per declared pane.
        """
        counters, _ = self.run_reconcile(model([tab("T", "/tmp",
                                          [pane("a", agent="claude"),
                                           pane("b", "right", command="htop")])]))
        self.assertEqual(counters, {"created": 2, "adopted": 0, "matched": 0,
                                    "skipped": 0, "failed": 0})
        methods = self.calls_after()
        self.assertEqual(methods[0], "workspace.list")  # empty-server snapshot
        self.assertEqual(methods[1:4],
                         ["workspace.create", "layout.apply", "tab.list"])
        apply_params = next(p for m, p in self.fake.calls[self.mark:]
                            if m == "layout.apply")
        # tab_id form: bound to the implicit root, labeled for re-resolution
        self.assertEqual(sorted(apply_params), ["root", "tab_id", "tab_label"])
        self.assertEqual(apply_params["tab_label"], "T")
        ws_id = self.only_ws_id()
        # replacement, not addition: exactly one tab, the applied one
        self.assertEqual(self.tab_labels(ws_id), ["T"])
        panes = self.panes_by_label(ws_id, self.tab_id_of(ws_id, "T"))
        self.assertEqual(panes["a"]["agent"], "claude")
        self.assertEqual(panes["a"]["agent_name"], "a")
        self.assertEqual(self.typed_of(panes["b"]), ["htop", "Enter"])

    def test_fresh_ws_paneless_tab_cwd_match_adopts_root(self):
        """Pane-less tab #1 whose path equals the workspace's: the implicit
        root is rename-adopted (junk-tab disposal) -- no fresh tab, no
        leftover, no warning.
        """
        counters, lines = self.run_reconcile(model([tab("T", "/tmp", [])]))
        self.assertEqual(counters, {"created": 2, "adopted": 0, "matched": 0,
                                    "skipped": 0, "failed": 0})
        methods = self.calls_after()
        self.assertEqual(methods[1:4], ["workspace.create", "pane.list", "tab.rename"])
        self.assertNotIn("tab.create", methods)
        ws_id = self.only_ws_id()
        self.assertEqual(self.tab_labels(ws_id), ["T"])  # no leftover "1"
        self.assertNotIn("implicit '1' tab remains", "\n".join(lines))

    def test_fresh_ws_paneless_tab_cwd_mismatch_leaves_root_visible(self):
        """Codex (2026-09-08), walk level: the implicit root's pane sits at
        the WORKSPACE cwd; a pane-less tab #1 declaring another path must
        NOT adopt it by rename (the tab would live in the wrong directory
        forever -- matched tabs are never mutated). Fresh tab.create at the
        declared path; the leftover root is warned, never silent.
        """
        counters, lines = self.run_reconcile(model([tab("T", "/opt/elsewhere", [])]))
        self.assertEqual(counters, {"created": 2, "adopted": 0, "matched": 0,
                                    "skipped": 0, "failed": 0})
        methods = self.calls_after()
        self.assertEqual(methods[1:4], ["workspace.create", "pane.list", "tab.create"])
        self.assertNotIn("tab.rename", methods)
        ws_id = self.only_ws_id()
        self.assertEqual(sorted(self.tab_labels(ws_id)), ["1", "T"])
        # the fresh tab was created at the DECLARED path, not the ws cwd
        created = next(t for t in self.client.call(
            "tab.list", {"workspace_id": ws_id})["tabs"] if t["label"] == "T")
        self.assertEqual(self.fake.tabs[created["tab_id"]]["cwd"], "/opt/elsewhere")
        self.assertIn("implicit '1' tab remains", "\n".join(lines))

    # --- scenario 3 -----------------------------------------------------------

    def test_add_pane_adopts_root_then_splits(self):
        """1-pane pre-existing tab, root unnamed: pane #1 adopted (renamed),
        pane #2 split off it and renamed, one-shots fired on BOTH -- and
        never on a matched pane (scenario 1's untouched contract).
        """
        ids = self.seed_preexisting()  # single unnamed root pane
        counters, lines = self.run_reconcile(model([tab("T", "/tmp",
                                              [pane("shell", command="echo hi"),
                                               pane("logs", "right",
                                                    command="htop")])]))
        self.assertEqual(counters, {"created": 1, "adopted": 1, "matched": 0,
                                    "skipped": 0, "failed": 0})
        self.assertIn("adopted root pane -> 'shell'", "\n".join(lines))
        panes = self.panes_by_label(ids["ws_id"], ids["tab_id"])
        self.assertEqual(sorted(panes), ["logs", "shell"])
        # both new-by-adoption/split panes got their one-shot
        self.assertEqual(self.typed_of(panes["shell"]), ["echo hi", "Enter"])
        self.assertEqual(self.typed_of(panes["logs"]), ["htop", "Enter"])
        # the split landed on the adopted root and was renamed immediately
        self.assertIn("pane.split", self.calls_after())
        self.assertNotEqual(panes["logs"]["pane_id"], ids["root_pane"])

    # --- scenario 4 -----------------------------------------------------------

    def test_orphan_reclaim_skips_split(self):
        """Unnamed + agentless pane whose cwd matches pane #2's declaration:
        adopted by rename -- no pane.split, no pane growth.
        """
        ids = self.seed_preexisting(root_label="shell", orphan_cwd="/tmp/data")
        counters, lines = self.run_reconcile(model([tab("T", "/tmp",
                                              [pane("shell"),
                                               pane("logs", "right",
                                                    cwd="/tmp/data")])]))
        self.assertEqual(counters, {"created": 0, "adopted": 1, "matched": 1,
                                    "skipped": 0, "failed": 0})
        self.assertNotIn("pane.split", self.calls_after())
        self.assertIn("adopts unnamed orphan", "\n".join(lines))
        panes = self.panes_by_label(ids["ws_id"], ids["tab_id"])
        self.assertEqual(panes["logs"]["pane_id"], ids["orphan"])

    # --- scenario 5 -----------------------------------------------------------

    def test_anchor_failure_skips_dependents(self):
        """pane.split fails for pane #2: counted failed, and pane #3 (its
        dependent) SKIPS with the anchor named -- never re-anchored onto
        pane #1.
        """
        ids = self.seed_preexisting(root_label="shell")
        self.fake.fail_next("pane.split")
        counters, lines = self.run_reconcile(model([tab("T", "/tmp",
                                              [pane("shell"),
                                               pane("logs", "right", cwd="/x"),
                                               pane("spare", "down", cwd="/y")])]))
        self.assertEqual(counters, {"created": 0, "adopted": 0, "matched": 1,
                                    "skipped": 1, "failed": 1})
        # exactly ONE split attempt: the dependent did not re-anchor onto
        # pane #1 and try again
        self.assertEqual(self.calls_after().count("pane.split"), 1)
        self.assertNotIn("pane.rename", self.calls_after())
        text = "\n".join(lines)
        self.assertIn("'spare' skipped (no live anchor)", text)
        self.assertIn("split failed", text)
        # nothing grew in the tab
        panes = self.m.panes_of(self.client, ids["ws_id"], ids["tab_id"])
        self.assertEqual([p["label"] for p in panes], ["shell"])

    # --- scenario 6 -----------------------------------------------------------

    def test_pos1_cwd_mismatch_skips_tab_naming_paths(self):
        """Position-1 adoption refuses on the cwd guard: tab skipped, and the
        warning names BOTH the observed and the declared path (spec: "naming
        the observed values").
        """
        ids = self.seed_preexisting()  # single unnamed root pane
        self.fake.panes[ids["root_pane"]]["foreground_cwd"] = "/elsewhere"
        counters, lines = self.run_reconcile(model([tab("T", "/tmp", [pane("shell")])]))
        self.assertEqual(counters, {"created": 0, "adopted": 0, "matched": 0,
                                    "skipped": 1, "failed": 0})
        text = "\n".join(lines)
        self.assertIn("/elsewhere", text)   # observed
        self.assertIn("/tmp", text)         # declared
        # refusal, not damage: no rename, pane stays unnamed
        self.assertNotIn("pane.rename", self.calls_after())
        panes = self.m.panes_of(self.client, ids["ws_id"], ids["tab_id"])
        self.assertIsNone(panes[0]["label"])

    # --- scenario 7 -----------------------------------------------------------

    def test_multi_pane_no_match_skips_tab(self):
        """Multi-pane pre-existing tab where nothing matches pane #1: the
        WHOLE tab is skipped with a warning naming pane #1 and the pane
        count (naming pane #1 of a multi-pane tab is intentionally
        unsupported).
        """
        self.seed_preexisting(root_label="x", orphan_cwd="/tmp")
        counters, lines = self.run_reconcile(model([tab("T", "/tmp",
                                              [pane("shell"),
                                               pane("logs", "right")])]))
        self.assertEqual(counters, {"created": 0, "adopted": 0, "matched": 0,
                                    "skipped": 1, "failed": 0})
        text = "\n".join(lines)
        self.assertIn("no live pane matches pane #1 'shell'", text)
        self.assertIn("2 live panes", text)
        self.assertEqual(self.calls_after(),
                         ["workspace.list", "tab.list", "pane.list", "pane.list"])

    # --- scenario 8 -----------------------------------------------------------

    def test_occupied_agent_blocks_start(self):
        """One-shot pre-check: the pane already reports an agent ->
        agent.start is NOT called, a note is logged, nothing counted failed.
        """
        self.env(AutoAgent())
        counters, lines = self.run_reconcile(model([tab("T", "/tmp",
                                              [pane("a", agent="claude")])]))
        self.assertEqual(counters, {"created": 2, "adopted": 0, "matched": 0,
                                    "skipped": 0, "failed": 0})
        self.assertNotIn("agent.start", self.calls_after())
        self.assertIn("  ~ tab 'T': pane 'a': agent 'claude' already present, "
                      "skipping start", "\n".join(lines))

    # --- scenario 9 -----------------------------------------------------------

    def test_agent_start_failure_confirmed_by_postcheck(self):
        """agent.start reports failure but the agent landed (timeout shape):
        the post-failure re-list confirms it -- counted success, not failed.
        The command one-shot rides the same run: typed ends [text, "Enter"].
        """
        self.env(StartedButFailed())
        counters, lines = self.run_reconcile(model([tab("T", "/tmp",
                                              [pane("a", agent="claude"),
                                               pane("b", "right",
                                                    command="htop")])]))
        self.assertEqual(counters, {"created": 2, "adopted": 0, "matched": 0,
                                    "skipped": 0, "failed": 0})
        self.assertIn("  ~ tab 'T': pane 'a': started (confirmed after "
                      "timeout/error)", "\n".join(lines))
        ws_id = self.only_ws_id()
        tab_id = self.tab_id_of(ws_id, "T")
        panes = self.panes_by_label(ws_id, tab_id)
        self.assertEqual(panes["a"]["agent"], "claude")
        self.assertEqual(self.typed_of(panes["b"]), ["htop", "Enter"])

    # --- branch pins beyond the nine -----------------------------------------

    def test_split_rename_window_failure_leaves_orphan(self):
        """The split->rename recovery window: split lands, rename fails ->
        counted failed, dependents lose their anchor, and the UNNAMED pane
        stays behind -- the next run's orphan adoption reclaims it (the
        convergence loop, end to end).
        """
        ids = self.seed_preexisting(root_label="shell")
        self.fake.fail_next("pane.rename")
        counters, lines = self.run_reconcile(model([tab("T", "/tmp",
                                              [pane("shell"),
                                               pane("logs", "right",
                                                    cwd="/x")])]))
        self.assertEqual(counters, {"created": 0, "adopted": 0, "matched": 1,
                                    "skipped": 0, "failed": 1})
        self.assertIn("rename failed", "\n".join(lines))
        panes = self.m.panes_of(self.client, ids["ws_id"], ids["tab_id"])
        self.assertEqual([p["label"] for p in panes], ["shell", None])
        # next run: the leftover orphan is reclaimed, not re-split
        counters2, _ = self.run_reconcile(model([tab("T", "/tmp",
                                           [pane("shell"),
                                            pane("logs", "right",
                                                 cwd="/x")])]))
        self.assertEqual(counters2, {"created": 0, "adopted": 1, "matched": 1,
                                     "skipped": 0, "failed": 0})
        self.assertNotIn("pane.split", self.calls_after())
        panes = self.m.panes_of(self.client, ids["ws_id"], ids["tab_id"])
        self.assertEqual([p["label"] for p in panes], ["shell", "logs"])

    def test_loader_skip_reason_tab_counts_skipped(self):
        """A tab the loader marked skip_reason is reported and counted with
        zero socket work -- before any tab matching, let alone mutation.
        """
        counters, lines = self.run_reconcile(model([tab("Bad", "/nope", [pane("a")],
                                              skip_reason="resolved path does "
                                                        "not exist: /nope")]))
        self.assertEqual(counters, {"created": 1, "adopted": 0, "matched": 0,
                                    "skipped": 1, "failed": 0})
        self.assertIn("skipped (resolved path does not exist: /nope)",
                      "\n".join(lines))
        # workspace.create happened (the workspace itself is fine); nothing
        # else did -- no tab.create/layout.apply for the skipped tab
        self.assertEqual(self.calls_after(), ["workspace.list", "workspace.create"])

    def test_matched_agent_degradation_fails_no_respawn(self):
        """Matched pane whose declared agent differs from the live report:
        convergence FAILS (the oracle's clause), logged with both agent
        names -- and the seeder does NOT respawn (matched panes are never
        mutated).
        """
        ids = self.seed_preexisting(root_label="shell")
        self.client.call("agent.start", {"pane_id": ids["root_pane"],
                                         "kind": "codex", "name": "shell"})
        self.mark = len(self.fake.calls)
        counters, lines = self.run_reconcile(model([tab("T", "/tmp",
                                              [pane("shell", agent="claude")])]))
        self.assertEqual(counters, {"created": 0, "adopted": 0, "matched": 1,
                                    "skipped": 0, "failed": 1})
        text = "\n".join(lines)
        self.assertIn("declared agent 'claude'", text)
        self.assertIn("'codex'", text)
        self.assertNotIn("agent.start", self.calls_after())
        self.assertNotIn("pane.rename", self.calls_after())

    # --- rider pins: per-item containment (Task 7 review follow-ups) ---------

    def test_adoption_rename_failure_is_contained(self):
        """The root-adoption rename fails: counted failed, the pane stays
        unnamed, the run does NOT abort -- and the next run's adoption
        reclaims the pane (the convergence loop, adoption flavor).
        """
        ids = self.seed_preexisting()  # single unnamed root pane
        self.fake.fail_next("pane.rename")
        counters, lines = self.run_reconcile(model([tab("T", "/tmp", [pane("shell")])]))
        self.assertEqual(counters, {"created": 0, "adopted": 0, "matched": 0,
                                    "skipped": 0, "failed": 1})
        self.assertIn("adoption rename failed", "\n".join(lines))
        panes = self.m.panes_of(self.client, ids["ws_id"], ids["tab_id"])
        self.assertIsNone(panes[0]["label"])
        counters2, _ = self.run_reconcile(model([tab("T", "/tmp", [pane("shell")])]))
        self.assertEqual(counters2, {"created": 0, "adopted": 1, "matched": 0,
                                     "skipped": 0, "failed": 0})

    def test_command_one_shot_failure_is_contained(self):
        """A failing command one-shot is logged and counted, LATER one-shots
        still fire (per-pane containment), and the walk itself survives.

        Wire order pinned here (live-found bug, 2026-09-08): the command
        text goes out as pane.send_text and ONLY Enter as pane.send_keys --
        the real server rejects literal text inside send_keys ("unsupported
        key ..."), and the first fake accepted anything, hiding that.
        """
        ids = self.seed_preexisting()
        self.fake.fail_next("pane.send_text")
        counters, lines = self.run_reconcile(model([tab("T", "/tmp",
                                              [pane("shell", command="echo hi"),
                                               pane("logs", "right",
                                                    command="htop")])]))
        self.assertEqual(counters, {"created": 1, "adopted": 1, "matched": 0,
                                    "skipped": 0, "failed": 1})
        text = "\n".join(lines)
        self.assertIn("pane 'shell': command failed", text)
        self.assertIn("pane 'logs': command sent", text)
        panes = self.panes_by_label(ids["ws_id"], ids["tab_id"])
        self.assertEqual(self.typed_of(panes["logs"]), ["htop", "Enter"])
        self.assertEqual(self.typed_of(panes["shell"]), [])
        send_calls = [(m, p) for m, p in self.fake.calls if "send" in m]
        self.assertIn(("pane.send_text",
                       {"pane_id": panes["logs"]["pane_id"], "text": "htop"}),
                      send_calls)
        self.assertIn(("pane.send_keys",
                       {"pane_id": panes["logs"]["pane_id"], "keys": ["Enter"]}),
                      send_calls)
        self.assertNotIn(("pane.send_keys",
                          {"pane_id": panes["shell"]["pane_id"],
                           "keys": ["Enter"]}), send_calls)

    # --- coverage-audit pins: untested error handlers + edge guards ----------

    def test_agent_start_failure_counts_failed(self):
        """agent.start fails AND the post-failure re-list finds no agent (a
        real refusal, NOT the timeout shape of scenario 9): counted failed,
        the message names the pane, and the run goes on."""
        self.fake.fail_next("agent.start")
        counters, lines = self.run_reconcile(model([tab("T", "/tmp",
                                              [pane("a", agent="claude")])]))
        # fresh flow: workspace + applied tab count as created=2; the pane
        # came into being with the tree, so there was nothing to adopt
        self.assertEqual(counters, {"created": 2, "adopted": 0, "matched": 0,
                                    "skipped": 0, "failed": 1})
        self.assertIn("pane 'a': agent.start failed", "\n".join(lines))

    def test_orphan_adoption_rename_failure_is_contained(self):
        """The orphan-flavor adoption rename fails (scenario: root matched,
        orphan adoptable): counted failed, the orphan stays unnamed for the
        next run, and the run does not abort -- same containment as the
        root-adoption flavor."""
        ids = self.seed_preexisting(root_label="shell", orphan_cwd="/tmp/data")
        self.fake.fail_next("pane.rename")
        counters, lines = self.run_reconcile(model([tab("T", "/tmp",
                                              [pane("shell"),
                                               pane("logs", "right",
                                                    cwd="/tmp/data")])]))
        self.assertEqual(counters, {"created": 0, "adopted": 0, "matched": 1,
                                    "skipped": 0, "failed": 1})
        self.assertIn("pane 'logs' adoption rename failed", "\n".join(lines))
        orphan_wire = next(p for p in
                           self.m.panes_of(self.client, ids["ws_id"], ids["tab_id"])
                           if p["pane_id"] == ids["orphan"])
        self.assertIsNone(orphan_wire["label"])
        # next run: the orphan is reclaimed, not re-split
        counters2, _ = self.run_reconcile(model([tab("T", "/tmp",
                                           [pane("shell"),
                                            pane("logs", "right",
                                                 cwd="/tmp/data")])]))
        self.assertEqual(counters2, {"created": 0, "adopted": 1, "matched": 1,
                                     "skipped": 0, "failed": 0})
        self.assertNotIn("pane.split", self.calls_after())

    def test_index_panes_duplicate_names_keep_first(self):
        """Duplicate live pane names: warn once naming the label, index the
        FIRST occurrence (position order); unnamed panes never enter the
        index. Pure-function pin -- no socket involved."""
        live = [{"pane_id": "p1", "label": "dup"},
                {"pane_id": "p2", "label": "dup"},
                {"pane_id": "p3", "label": None}]
        lines = []
        by = self.m.index_panes(live, lines.append, "T")
        self.assertEqual(set(by), {"dup"})
        self.assertEqual(by["dup"]["pane_id"], "p1")  # first, not last
        text = "\n".join(lines)
        self.assertIn("duplicate live pane names", text)
        self.assertIn("'dup'", text)

    def test_adoption_falls_back_to_spawn_cwd_when_foreground_missing(self):
        """_eff_cwd's fallback: a live pane reporting NO foreground_cwd is
        matched on its spawn cwd (the fake normally sets both; live may
        not) -- position-1 adoption must not refuse just because the
        foreground report is missing."""
        ids = self.seed_preexisting()
        self.fake.panes[ids["root_pane"]]["foreground_cwd"] = None
        counters, _ = self.run_reconcile(model([tab("T", "/tmp",
                                              [pane("shell")])]))
        self.assertEqual(counters, {"created": 0, "adopted": 1, "matched": 0,
                                    "skipped": 0, "failed": 0})
        panes = self.m.panes_of(self.client, ids["ws_id"], ids["tab_id"])
        self.assertEqual([p["label"] for p in panes], ["shell"])

    # --- red-team pins: re-fire, scoping, adoption refusal --------------------

    def test_matched_pane_command_never_refires(self):
        """The convergence contract at its sharpest: a declared pane whose
        command ALREADY ran (pane matched by name) is never mutated -- no
        send_text/send_keys in the call window, typed buffer untouched.
        Re-runs must stay idempotent, not re-execute the user's commands."""
        ids = self.seed_preexisting(root_label="shell")
        counters, _ = self.run_reconcile(model([tab("T", "/tmp",
                                              [pane("shell",
                                                    command="make all")])]))
        self.assertEqual(counters, {"created": 0, "adopted": 0, "matched": 1,
                                    "skipped": 0, "failed": 0})
        methods = self.calls_after()
        self.assertNotIn("pane.send_text", methods)
        self.assertNotIn("pane.send_keys", methods)
        shell = self.panes_by_label(ids["ws_id"], ids["tab_id"])["shell"]
        self.assertEqual(self.typed_of(shell), [])

    def test_tab_labels_scoped_per_workspace_end_to_end(self):
        """W1 and W2 both declare tab 'T': each workspace ends with exactly
        one 'T' carrying ITS OWN declaration. The snapshot's label maps are
        per-workspace by construction; this pins that the whole walk --
        match, create, one-shots -- never lets a tab label leak across the
        workspace boundary."""
        mdl = {"workspaces": [
            {"label": "W1", "path": "/tmp",
             "tabs": [tab("T", "/tmp", [pane("w1only")])]},
            {"label": "W2", "path": "/tmp",
             "tabs": [tab("T", "/tmp", [pane("w2only")])]},
        ]}
        counters, _ = self.run_reconcile(mdl)
        self.assertEqual(counters, {"created": 4, "adopted": 0, "matched": 0,
                                    "skipped": 0, "failed": 0})
        for ws_label, pane_name in (("W1", "w1only"), ("W2", "w2only")):
            ws_id = next(w["workspace_id"] for w in
                         self.client.call("workspace.list")["workspaces"]
                         if w["label"] == ws_label)
            self.assertEqual(self.tab_labels(ws_id), ["T"])  # one tab, exactly
            panes = self.panes_by_label(ws_id, self.tab_id_of(ws_id, "T"))
            self.assertEqual(sorted(panes), [pane_name])  # the OWN declaration

    def test_pos1_adoption_refuses_agent_occupied_pane(self):
        """Spec step 3: a single unnamed live pane already carrying an agent
        is NOT adoptable for pane #1 -- skipped (not failed), the warning
        names the agent, and no pane.rename ever touches the pane (the
        user's agent pane keeps its identity for the next run)."""
        ids = self.seed_preexisting()  # single unnamed root pane
        self.fake.panes[ids["root_pane"]]["agent"] = "claude"
        counters, lines = self.run_reconcile(model([tab("T", "/tmp",
                                              [pane("shell")])]))
        self.assertEqual(counters, {"created": 0, "adopted": 0, "matched": 0,
                                    "skipped": 1, "failed": 0})
        self.assertIn("'claude'", "\n".join(lines))
        self.assertNotIn("pane.rename", self.calls_after())
        wire = self.m.panes_of(self.client, ids["ws_id"], ids["tab_id"])
        self.assertIsNone(wire[0]["label"])  # still unnamed, still adoptable later

    def test_one_shot_pane_missing_from_fresh_tab_counts_failed(self):
        """A declared pane absent from the inventory of the tab this run
        just filled (server dropped/mangled the label) must NOT skip
        silently behind a clean exit: the log names the pane and failed=1
        keeps the run's exit code honest. Fake subclass strips one pane's
        label right where layout.apply lands it."""
        class LabelDropper(fake_herdr.FakeHerdr):
            def m_layout_apply(self, p):
                r = super().m_layout_apply(p)
                for pi in self.tabs[r["tab"]["tab_id"]]["pane_ids"]:
                    if self.panes[pi].get("label") == "b":
                        self.panes[pi]["label"] = None
                return r

        self.env(fake=LabelDropper())
        counters, lines = self.run_reconcile(model([tab("T", "/tmp",
                                              [pane("a"),
                                               pane("b", "right",
                                                    command="htop")])]))
        self.assertIn("pane 'b': not found in fresh tab -- one-shot skipped",
                      "\n".join(lines))
        self.assertEqual(counters["failed"], 1)


if __name__ == "__main__":
    unittest.main()
