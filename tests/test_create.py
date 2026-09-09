"""Fresh-tab creation: bin/vergent create_tab / build_tree / panes_of.

Fake-based only -- no live socket. The live behavior these tests mirror was
probed 2026-09-08 on herdr 0.8.2 (replacement closes the old tab; apply
without tab_id creates one; receipts in the design spec's Evidence
appendix); tests/fake_herdr.py encodes those semantics.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import fake_herdr
import harness
from test_loader import load_module


def pane(name, direction=None, ratio=None, cwd="/tmp"):
    """One loader-model pane entry (same shape _load_pane returns)."""
    return {"name": name, "cwd": cwd, "direction": direction,
            "ratio": ratio, "agent": None, "command": None}


def tab(label="T", path="/tmp", panes=()):
    """One loader-model tab entry."""
    return {"label": label, "path": path, "panes": list(panes)}


class CreateCase(unittest.TestCase):
    def setUp(self):
        self.m, self.fake, self.client = harness.make_env(self)
        r = self.client.call("workspace.create", {"label": "W", "cwd": "/tmp"})
        self.ws_id = r["workspace"]["workspace_id"]
        self.root_tab = r["tab"]["tab_id"]
        self.root_pane = r["root_pane"]["pane_id"]

    def _tab_ids(self):
        """All tab ids of the fixture workspace, via the production path."""
        return [t["tab_id"] for t in
                self.client.call("tab.list", {"workspace_id": self.ws_id})["tabs"]]

    # --- build_tree ---------------------------------------------------------

    def test_build_tree_chains_per_spec(self):
        """Pane i (i >= 2) splits off the tree accumulated SO FAR, in its own
        direction with its ratio: the outermost split comes from the LAST
        pane, declared order stays the first-to-last spine.
        """
        tree = self.m.build_tree([pane("a"), pane("b", "down", 0.7),
                                  pane("c", "right")])
        self.assertEqual(tree, {
            "type": "split", "direction": "right", "ratio": 0.5,
            "first": {"type": "split", "direction": "down", "ratio": 0.7,
                      "first": {"type": "pane", "label": "a", "cwd": "/tmp"},
                      "second": {"type": "pane", "label": "b", "cwd": "/tmp"}},
            "second": {"type": "pane", "label": "c", "cwd": "/tmp"}})

    def test_build_tree_defaults_omitted_ratio(self):
        """The loader stores ratio=None when the TOML omitted it; the server
        schema REQUIRES ratio on every split node, so the tree must carry
        the 0.5 neutral split (a .get() default would never fire on a
        present-but-None key).
        """
        tree = self.m.build_tree([pane("a"), pane("b", "right")])
        self.assertEqual(tree["ratio"], 0.5)
        self.assertEqual(tree["direction"], "right")

    # --- panes_of -----------------------------------------------------------

    def test_panes_of_returns_one_tabs_panes_in_order(self):
        """Inventory of ONE tab: full pane dicts in pane.list order, never
        name-keyed -- the reconciler's cardinality/order source.
        """
        t = self.client.call("tab.create", {"workspace_id": self.ws_id,
                                            "label": "T2", "cwd": "/tmp"})
        tab2, t2_root = t["tab"]["tab_id"], t["root_pane"]["pane_id"]
        split = self.client.call("pane.split", {"target_pane_id": t2_root,
                                                "direction": "right",
                                                "cwd": "/tmp"})
        t2_split = split["pane"]["pane_id"]
        self.client.call("pane.rename", {"pane_id": t2_root, "label": "x"})
        self.client.call("pane.rename", {"pane_id": t2_split, "label": "y"})

        panes = self.m.panes_of(self.client, self.ws_id, tab2)
        self.assertEqual([p["pane_id"] for p in panes], [t2_root, t2_split])
        self.assertEqual([p["label"] for p in panes], ["x", "y"])
        # tab-scoped: the workspace root tab's pane is not in T2's inventory
        self.assertNotIn(self.root_pane, [p["pane_id"] for p in panes])
        # ...and the root tab inventories exactly its own pane
        root_panes = self.m.panes_of(self.client, self.ws_id, self.root_tab)
        self.assertEqual([p["pane_id"] for p in root_panes], [self.root_pane])

    # --- create_tab: the creation forms --------------------------------------

    def test_implicit_root_tab_with_panes_replaces(self):
        """Tab #1 of an adopted workspace, with panes: one layout.apply onto
        the implicit root tab. Old tab closed, replacement carries the label
        and the tree, and the RETURNED id is the replacement's (the implicit
        id died with the apply).
        """
        got, consumed = self.m.create_tab(self.client, self.ws_id,
                                          tab(panes=[pane("a"),
                                                     pane("b", "right", 0.6)]),
                                          "fallback", implicit_root_tab_id=self.root_tab)
        self.assertTrue(consumed)  # the apply disposed of the implicit tab
        self.assertNotIn(self.root_tab, self.fake.tabs)  # implicit tab disposed
        ids = self._tab_ids()
        self.assertEqual(len(ids), 1)  # replacement, not an extra tab
        self.assertEqual(got, ids[0])
        self.assertNotEqual(got, self.root_tab)  # replacement semantics
        panes = self.m.panes_of(self.client, self.ws_id, got)
        self.assertEqual([p["label"] for p in panes], ["a", "b"])

    def test_paneless_with_implicit_root_tab_renames(self):
        """Tab #1 without panes on an adopted workspace, root pane's cwd
        equal to the declared tab path (both /tmp here): RENAME the implicit
        root tab -- the v1 junk-tab bug class (a leftover nameless tab next
        to the real one) must not come back. One pane.list probe precedes
        the rename (the Codex 2026-09-08 cwd check).
        """
        first = len(self.fake.calls)
        got, consumed = self.m.create_tab(self.client, self.ws_id, tab(panes=[]),
                                          "fallback", implicit_root_tab_id=self.root_tab)
        self.assertEqual(got, self.root_tab)  # same tab, no replacement
        self.assertTrue(consumed)
        self.assertEqual([m for m, _ in self.fake.calls[first:]],
                         ["pane.list", "tab.rename"])
        self.assertEqual(self.fake.tabs[self.root_tab]["label"], "T")

    def test_paneless_implicit_cwd_mismatch_creates_fresh(self):
        """The implicit root's pane sits at the WORKSPACE cwd; a tab
        declaring another path must NOT adopt it by rename -- the tab would
        silently live in the wrong directory forever (matched tabs are
        never mutated). Fresh tab.create at the DECLARED path instead; the
        implicit root survives, unconsumed, for the walk's leftover
        warning.
        """
        first = len(self.fake.calls)
        got, consumed = self.m.create_tab(
            self.client, self.ws_id, tab(path="/opt/elsewhere", panes=[]),
            "fallback", implicit_root_tab_id=self.root_tab)
        self.assertNotEqual(got, self.root_tab)
        self.assertFalse(consumed)
        self.assertEqual([m for m, _ in self.fake.calls[first:]],
                         ["pane.list", "tab.create"])
        self.assertEqual(self.fake.tabs[got]["cwd"], "/opt/elsewhere")
        self.assertIn(self.root_tab, self.fake.tabs)  # root untouched
        self.assertEqual(self.fake.tabs[self.root_tab]["label"], "1")

    def test_create_in_apply_form(self):
        """Declared tab #2..n (fact a): ONE layout.apply with workspace_id +
        tab_label and NO tab_id creates the tab.
        """
        first = len(self.fake.calls)
        got, consumed = self.m.create_tab(self.client, self.ws_id,
                                          tab(panes=[pane("a"), pane("b", "down")]),
                                          "create-in-apply")
        self.assertFalse(consumed)
        calls = self.fake.calls[first:]
        self.assertEqual([m for m, _ in calls], ["layout.apply", "tab.list"])
        params = calls[0][1]
        # creation form: workspace-scoped, labeled, and NOT bound to a tab_id
        self.assertEqual(sorted(params), ["root", "tab_label", "workspace_id"])
        self.assertEqual(params["workspace_id"], self.ws_id)
        self.assertEqual(params["tab_label"], "T")
        # the created tab exists with its tree, next to the untouched root
        self.assertEqual(self._tab_ids(), [self.root_tab, got])
        panes = self.m.panes_of(self.client, self.ws_id, got)
        self.assertEqual([p["label"] for p in panes], ["a", "b"])

    def test_fallback_form_creates_then_applies(self):
        """Fallback (or fact-a regression): tab.create, THEN layout.apply
        onto the fresh tab_id. The returned id is the replacement's -- the
        tab.create id died with the apply (replacement semantics).
        """
        first = len(self.fake.calls)
        got, consumed = self.m.create_tab(self.client, self.ws_id,
                                          tab(panes=[pane("a"), pane("b", "right")]),
                                          "fallback")
        self.assertFalse(consumed)
        calls = self.fake.calls[first:]
        self.assertEqual([m for m, _ in calls],
                         ["tab.create", "layout.apply", "tab.list"])
        apply_params = calls[1][1]
        # the apply was bound to a concrete tab id and carried the label
        self.assertEqual(sorted(apply_params), ["root", "tab_id", "tab_label"])
        self.assertEqual(apply_params["tab_label"], "T")
        created_id = apply_params["tab_id"]
        # it targeted the tab create had just made -- not the implicit root
        self.assertNotEqual(created_id, self.root_tab)
        # replacement semantics: the created id is gone, the returned id is
        # its replacement, and the untouched implicit root tab survives
        self.assertNotIn(created_id, self.fake.tabs)
        self.assertIn(got, self.fake.tabs)
        self.assertEqual(sorted(self._tab_ids()), sorted([self.root_tab, got]))
        panes = self.m.panes_of(self.client, self.ws_id, got)
        self.assertEqual([p["label"] for p in panes], ["a", "b"])

    def test_fact_a_regression_switches_to_fallback(self):
        """The FACT_A_CONFIRMED = False escape hatch: a fresh module copy
        with the flag flipped routes the create-in-apply form through the
        fallback (tab.create, then layout.apply bound to that tab id), and
        the returned id is the post-apply replacement. The pin runs on its
        OWN module load (the flag is per-load state) and hands it to
        make_env so the client is built from that same load -- flag reads
        and any raised errors must come from one module, not two
        (assertRaises matches exception classes by identity).
        """
        m2 = load_module()
        m2.FACT_A_CONFIRMED = False
        module, fake, client = harness.make_env(self, module=m2)
        r = client.call("workspace.create", {"label": "W", "cwd": "/tmp"})
        ws_id = r["workspace"]["workspace_id"]
        first = len(fake.calls)
        got, consumed = module.create_tab(client, ws_id,
                                          tab(panes=[pane("a"), pane("b", "right")]),
                                          "create-in-apply")
        self.assertFalse(consumed)
        calls = fake.calls[first:]
        self.assertEqual([meth for meth, _ in calls],
                         ["tab.create", "layout.apply", "tab.list"])
        apply_params = calls[1][1]
        # the apply was bound to a concrete tab id and carried the label
        self.assertEqual(sorted(apply_params), ["root", "tab_id", "tab_label"])
        created_id = apply_params["tab_id"]
        # replacement semantics: the tab.create id died with the apply, and
        # the returned id is its post-apply replacement
        self.assertNotIn(created_id, fake.tabs)
        self.assertIn(got, fake.tabs)
        self.assertNotEqual(got, created_id)
        self.assertEqual([p["label"] for p in module.panes_of(client, ws_id, got)],
                         ["a", "b"])

    def test_paneless_without_implicit_creates_plainly(self):
        """A pane-less tab with no implicit root tab: plain tab.create (v1
        shape) -- no layout.apply even when the create-in-apply form is
        requested, because there is no tree to apply.
        """
        first = len(self.fake.calls)
        got, consumed = self.m.create_tab(self.client, self.ws_id, tab(panes=[]),
                                          "create-in-apply")
        self.assertFalse(consumed)
        self.assertEqual([m for m, _ in self.fake.calls[first:]], ["tab.create"])
        self.assertIn(got, self.fake.tabs)
        self.assertEqual(self.fake.tabs[got]["label"], "T")

    def test_unresolvable_label_raises_herdr_error(self):
        """If tab.list does not return the applied label (tab gone between
        calls, server anomaly), the failure must be a loud HerdrError naming
        the label -- never a silent None tab id.
        """

        class Hider(fake_herdr.FakeHerdr):
            def m_tab_list(self, p):
                base = super().m_tab_list(p)
                base["tabs"] = [t for t in base["tabs"] if t.get("label") != "T"]
                return base

        m, fake, client = harness.make_env(self, fake=Hider())
        r = client.call("workspace.create", {"label": "W", "cwd": "/tmp"})
        with self.assertRaises(m.HerdrError) as cm:
            m.create_tab(client, r["workspace"]["workspace_id"],
                         tab(panes=[pane("a")]), "create-in-apply")
        self.assertIn("'T'", str(cm.exception))
        # the apply itself DID land (the tab exists; only its re-resolution
        # failed) -- create_tab is honest about what it cannot promise
        self.assertEqual(len(fake.tabs), 2)  # implicit root + created tab


if __name__ == "__main__":
    unittest.main()
