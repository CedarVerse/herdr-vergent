#!/usr/bin/env python3
# The shim (bin/vergent.sh) owns interpreter selection; this shebang is
# informational, for direct ./invocation only.
"""vergent seeder: declarative reconcile of herdr state from TOML.

Declares workspaces -> tabs -> panes (schema v3, see projects.example.toml
and the README); converges the running server to the file. Stdlib only.
Never spawns herdr; talks to the running server's socket (HERDR_SOCKET_PATH,
injected by the plugin runtime, or ~/.config/herdr/herdr.sock).
"""
import argparse
import json
import os
import socket
import sys
import time
# Import order is load-bearing here: sys must precede tomllib so the guard
# below can print and exit on the box's default interpreter (3.10 has no
# tomllib). Direct `./vergent.py` invocation bypasses the shim's
# interpreter probe AND its flock + no-lazy-spawn guard -- a raw
# ModuleNotFoundError would be ugly, but worse, a 3.11+ python would run
# unguarded. The clean exit-2 steers every invocation back through the shim.
try:
    import tomllib
except ModuleNotFoundError:
    print("vergent: python >= 3.11 required (tomllib missing); "
          "run bin/vergent.sh (it probes the right interpreter)",
          file=sys.stderr)
    sys.exit(2)

# Contract (see shim + plan): 0 ok | 1 failures | 2 validation | 3 lock-busy (returned by the shim, not here)
EXIT_OK, EXIT_FAILED, EXIT_VALIDATION, EXIT_LOCKED = 0, 1, 2, 3

SPLIT_DIRECTIONS = ("right", "down")

# Live probe 2026-09-08 (herdr 0.8.2, protocol 20, throwaway workspace wJ):
# layout.apply with workspace_id + tab_label and NO tab_id CREATES a tab --
# tab.list afterwards listed the replacement tab (wJ:t2 "probe-tab1") AND the
# newly created wJ:t3 "probe-created". If a future herdr regresses, flip this
# to False: create_tab() then routes every tree apply through the
# tab.create + apply-on-fresh-id fallback, no other change needed. Full
# receipts: design spec, Evidence appendix.
FACT_A_CONFIRMED = True

# The probed one-call creation form (layout.apply with workspace_id +
# tab_label, no tab_id). Named once so reconcile/create_tab/tests agree on
# the string; any OTHER value deliberately takes the fallback path.
CREATION_FORM = "create-in-apply"


class ValidationError(Exception):
    """Structural/schema problem: aborts before any socket connection."""


def _expand(p):  # rule 1: bare ~ only
    if p == "~" or p.startswith("~/"):
        # normpath kills the trailing separator bare "~" would produce
        # (os.path.join(home, "") == "$HOME/"), which would otherwise leak
        # into tab.create/pane.split payloads.
        return os.path.normpath(os.path.join(os.path.expanduser("~"), p[2:] if len(p) > 1 else ""))
    if p.startswith("~"):
        raise ValidationError(f"~user paths are not supported: {p!r}")
    return p


def _is_rel(p):
    return not p.startswith(("/", "~"))


def _join(base, rel):
    return os.path.normpath(os.path.join(base, rel))


def _nonempty(v, what):
    if not isinstance(v, str) or not v.strip():
        raise ValidationError(f"{what}: required and non-empty")
    return v


def _str_field(v, what):
    # TOML scalars are typed; a wrong-typed path/cwd would otherwise escape
    # as TypeError/AttributeError from startswith/join instead of a
    # ValidationError naming the offending entry.
    if v is not None and not isinstance(v, str):
        raise ValidationError(f"{what} must be a string")
    return v


def _load_pane(p, ctx, pane_index, tab_path):
    """Validate one [[workspace.tab.pane]] entry and return its model dict.

    ctx is the enclosing named path (e.g. 'System -> "ShellBase" -> ');
    once the pane's own name is parsed, every message carries it. Numbers
    appear only where there is nothing else to name (a pane whose name
    itself is missing). Name uniqueness is the CALLER's concern (seen_pane
    spans entries, this function sees one); everything intrinsic to a
    single pane lives here.
    """
    name = _nonempty(p.get("name"), f"{ctx}pane #{pane_index}: name is required")
    what = f'{ctx}"{name}"'
    direction = p.get("direction")
    if pane_index == 1:
        # The root pane splits off nothing, so it takes no direction. Its
        # cwd is OPTIONAL though (schema relax, 2026-09-09): omitting it
        # keeps the tab path as the inheritance base (the original rule --
        # "pane #1's cwd IS the tab path"), while allowing an explicit one
        # makes the whole pane list uniform (every pane = name + optional
        # cwd relative to the tab), so a tab can inherit its workspace's
        # base and declare all panes as sibling deltas. No ../ gymnastics.
        if direction is not None:
            raise ValidationError(f"{what}: direction forbidden (root pane)")
        cwd = p.get("cwd")
    else:
        if direction not in SPLIT_DIRECTIONS:
            raise ValidationError(f"{what}: direction must be one of {SPLIT_DIRECTIONS}")
        cwd = p.get("cwd")
    if cwd is None:
        cwd = tab_path  # normalized: the tab path is already realpath'd
    else:
        cwd = _expand(_str_field(cwd, f"{what}: cwd"))
        if _is_rel(cwd):
            cwd = _join(tab_path, cwd)
        # Same symlink canonicalization as the tab path: the pane's own
        # component may route through a symlink (../data -> /srv/data).
        cwd = os.path.realpath(cwd)
    ratio = p.get("ratio")
    if ratio is not None:
        # bool is an int subclass in Python; ratio = true must not pass
        # as 1 (and a TOML string must not die on `0 < "0.5"`).
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
            raise ValidationError(f"{what}: ratio must be a number")
        if not (0 < ratio < 1):
            raise ValidationError(f"{what}: ratio must be 0 < ratio < 1")
    # These feed agent.start/--kind or a shell later: a non-string here
    # must be rejected at the TOML boundary, not surface as garbage in
    # the model (or a confusing TypeError deep in the reconciler).
    agent = _str_field(p.get("agent"), f"{what}: agent")
    command = _str_field(p.get("command"), f"{what}: command")
    if agent is not None and command is not None:
        raise ValidationError(f"{what}: agent and command are mutually exclusive")
    return {"name": name, "cwd": cwd, "direction": direction,
            "ratio": ratio, "agent": agent, "command": command}


