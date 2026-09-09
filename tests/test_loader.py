import importlib.util
import os
import pathlib
import shutil
import tempfile
import unittest


def load_module():
    root = pathlib.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("hp", root / "bin" / "vergent.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class LoaderCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # once per class: loader functions are stateless (module flags like
        # FACT_A_CONFIRMED are per-load -- see tests/test_create.py)
        cls.m = load_module()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.real = os.path.realpath(self.tmp)
        os.makedirs(os.path.join(self.real, "repo"))
        # The nested dir the happy chain's relative pane cwd ("repo" under
        # the tab path) resolves to: since the pane-cwd existence gate
        # (Codex 2026-09-08), fixture pane cwds must exist for a tab to
        # load as non-skipped.
        os.makedirs(os.path.join(self.real, "repo", "repo"))
        # A symlinked route INTO repo (lives in the tmpdir itself, which is
        # a real dir): lets the loader rows pin realpath canonicalization.
        self.link = os.path.join(self.tmp, "link-to-repo")
        os.symlink(os.path.join(self.real, "repo"), self.link)
        self.toml = os.path.join(self.tmp, "projects.toml")

    def write(self, body):
        with open(self.toml, "w") as f:
            f.write(body)
        return self.m.load_model(self.toml)

    # --- schema v3 fixture helpers -------------------------------------------
    # File identity key is `name` at every level; tabs nest as
    # [[workspace.tab]] under the workspace, panes as [[workspace.tab.pane]]
    # under the tab. The MODEL keeps herdr's wire word `label`.

    def ok_ws(self):
        return '[[workspace]]\nname = "W"\npath = "%s"\n' % self.real

    def ok_tab(self, extra=""):
        return '[[workspace.tab]]\nname = "T"\n' + extra

    def p1(self):
        return '[[workspace.tab.pane]]\nname = "a"\n'  # valid root pane (plain)

    def test_happy_chain(self):
        m = self.write(self.ok_ws() + self.ok_tab('path = "repo"\n')
                       + self.p1()
                       + '[[workspace.tab.pane]]\nname = "logs"\ncwd = "repo"\ndirection = "down"\nratio = 0.7\n')
        t = m["workspaces"][0]["tabs"][0]
        self.assertNotIn("skip_reason", t)  # every cwd (tab + panes) exists
        self.assertEqual(t["path"], os.path.join(self.real, "repo"))
        self.assertEqual(t["panes"][0]["cwd"], os.path.join(self.real, "repo"))
        # pane.cwd is relative to the TAB's resolved path (design spec, rules #4):
        # "repo" under $real/repo -> $real/repo/repo. (The plan's original line here
        # duplicated the pane-1 assertion, contradicting both the spec and the
        # plan's own loader snippet.)
        self.assertEqual(t["panes"][1]["cwd"], os.path.join(self.real, "repo", "repo"))
        self.assertIsNone(t["panes"][0]["direction"])
        self.assertEqual(t["panes"][1]["direction"], "down")

    def test_missing_dir_skips_tab(self):
        m = self.write(self.ok_ws() + self.ok_tab('path = "nope"\n') + self.p1())
        t = m["workspaces"][0]["tabs"][0]
        self.assertIn("skip_reason", t)

    def test_root_pane_explicit_cwd(self):
        """Schema relax (2026-09-09): pane #1 accepts an optional cwd,
        resolved like any other pane's (relative joins the TAB path, then
        realpath) -- so a tab can inherit its workspace's base and declare
        every pane as a sibling delta, no ../ gymnastics. Omitted cwd
        still means the tab path (happy chain pins that branch).
        """
        m = self.write(self.ok_ws() + self.ok_tab()  # inherits the workspace base (self.real)
                       + '[[workspace.tab.pane]]\nname = "a"\ncwd = "repo"\n'
                       + '[[workspace.tab.pane]]\nname = "b"\ncwd = "%s"\ndirection = "down"\n'
                       % self.link)
        t = m["workspaces"][0]["tabs"][0]
        self.assertNotIn("skip_reason", t)
        self.assertEqual(t["path"], self.real)          # inherited workspace base
        self.assertEqual(t["panes"][0]["cwd"], os.path.join(self.real, "repo"))
        self.assertIsNone(t["panes"][0]["direction"])
        self.assertEqual(t["panes"][1]["cwd"], os.path.join(self.real, "repo"))
        self.assertEqual(t["panes"][1]["direction"], "down")

    def test_missing_pane_cwd_skips_tab(self):
        """Codex (2026-09-08): pane #2+ resolve their own cwd; an unchecked
        one used to sail through the loader and fail server-side mid-walk
        at layout.apply/pane.split -- aborting later tabs instead of the
        promised per-tab skip. The whole tab skips, naming the pane.
        """
        m = self.write(self.ok_ws() + self.ok_tab('path = "repo"\n') + self.p1()
                       + '[[workspace.tab.pane]]\nname = "b"\ncwd = "nope"\ndirection = "down"\n')
        t = m["workspaces"][0]["tabs"][0]
        self.assertIn("skip_reason", t)
        self.assertIn("'b'", t["skip_reason"])
        self.assertIn("nope", t["skip_reason"])
        # the panes ride along (skip_reason_tab parity) so the reconciler
        # can warn per pane if it ever needs to
        self.assertEqual([p["name"] for p in t["panes"]], ["a", "b"])

    def test_workspace_without_tabs_is_legal(self):
        """Schema v3: a workspace may declare zero tabs -- an empty Space is
        a real declaration (the walk creates it and warns about herdr's
        unconsumed implicit root tab). v2's global "no [[tab]] declared"
        rule died with the nesting: emptiness is per-workspace now.
        """
        m = self.write(self.ok_ws())
        self.assertEqual(m["workspaces"][0]["label"], "W")
        self.assertEqual(m["workspaces"][0]["tabs"], [])

    def test_validation_matrix(self):
        """One rule per named row; the expected fragment pins WHICH rule fires.

        Fragments are what make mis-aimed cases impossible to miss: a row whose
        violation triggers an earlier, stricter rule fails its assertIn instead
        of passing silently (three rows were mis-aimed exactly that way before).
        subTest reports every failing row in one run rather than stopping at the
        first. Error messages carry the NAMED path ('W' -> "T" -> "a") once the
        names are known; numbers survive only where there is nothing else to
        name (the name itself is the defect).
        """
        ws = self.ok_ws()
        ws_none = '[[workspace]]\nname = "W"\n'  # workspace without a path
        tab = ws + self.ok_tab()
        tab_none = ws_none + self.ok_tab()
        p1 = self.p1()
        rows = [
            ("empty document", "", "no [[workspace]] declared"),
            ("workspace entry not a table", 'workspace = [1]\n',
             "[[workspace]] #1: expected a table"),
            ("workspace scalar instead of array", 'workspace = "W"\n',
             "expected a list of tables"),
            # Schema v2 strays must fail LOUDLY (they would otherwise parse
            # as an empty v3 document with every tab silently missing):
            # a top-level [[tab]] is the dead back-ref form.
            ("v2 stray top-level tab", ws + '[[tab]]\nname = "T"\n',
             "top-level [[tab]] is schema v2"),
            ("v2 stray top-level pane", ws + '[[pane]]\nname = "p"\n',
             "top-level [[pane]] is schema v2"),
            # `tab = ...` must precede any [[table]] header or it lands INSIDE
            # that table (TOML key-values bind to the most recent header).
            ("nested tab not an array", ws + '[workspace.tab]\nname = "T"\n',
             '"tab" must be a list of tables'),
            ("pane entry not a table", tab + 'pane = [1]\n',
             'pane #1: expected a table'),
            ("pane scalar instead of array", tab + 'pane = "x"\n',
             '"pane" must be a list of tables'),
            ("empty workspace name", '[[workspace]]\nname = ""\npath = "/tmp"\n',
             "[[workspace]] #1: name is required"),
            ("missing workspace name", '[[workspace]]\npath = "/tmp"\n',
             "[[workspace]] #1: name is required"),
            ("duplicate workspace name",
             ws + '[[workspace]]\nname = "W"\npath = "/tmp"\n',
             'duplicate workspace name'),
            ("relative workspace path", '[[workspace]]\nname = "W"\npath = "rel"\n',
             "absolute or ~-prefixed"),
            ("~user path", '[[workspace]]\nname = "W"\npath = "~ecc/x"\n',
             "~user paths are not supported"),
            ("workspace path wrong type", '[[workspace]]\nname = "W"\npath = 5\n',
             "path must be a string"),
            ("empty tab name", ws + '[[workspace.tab]]\nname = ""\n',
             'tab #1: name is required'),
            ("missing tab name", ws + '[[workspace.tab]]\npath = "repo"\n',
             'tab #1: name is required'),
            ("duplicate tab name per workspace",
             tab + '[[workspace.tab]]\nname = "T"\n' + p1,
             'duplicate tab name'),
            ("tab path wrong type",
             ws + self.ok_tab('path = 5\n') + p1,
             "path must be a string"),
            ("relative tab path under path-less workspace",
             tab_none + 'path = "sub"\n' + p1, "relative path but workspace"),
            ("no path anywhere", tab_none + p1, "no path and workspace"),
            ("direction forbidden on root pane",
             tab + '[[workspace.tab.pane]]\nname = "a"\ndirection = "right"\n',
             'direction forbidden'),
            ("wrong-typed cwd on root pane",
             tab + '[[workspace.tab.pane]]\nname = "a"\ncwd = 5\n',
             "cwd must be a string"),
            ("missing direction on pane #2", tab + p1 + '[[workspace.tab.pane]]\nname = "b"\n',
             "direction must be one of"),
            ("invalid direction enum on pane #2",
             tab + p1 + '[[workspace.tab.pane]]\nname = "b"\ndirection = "up"\n',
             "direction must be one of"),
            ("duplicate pane name",
             tab + p1 + '[[workspace.tab.pane]]\nname = "a"\ndirection = "right"\n',
             "duplicate pane name 'a'"),
            ("empty pane name", tab + '[[workspace.tab.pane]]\nname = ""\n',
             "pane #1: name is required"),
            ("ratio out of range",
             tab + p1 + '[[workspace.tab.pane]]\nname = "b"\ndirection = "down"\nratio = 1.5\n',
             "0 < ratio < 1"),
            # The boundary values themselves: the schema is the OPEN interval,
            # so 0 and 1 are exactly as invalid as 1.5 (and a TOML `0`/`1`
            # arrives as int -- the numeric-type gate must let ints through
            # to the range check, not trip "must be a number" first).
            ("ratio zero is not neutral",
             tab + p1 + '[[workspace.tab.pane]]\nname = "b"\ndirection = "down"\nratio = 0\n',
             "0 < ratio < 1"),
            ("ratio one is not neutral",
             tab + p1 + '[[workspace.tab.pane]]\nname = "b"\ndirection = "down"\nratio = 1\n',
             "0 < ratio < 1"),
            ("ratio true is not a number",
             tab + p1 + '[[workspace.tab.pane]]\nname = "b"\ndirection = "down"\nratio = true\n',
             "ratio must be a number"),
            ("ratio string is not a number",
             tab + p1 + '[[workspace.tab.pane]]\nname = "b"\ndirection = "down"\nratio = "0.5"\n',
             "ratio must be a number"),
            ("agent wrong type",
             tab + p1 + '[[workspace.tab.pane]]\nname = "b"\ndirection = "down"\nagent = 5\n',
             "agent must be a string"),
            ("command wrong type",
             tab + p1 + '[[workspace.tab.pane]]\nname = "b"\ndirection = "down"\ncommand = true\n',
             "command must be a string"),
            ("agent and command together",
             tab + '[[workspace.tab.pane]]\nname = "a"\nagent = "claude"\ncommand = "x"\n',
             "mutually exclusive"),
            # _str_field guards (typed TOML scalars must die at the boundary,
            # never as TypeError deep in the reconciler).
            ("pane cwd wrong type",
             tab + p1 + '[[workspace.tab.pane]]\nname = "b"\ndirection = "down"\ncwd = 5\n',
             "cwd must be a string"),
        ]
        for name, body, fragment in rows:
            with self.subTest(case=name):
                with self.assertRaises(self.m.ValidationError) as cm:
                    self.write(body)
                self.assertIn(fragment, str(cm.exception))

    def test_error_messages_carry_named_paths(self):
        """Once a name exists, errors name it (v3): 'W' -> "T" -> "a" style,
        never an index. Numbers remain only for unnameable defects (the
        name itself missing), pinned by the matrix rows above.
        """
        body = (self.ok_ws() + self.ok_tab()
                + '[[workspace.tab.pane]]\nname = "a"\n'
                + '[[workspace.tab.pane]]\nname = "b"\ndirection = "sideways"\n')
        with self.assertRaises(self.m.ValidationError) as cm:
            self.write(body)
        self.assertIn('"W" -> "T" -> "b"', str(cm.exception))

    def test_valid_documents(self):
        """Must-load rows: (name, toml, checker); checkers pin resolved values."""
        ws = self.ok_ws()
        tab = ws + self.ok_tab()
        p1 = self.p1()

        def check_labels_across_workspaces(model):
            self.assertEqual([w["label"] for w in model["workspaces"]], ["W1", "W2"])
            self.assertEqual([t["label"] for w in model["workspaces"] for t in w["tabs"]],
                             ["T", "T"])

        def check_tab_path_inherits_workspace(model):
            t = model["workspaces"][0]["tabs"][0]
            self.assertEqual(t["path"], self.real)
            self.assertEqual(t["panes"][0]["cwd"], self.real)

        def check_bare_tilde_expansion(model):
            home = os.path.expanduser("~")
            w = model["workspaces"][0]
            # regression: bare "~" used to expand to "$HOME/" (trailing separator
            # from os.path.join(home, "")) before _expand learned normpath
            self.assertEqual(w["path"], home)
            self.assertEqual(w["tabs"][0]["path"], home)  # inherited

        def check_paneless_tab_over_missing_dir_is_skipped(model):
            t = model["workspaces"][0]["tabs"][0]
            self.assertIn("skip_reason", t)
            self.assertEqual(t["panes"], [])  # gate is isdir, not pane count

        def check_symlinked_tab_path_resolves(model):
            # A tab whose declared path only exists THROUGH a symlink is a
            # real tab, not a skip: realpath canonicalizes it to the target,
            # the dir herdr itself will report -- matching/adoption compare
            # canonical strings (live case: ~/sls-projects ->
            # ~/storyloom-studio-projects).
            t = model["workspaces"][0]["tabs"][0]
            self.assertNotIn("skip_reason", t)
            self.assertEqual(t["path"], os.path.join(self.real, "repo"))
            self.assertEqual(t["panes"][0]["cwd"], os.path.join(self.real, "repo"))

        def ws_row(name, path):
            return f'[[workspace]]\nname = "{name}"\npath = "{path}"\n'

        def ws_tab(name):
            return f'[[workspace.tab]]\nname = "{name}"\n'

        # (kept explicit rather than a clever one-liner: nesting makes the
        # two-workspace case a sequence of ws-then-its-tab blocks)
        rows = [
            ("same tab name allowed across different workspaces",
             ws_row("W1", self.real) + ws_tab("T") + p1
             + ws_row("W2", "/tmp") + ws_tab("T") + p1,
             check_labels_across_workspaces),
            ("omitted tab.path inherits workspace path",
             tab + p1, check_tab_path_inherits_workspace),
            ("bare ~ expands to $HOME without trailing separator",
             '[[workspace]]\nname = "W"\npath = "~"\n' + ws_tab("T") + p1,
             check_bare_tilde_expansion),
            ("pane-less tab over missing directory is skipped",
             ws + self.ok_tab('path = "nope"\n'),
             check_paneless_tab_over_missing_dir_is_skipped),
            ("tab path through a symlinked directory resolves to the real path",
             ws + self.ok_tab(f'path = "{self.link}"\n') + p1,
             check_symlinked_tab_path_resolves),
        ]
        for name, body, check in rows:
            with self.subTest(case=name):
                check(self.write(body))

    def test_skip_reason_tab_carries_panes(self):
        """A skipped tab keeps its panes so the reconciler can warn per pane."""
        m = self.write(self.ok_ws() + self.ok_tab('path = "nope"\n')
                       + '[[workspace.tab.pane]]\nname = "a"\n'
                       + '[[workspace.tab.pane]]\nname = "b"\ndirection = "down"\n')
        t = m["workspaces"][0]["tabs"][0]
        self.assertIn("skip_reason", t)
        self.assertEqual([p["name"] for p in t["panes"]], ["a", "b"])

    # --- open()/parse failures: the other half of the exit-2 contract -------

    def test_malformed_toml_is_a_validation_error(self):
        """tomllib.TOMLDecodeError must surface as ValidationError ("TOML
        parse error"), never as an uncaught traceback: a malformed document
        is a TOML problem, so the exit-2-before-any-socket contract applies
        exactly like a schema violation."""
        with open(self.toml, "w") as f:
            f.write('[[workspace]\nname = "W"\n')  # unterminated header
        with self.assertRaises(self.m.ValidationError) as cm:
            self.m.load_model(self.toml)
        self.assertIn("TOML parse error", str(cm.exception))

    def test_unreadable_toml_is_a_validation_error(self):
        """Every open() failure keeps the validation contract: the OSError
        handler deliberately groups FileNotFoundError AND the rest
        (IsADirectoryError on `--toml <dir>`, PermissionError, ...) because
        they are all pre-connection -- neither may escape as a raw OSError."""
        missing = os.path.join(self.tmp, "nope.toml")
        rows = [
            ("missing file", missing),
            ("path is a directory", self.tmp),  # IsADirectoryError
        ]
        for name, path in rows:
            with self.subTest(case=name):
                with self.assertRaises(self.m.ValidationError) as cm:
                    self.m.load_model(path)
                self.assertIn("cannot read TOML", str(cm.exception))

if __name__ == "__main__":
    unittest.main()
