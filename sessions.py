"""Locate Claude Code sessions and their on-disk state under ~/.claude."""

from __future__ import annotations

import json
import os
import re
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


def find_transcript(session_id: str, home: Path | None = None) -> Path | None:
    home = home or claude_home()
    return next((home / "projects").glob(f"*/{session_id}.jsonl"), None)


def read_records(path: Path) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as f:
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