def load_model(toml_path):
    """Parse projects.toml (schema v3) into the reconciler's model.

    File shape (v3, 2026-09-09): full nesting -- [[workspace]] holds
    [[workspace.tab]] tables, which hold [[workspace.tab.pane]] tables.
    tomllib builds the association natively (workspace[i]["tab"][j]
    ["pane"][k]), so there is no back-reference to validate and no
    dangling-reference error class. The file-facing identity key is
    `name` at every level; the MODEL keeps `label` -- herdr's wire word
    (workspace.list / tab.rename / pane.rename all say label) -- so the
    reconciler below is untouched by the rename. A workspace with no
    [[workspace.tab]] tables is legal (an empty Space; the walk creates
    it and warns about the unconsumed implicit root tab).
    """
    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValidationError(f"TOML parse error: {e}")
    except OSError as e:
        # FileNotFoundError AND the other open() failures (IsADirectoryError
        # on `--toml <dir>`, PermissionError, ...) -- all pre-connection,
        # so all keep the validation-error / exit-2 contract.
        raise ValidationError(f"cannot read TOML: {e}")

    # A v2 file (top-level [[tab]] / [[tab.pane]]) must fail LOUDLY, not
    # parse as an empty-v3 document with every tab silently missing.
    for stray in ("tab", "pane"):
        if stray in data:
            raise ValidationError(
                f"top-level [[{stray}]] is schema v2; nest it: "
                f"[[workspace.tab]] / [[workspace.tab.pane]]")

    ws_in = data.get("workspace")
    if ws_in is None:
        raise ValidationError("no [[workspace]] declared")
    if not isinstance(ws_in, list):
        raise ValidationError("[[workspace]]: expected a list of tables")
    workspaces, seen_ws = [], set()
    for i, w in enumerate(ws_in, 1):
        if not isinstance(w, dict):
            raise ValidationError(f"[[workspace]] #{i}: expected a table")
        name = _nonempty(w.get("name"), f"[[workspace]] #{i}: name is required")
        what = f'workspace "{name}"'
        if name in seen_ws:
            raise ValidationError(f"{what}: duplicate workspace name")
        seen_ws.add(name)
        raw = _str_field(w.get("path"), f"{what}: path")
        wpath = _expand(raw) if raw is not None else None
        if wpath is not None and _is_rel(wpath):
            raise ValidationError(f"{what}: path must be absolute or ~-prefixed")
        if wpath is not None:
            # Canonicalize through symlinks AFTER the relative-path rejection
            # (realpath would silently absolutize a relative path and dodge
            # the check): every model path is matched against LIVE state
            # herdr reports canonicalized, so ~/sls-projects (a symlink to
            # ~/storyloom-studio-projects) must load as its target or
            # adoption matching would compare divergent strings.
            wpath = os.path.realpath(wpath)
        wmodel = {"label": name, "path": wpath, "tabs": []}

        tabs_in = w.get("tab")
        if tabs_in is not None and not isinstance(tabs_in, list):
            raise ValidationError(f"{what}: \"tab\" must be a list of tables")
        seen_tab = set()
        for j, t in enumerate(tabs_in or [], 1):
            if not isinstance(t, dict):
                raise ValidationError(f"{what}: tab #{j}: expected a table")
            tname = _nonempty(t.get("name"),
                              f'{what}: tab #{j}: name is required')
            tctx = f'"{name}" -> "{tname}"'
            if tname in seen_tab:
                raise ValidationError(f"{tctx}: duplicate tab name")
            seen_tab.add(tname)
            raw = _str_field(t.get("path"), f"{tctx}: path")
            if raw is not None:
                path = _expand(raw)
                if _is_rel(path):
                    if wpath is None:
                        raise ValidationError(
                            f"{tctx}: relative path but workspace \"{name}\" has no path")
                    path = _join(wpath, path)
            else:
                if wpath is None:
                    raise ValidationError(
                        f"{tctx}: no path and workspace \"{name}\" has no path")
                path = wpath
            # Canonicalize AFTER resolution, BEFORE panes are loaded and the
            # isdir gate runs: pane cwds inherit/join onto this path (so they
            # need no second realpath for the workspace's own symlinks), and the
            # exists-vs-skip decision must follow symlinks too -- a tab whose
            # path only exists THROUGH a symlink is a real tab, not a skip.
            path = os.path.realpath(path)

            panes_in = t.get("pane")
            if panes_in is not None and not isinstance(panes_in, list):
                raise ValidationError(f"{tctx}: \"pane\" must be a list of tables")
            panes, seen_pane = [], set()  # names are the reconciler's anchors: unique per tab
            for k, p in enumerate(panes_in or [], 1):
                if not isinstance(p, dict):
                    raise ValidationError(f'{tctx} -> pane #{k}: expected a table')
                pane = _load_pane(p, f"{tctx} -> ", k, path)
                if pane["name"] in seen_pane:
                    raise ValidationError(f'{tctx}: duplicate pane name {pane["name"]!r}')
                seen_pane.add(pane["name"])
                panes.append(pane)

            # Every resolved path must exist -- the tab's AND every pane's.
            # Codex (2026-09-08): pane #2+ resolve their own cwd, and an
            # unchecked one used to sail through and fail server-side
            # mid-walk at layout.apply/pane.split -- aborting later tabs
            # instead of the promised per-tab skip. Scanning all panes
            # covers the whole tab (pane #1's cwd is the tab path when not
            # declared); a pane-less tab over a missing dir still skips
            # here rather than failing far away at tab.create.
            missing = None
            if not os.path.isdir(path):
                missing = f"resolved path does not exist: {path}"
            else:
                for pane in panes:
                    if not os.path.isdir(pane["cwd"]):
                        missing = (f"pane {pane['name']!r} resolved path "
                                   f"does not exist: {pane['cwd']}")
                        break
            if missing is not None:
                wmodel["tabs"].append({"label": tname, "path": path,
                                       "panes": panes, "skip_reason": missing})
            else:
                wmodel["tabs"].append({"label": tname, "path": path,
                                       "panes": panes})
        workspaces.append(wmodel)
    return {"workspaces": workspaces}


