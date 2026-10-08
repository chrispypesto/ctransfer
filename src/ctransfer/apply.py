"""Apply an export bundle from the cloud over the local session (the pull direction)."""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

from . import sessions, workspace


class ApplyError(Exception):
    pass


def is_prefix(local: list[str], remote: list[str]) -> bool:
    return len(local) <= len(remote) and remote[: len(local)] == local


def _backup(home: Path, session_id: str, project_dir: str) -> Path | None:
    proj = home / "projects" / project_dir
    items = [proj / f"{session_id}.jsonl", proj / session_id, proj / "memory", home / "file-history" / session_id]
    if not any(p.exists() for p in items):
        return None
    dest = home / "ctransfer-backups" / session_id / time.strftime("%Y%m%dT%H%M%S")
    for p in items:
        if p.is_dir():
            shutil.copytree(p, dest / p.relative_to(home))
        elif p.exists():
            (dest / p.relative_to(home)).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest / p.relative_to(home))
    return dest


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.ctransfer-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _merge_tree(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)


def _fork_records(data: bytes, old: str, new: str) -> bytes:
    out = []
    for line in data.decode().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        for key in ("sessionId", "session_id"):
            if rec.get(key) == old:
                rec[key] = new
        out.append(json.dumps(rec))
    return ("\n".join(out) + "\n").encode()


def apply_export(bundle_dir, manifest: dict, *, home: Path | None = None, force: bool = False,
                 as_fork: bool = False, stash: bool = False) -> dict:
    """Overwrite the local transcript, file-history, memory and workspace with the cloud copy.

    Every check runs before anything is written; the previous local state is backed up under
    ~/.claude/ctransfer-backups/<session>/<timestamp>/ and the old git HEAD under refs/ctransfer/backup/.
    Returns {"session_id", "backup", "transcript"}; transcript is None when the cloud session never recorded
    one (a `ctransfer new` session nobody prompted), in which case only the workspace comes back.
    """
    bundle_dir = Path(bundle_dir)
    home = Path(home or sessions.claude_home())
    sid, project_dir = manifest["session_id"], manifest["project_dir"]
    src_proj = bundle_dir / "claude" / "projects" / project_dir
    remote_transcript = src_proj / f"{sid}.jsonl"
    local_transcript = home / "projects" / project_dir / f"{sid}.jsonl"

    live = sessions.find_live(sid, home)
    if live:
        raise ApplyError(f"session {sid} is open locally (pid {live['pid']}); exit it before pulling")
    has_transcript = remote_transcript.is_file()
    if has_transcript and local_transcript.exists() and not as_fork:
        local_ids, remote_ids = sessions.record_uuids(local_transcript), sessions.record_uuids(remote_transcript)
        if not is_prefix(local_ids, remote_ids) and not force:
            raise ApplyError(
                "the local transcript has records the cloud copy lacks (you kept working locally after pushing). "
                "Pass --force to overwrite them (a backup is kept) or --as-fork to pull into a new session id.")

    ws = manifest["workspace"]
    problems = workspace.apply_existing(ws, bundle_dir / "workspace", ws["root"], stash=stash, force=force, dry_run=True)
    if problems:
        raise ApplyError("; ".join(problems))

    backup = _backup(home, sid, project_dir)
    workspace.apply_existing(ws, bundle_dir / "workspace", ws["root"], stash=stash, force=force)

    if not has_transcript:
        return {"session_id": sid, "backup": str(backup) if backup else None, "transcript": None}
    target_sid = str(uuid.uuid4()) if as_fork else sid
    data = remote_transcript.read_bytes()
    if as_fork:
        data = _fork_records(data, sid, target_sid)
    dst_proj = home / "projects" / project_dir
    _atomic_write(dst_proj / f"{target_sid}.jsonl", data)
    for name in manifest.get("extra_transcripts", []):
        if not (dst_proj / name).exists():
            shutil.copy2(src_proj / name, dst_proj / name)
    _merge_tree(src_proj / sid, dst_proj / target_sid)
    _merge_tree(src_proj / "memory", dst_proj / "memory")
    _merge_tree(bundle_dir / "claude" / "file-history" / sid, home / "file-history" / target_sid)
    return {"session_id": target_sid, "backup": str(backup) if backup else None, "transcript": str(dst_proj / f"{target_sid}.jsonl")}
