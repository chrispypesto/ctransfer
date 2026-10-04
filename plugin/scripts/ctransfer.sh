#!/usr/bin/env bash
# /ctransfer dispatcher: maps the slash command's arguments onto the ctransfer CLI.
set -uo pipefail
exec 2>&1

bin="${CTRANSFER_BIN:-}"
[ -z "$bin" ] && bin="$(command -v ctransfer 2>/dev/null)"
[ -z "$bin" ] && [ -n "${CONDA_EXE:-}" ] && [ -x "${CONDA_EXE%/bin/conda}/envs/ctransfer/bin/ctransfer" ] \
  && bin="${CONDA_EXE%/bin/conda}/envs/ctransfer/bin/ctransfer"
if [ -z "$bin" ]; then
  cat <<'MSG'
ctransfer CLI not found. Install it, then retry /ctransfer:
  conda create -y -n ctransfer python=3.12
  conda run -n ctransfer pip install "git+https://github.com/chrispypesto/ctransfer.git"
  conda run -n ctransfer ctransfer configure --api-url <your control plane api_url> --region <region>
It is found on PATH, in a conda env named "ctransfer", or at $CTRANSFER_BIN.
ctransfer also needs its control plane deployed in your AWS account: see the ctransfer README.
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
  ssh) echo "ssh needs an interactive terminal. In another terminal run:  ctransfer ssh $*"; exit 1 ;;
  *)
    echo "usage: /ctransfer [push [--remote-control] [--no-file-history] | status [id] | discover | ls | pull <id> [--force|--as-fork|--stash] | rm <id> | ssh <id>]"
    exit 2 ;;
esac