SOCKET_FALLBACK = "~/.config/herdr/herdr.sock"
REQUEST_TIMEOUT = 10.0
# agent.start's own server-side wait: below the 10s socket budget so the
# socket layer never fires first and the call keeps its timeout shape.
AGENT_START_TIMEOUT_MS = 5000


class HerdrError(Exception):
    """Request failed server-side, or the transport broke mid-call."""


class HerdrSocket:
    """JSON-RPC client for herdr's newline-delimited unix-socket protocol.

    herdr 0.8.2 serves ONE request per connection: after every reply the
    server closes the socket, and the NEXT request on it dies with EPIPE
    on send (or RST on recv) -- live-probed 2026-09-08 with ping x2 and
    workspace.list x2; read-only or mutating makes no difference (design
    spec, Evidence appendix, side-finding 1). call() therefore opens a
    FRESH connection per request: a unix-socket connect is cheap, and a
    per-call socket has no "was my request processed before the drop?"
    ambiguity to retry around. tests/test_socket.py's OneRequestPerConn
    fake pins this against a reuse regression.

    Each request carries a unique "seeder:N" id; response frames whose id
    does not match are server-pushed event notifications and are skipped
    (see tests/test_socket.py for the interleaving proof). The
    explicit-path arg is for tests; HERDR_SOCKET_PATH covers non-default
    installs.
    """

    def __init__(self, path=None, timeout=REQUEST_TIMEOUT):
        self.path = path or os.environ.get("HERDR_SOCKET_PATH") \
            or os.path.expanduser(SOCKET_FALLBACK)
        self.timeout = timeout
        self._seq = 0

    def call(self, method, params=None):
        # One guard around the whole transport path (connect -> send -> recv
        # -> decode): any failure is closed up and re-raised as HerdrError
        # naming the method, so callers never see raw socket or JSON
        # exceptions. OSError alone covers per-recv timeouts, refused
        # connections, broken pipes and resets; the decode pair covers
        # garbled frames. Server-side method failures (the "error" branch)
        # deliberately skip the guard: they are protocol answers, not
        # transport damage, and the raise there is HerdrError directly.
        #
        # finally-close is load-bearing, not tidy: connections are
        # single-request by server contract (class docstring), so every
        # call -- success or failure -- must release its socket or fds
        # pile up across a long reconcile run.
        #
        # settimeout() is per-recv, so event frames trickling in <timeout
        # apart could otherwise keep this loop alive forever; deadline is
        # the true per-request budget, re-clamped into every recv by
        # _readline.
        deadline = time.monotonic() + self.timeout
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)  # bounds connect() too
            sock.connect(self.path)
            self._seq += 1
            rid = f"seeder:{self._seq}"
            frame = json.dumps({"id": rid, "method": method, "params": params or {}})
            sock.sendall(frame.encode() + b"\n")
            buf = b""
            while True:
                line, buf = self._readline(sock, buf, deadline)
                resp = json.loads(line)
                if not isinstance(resp, dict):
                    # A garbled-but-valid-JSON frame ("[1,2]", a bare string)
                    # would otherwise die on resp.get with a raw AttributeError
                    # past every guard. Shaped here so the finally below still
                    # closes the socket and the message names the method.
                    raise HerdrError(f"{method}: non-object server frame") from None
                if resp.get("id") != rid:
                    continue  # server event frame: no id / not ours
                if "error" in resp:
                    # Real herdr error frames carry {"code", "message"}
                    # objects, so a bare f-string would repr the whole dict
                    # into the text. Prefer "message" when present; a missing
                    # key (or a legacy string frame) falls back to the raw
                    # value. Deliberately OUTSIDE the transport guard: this
                    # is an answer, not damage.
                    err = resp["error"]
                    msg = err.get("message", err) if isinstance(err, dict) else err
                    raise HerdrError(f"{method}: {msg}")
                return resp.get("result") or {}  # explicit null result -> {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            raise HerdrError(f"{method}: {e}") from e
        finally:
            sock.close()

    def close(self):
        # Kept for API compatibility (tests and future callers). With one
        # connection per call there is no persistent socket to release
        # between requests, so this is deliberately a no-op: calling it
        # before, between, or after calls must change nothing (pinned by
        # test_close_then_reconnect).
        pass

    def _readline(self, sock, buf, deadline):
        # Manual byte buffering instead of a file wrapper: herdr frames can
        # coalesce in one recv(), so split on b"\n" and keep the remainder.
        # The buffer is threaded through the caller rather than stored on
        # self: a per-call local can never leak a stale partial frame into
        # the next call, which was the reuse design's main poison risk.
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # OSError subclass: the call() guard shapes it.
                raise TimeoutError(f"no reply within {self.timeout}s")
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                # EOF is a transport failure like any other: raise OSError
                # (ConnectionResetError) and let call()'s single guard do
                # the shaping, so the message gets the method prefix
                # exactly like timeouts/resets do.
                raise ConnectionResetError("socket closed by server")
            buf += chunk
        return buf.split(b"\n", 1)


# --- Dry-run overlay ---------------------------------------------------------

