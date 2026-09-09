# Changelog

All notable changes to vergent are documented here. Format based on Keep a Changelog.

## [0.2.0] - 2026-09-09

### Added

- First-run onboarding: when the default config file is absent, the hook writes a starter config (creating parent dirs; never overwriting) that declares a `vergent-start-here` Space whose pane prints the config's absolute path, how to edit it, and the starter contents — delivered through the same one-shot command machinery as user commands, so it fires exactly once per pane lifetime. Once the user replaces the file's contents, the Space is no longer declared: the superset rule leaves it until they close it, and it never returns.
- The starter is written for the default config path only. Explicit `--toml` / `HERDR_VERGENT_TOML` targets stay strict — a missing explicit path is an error (a typo must fail loudly, not materialize a file at the wrong place).

## [0.1.0] - 2026-09-09

### Added

- Declarative session file (schema v3): `[[workspace]]` → `[[workspace.tab]]` → `[[workspace.tab.pane]]`, with `name` required at every level (herdr's "label"), relative-path inheritance (workspace → tab → pane), per-pane `cwd`/`direction`/`ratio`, optional per-pane `agent` (started via herdr) or `command` (typed once into the pane's shell).
- Boot-time convergence via a plugin `[[startup]]` hook: creates missing workspaces/tabs/panes by name and exits; verified no-op when live state already matches.
- Superset rule: live state the file does not declare is never touched.
- One-shot effects: agents and commands fire exactly once per pane lifetime (pre-check + post-failure confirmation; no blind retries).
- `--dry-run`: full plan through a shadow overlay — zero mutations reach the server.
- Exit-code contract: 0 converged · 1 skipped/failed · 2 invalid TOML (pre-socket) · 3 lock busy.
- 78-test suite driven against a scripted fake herdr server (stdlib-only; `python3 -m unittest discover -s tests`).
