---
name: ctransfer
description: Move this session to a cloud VM with ctransfer, start a new cloud session, or show status, list, pull transfers
argument-hint: "[push [--remote-control] | new [prompt] | status [id] | ls | pull <id> | rm <id>]"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/ctransfer.sh:*)
disable-model-invocation: true
---

```!
"${CLAUDE_PLUGIN_ROOT}/scripts/ctransfer.sh" --stdin <<'CTRANSFER_ARGS'
$ARGUMENTS
CTRANSFER_ARGS
```

The output above comes from the ctransfer CLI, which already did everything. Do not call any tools or run anything else. Reply in at most two short lines:

- Push queued (normally the plugin's hook confirms this and you never see it): say the push starts once this session is idle and to `/exit` when it says running. Nothing else: everything recorded after the push diverges from the cloud copy.
- New cloud session started: say it is starting on a VM in the background (this session is unaffected), and that `/ctransfer ls` shows its transfer id; bring it back with `ctransfer pull <id>`.
- Error, missing CLI, or usage text: relay it briefly with the fix it suggests.
- Anything else (status, ls, pull): summarize the result. After a pull, tell the user to continue with the `ctransfer resume <session>` command it printed, in a terminal.
