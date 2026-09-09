# ── Strict Preamble ──────────────────────────────────────────────
SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c
MAKEFLAGS += --warn-undefined-variables
MAKEFLAGS += --no-builtin-rules
MAKEFLAGS += --no-print-directory

.DEFAULT_GOAL := help

# vergent is STDLIB-ONLY Python: no project env, no dependency manifest.
# Following the Java precedent in the canonical taxonomy, the environment
# targets (dev-env-init / deps-sync / deps-normalize) are absent rather
# than stubbed -- a target is a guarantee, and there is nothing to
# provision or reconcile.

# Interpreter probe (same chain as bin/vergent.sh): the suite needs
# tomllib, i.e. Python >= 3.11. Override per invocation for version
# matrices:  make test-unit PYTHON=python3.11
PYTHON ?= $(shell for c in python3.14 python3.13 python3.12 python3.11 python3; do \
	command -v $$c >/dev/null 2>&1 && $$c -c 'import tomllib' 2>/dev/null && echo $$c && break; done)

# Tools run ephemerally via uvx: no dev-env, pinned by convention (ruff, ty).
UVX := uvx

# --- Phony declarations -------------------------------------------
# All targets here are commands, not files. .PHONY prevents Make from
# skipping a target when a same-named file appears in the directory.
.PHONY: doctor format format-verbose format-check lint lint-verbose \
    typecheck typecheck-verbose \
    sanitize-ro sanitize-ro-verbose sanitize \
    test-unit test-unit-verbose test test-verbose \
    check check-verbose check-ro validate validate-ro \
    pre-commit help

# --- TTY detection -------------------------------------------------
# WHY tput colors, not [ -t 1 ]: Make's $(shell) pipes stdout, so [ -t 1 ]
# always returns false. tput queries terminfo via $TERM — works in $(shell).
# Define the color vars unconditionally (empty) FIRST, so referencing
# $(GREEN)/$(RESET) never trips MAKEFLAGS' --warn-undefined-variables in a
# non-TTY/CI shell.
GREEN  :=
RED    :=
YELLOW :=
BLUE   :=
RESET  :=

TERM_COLOR := $(shell tput colors 2>/dev/null)