class DryRunSocket:
    """Dry-run client: records mutations without forwarding them, passes the
    three list calls through to the live server, and OVERLAYS a coherent
    in-memory model of the planned tabs/panes on top.

    Why the overlay: the walk re-lists after every mutation (create_tab
    re-resolves the replacement tab by label, panes_of inventories one tab
    for one-shots). Fixed dummy ids would crash that walk, and stale lists
    would hide the planned tabs -- so every recorded mutation also updates a
    shadow model, and list answers are re-assembled as live state MINUS
    closed tabs PLUS planned ones. The reconcile walk therefore runs
    UNCHANGED against the plan.

    Two deliberate deviations from the plan sketch, both required for that
    coherence:
      - a planned workspace exists only here, so a list scoped to a dry id
        is answered from the shadow model ALONE -- forwarding a "dry:ws1"
        filter to the live server would return junk at best and abort the
        dry run at worst (a server that validates ids errors on it);
      - layout.apply ONTO a dry tab (the implicit-root form) pops that tab
        from the shadow model instead of marking it "replaced", mirroring
        live replacement semantics: old id closed, workspace inherited, new
        id issued. replaced_real stays for the real-tab case.
    """

    # Documentary, not load-bearing: call() records EVERY non-list call (an
    # unknown mutating method must be recorded too, never silently
    # forwarded), so this set is the expected universe, kept for readers and
    # future assertions. tab.rename is in it even though the plan sketch
    # omitted it -- create_tab's implicit-root form issues it.
    MUTATING = {"workspace.create", "tab.create", "tab.rename", "layout.apply",
                "pane.split", "pane.rename", "pane.send_text",
                "pane.send_keys", "agent.start"}

    def __init__(self, inner):
        self.inner = inner
        self.planned = []           # (method, params) in call order
        self.dry_workspaces = {}    # dry ws id -> {"label", "tabs": [dry tab ids]}
        self.dry_tabs = {}          # dry tab id -> {"label", "workspace_id", "panes": [...]}
        self.replaced_real = set()  # real tab ids a planned layout.apply closed
        self._n = 0

    def _dry(self, kind):
        self._n += 1
        return f"dry:{kind}{self._n}"

    def _add_tab(self, tab_id, label, workspace_id, root):
        # Mirror of the fake's/live server's tree walk: flat leaf list with
        # the wire fields pane.list reports. cwd is always set -- build_tree
        # emits it on every leaf (pane #1's cwd IS the tab path).
        leaves = []

        def walk(node):
            if node["type"] == "pane":
                leaves.append({"pane_id": self._dry("p"), "label": node.get("label"),
                               "tab_id": tab_id, "cwd": node.get("cwd"),
                               "foreground_cwd": node.get("cwd"), "agent": None})
            else:
                walk(node["first"])
                walk(node["second"])

        walk(root)
        self.dry_tabs[tab_id] = {"label": label, "workspace_id": workspace_id,
                                 "panes": leaves}

    def call(self, method, params=None):
        params = params or {}
        if method in ("workspace.list", "tab.list", "pane.list"):
            wid = params.get("workspace_id")
            result = ({} if wid in self.dry_workspaces
                      else self.inner.call(method, params))
            if method == "tab.list":
                tabs = [t for t in result.get("tabs", [])
                        if t["tab_id"] not in self.replaced_real]
                tabs += [{"tab_id": i, "label": t["label"],
                          "workspace_id": t["workspace_id"],
                          "pane_count": len(t["panes"])}
                         for i, t in self.dry_tabs.items()
                         if wid in (None, t["workspace_id"])]
                result["tabs"] = tabs
            if method == "pane.list":
                panes = [q for q in result.get("panes", [])
                         if q["tab_id"] not in self.replaced_real]
                panes += [q for t in self.dry_tabs.values()
                          for q in t["panes"]
                          if wid in (None, t["workspace_id"])]
                result["panes"] = panes
            return result
        # Everything below is a mutation: record, never forward, answer from
        # the shadow model.
        self.planned.append((method, params))
        if method == "workspace.create":
            wid = self._dry("ws")
            self.dry_workspaces[wid] = {"label": params.get("label"), "tabs": []}
            tid = self._dry("t")
            # Live/fake fidelity: the fresh workspace's implicit root tab
            # (labeled "1" server-side; label irrelevant here -- it is either
            # renamed or replaced before anything matches by label).
            self.dry_tabs[tid] = {"label": None, "workspace_id": wid, "panes": []}
            self.dry_workspaces[wid]["tabs"].append(tid)
            return {"workspace": {"workspace_id": wid, "label": params.get("label")},
                    "tab": {"tab_id": tid}, "root_pane": {"pane_id": self._dry("p")}}
        if method == "tab.rename":
            # In dry-run mode this only ever aims at a planned tab (the
            # implicit-root form of a workspace created THIS run); a real-id
            # rename is recorded but cannot be mirrored into live state.
            t = self.dry_tabs.get(params["tab_id"])
            if t is not None:
                t["label"] = params.get("label")
            return {}
        if method == "tab.create":
            tid = self._dry("t")
            self.dry_tabs[tid] = {"label": params.get("label"),
                                  "workspace_id": params.get("workspace_id"), "panes": []}
            return {"tab": {"tab_id": tid, "label": params.get("label")},
                    "root_pane": {"pane_id": self._dry("p")}}
        if method == "layout.apply":
            tid = self._dry("t")
            if params.get("tab_id"):
                stale = self.dry_tabs.pop(params["tab_id"], None)
                if stale is not None:
                    # Replacing a PLANNED tab: old id closed, workspace
                    # inherited -- the dry twin of the live replacement.
                    wid = stale["workspace_id"]
                    ws = self.dry_workspaces.get(wid)
                    if ws is not None:
                        ws["tabs"] = [tid if i == params["tab_id"] else i
                                      for i in ws["tabs"]]
                else:
                    # Replacing a LIVE tab: hide it (and its panes) from every
                    # later list -- server-side it is closed the moment this
                    # plan executes.
                    self.replaced_real.add(params["tab_id"])
                    wid = None
                    for t in self.inner.call("tab.list", {}).get("tabs", []):
                        if t["tab_id"] == params["tab_id"]:
                            wid = t["workspace_id"]
                    wid = wid or params.get("workspace_id")
            else:
                wid = params.get("workspace_id")
            self._add_tab(tid, params.get("tab_label"), wid, params["root"])
            return {"tab": {"tab_id": tid, "label": params.get("tab_label")}}
        # pane.split / pane.rename / pane.send_text / pane.send_keys /
        # agent.start (and any unknown method -- fail-safe): the walk
        # consumes a fresh pane id.
        # Pre-existing-tab mutations need no shadow update: the walk keeps
        # its own local live inventory; no later list call re-inventories
        # that tab.
        return {"pane": {"pane_id": self._dry("p")}}


# --- Live snapshot -----------------------------------------------------------

