#!/usr/bin/env bash
# /ctransfer dispatcher: maps the slash command's arguments onto the ctransfer CLI.
set -uo pipefail
exec 2>&1

bin="${CTRANSFER_BIN:-}"
[ -z "$bin" ] && bin="$(command -v ctransfer 2>/dev/null)"
if [ -z "$bin" ]; then
  cat <<'MSG'
ctransfer CLI not found. Install it into any Python 3.10+ environment, then retry /ctransfer:
  pip install "git+https://github.com/chrispypesto/ctransfer.git"   # or pipx / uv tool install
  ctransfer login
Claude Code finds it on PATH, or at $CTRANSFER_BIN (path to the ctransfer executable).
MSG
  exit 1
fi

push() { exec "$bin" push --current --when-idle --detach "$@"; }

sub="${1:-push}"
[ $# -gt 0 ] && shift
case "$sub" in
  push) push "$@" ;;
  -*) push "$sub" "$@" ;;  # /ctransfer --remote-control
  status) if [ $# -gt 0 ]; then exec "$bin" status "$@"; else exec "$bin" status --current; fi ;;
  discover) exec "$bin" discover --current "$@" ;;
  pull | ls | rm) exec "$bin" "$sub" "$@" ;;
  ssh | resume) echo "$sub needs an interactive terminal. In another terminal run:  ctransfer $sub $*"; exit 1 ;;
  *)
    echo "usage: /ctransfer [push [--remote-control] [--no-file-history] | status [id] | discover | ls | pull <id> [--force|--as-fork|--stash] | rm <id> | ssh <id> | resume <session>]"
    exit 2 ;;
esac
