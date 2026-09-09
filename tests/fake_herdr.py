"""Scripted herdr replacement for tests. Mirrors the semantics the seeder
relies on: layout.apply with a tab_id REPLACES the whole tab (new id, old
closed); pane.split has no label; agent.start sets PaneInfo.agent."""
import json
import os
import socket
import threading


class FakeHerdr:
    def __init__(self):
        self.workspaces = {}   # id -> {label, path, tab_ids}
        self.tabs = {}         # id -> {label, workspace_id, pane_ids, cwd}
        self.panes = {}        # id -> {label, tab_id, cwd, foreground_cwd, agent}
        self.calls = []
        self._fail = None
        self.drop_next = False  # close the conn instead of replying (EOF tests)
        self._n = 0
        self.tmpdir = None
        self.path = None
        self._sock = None

    def start(self):
        assert self.tmpdir, "set fake.tmpdir before start()"
        path = os.path.join(self.tmpdir, "herdr.sock")
        if os.path.exists(path):
            os.unlink(path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(4)
        self.path = path
        self._sock = srv
        threading.Thread(target=self._serve, daemon=True).start()
        return path

    def fail_next(self, method, message="scripted failure"):
        self._fail = (method, message)

    def stop(self):
        # Close the listener so the accept thread exits: deterministic
        # teardown, instead of leaving the thread parked in accept().
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn):
        buf = b""
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    req = json.loads(line)
                    if self.drop_next:
                        self.drop_next = False
                        return  # finally closes: parsed but never replied
                    self.pre_reply(conn, req)
                    conn.sendall(json.dumps(self._dispatch(req)).encode() + b"\n")
        except OSError:
            pass  # client hung up mid-frame or mid-reply: nothing left to answer
        finally:
            # Always close our side -- else accepted fds linger until the GC
            # finalizes them (ResourceWarning noise in test output).
            conn.close()

    def pre_reply(self, conn, req):
        """Optional hook: runs after a request is parsed, before its reply.

        Override to put extra frames on the wire ahead of the response --
        deterministic event injection for the client's skip path
        (tests/test_socket.py: Noisy, Trickle)."""


    def _dispatch(self, req):
        method, params = req["method"], req.get("params", {})
        self.calls.append((method, params))
        if self._fail and self._fail[0] == method:
            msg = self._fail[1]
            self._fail = None
            return {"id": req["id"], "error": f"{method}: {msg}"}
        try:
            result = getattr(self, "m_" + method.replace(".", "_"))(params)
        except Exception as e:
            return {"id": req["id"], "error": str(e)}
        return {"id": req["id"], "result": result}

    def _id(self, kind):
        self._n += 1
        return f"w1:{kind}{self._n}"

    def m_workspace_list(self, p):
        return {"workspaces": [{"workspace_id": i, "label": w["label"]}
                               for i, w in self.workspaces.items()]}

    def m_workspace_create(self, p):
        i = self._id("ws")
        self.workspaces[i] = {"label": p.get("label"), "path": p.get("cwd"), "tab_ids": []}
        ti = self._id("t")
        pi = self._id("p")
        # Live-probed fidelity (design spec, Evidence appendix, side-finding
        # 2): herdr's implicit root tab arrives LABELED, number-derived --
        # {"tab_id": "wJ:t1", "number": 1, "label": "1"} -- never unnamed.
        # Label-based snapshot matching and the seeder's adopt/rename of the
        # implicit tab both depend on this.
        self.tabs[ti] = {"label": "1", "workspace_id": i, "pane_ids": [pi],
                         "cwd": p.get("cwd")}
        self.panes[pi] = {"label": None, "tab_id": ti, "cwd": p.get("cwd"),
                          "foreground_cwd": p.get("cwd"), "agent": None}
        self.workspaces[i]["tab_ids"].append(ti)
        return {"workspace": {"workspace_id": i, "label": p.get("label")},
                "tab": {"tab_id": ti}, "root_pane": {"pane_id": pi}}

    def m_tab_list(self, p):
        tabs = [{"tab_id": i, "label": t["label"], "workspace_id": t["workspace_id"],
                 "pane_count": len(t["pane_ids"])}
                for i, t in self.tabs.items()
                if p.get("workspace_id") in (None, t["workspace_id"])]
        return {"tabs": tabs}

    def m_tab_create(self, p):
        i = self._id("t")
        pi = self._id("p")
        self.tabs[i] = {"label": p.get("label"), "workspace_id": p["workspace_id"],
                        "pane_ids": [pi], "cwd": p.get("cwd")}
        self.workspaces[p["workspace_id"]]["tab_ids"].append(i)
        self.panes[pi] = {"label": None, "tab_id": i, "cwd": p.get("cwd"),
                          "foreground_cwd": p.get("cwd"), "agent": None}
        return {"tab": {"tab_id": i, "label": p.get("label"),
                        "workspace_id": p["workspace_id"]},
                "root_pane": {"pane_id": pi}}

    def m_tab_rename(self, p):
        self.tabs[p["tab_id"]]["label"] = p.get("label")
        return {}

    def m_pane_list(self, p):
        out = []
        for i, t in self.tabs.items():
            if p.get("workspace_id") in (None, t["workspace_id"]):
                for pi in t["pane_ids"]:
                    pn = self.panes[pi]
                    out.append({"pane_id": pi, "label": pn["label"], "tab_id": i,
                                "cwd": pn["cwd"], "foreground_cwd": pn["foreground_cwd"],
                                "agent": pn["agent"],
                                "agent_name": pn.get("agent_name")})
        return {"panes": out}

    def _walk(self, node, tab_id):
        if node["type"] == "pane":
            pi = self._id("p")
            self.panes[pi] = {"label": node.get("label"), "tab_id": tab_id,
                              "cwd": node.get("cwd"), "foreground_cwd": node.get("cwd"),
                              "agent": None}
            self.tabs[tab_id]["pane_ids"].append(pi)
            return
        self._walk(node["first"], tab_id)
        self._walk(node["second"], tab_id)

    def m_layout_apply(self, p):
        # Replacement semantics (the governing fact): new tab created, old closed.
        if p.get("tab_id"):
            wid = self.tabs[p["tab_id"]]["workspace_id"]
        else:
            wid = p.get("workspace_id")
        ni = self._id("t")
        label = p.get("tab_label")
        if label is None and p.get("tab_id"):
            label = self.tabs[p["tab_id"]]["label"]
        self.tabs[ni] = {"label": label, "workspace_id": wid, "pane_ids": [], "cwd": None}
        self._walk(p["root"], ni)
        if p.get("tab_id"):
            old = self.tabs.pop(p["tab_id"])
            for pi in old["pane_ids"]:
                self.panes.pop(pi, None)
            self.workspaces[wid]["tab_ids"] = [
                i for i in self.workspaces[wid]["tab_ids"] if i != p["tab_id"]]
            self.workspaces[wid]["tab_ids"].append(ni)
        elif p.get("workspace_id"):
            self.workspaces[p["workspace_id"]]["tab_ids"].append(ni)
        return {"tab": {"tab_id": ni, "label": self.tabs[ni]["label"]}}

    def m_pane_split(self, p):
        pi = self._id("p")
        tab_id = self.panes[p["target_pane_id"]]["tab_id"]
        self.panes[pi] = {"label": None, "tab_id": tab_id, "cwd": p.get("cwd"),
                          "foreground_cwd": p.get("cwd"), "agent": None}
        self.tabs[tab_id]["pane_ids"].append(pi)
        return {"pane": {"pane_id": pi}}

    def m_pane_rename(self, p):
        self.panes[p["pane_id"]]["label"] = p.get("label")
        return {}

    # Key names the fake accepts on pane.send_keys. Faithful to the REAL
    # server (live probe 2026-09-08): send_keys takes KEY NAMES only -- it
    # rejected literal text with "unsupported key echo scratch-logs-marker".
    # The first fake accepted anything, which is exactly why the seeder's
    # text-as-key bug survived four fake-based test tasks.
    KEY_NAMES = {"Enter", "Escape", "Tab", "Backspace", "Delete",
                 "Up", "Down", "Left", "Right", "Home", "End",
                 "PageUp", "PageDown"}

    def m_pane_send_keys(self, p):
        # Validate the WHOLE list before appending any: the real server
        # rejects atomically (nothing typed when one element is bad), never
        # applies a prefix of the keys. Bare message -- the socket client
        # prepends "method: " itself; a self-prefixed error would reach the
        # caller doubled ("pane.send_keys: pane.send_keys: ...").
        keys = p.get("keys", [])
        for k in keys:
            if k not in self.KEY_NAMES:
                raise ValueError(f"unsupported key {k}")
        self.panes[p["pane_id"]].setdefault("typed", []).extend(keys)
        return {}

    def m_pane_send_text(self, p):
        self.panes[p["pane_id"]].setdefault("typed", []).append(p["text"])
        return {}

    def m_agent_start(self, p):
        self.panes[p["pane_id"]]["agent"] = p["kind"]
        self.panes[p["pane_id"]]["agent_name"] = p["name"]
        return {}