def snapshot(sock):
    """Live herdr state, for LABEL matching only:
    {ws_label: {"workspace_id", "tabs": {tab_label: {"tab_id", "panes"}},
                "tab_id_to_label": {tab_id: tab_label_or_None}}}.

    Assembled from exactly three list calls per the design: workspace.list
    once, then tab.list + pane.list scoped per workspace (panes grouped by
    tab_id -- never one pane.list per tab). Anything needing pane ORDER or
    cardinality must re-list via panes_of(); this tree is not authoritative
    for that.
    """
    snap = {}
    for w in sock.call("workspace.list", {}).get("workspaces", []):
        snap[w["label"]] = {"workspace_id": w["workspace_id"], "tabs": {},
                            "tab_id_to_label": {}}
    for entry in snap.values():
        tabs = sock.call("tab.list", {"workspace_id": entry["workspace_id"]}).get("tabs", [])
        for t in tabs:
            # Twin of the pane-collapse note below: duplicate live TAB labels
            # within a workspace collapse the same way (last wins) --
            # deliberate at snapshot level. The loader rejects duplicate
            # declared labels, and herdr tab labels are user-facing, so a
            # live collision is server state drifting from the file, not a
            # model bug; the matcher only needs ONE tab per label to anchor.
            entry["tabs"][t.get("label")] = {"tab_id": t["tab_id"], "panes": {}}
            entry["tab_id_to_label"][t["tab_id"]] = t.get("label")
        panes = sock.call("pane.list", {"workspace_id": entry["workspace_id"]}).get("panes", [])
        for p in panes:
            tl = entry["tab_id_to_label"].get(p["tab_id"])
            # Duplicate pane labels within a tab collapse here (last wins) --
            # that is EXPECTED at snapshot level; the reconciler (Task 6)
            # works on the ordered panes_of() list for cardinality and emits
            # its own duplicate warnings. Do NOT "fix" this into a list.
            # tl is None likewise SKIPS panes whose tab has no label, or
            # whose tab_id tab.list did not return (a race between the two
            # list calls): tabs are matched by label only, so a label-less
            # tab can never match, and the reconciler re-lists its panes
            # via panes_of() when needed. Note herdr's AUTO-created root
            # tab is not in this class -- it arrives labeled "1"
            # (number-derived; design-spec Evidence appendix, side-finding 2).
            if tl is not None:
                entry["tabs"][tl]["panes"][p.get("label")] = p
    return snap


# --- Fresh-tab creation ------------------------------------------------------

def panes_of(sock, workspace_id, tab_id):
    """Ordered live pane inventory of ONE tab (full list, never name-keyed).

    snapshot()'s pane map is label-keyed and collapses duplicates by
    design, so anything that needs pane ORDER or CARDINALITY (creation,
    orphan adoption) re-lists through here instead of trusting the tree.
    """
    return [p for p in sock.call("pane.list", {"workspace_id": workspace_id}).get("panes", [])
            if p["tab_id"] == tab_id]


def _eff_cwd(p):
    """A wire pane's effective cwd. herdr reports BOTH the spawn cwd and the
    pane's current foreground directory; adoption matching must compare the
    one the user would see, falling back to the spawn cwd when the foreground
    report is missing (the fake always sets both; live may not)."""
    return p.get("foreground_cwd") or p.get("cwd")


# herdr's split-node schema REQUIRES ratio on every split (server schema,
# `herdr api schema --json`, LayoutNode split variant); 0.5 is herdr's
# neutral split, i.e. what an omitted TOML ratio means on the wire.
NEUTRAL_RATIO = 0.5


def _wire_ratio(r):
    """None -> the neutral split. The loader stores the ratio key with None
    when the TOML omitted it, so a .get() default would never fire -- both
    ratio sites (build_tree, reconcile_preexisting) normalize here."""
    return NEUTRAL_RATIO if r is None else r


def build_tree(panes):
    """Fold a declared pane list into one layout.apply tree.

    Pre-condition: `panes` is non-empty -- pane #1 becomes the root leaf,
    and an empty list has no tree. create_tab honors this by routing
    pane-less tabs to tab.rename/tab.create before any tree is built.

    Spec chaining rule: pane #1 is the root leaf; pane i (i >= 2) splits
    off the tree accumulated SO FAR, in ITS OWN direction with ITS ratio --
    declared order becomes the layout's first-to-last spine, and each
    pane's direction/ratio stay attached to the split that creates it.

    ratio: the loader stores the key with None when the TOML omitted it,
    so a .get() default would never fire; the server schema REQUIRES ratio
    on every split node -- _wire_ratio fills the neutral value. (Live probe
    2026-09-08 sent explicit ratios; the requirement itself is from
    `herdr api schema --json`, LayoutNode split variant.)
    """

    def leaf(p):
        return {"type": "pane", "label": p["name"], "cwd": p["cwd"]}

    tree = leaf(panes[0])
    for p in panes[1:]:
        tree = {"type": "split", "direction": p["direction"],
                "ratio": _wire_ratio(p["ratio"]),
                "first": tree, "second": leaf(p)}
    return tree


