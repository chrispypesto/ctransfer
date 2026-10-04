#!/usr/bin/env bash
# UserPromptSubmit: cheap exit unless some ctransfer push is recorded; otherwise let guard.py decide.
dir="$(dirname "${CTRANSFER_CONFIG:-$HOME/.config/ctransfer/config.json}")/pushed"
compgen -G "$dir/*.json" >/dev/null || exit 0
exec python3 "$(dirname "$0")/guard.py"
