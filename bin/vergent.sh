#!/usr/bin/env bash
# vergent shim: lock -> toml resolution -> no-lazy-spawn guard ->
# interpreter probe -> exec the python seeder. All real logic lives in
# bin/vergent.py (see the README).
set -euo pipefail

DRY_RUN=0
TOML_FILE="${HERDR_VERGENT_TOML:-}"

# Parse args BEFORE canonicalizing paths: readlink -f fails under set -e when
# a path's parent directory is missing, so canonicalizing $HERDR_VERGENT_TOML
# first would kill every invocation with a silent exit 1 even when --toml is
# valid. Canonicalization happens only after --toml has had its chance to win.
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --toml) shift; [ $# -gt 0 ] || { echo "usage: vergent.sh [--dry-run] [--toml <path>]" >&2; exit 2; }; TOML_FILE="$1" ;;
    *) echo "usage: vergent.sh [--dry-run] [--toml <path>]" >&2; exit 2 ;;
  esac
  shift
done

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
: "${TOML_FILE:=$SCRIPT_DIR/../.config/herdr/plugins-local/vergent/projects.toml}"
# readlink -f fails (exit 1, no output) when the path's PARENT directory does
# not exist -- e.g. `--toml ~/newproj/projects.toml` before the first checkout.
# Bash gotcha pinned here: on a failed command-substitution assignment the
# variable is CLOBBERED to empty even when `|| {}` shields it from set -e, so
# the original argument is stashed first or the error would print nothing.
TOML_ARG="$TOML_FILE"
TOML_FILE="$(readlink -f "$TOML_FILE" 2>/dev/null)" \
  || { echo "cannot resolve TOML path: $TOML_ARG" >&2; exit 2; }

# Single-flight: the [[startup]] hook re-runs on live handoff while a manual
# run may be in progress. Busy lock = exit 3 (distinct from validation's 2).
LOCK_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/herdr"
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK_DIR/vergent.lock"
flock -n 9 || { echo "vergent: another run is active" >&2; exit 3; }

# No-lazy-spawn guard (cardinal failure mode, docs/herdr-notes.md): never
# start an unmanaged server. The plugin hook always runs against a live
# server; manual runs refuse when the managed unit exists but is stopped.
# The state query can come back EMPTY on a transient dbus/bus error -- that
# is an INDETERMINATE state, not a stopped one: retry once, and if it is
# still empty proceed without the guard (the socket connect fails harmlessly
# when no server is up; a refusal here would break every [[startup]] hook
# run on a flapping bus). Only a definitive non-active state keeps the
# refusal.
if command -v systemctl >/dev/null 2>&1 \
   && systemctl --user is-enabled herdr.service >/dev/null 2>&1; then
  # `|| state=""` is load-bearing, not decoration: a bus error makes
  # systemctl exit NONZERO with empty output, and under set -e a plain
  # assignment would kill the script before the emptiness check below --
  # silently, i.e. exactly the flapping-bus behavior this guard forbids.
  state="$(systemctl --user show herdr.service -p ActiveState --value 2>/dev/null)" || state=""
  if [ -z "$state" ]; then
    sleep 1
    state="$(systemctl --user show herdr.service -p ActiveState --value 2>/dev/null)" || state=""
  fi
  if [ -z "$state" ]; then
    echo "cannot determine herdr.service state -- proceeding without the guard" >&2
  elif [ "$state" != "active" ]; then
    echo "herdr.service is '$state' - refusing to lazy-spawn an unmanaged server." \
         "Start it first: systemctl --user start herdr" >&2
    exit 1
  fi
fi

# Interpreter probe: tomllib needs >= 3.11 (Pop!_OS 22.04 default is 3.10).
# Bare python3 is the last-resort fallback, still gated by the tomllib probe.
PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import tomllib' 2>/dev/null; then
    PY="$(command -v "$cand")"; break
  fi
done
[ -n "$PY" ] || { echo "no python >= 3.11 with tomllib found" >&2; exit 1; }

FLAGS=()
[ "$DRY_RUN" = 1 ] && FLAGS+=(--dry-run)
# Vouch for the exec'd python: the script refuses direct execution
# (its __main__ guard, added 2026-09-08) unless this var is set -- by the
# time we export it, the flock is held (fd 9) and the no-lazy-spawn guard
# has passed, which is exactly what the script cannot do for itself.
export HERDR_VERGENT_VIA_SHIM=1
exec "$PY" "$SCRIPT_DIR/vergent.py" --toml "$TOML_FILE" "${FLAGS[@]}"
