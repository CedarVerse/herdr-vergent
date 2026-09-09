"""Live snapshot assembly (bin/vergent.snapshot).

The fake is seeded through the same client calls the seeder uses in
production (workspace.create / tab.create / pane.split / pane.rename), so the
assertions pin the herdr semantics tests/fake_herdr.py mirrors: a fresh
workspace arrives with an implicit root tab labeled "1" (number-derived --
live-probed side-finding 2 of the design spec; the fake's fidelity fix),
pane.split adds an unnamed pane to the target pane's tab, and pane.rename
labels exactly one pane.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import harness


class SnapshotCase(unittest.TestCase):
    # Same env as SocketCase in tests/test_socket.py -- now shared via
    # tests/harness.py (the third consumer, test_create, triggered the lift).
    def setUp(self):
        self.m, self.fake, self.client = harness.make_env(self)

    def _seed(self):
        """workspace 'W' with three tabs: the implicit root tab '1', tab 'T',
        and a tab created WITHOUT a label (exercises the tl-None skip; herdr
        itself would only produce label-less tabs via races or foreign
        clients, and the fake can make that deterministic).

        Tab 'T' ends with two panes: its root pane renamed 'shell' and an
        unnamed split pane. Returns every id the assertions pin against.
        """
        r = self.client.call("workspace.create", {"label": "W", "cwd": "/tmp"})
        ws_id = r["workspace"]["workspace_id"]
        root_tab_id = r["tab"]["tab_id"]
        root_pane_id = r["root_pane"]["pane_id"]
        t = self.client.call(
            "tab.create", {"workspace_id": ws_id, "label": "T", "cwd": "/tmp"}
        )
        tab_t_id = t["tab"]["tab_id"]
        t_root_pane = t["root_pane"]["pane_id"]
        split = self.client.call(
            "pane.split",
            {"target_pane_id": t_root_pane, "direction": "right", "cwd": "/tmp"},
        )
        split_pane = split["pane"]["pane_id"]
        self.client.call("pane.rename", {"pane_id": t_root_pane, "label": "shell"})
        u = self.client.call("tab.create", {"workspace_id": ws_id, "cwd": "/tmp"})
        unlabeled_tab_id = u["tab"]["tab_id"]
        return {
            "ws_id": ws_id,
            "root_tab_id": root_tab_id,
            "root_pane": root_pane_id,
            "tab_t_id": tab_t_id,
            "t_root_pane": t_root_pane,
            "split_pane": split_pane,
            "unlabeled_tab_id": unlabeled_tab_id,
        }

    def test_snapshot_structure(self):
        ids = self._seed()
        snap = self.m.snapshot(self.client)
        self.assertEqual(set(snap), {"W"})
        w = snap["W"]
        self.assertEqual(w["workspace_id"], ids["ws_id"])
        self.assertEqual(w["tabs"]["T"]["tab_id"], ids["tab_t_id"])
        # tab 'T': named + unnamed panes, each mapped to the right pane id
        panes = w["tabs"]["T"]["panes"]
        self.assertEqual(set(panes), {"shell", None})  # unnamed pane keyed None
        self.assertEqual(panes["shell"]["pane_id"], ids["t_root_pane"])
        self.assertEqual(panes[None]["pane_id"], ids["split_pane"])
        # panes carry the full pane.list dict, not a projection of it
        self.assertEqual(panes["shell"]["foreground_cwd"], "/tmp")
        # the implicit root tab is present too, keyed by its number-derived
        # label ("1" -- the fake mirrors the live probe, side-finding 2)
        self.assertEqual(w["tabs"]["1"]["tab_id"], ids["root_tab_id"])
        # ...and its single pane IS mapped now that the tab carries a label
        self.assertEqual(w["tabs"]["1"]["panes"][None]["pane_id"], ids["root_pane"])
        # a genuinely label-less tab (created without one) has NO pane
        # entries in the snapshot (the tl-None guard skips its panes); the
        # reconciler re-lists via panes_of()
        self.assertEqual(w["tabs"][None]["tab_id"], ids["unlabeled_tab_id"])
        self.assertEqual(w["tabs"][None]["panes"], {})
        # all three tab ids are mapped to their labels
        self.assertEqual(
            w["tab_id_to_label"],
            {
                ids["root_tab_id"]: "1",
                ids["tab_t_id"]: "T",
                ids["unlabeled_tab_id"]: None,
            },
        )

    def test_snapshot_assembles_from_three_list_calls(self):
        """Exactly the three list calls, in order, on a one-workspace server:
        workspace.list once, then tab.list + pane.list scoped per workspace
        (panes grouped by tab_id -- never one pane.list per tab). The params
        are pinned too: the workspace_id scopes both per-workspace calls, so
        a snapshot that leaked another workspace's scope cannot pass.
        """
        ids = self._seed()
        first = len(self.fake.calls)  # skip the seeding calls
        snap = self.m.snapshot(self.client)
        self.assertEqual(set(snap), {"W"})
        self.assertEqual(
            [(m, p) for m, p in self.fake.calls[first:]],
            [
                ("workspace.list", {}),
                ("tab.list", {"workspace_id": ids["ws_id"]}),
                ("pane.list", {"workspace_id": ids["ws_id"]}),
            ],
        )

    def test_duplicate_pane_labels_collapse_last_wins(self):
        """Two live panes sharing a label in ONE tab: the snapshot map keeps
        the LAST pane.list hit. This collapse is documented and deliberate
        (the reconciler works on the ordered panes_of() list and warns on
        its own) -- the pin here is that the overwrite is deterministic and
        silent, never a crash or a duplicate key."""
        r = self.client.call("workspace.create", {"label": "W", "cwd": "/tmp"})
        ws_id = r["workspace"]["workspace_id"]
        t = self.client.call(
            "tab.create", {"workspace_id": ws_id, "label": "T", "cwd": "/tmp"}
        )
        root_pane = t["root_pane"]["pane_id"]
        split = self.client.call(
            "pane.split",
            {"target_pane_id": root_pane, "direction": "right", "cwd": "/tmp"},
        )
        split_pane = split["pane"]["pane_id"]
        self.client.call("pane.rename", {"pane_id": root_pane, "label": "shell"})
        self.client.call("pane.rename", {"pane_id": split_pane, "label": "shell"})
        snap = self.m.snapshot(self.client)
        panes = snap["W"]["tabs"]["T"]["panes"]
        self.assertEqual(set(panes), {"shell"})  # collapsed to one key
        self.assertEqual(panes["shell"]["pane_id"], split_pane)  # last wins


if __name__ == "__main__":
    unittest.main()
