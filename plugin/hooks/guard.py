"""UserPromptSubmit guard for a session that /ctransfer has pushed.

Once a push is queued, anything recorded locally diverges from the cloud copy (and makes
`ctransfer pull` refuse), so every prompt is blocked and the push's progress shown instead.
A failed push is reported once, then prompts go through again.
"""

import json
import os
import re
import sys
from pathlib import Path

ACTIVE = {
    "queued": "push queued: it starts when the current turn ends",
    "pushing": "uploading to the cloud",
    "launching": "VM starting",
    "running": "running in the cloud",
}


def last_log_line(path):
    try:
        return next((ln for ln in reversed(Path(path).read_text().splitlines()) if ln.strip()), "")
    except (OSError, TypeError):
        return ""


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    sid = event.get("session_id")
    if not sid:
        return
    cfg = Path(os.environ.get("CTRANSFER_CONFIG") or Path.home() / ".config" / "ctransfer" / "config.json")
    marker_path = cfg.parent / "pushed" / f"{sid}.json"
    try:
        marker = json.loads(marker_path.read_text())
    except (OSError, ValueError):
        return
    state, tid, log = marker.get("state"), marker.get("transfer_id"), marker.get("log")

    if state == "failed":
        marker["state"] = "failed_reported"
        marker_path.write_text(json.dumps(marker, indent=2))
        reason = (f"ctransfer: the push of this session failed: {marker.get('error', 'unknown error')}\n"
                  f"Log: {log}\nThe session stays local; resend your prompt to continue here, or /ctransfer to retry.")
    elif state == "queued" and re.match(r"/ctransfer(:ctransfer)?(\s+push(\s.*)?|\s+-.*)?\s*$", event.get("prompt", ""),
                                        re.S):
        # The skill's shell block runs before this hook, so this is the /ctransfer that just queued the push.
        # Blocking it keeps the transcript ending at the previous turn, which is what gets pushed.
        reason = ("ctransfer: push queued. This session moves to the cloud as soon as it is idle (a few seconds).\n"
                  "Don't continue here: anything done locally now would diverge from the cloud copy.\n"
                  "Send any prompt to check progress; /exit once it says running.")
    elif state in ACTIVE:
        where = ACTIVE[state] + (f" (transfer {tid})" if tid else "")
        lines = [f"ctransfer: this session is {where}.",
                 "Your prompt was not sent: anything done here now would diverge from the cloud copy."]
        if state == "running":
            lines.append(f"/exit now. Attach with `ctransfer ssh {tid}`; bring it back with `ctransfer pull {tid}`.")
        else:
            last = last_log_line(log)
            lines.append(f"Latest: {last}" if last else f"Log: {log}")
            lines.append("Send any prompt to check again; /exit once it says running.")
        lines.append(f"Transfer gone or pushed by mistake? `ctransfer rm {tid or '<id>'}`, or delete {marker_path}.")
        reason = "\n".join(lines)
    else:
        return
    print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    main()
