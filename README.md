# herdr-vergent

**Convergent sessions for [herdr](https://herdr.dev).** Declare your workspaces, tabs, and panes in one TOML file; at every server boot, vergent converges the running session to it.

```
herdr plugin install elifarley/herdr-vergent
```

✅ Idempotent by contract — a re-run against a converged session is a verified no-op
🗿 Deletion-free — never touches live state the file does not declare
1️⃣ One-shot effects — declared agents and commands fire exactly once per pane lifetime

## Why

herdr restores *whatever was running* when the server restarts. That is a snapshot, not a spec: nothing says what your session **should** look like, and nothing re-creates it on a new machine, a wiped session, or after you reorganize. vergent is the spec — kitty's `startup_session` and zellij's layout files, rebuilt as a reconciler: it does not fire once at creation, it converges toward the file, every boot, by name.

Upstream provides the primitives (a tab-scoped, one-shot, replace-on-apply `layout.apply`; plugin `[[startup]]` hooks) — vergent is the desired-state loop on top.

## The file

Schema v3 — containment nests, all the way down. `name` (herdr's "label") is required at every level and is the match anchor; paths inherit downward (`workspace -> tab -> pane`), so each level declares only its delta.

```toml
[[workspace]]
name = "Dev"
path = "~/src"

[[workspace.tab]]
name = "Dev"                    # inherits ~/src

[[workspace.tab.pane]]
name = "editor"
cwd = "my-project"              # -> ~/src/my-project

[[workspace.tab.pane]]
name = "logs"
cwd = "my-project/var"
direction = "down"              # panes #2+ split; "right" or "down"

[[workspace.tab.pane]]
name = "agent"
direction = "right"
agent = "claude"                # started via herdr, exactly once
```

Full annotated example: [`projects.example.toml`](projects.example.toml) — ratios, commands, absolute-path escapes, pane-less tabs.

## Install & run

```sh
herdr plugin install elifarley/herdr-vergent
mkdir -p "$(herdr plugin config-dir elifarley.vergent)"
cp projects.example.toml "$(herdr plugin config-dir elifarley.vergent)/projects.toml"
$EDITOR "$(herdr plugin config-dir elifarley.vergent)/projects.toml"
```

Then restart herdr (or run the seeder by hand: `bin/vergent.sh` from the plugin directory, `--dry-run` to preview). At boot the startup hook converges and exits; on live handoff it runs again — a no-op when state already matches.

Override the file location with `--toml <path>` or `HERDR_VERGENT_TOML`.

## The contract

- **Match by name, converge by creation.** Existing workspaces/tabs/panes whose names match the file are left exactly as they are. Missing ones are created. Nothing else.
- **Superset rule.** Live state the file does not declare is inviolable — closing, deleting, or "cleaning up" is always yours.
- **One-shots are one-shot.** `agent` and `command` fire only on the run that creates the pane; matched panes are never re-typed or re-started.
- **Honest exits.** `0` converged · `1` anything skipped or failed (a skipped tab always names its reason) · `2` the TOML is invalid (before any socket traffic) · `3` single-flight lock busy. A `--dry-run` plans through a shadow overlay and executes nothing.
- **Single-flight.** The boot hook re-runs on live handoff while you might be running it by hand; a `flock` makes the second runner exit 3 instead of racing.

## How it differs from its neighbors

The marketplace has good declarative-layout plugins; vergent's difference is the *trigger* and the *contract*, not the file format.

| | trigger | semantics |
|---|---|---|
| herdr-plus "Projects" | you pick from a menu | opens the layout (one-shot) |
| herdr-spreader | you invoke `apply` | opens the layout (one-shot), dry-run available |
| herdr-sessionizer | you pick with fzf | opens on creation only |
| workspace-manager | worktree created | applies a routed layout per event |
| herdr-resurrect | server boot (opt-in) | restores a *snapshot* of what was running |
| **vergent** | **every server boot** | **converges live state toward an authored spec; idempotent; deletion-free** |

Prior art beyond herdr: zellij's runtime `override-layout` re-applies a layout to a live tab but is destructive by default (keeping existing panes is an opt-in flag); tmux-resurrect is idempotent — toward a snapshot, not a spec. Converge-toward-authored-file is the combination vergent is built around.

## Requirements

- herdr >= 0.8.2 (developed and daily-run on 0.8.2; the 0.9.0 socket surface this plugin uses — `workspace.*`, `tab.*`, `pane.*`, `agent.start`, `layout.apply` — is unchanged per the 0.9.0 release notes and docs)
- Linux, `flock`, and any Python ≥ 3.11 with `tomllib` (the shim probes `python3.11`–`python3.14`, then bare `python3`)

## Development

Stdlib-only Python, no install step. The suite (78 tests, driven against a scripted fake herdr server) runs anywhere:

```sh
python3 -m unittest discover -s tests -v
```

`--dry-run` against a live server is the convergence oracle: all-zero counters plus `exit 0` proves the file and the session agree.

## License

MIT — see [LICENSE](LICENSE).