def create_tab(sock, workspace_id, tab, creation_form, implicit_root_tab_id=None):
    """Create a fresh tab carrying its declared pane tree; returns the
    CURRENT tab id.

    layout.apply's replacement semantics (probed: the old tab is closed, a
    new tab id issued) invalidate every pre-apply id, so the caller gets
    the post-apply id re-resolved by label from tab.list -- never an id
    captured before the apply.

    Forms, in decision order (`tab` is a loader model tab: label/path/
    panes):
      - pane-less + implicit_root_tab_id (tab #1 of a workspace THIS RUN
        adopted): rename the implicit root tab instead of creating one --
        this both fills the declared tab and disposes of the implicit tab
        (the v1 junk-tab bug class). ONLY when the root pane's effective
        cwd equals the declared tab path, though: the root pane was created
        at the WORKSPACE's cwd, and renaming it onto a divergent tab would
        seed the wrong directory invisibly (later runs see the tab matched
        and never correct it). On mismatch the tab is created fresh (next
        form) and the implicit root is left for the walk's leftover
        warning.
      - pane-less: plain tab.create (v1 shape; creation_form is moot).
      - panes + implicit_root_tab_id: one layout.apply ONTO that tab --
        fills the tab and disposes of the implicit one in a single call.
      - panes, create-in-apply (CREATION_FORM; fact a, see
        FACT_A_CONFIRMED): one layout.apply with workspace_id + tab_label
        creates the tab.
      - fallback: tab.create, then layout.apply onto the fresh tab_id.
        Unknown creation_form values land here DELIBERATELY (fail closed
        onto the probed-safe two-call path, never raise or pass through).

    Returns (tab_id, implicit_consumed): the id the declared tab now lives
    at, and whether the implicit root tab was disposed of (renamed away or
    replaced by an apply). implicit_consumed=False with a non-None
    implicit_root_tab_id means the root tab SURVIVES -- the caller keeps
    it booked so the end-of-walk warning fires (a silent leftover is the
    v1 junk-tab bug class seen from the outside).

    Pre-conditions honored here: the label is unique per workspace (loader
    guarantees it within the TOML; Task 6's matcher must not aim this
    function at an already-occupied label), and the tab path exists (the
    loader skips otherwise).

    Checked 2026-09-08: a TOML tab labeled "1" cannot collide with herdr's
    implicit root tab (which arrives labeled "1" too) -- the root is
    replaced or renamed BEFORE the post-apply re-resolution ever runs, and
    the loader rejects duplicate labels, so the first-exact-match lookup
    stays sound.
    """
    if not tab["panes"]:
        if implicit_root_tab_id is not None:
            root = panes_of(sock, workspace_id, implicit_root_tab_id)
            if len(root) == 1 and _eff_cwd(root[0]) == tab["path"]:
                sock.call("tab.rename", {"tab_id": implicit_root_tab_id,
                                         "label": tab["label"]})
                return implicit_root_tab_id, True
            # cwd mismatch (or an unreadable/ambiguous root inventory):
            # fall through to a fresh tab at the DECLARED path; the
            # implicit root stays and the caller keeps it booked.
        r = sock.call("tab.create", {"workspace_id": workspace_id, "cwd": tab["path"],
                                     "label": tab["label"]})
        return r["tab"]["tab_id"], False

    tree = build_tree(tab["panes"])
    consumed = False
    if implicit_root_tab_id is not None:
        sock.call("layout.apply", {"tab_id": implicit_root_tab_id,
                                   "tab_label": tab["label"], "root": tree})
        consumed = True  # the apply disposes of the implicit tab
    # Tolerant dispatch, deliberately: ANY creation_form other than the
    # probed CREATION_FORM (or fact a unconfirmed) takes the fallback --
    # an unknown form value must fail closed onto the probed-safe path,
    # never raise or pass through silently.
    elif creation_form == CREATION_FORM and FACT_A_CONFIRMED:
        sock.call("layout.apply", {"workspace_id": workspace_id,
                                   "tab_label": tab["label"], "root": tree})
    else:
        r = sock.call("tab.create", {"workspace_id": workspace_id, "cwd": tab["path"],
                                     "label": tab["label"]})
        sock.call("layout.apply", {"tab_id": r["tab"]["tab_id"],
                                   "tab_label": tab["label"], "root": tree})

    # Re-resolve by label: the only id source that survives replacement.
    # Exact match, first hit -- labels are unique per workspace by the
    # pre-conditions above.
    tabs = sock.call("tab.list", {"workspace_id": workspace_id}).get("tabs", [])
    for t in tabs:
        if t.get("label") == tab["label"]:
            return t["tab_id"], consumed
    raise HerdrError(f"layout.apply replacement tab not found for {tab['label']!r}")


# --- Reconcile: pre-existing tabs + one-shot effects -------------------------

def index_panes(live, log, tab_label):
    """Name index over the ordered inventory; duplicate names warn once and
    keep only the first (position order)."""
    by_name, dupes = {}, []
    for p in live:
        lbl = p.get("label")
        if lbl is None:
            continue
        if lbl in by_name:
            dupes.append(lbl)
        else:
            by_name[lbl] = p
    if dupes:
        log(f"  ~ tab {tab_label!r}: duplicate live pane names, keeping first: {dupes}")
    return by_name


