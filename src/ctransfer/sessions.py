"""Locate Claude Code sessions and their on-disk state under ~/.claude."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def global_config_path(home: Path | None = None) -> Path:
    """~/.claude.json, or <CLAUDE_CONFIG_DIR>/.claude.json when the config dir is overridden."""
    if home is not None or os.environ.get("CLAUDE_CONFIG_DIR"):
        return (home or claude_home()) / ".claude.json"
    return Path.home() / ".claude.json"


def encode_project_dir(cwd: str) -> str:
    """Claude names a project's transcript dir after its cwd with every non-alphanumeric char as '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_sessions(home: Path | None = None) -> list[dict]:
    """Entries from sessions/<pid>.json whose process is still running."""
    home = home or claude_home()
    out = []
    for f in sorted((home / "sessions").glob("*.json")):
        try:
            entry = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(entry.get("pid"), int) and pid_alive(entry["pid"]):
            out.append(entry)
    return out


def find_live(session_id: str, home: Path | None = None) -> dict | None:
    return next((s for s in live_sessions(home) if s.get("sessionId") == session_id), None)


def _parent_pid(pid: int) -> int | None:
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return int(out) if out else None
    except (OSError, ValueError):
        return None


def current_session_id(home: Path | None = None) -> str | None:
    """The Claude session that started this process: $CLAUDE_CODE_SESSION_ID (set in Claude's shells),
    else the nearest ancestor process registered in sessions/<pid>.json."""
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return os.environ["CLAUDE_CODE_SESSION_ID"]
    home = home or claude_home()
    pid = os.getpid()
    for _ in range(64):
        if not pid or pid <= 1:
            break
        try:
            entry = json.loads((home / "sessions" / f"{pid}.json").read_text())
        except (OSError, ValueError):
            entry = {}
        if isinstance(entry.get("sessionId"), str):
            return entry["sessionId"]
        pid = _parent_pid(pid)
    return None


def find_transcript(session_id: str, home: Path | None = None) -> Path | None:
    home = home or claude_home()
    return next((home / "projects").glob(f"*/{session_id}.jsonl"), None)


def read_records(path: Path) -> list[dict]:
    records = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                records.append(rec)
    return records


def record_uuids(path: Path) -> list[str]:
    return [r["uuid"] for r in read_records(path) if isinstance(r.get("uuid"), str)]


def transcript_cwd(path: Path) -> str | None:
    return next((r["cwd"] for r in read_records(path) if isinstance(r.get("cwd"), str)), None)


def transcript_version(path: Path) -> str | None:
    versions = [r["version"] for r in read_records(path) if isinstance(r.get("version"), str)]
    return versions[-1] if versions else None


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# A model id or alias as claude --model takes it, e.g. claude-opus-5-5 or opus[1m]; never a flag.
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]*")


def model_args(path: Path) -> list[str]:
    """`--model`/`--effort` flags that resume a session on the model and effort level of its last reply.

    Every assistant record notes the model asked for (requestedModel) and the session's effort level, so a session
    picks up whatever /model and /effort last set, not the defaults of the machine it lands on."""
    if not path.is_file():
        return []
    for rec in reversed(read_records(path)):
        if rec.get("type") != "assistant" or rec.get("isSidechain"):
            continue
        model = rec.get("requestedModel") or (rec.get("message") or {}).get("model")
        if not isinstance(model, str) or not MODEL_NAME.fullmatch(model):
            continue  # e.g. "<synthetic>" on records Claude Code writes for API errors
        args = ["--model", model]
        if rec.get("effort") in EFFORT_LEVELS:
            args += ["--effort", rec["effort"]]
        return args
    return []


@dataclass
class SessionInfo:
    session_id: str
    cwd: str
    transcript: Path
    live: dict | None

    @property
    def project_dir(self) -> str:
        return self.transcript.parent.name

    @property
    def status(self) -> str:
        return (self.live or {}).get("status") or "stopped"


def discover(session_id: str | None = None, cwd: str | None = None, home: Path | None = None) -> SessionInfo:
    """Find a session by id, or the most recent one for cwd (live sessions first)."""
    home = home or claude_home()
    if session_id:
        transcript = find_transcript(session_id, home)
        if transcript is None:
            raise LookupError(f"no transcript for session {session_id} under {home / 'projects'}")
        live = find_live(session_id, home)
    else:
        cwd = os.path.realpath(cwd or os.getcwd())
        candidates = [s for s in live_sessions(home) if s.get("cwd") and os.path.realpath(s["cwd"]) == cwd]
        candidates.sort(key=lambda s: s.get("updatedAt") or 0, reverse=True)
        live = next((s for s in candidates if find_transcript(s.get("sessionId", ""), home)), None)
        if live:
            session_id = live["sessionId"]
            transcript = find_transcript(session_id, home)
        else:
            jsonls = sorted((home / "projects" / encode_project_dir(cwd)).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
            if not jsonls:
                raise LookupError(f"no Claude sessions found for {cwd}")
            transcript = jsonls[-1]
            session_id = transcript.stem
    session_cwd = (live or {}).get("cwd") or transcript_cwd(transcript) or cwd
    return SessionInfo(session_id=session_id, cwd=session_cwd, transcript=transcript, live=live)