ifneq ($(TERM_COLOR),)
ifneq ($(TERM_COLOR),0)
GREEN  := \033[0;32m
RED    := \033[0;31m
YELLOW := \033[0;33m
BLUE   := \033[0;34m
RESET  := \033[0m
endif
endif

# --- Environment inspection ---------------------------------------

doctor: ## Inspect environment readiness
	@[ -n "$(PYTHON)" ] || (printf "$(RED)❌ no Python >= 3.11 with tomllib found (need python3.11+ or python3.14/3.13/3.12)$(RESET)\n" && exit 1)
	@command -v shellcheck >/dev/null || (printf "$(RED)❌ shellcheck not found$(RESET)\n" && exit 1)
	@command -v flock >/dev/null || (printf "$(RED)❌ flock not found$(RESET)\n" && exit 1)
	@command -v uv >/dev/null || (printf "$(RED)❌ uv not found (provides uvx for ruff/ty)$(RESET)\n" && exit 1)
	@$(PYTHON) -c 'import tomllib' || (printf "$(RED)❌ $(PYTHON) lacks tomllib$(RESET)\n" && exit 1)
	@printf "$(GREEN)✅ Environment OK (PYTHON=$(PYTHON), shellcheck, flock, uv)$(RESET)\n"

# --- Quality ------------------------------------------------------

# --quiet: suppress informational output but preserve unfixable error output
# (gives earlier feedback than waiting for lint). || true: format should never block.
format: ## Format code (minimal output)
	@$(UVX) ruff check --fix --quiet . || true
	@$(UVX) ruff format --quiet .
	@printf "$(GREEN)✅ Formatting OK$(RESET)\n"

format-verbose: ## Format code (show changes)
	@$(UVX) ruff check --fix . || true
	@$(UVX) ruff format .

# Read-only format verification for CI gates. ruff format --check exits
# non-zero if any file would be reformatted. Without this, sanitize-ro
# skips format verification — badly-formatted PRs silently pass check-ro.
format-check: ## Verify formatting compliance (read-only)
	@$(UVX) ruff format --check --quiet .
	@printf "$(GREEN)✅ Format check OK$(RESET)\n"

# shellcheck covers the bash shim (half the shipped surface); ruff covers
# the Python seeder and the suite.
lint: ## Run linting (minimal output)
	@$(UVX) ruff check --output-format=concise .
	@shellcheck bin/vergent.sh
	@printf "$(GREEN)✅ Linting OK$(RESET)\n"

lint-verbose: ## Run linting (detailed)
	@$(UVX) ruff check .
	@shellcheck bin/vergent.sh

# ty has clean exit codes (exit 1 on errors, 0 on success) — no
# variable-capture or grep workaround needed.
typecheck: ## Type check (minimal output)
	@$(UVX) ty check bin --output-format concise
	@printf "$(GREEN)✅ Type checking OK$(RESET)\n"

typecheck-verbose: ## Type check (detailed)
	@$(UVX) ty check bin

sanitize-ro: ## Read-only static checks (format-check + lint + typecheck)
	@$(MAKE) format-check
	@$(MAKE) lint
	@$(MAKE) typecheck

sanitize-ro-verbose: ## Read-only static checks (detailed)
	@$(MAKE) format-check
	@$(MAKE) lint-verbose
	@$(MAKE) typecheck-verbose

sanitize: ## format + lint + typecheck
	@$(MAKE) format
	@$(MAKE) sanitize-ro

# --- Tests --------------------------------------------------------

# unittest's own summary is already O(1) (dots + one OK/FAILED line); -v is
# the per-test verbose variant. There is no integration suite: the live
# convergence oracle (vergent.sh --dry-run against a real server) is a
# manual receipt, not an automated target.
test-unit: ## Run unit tests (LLM-friendly output)
	@$(PYTHON) -m unittest discover -s tests
	@printf "$(GREEN)✅ Unit tests passed$(RESET)\n"

test-unit-verbose: ## Run unit tests (detailed output)
	@$(PYTHON) -m unittest discover -s tests -v

# No integration suite exists: test == test-unit by composition (the live
# convergence oracle, vergent.sh --dry-run against a real server, is a
# manual receipt, not an automated target). The validator warns on the
# missing test-integration leg; that is the honest shape here.
test: ## Run all tests
	@$(MAKE) test-unit

test-verbose: ## Run all tests (detailed output)
	@$(MAKE) test-unit-verbose

# --- Gates --------------------------------------------------------

check: ## sanitize + test-unit (fast merge gate)
	@$(MAKE) sanitize
	@$(MAKE) test-unit

check-verbose: ## sanitize + test-unit-verbose (detailed)
	@$(MAKE) sanitize
	@$(MAKE) test-unit-verbose

# CI gate: mirrors check but uses sanitize-ro (read-only).
# Prevents format from mutating files in CI while keeping gate
# composition in the Makefile (not the CI YAML).
check-ro: ## CI merge gate — sanitize-ro + test-unit (read-only)
	@$(MAKE) sanitize-ro
	@$(MAKE) test-unit

# Release gate adds the one vergent-specific guarantee beyond check: the
# plugin manifest parses and carries the keys herdr's installer requires.
# Spelled as sanitize + test (not nested check) per the composite contract.
validate: ## Release gate (sanitize + all tests + plugin manifest smoke)
	@$(MAKE) sanitize
	@$(MAKE) test
	@$(PYTHON) -c 'import tomllib; d = tomllib.load(open("herdr-plugin.toml", "rb")); missing = [k for k in ("id", "name", "version", "min_herdr_version", "platforms") if k not in d]; assert not missing, f"manifest missing keys: {missing}"; startup = d.get("startup") or []; assert startup and startup[0].get("command"), "manifest has no [[startup]] command"; print("manifest OK:", d["id"], d["version"])'
	@printf "$(GREEN)✅ Validate OK$(RESET)\n"

validate-ro: ## CI release gate — sanitize-ro + all tests + manifest smoke (read-only)
	@$(MAKE) sanitize-ro
	@$(MAKE) test
	@$(PYTHON) -c 'import tomllib; d = tomllib.load(open("herdr-plugin.toml", "rb")); missing = [k for k in ("id", "name", "version", "min_herdr_version", "platforms") if k not in d]; assert not missing, f"manifest missing keys: {missing}"; startup = d.get("startup") or []; assert startup and startup[0].get("command"), "manifest has no [[startup]] command"; print("manifest OK:", d["id"], d["version"])'

# --- Pre-commit hook ----------------------------------------------

# Pre-commit hook target - customize as needed for your project
# Default: runs fast checks (sanitize + unit tests)
pre-commit: ## Pre-commit hook (customizable)
	@$(MAKE) check

# --- Help ---------------------------------------------------------

# Regex includes 0-9: [a-zA-Z_-]+ silently drops targets with digits.
# Auto-discovery beats hardcoded target lists.
help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-28s %s\n", $$1, $$2}'