def reconcile(sock, model, log):
    counters = {"created": 0, "adopted": 0, "matched": 0, "skipped": 0, "failed": 0}
    snap = snapshot(sock)
    form = CREATION_FORM  # FACT_A_CONFIRMED gates the form inside
    # create_tab; passing "fallback" (or flipping the flag, as the tests do)
    # forces the two-call path.

    def one_shots(tab, live_list, only_names):
        by = {p["label"]: p for p in live_list if p.get("label")}
        for d in tab["panes"]:
            if d["name"] not in only_names:
                continue
            p = by.get(d["name"])
            if p is None:
                # Defensive: a declared pane absent from the inventory of the
                # tab this run just filled means the server dropped/mangled a
                # label -- a SILENT skip here would hide a convergence hole
                # behind a clean exit, so count it like any other failure.
                log(f"  ~ tab {tab['label']!r}: pane {d['name']!r}: not found "
                    f"in fresh tab -- one-shot skipped")
                counters["failed"] += 1
                continue
            if d.get("agent"):
                if p.get("agent"):
                    log(f"  ~ tab {tab['label']!r}: pane {d['name']!r}: agent "
                        f"{p['agent']!r} already present, skipping start")
                    continue
                try:
                    sock.call("agent.start", {"name": d["name"], "kind": d["agent"],
                                              "pane_id": p["pane_id"],
                                              "timeout_ms": AGENT_START_TIMEOUT_MS})
                except HerdrError as e:
                    fresh = sock.call("pane.list", {}).get("panes", [])
                    now = next((q for q in fresh if q["pane_id"] == p["pane_id"]), {})
                    if now.get("agent"):
                        log(f"  ~ tab {tab['label']!r}: pane {d['name']!r}: started "
                            f"(confirmed after timeout/error)")
                    else:
                        log(f"  ~ tab {tab['label']!r}: pane {d['name']!r}: "
                            f"agent.start failed: {e}")
                        counters["failed"] += 1
                    continue
                log(f"  ~ tab {tab['label']!r}: pane {d['name']!r}: agent {d['agent']} started")
            elif d.get("command"):
                # Per-item containment, like the walk's other pane effects:
                # one pane's keystrokes failing must not abort the tab (or
                # hide the remaining one-shots) -- count it and move on.
                #
                # LIVE-FOUND BUG (2026-09-08, Task 9 scratch cycle): the
                # command went out as pane.send_keys {keys: [<text>, Enter]}
                # and the REAL server rejected it -- send_keys takes KEY
                # NAMES only ("unsupported key echo scratch-logs-marker").
                # Literal text is pane.send_text {pane_id, text}; Enter is a
                # key, so execution is a second, separate call. Two calls,
                # one failure counter: text-first so a refused command
                # leaves nothing half-typed. Accepted residual gap: if
                # send_text lands but the Enter send is refused, the text
                # sits at the prompt and is NEVER retried -- later runs
                # count the pane as matched, and matched panes are never
                # mutated.
                try:
                    sock.call("pane.send_text", {"pane_id": p["pane_id"],
                                                 "text": d["command"]})
                    sock.call("pane.send_keys", {"pane_id": p["pane_id"],
                                                 "keys": ["Enter"]})
                except HerdrError as e:
                    log(f"  ~ tab {tab['label']!r}: pane {d['name']!r}: "
                        f"command failed: {e}")
                    counters["failed"] += 1
                    continue
                log(f"  ~ tab {tab['label']!r}: pane {d['name']!r}: command sent")

    for ws in model["workspaces"]:
        live_ws = snap.get(ws["label"])
        if live_ws is None:
            log(f"+ workspace {ws['label']}")
            create_params = {"label": ws["label"], "focus": False}
            if ws["path"] is not None:
                # cwd sent only when declared: sending cwd: null is UNPROBED
                # against the live server, and omission is the schema-clean
                # way to say "no path".
                create_params["cwd"] = ws["path"]
            r = sock.call("workspace.create", create_params)
            live_ws = {"workspace_id": r["workspace"]["workspace_id"],
                       "tabs": {}, "tab_id_to_label": {},
                       "implicit_root_tab_id": r["tab"]["tab_id"]}
            counters["created"] += 1
            fresh_ws = True
        else:
            fresh_ws = False

        for idx, tab in enumerate(ws["tabs"]):
            if tab.get("skip_reason"):
                log(f"  = tab {tab['label']!r}: skipped ({tab['skip_reason']})")
                counters["skipped"] += 1
                continue
            live_tab = live_ws["tabs"].get(tab["label"])
            if live_tab is None:
                # "via layout.apply" only when a tree is applied: a pane-less
                # tab rides plain tab.create, so naming layout.apply would lie.
                how = " via layout.apply" if tab["panes"] else ""
                log(f"  + tab {tab['label']} ({len(tab['panes'])} panes{how})")
                implicit = (live_ws.get("implicit_root_tab_id")
                            if fresh_ws and idx == 0 else None)
                tab_id, implicit_consumed = create_tab(
                    sock, live_ws["workspace_id"], tab, form,
                    implicit_root_tab_id=implicit)
                if implicit is not None and implicit_consumed:
                    live_ws["implicit_root_tab_id"] = None  # consumed: filled
                    # (panes) or renamed away. Unconsumed (fresh tab created
                    # at the declared path instead) keeps the id booked so
                    # the end-of-walk warning below fires -- a silent
                    # leftover is the v1 junk-tab bug class seen from
                    # the outside.
                live = panes_of(sock, live_ws["workspace_id"], tab_id)
                one_shots(tab, live, {d["name"] for d in tab["panes"]})
                counters["created"] += 1
                continue
            reconcile_preexisting(sock, live_ws, live_tab, tab, log, counters, one_shots)

        if fresh_ws and live_ws.get("implicit_root_tab_id") is not None:
            # Nothing consumed workspace.create's implicit tab: declared
            # tab #1 was skip_reason'd, the workspace declared no tabs, or
            # tab #1 is pane-less and the root pane's cwd diverged from the
            # declared tab path (create_tab created fresh instead of
            # adopting). Leftover "1" tabs are exactly how the v1 junk-tab
            # bug class looked from the outside -- make it visible; the
            # superset rule protects the tab, so this is a warning, not a
            # failure.
            log(f"  ~ workspace {ws['label']!r}: herdr's implicit '1' tab "
                f"remains (nothing consumed it); superset rule protects it")
    return counters


