#!/usr/bin/env bash
# /ctransfer dispatcher: maps the slash command's arguments onto the ctransfer CLI.
set -uo pipefail
exec 2>&1

bin="${CTRANSFER_BIN:-}"
[ -z "$bin" ] && bin="$(command -v ctransfer 2>/dev/null)"
if [ -z "$bin" ]; then
  cat <<'MSG'
ctransfer CLI not found. Install it into any Python 3.11+ environment, then retry /ctransfer:
  pip install "git+https://github.com/chrispypesto/ctransfer.git"   # or pipx / uv tool install
  ctransfer login
Claude Code finds it on PATH, or at $CTRANSFER_BIN (path to the ctransfer executable).
MSG
  exit 1
fi

push() {  # leading --flags are options (--size=large: one word), the rest is the prompt the cloud session runs on arrival
  flags=()
  while [ $# -gt 0 ] && [ "${1#--}" != "$1" ]; do flags+=("$1"); shift; done
  exec "$bin" push --current --when-idle --detach ${flags[@]+"${flags[@]}"} -- "$@"
}

# The skill passes its arguments on stdin through a quoted heredoc, so no shell quoting or expansion touches
# them (an unquoted $ARGUMENTS breaks on "don't"). Split them into words as typed, without globbing.
if [ "${1:-}" = "--stdin" ]; then
  set -f
  # shellcheck disable=SC2046
  set -- $(cat)
  set +f
fi

sub="${1:-push}"
[ $# -gt 0 ] && shift
case "$sub" in
  push) push "$@" ;;
  -*) push "$sub" "$@" ;;  # /ctransfer --remote-control
  status) if [ $# -gt 0 ]; then exec "$bin" status "$@"; else exec "$bin" status --current; fi ;;
  pull | ls | rm | usage) exec "$bin" "$sub" "$@" ;;
  new)  # leading --flags are options, the rest is the new session's first prompt
    flags=()
    while [ $# -gt 0 ] && [ "${1#--}" != "$1" ]; do flags+=("$1"); shift; done
    exec "$bin" new --detach ${flags[@]+"${flags[@]}"} -- "$@" ;;
  ssh | resume) echo "$sub needs an interactive terminal. In another terminal run:  ctransfer $sub $*"; exit 1 ;;
  *)
    echo "usage: /ctransfer [push [--remote-control] [--no-file-history] [--size=small|large] [prompt] | new [--remote-control] [--size=small|large] [prompt] | status [id] | ls | usage | pull <id> [--force|--as-fork|--stash] | rm <id> | ssh <id> | resume <session>]"
    exit 2 ;;
esac
