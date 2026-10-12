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


TRANSCRIPT_DIVERGED = (
    "the local transcript has records the cloud copy lacks (you kept working locally after pushing). "
    "Pass --force to overwrite them (a backup is kept) or --as-fork to pull into a new session id.")


def check_not_open(session_id: str, home: Path) -> None:
    live = sessions.find_live(session_id, home)
    if live:
        raise ApplyError(f"session {session_id} is open locally (pid {live['pid']}); exit it before pulling")


def check_before_export(pushed: dict, *, home: Path | None = None, force: bool = False, as_fork: bool = False,
                        stash: bool = False) -> None:
    """Run apply_export's checks against what was pushed (see cli.write_push_record), before the cloud copy
    exists. Asking for the export shuts the VM down, so a pull that would be refused must stop here, with nothing
    changed anywhere. The cloud copy only adds to what was pushed, so whatever passes here passes there too, as
    long as the local copy doesn't change in between."""
    home = Path(home or sessions.claude_home())
    sid = pushed["session_id"]
    check_not_open(sid, home)
    local_transcript = home / "projects" / pushed["project_dir"] / f"{sid}.jsonl"
    if local_transcript.exists() and not as_fork and not force:
        local_ids = sessions.record_uuids(local_transcript)
        count = pushed["record_count"]
        if len(local_ids) > count or (local_ids and len(local_ids) == count and local_ids[-1] != pushed["last_uuid"]):
            raise ApplyError(TRANSCRIPT_DIVERGED)
    ws = pushed["workspace"]
    if ws["kind"] == "git":
        problems = workspace.check_local(ws["root"], branch=ws.get("branch"), target=ws["head"], stash=stash,
                                         force=force, pushed_changes=ws.get("changes"))
        if problems:
            raise ApplyError("; ".join(problems))


def apply_export(bundle_dir, manifest: dict, *, home: Path | None = None, force: bool = False,
                 as_fork: bool = False, stash: bool = False, workspace_too: bool = True,
                 pushed_changes: str | None = None) -> dict:
    """Overwrite the local transcript, file-history, memory and workspace (unless not workspace_too) with the
    cloud copy.

    Every check runs before anything is written; the previous local state is backed up under
    ~/.claude/ctransfer-backups/<session>/<timestamp>/ and the old git HEAD under refs/ctransfer/backup/.
    pushed_changes is the fingerprint of the uncommitted work that was pushed (workspace.changes_digest): local
    changes matching it are replaced, not refused or stashed.
    Returns {"session_id", "backup", "transcript", "stash"}; transcript is None when the cloud session never
    recorded one (a `ctransfer new` session nobody prompted), in which case only the workspace comes back. stash
    is {"root", "message"} of the git stash holding local changes set aside with stash, else None.
    """
    bundle_dir = Path(bundle_dir)
    home = Path(home or sessions.claude_home())
    sid, project_dir = manifest["session_id"], manifest["project_dir"]
    src_proj = bundle_dir / "claude" / "projects" / project_dir
    remote_transcript = src_proj / f"{sid}.jsonl"
    local_transcript = home / "projects" / project_dir / f"{sid}.jsonl"

    check_not_open(sid, home)
    has_transcript = remote_transcript.is_file()
    if has_transcript and local_transcript.exists() and not as_fork:
        local_ids, remote_ids = sessions.record_uuids(local_transcript), sessions.record_uuids(remote_transcript)
        if not is_prefix(local_ids, remote_ids) and not force:
            raise ApplyError(TRANSCRIPT_DIVERGED)

    ws = manifest["workspace"]
    if workspace_too:
        problems = workspace.check_existing(ws, bundle_dir / "workspace", ws["root"], stash=stash, force=force,
                                            pushed_changes=pushed_changes)
        if problems:
            raise ApplyError("; ".join(problems))

    backup = _backup(home, sid, project_dir)
    stash_info = None
    if workspace_too:
        message = workspace.apply_existing(ws, bundle_dir / "workspace", ws["root"], stash=stash, force=force,
                                           pushed_changes=pushed_changes)
        if message:
            stash_info = {"root": ws["root"], "message": message}

    result = {"session_id": sid, "backup": str(backup) if backup else None, "transcript": None, "stash": stash_info}
    if not has_transcript:
        return result
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
    return {**result, "session_id": target_sid, "transcript": str(dst_proj / f"{target_sid}.jsonl")}