def reconcile_preexisting(sock, live_ws, live_tab, tab, log, counters, one_shots):
    ws_id = live_ws["workspace_id"]
    tab_id = live_tab["tab_id"]
    live = panes_of(sock, ws_id, tab_id)      # ordered FULL inventory
    by_name = index_panes(live, log, tab["label"])
    # Names this run brought into being -- adopted ORPHANS and freshly split
    # panes alike (hence "touched", not "adopted"): they are exactly the
    # one-shot scope. Matched panes are never in it; they are never mutated.
    touched, anchor_label = [], None
    for idx, d in enumerate(tab["panes"]):
        if d["name"] in by_name:
            counters["matched"] += 1
            anchor_label = d["name"]
            declared_agent = d.get("agent")
            if declared_agent and by_name[d["name"]].get("agent") != declared_agent:
                # degraded declared agent: convergence FAILS (oracle), no respawn
                log(f"  ~ tab {tab['label']!r}: pane {d['name']!r} declared agent "
                    f"{declared_agent!r} but live agent is "
                    f"{by_name[d['name']].get('agent')!r}")
                counters["failed"] += 1
            continue
        cwd = tab["path"] if idx == 0 else d["cwd"]
        if idx == 0:
            unnamed = [p for p in live if not p.get("label")]
            adoptable = (len(live) == 1 and len(unnamed) == 1
                         and not unnamed[0].get("agent")
                         and _eff_cwd(unnamed[0]) == cwd)
            if adoptable:
                pu = unnamed[0]
                try:
                    sock.call("pane.rename", {"pane_id": pu["pane_id"], "label": d["name"]})
                except HerdrError as e:
                    # Per-item containment: the pane stays unnamed, the next
                    # run's adoption reclaims it, and the rest of the walk
                    # goes on (creation/transport failures still abort at the
                    # top level -- only this call's failure lands here).
                    log(f"  ~ tab {tab['label']!r}: pane {d['name']!r} adoption "
                        f"rename failed: {e}")
                    counters["failed"] += 1
                    continue
                pu["label"] = d["name"]
                by_name[d["name"]] = pu
                touched.append(d["name"])
                counters["adopted"] += 1
                log(f"  ~ tab {tab['label']!r}: adopted root pane -> {d['name']!r}")
                anchor_label = d["name"]
                continue
            if len(live) == 1 and len(unnamed) == 1:
                # Spec (step 3, position 1): "skip with a warning naming the
                # observed values" -- with a single unnamed candidate the
                # refusal is (agent, cwd) vs the declared path, so BOTH paths
                # go into the message; the count form below covers every
                # other refusal shape. (Deviation from the plan's single
                # unified message, required by the spec sentence above.)
                o = unnamed[0]
                log(f"  = tab {tab['label']!r}: skipped (pane #1 {d['name']!r}: single live "
                    f"pane not adoptable -- agent {o.get('agent')!r}, cwd "
                    f"{_eff_cwd(o)!r} vs declared {cwd!r})")
            else:
                log(f"  = tab {tab['label']!r}: skipped (no live pane matches pane #1 "
                    f"{d['name']!r}; {len(live)} live panes)")
            counters["skipped"] += 1
            return
        # position >= 2: orphan adoption first (unnamed + agentless + cwd match)
        orphan = next((q for q in live
                       if not q.get("label") and not q.get("agent")
                       and _eff_cwd(q) == d["cwd"]), None)
        if orphan is not None:
            try:
                sock.call("pane.rename", {"pane_id": orphan["pane_id"], "label": d["name"]})
            except HerdrError as e:
                # Same containment: the orphan stays unnamed for the next run;
                # dependents lose their anchor, unrelated items go on.
                log(f"  ~ tab {tab['label']!r}: pane {d['name']!r} adoption "
                    f"rename failed: {e}")
                counters["failed"] += 1
                anchor_label = None
                continue
            orphan["label"] = d["name"]
            by_name[d["name"]] = orphan
            touched.append(d["name"])
            counters["adopted"] += 1
            log(f"  ~ tab {tab['label']!r}: pane {d['name']!r} adopts unnamed orphan")
            anchor_label = d["name"]
            continue
        if anchor_label is None:
            log(f"  ~ tab {tab['label']!r}: pane {d['name']!r} skipped (no live anchor)")
            counters["skipped"] += 1
            continue
        anchor = by_name.get(anchor_label)
        if anchor is None:
            log(f"  ~ tab {tab['label']!r}: pane {d['name']!r} skipped (anchor "
                f"{anchor_label!r} unavailable)")
            counters["skipped"] += 1
            continue
        try:
            r = sock.call("pane.split", {"direction": d["direction"], "cwd": d["cwd"],
                                         # Loader always stores the key (None when
                                         # the TOML omitted it), so a .get() default
                                         # would never fire -- normalize like
                                         # build_tree does: None must not reach
                                         # the wire (server schema requires ratio).
                                         "ratio": _wire_ratio(d["ratio"]),
                                         "target_pane_id": anchor["pane_id"]})
        except HerdrError as e:
            log(f"  ~ tab {tab['label']!r}: pane {d['name']!r} split failed: {e}")
            counters["failed"] += 1
            anchor_label = None          # dependents skip cleanly
            continue
        new_id = r["pane"]["pane_id"]
        try:
            sock.call("pane.rename", {"pane_id": new_id, "label": d["name"]})
        except HerdrError as e:
            # the split->rename recovery window: leave the unnamed pane for the
            # next run's orphan adoption; dependents skip, unrelated items go on
            log(f"  ~ tab {tab['label']!r}: pane {d['name']!r} rename failed: {e}")
            counters["failed"] += 1
            anchor_label = None
            continue
        fresh = {"pane_id": new_id, "label": d["name"], "cwd": d["cwd"], "agent": None}
        live.append(fresh)
        by_name[d["name"]] = fresh
        touched.append(d["name"])
        counters["created"] += 1
        anchor_label = d["name"]
    one_shots(tab, live, set(touched))


def main(argv=None):
    """CLI entrypoint. Exit contract (see shim): 0 iff failed=0 AND skipped=0
    (a skip means herdr does NOT match the file -- the run must be visible in
    shell status, not just in the log); 1 otherwise; 2 when the TOML itself
    is bad (aborts before any socket traffic). Transport/reconcile errors
    also map to 1: the state is unknown-ish, so never report clean success.
    3 (lock-busy) is the shim's alone."""
    ap = argparse.ArgumentParser(description="reconcile herdr state from projects.toml")
    ap.add_argument("--toml", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        model = load_model(args.toml)
    except ValidationError as e:
        print(f"validation error: {e}", file=sys.stderr)
        return EXIT_VALIDATION
    sock = HerdrSocket()
    if args.dry_run:
        sock = DryRunSocket(sock)
    try:
        counters = reconcile(sock, model, log=print)
    except (ValidationError, HerdrError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FAILED
    print(f"created={counters['created']} adopted={counters['adopted']} "
          f"matched={counters['matched']} skipped={counters['skipped']} "
          f"failed={counters['failed']}")
    if args.dry_run:
        print(f"[dry-run] {len(sock.planned)} mutations planned - none executed")
    return EXIT_OK if counters["failed"] == 0 and counters["skipped"] == 0 else EXIT_FAILED


if __name__ == "__main__":
    # Codex (2026-09-08): on python >= 3.11 the tomllib guard at the top
    # falls through, so a DIRECT `python vergent.py` would run the
    # reconciler with no single-flight flock and no no-lazy-spawn guard --
    # a manual run could race the boot hook into duplicate workspaces.
    # The shim is the only supported entrypoint; it vouches for itself via
    # HERDR_VERGENT_VIA_SHIM, exported right before its exec (below the
    # flock). Importing this module (tests, tooling) is unaffected.
    if os.environ.get("HERDR_VERGENT_VIA_SHIM") != "1":
        print("vergent: direct execution refused; run "
              "bin/vergent.sh (flock + server guards live there)",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(main())
