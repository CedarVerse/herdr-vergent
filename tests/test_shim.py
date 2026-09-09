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


class ShimCase(unittest.TestCase):
    def run_shim(self, *args):
        return subprocess.run(["bash", str(SHIM), *args],
                              capture_output=True, text=True, timeout=30)

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


if __name__ == "__main__":
    unittest.main()
