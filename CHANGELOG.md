# Changelog

All notable changes to vergent are documented here. Format based on Keep a Changelog.

## [0.1.0] - 2026-09-09

### Added

- Declarative session file (schema v3): `[[workspace]]` → `[[workspace.tab]]` → `[[workspace.tab.pane]]`, with `name` required at every level (herdr's "label"), relative-path inheritance (workspace → tab → pane), per-pane `cwd`/`direction`/`ratio`, optional per-pane `agent` (started via herdr) or `command` (typed once into the pane's shell).
- Boot-time convergence via a plugin `[[startup]]` hook: creates missing workspaces/tabs/panes by name and exits; verified no-op when live state already matches.
- Superset rule: live state the file does not declare is never touched.
- One-shot effects: agents and commands fire exactly once per pane lifetime (pre-check + post-failure confirmation; no blind retries).
- `--dry-run`: full plan through a shadow overlay — zero mutations reach the server.
- Exit-code contract: 0 converged · 1 skipped/failed · 2 invalid TOML (pre-socket) · 3 lock busy.
- 78-test suite driven against a scripted fake herdr server (stdlib-only; `python3 -m unittest discover -s tests`).
