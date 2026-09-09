import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import fake_herdr
import harness


class SocketCase(unittest.TestCase):
    def setUp(self):
        self.m, self.fake, self.client = harness.make_env(self)

    def _spawn(self, fake, timeout=None):
        """Start a fake SUBCLASS on its own socket path (setUp's base fake
        keeps serving) and return (fake, client). Own temp dir per fake:
        two fakes must never share a socket file. The client is built from
        setUp's module (`module=self.m`) so identity-sensitive asserts --
        assertRaises on self.m.HerdrError -- see the same load."""
        return harness.make_env(self, fake=fake, module=self.m,
                                timeout=timeout)[1:]

    def test_roundtrip(self):
        r = self.client.call("workspace.create", {"label": "W", "cwd": "/tmp"})
        self.assertEqual(r["workspace"]["label"], "W")
        listing = self.client.call("workspace.list")
        self.assertEqual(listing["workspaces"][0]["label"], "W")

    def test_error_raises_with_message(self):
        self.fake.fail_next("workspace.create", "scripted boom")
        with self.assertRaises(self.m.HerdrError) as cm:
            self.client.call("workspace.create", {"label": "X"})
        self.assertIn("scripted boom", str(cm.exception))
        # A server-side error is a protocol ANSWER, not transport damage:
        # the same client keeps working afterwards.
        r = self.client.call("workspace.create", {"label": "Y"})
        self.assertEqual(r["workspace"]["label"], "Y")

    def test_dict_error_frame_message_extracted(self):
        """Real herdr error frames carry {'code', 'message'} objects, not
        plain strings; the client must surface "message", not the dict repr.

        Wraps every fake error into the real frame shape via a subclass (own
        socket path via _spawn, setUp's fake untouched). The code mirrors
        the real frames' STRING codes (e.g. "conflict"), not an int -- the
        client never reads "code", but the fake should not pin a wrong
        shape either.
        """

        class DictErrors(fake_herdr.FakeHerdr):
            def _dispatch(self, req):
                resp = super()._dispatch(req)
                if "error" in resp:
                    resp["error"] = {"code": "conflict", "message": resp["error"]}
                return resp

        fake, client = self._spawn(DictErrors())
        fake.fail_next("workspace.create", "scripted boom")
        with self.assertRaises(self.m.HerdrError) as cm:
            client.call("workspace.create", {"label": "X"})
        msg = str(cm.exception)
        self.assertIn("scripted boom", msg)
        self.assertNotIn("{", msg)  # no dict repr leaked into the text

    def test_one_request_per_connection(self):
        """herdr 0.8.2 closes the connection after EVERY reply (live-probed
        2026-09-08: a second request on the same socket dies with EPIPE/RST,
        read-only or not -- design spec, Evidence appendix, side-finding 1).

        The fake here mimics that contract. The client must open a fresh
        connection per call: with the old reuse design the second call
        raised HerdrError, which no fake keeping connections open could
        ever catch.
        """

        class OneRequestPerConn(fake_herdr.FakeHerdr):
            def _handle_conn(self, conn):
                # Parent framing logic, cut to exactly one request: read a
                # frame, answer it, close. EOF/short-read -> nothing to
                # answer; either way this connection is done.
                try:
                    buf = conn.recv(65536)
                    line, _ = buf.split(b"\n", 1)
                    conn.sendall(json.dumps(self._dispatch(json.loads(line))).encode() + b"\n")
                except (OSError, ValueError):
                    pass
                finally:
                    conn.close()

        fake, client = self._spawn(OneRequestPerConn())
        r = client.call("workspace.create", {"label": "W", "cwd": "/tmp"})
        ws_id = r["workspace"]["workspace_id"]
        # Second call on the SAME client: must silently ride a fresh
        # connection, not surface the server's close as an error.
        listing = client.call("tab.list", {"workspace_id": ws_id})
        self.assertEqual(listing["tabs"][0]["workspace_id"], ws_id)
        self.assertEqual([m for m, _ in fake.calls],
                         ["workspace.create", "tab.list"])

    def test_layout_apply_replacement(self):
        r = self.client.call("workspace.create", {"label": "W", "cwd": "/tmp"})
        ws_id, root_tab = r["workspace"]["workspace_id"], r["tab"]["tab_id"]
        tree = {"type": "split", "direction": "right", "ratio": 0.5,
                "first": {"type": "pane", "label": "a", "cwd": "/tmp"},
                "second": {"type": "pane", "label": "b", "cwd": "/tmp"}}
        self.client.call("layout.apply", {"tab_id": root_tab, "root": tree})
        self.assertNotIn(root_tab, self.fake.tabs)  # old tab closed
        new_tabs = [t for t in self.fake.tabs.values() if t["workspace_id"] == ws_id]
        self.assertEqual(len(new_tabs), 1)
        labels = [self.fake.panes[pi]["label"] for pi in new_tabs[0]["pane_ids"]]
        self.assertEqual(labels, ["a", "b"])  # tree order preserved

    def test_event_frames_are_skipped(self):
        """A server-pushed frame with no id must not be mistaken for a reply.

        The base fake only ever answers requests, so the client's skip path
        would go untested. The pre_reply override puts the event on the wire
        deterministically ahead of the awaited response; whether the kernel
        hands the client one coalesced recv or two, framing must skip the
        id-less frame and keep waiting.
        """

        class Noisy(fake_herdr.FakeHerdr):
            def pre_reply(self, conn, req):
                conn.sendall(json.dumps({"event": "pane.output",
                                         "data": "noise"}).encode() + b"\n")

        fake, client = self._spawn(Noisy())
        r = client.call("workspace.create", {"label": "W", "cwd": "/tmp"})
        self.assertEqual(r["workspace"]["label"], "W")
        self.assertEqual(fake.calls, [("workspace.create", {"label": "W", "cwd": "/tmp"})])

    def test_garbled_frames_raise_herdr_error_naming_the_method(self):
        """Both garble shapes must land in the same HerdrError, never as a
        raw exception: (a) bytes that are not JSON at all -- the decode
        pair of the transport guard; (b) VALID JSON that is not an object
        (a bare array) -- resp.get would die with an AttributeError past
        every guard, so call() shapes it explicitly. The method name in the
        message is what makes a reconcile log diagnosable."""
        rows = [
            ("not json at all", b"not json at all\n"),
            ("valid json, non-object", b'["not", "an", "object"]\n'),
        ]
        for name, frame in rows:
            with self.subTest(case=name):

                class Garbled(fake_herdr.FakeHerdr):
                    def pre_reply(self, conn, req):
                        conn.sendall(frame)

                fake, client = self._spawn(Garbled())
                with self.assertRaises(self.m.HerdrError) as cm:
                    client.call("workspace.list")
                msg = str(cm.exception)
                self.assertIn("workspace.list", msg)

    def test_send_keys_rejects_literal_text_atomically(self):
        """Fake-fidelity pin (live probe 2026-09-08, design-spec receipt 3):
        the REAL server takes pane.send_keys KEY NAMES only and rejects
        literal text; the fake must keep mirroring that -- atomically
        (nothing typed when any element is bad) -- or text-as-keys bugs
        survive fake-based tests exactly the way the one-shot bug did."""
        r = self.client.call("workspace.create", {"label": "W", "cwd": "/tmp"})
        pid = r["root_pane"]["pane_id"]
        with self.assertRaises(self.m.HerdrError) as cm:
            self.client.call("pane.send_keys", {"pane_id": pid,
                                                "keys": ["echo hi", "Enter"]})
        self.assertIn("unsupported key echo hi", str(cm.exception))
        self.assertIsNone(self.fake.panes[pid].get("typed"))  # nothing applied

    def test_close_then_reconnect(self):
        r = self.client.call("workspace.create", {"label": "W", "cwd": "/tmp"})
        ws_id = r["workspace"]["workspace_id"]
        self.client.close()  # API-compatible no-op (see HerdrSocket.close)
        # The next call must work exactly like any other: every call rides
        # its own connection, so nothing done before can poison it.
        listing = self.client.call("workspace.list")
        self.assertEqual(listing["workspaces"][0]["workspace_id"], ws_id)

    def test_server_eof_raises_herdr_error(self):
        # Server parsed the request but died before replying: the client must
        # surface the EOF as HerdrError, not hang or leak the open socket.
        self.fake.drop_next = True
        with self.assertRaises(self.m.HerdrError) as cm:
            self.client.call("workspace.list")
        self.assertIn("socket closed by server", str(cm.exception))

    def test_deadline_bounds_event_trickling(self):
        """The socket timeout is per-recv, so it alone cannot bound a call: a
        server trickling event frames <timeout apart would keep call() alive
        forever. The monotonic per-request deadline must fire even though
        every individual recv succeeds (0.05s gaps vs a 0.25s budget).
        """

        class Trickle(fake_herdr.FakeHerdr):
            def pre_reply(self, conn, req):
                for _ in range(12):  # ~0.6s of ticks; the reply lands last
                    try:
                        conn.sendall(json.dumps({"event": "tick"}).encode() + b"\n")
                    except OSError:
                        return  # client hit the deadline and hung up
                    time.sleep(0.05)

        _, client = self._spawn(Trickle(), timeout=0.25)
        with self.assertRaises(self.m.HerdrError) as cm:
            client.call("workspace.list")
        # Both deadline exits (clean budget check, or the recv clamped to the
        # remaining budget) name the method; either proves the call gave up
        # instead of riding the full 0.6s trickle into the late reply.
        self.assertIn("workspace.list", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
