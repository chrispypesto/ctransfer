"""Apply an export bundle from the cloud over the local session (the pull direction)."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from . import sessions, workspace


class ApplyError(Exception):
    pass


def is_prefix(local: list[str], remote: list[str]) -> bool:
    return len(local) <= len(remote) and remote[: len(local)] == local


def _backup(home: Path, session_id: str, project_dir: str) -> Path:
    """Copy the local session to ~/.claude/ctransfer-backups/<session>/<timestamp>/ (created only if there is
    something to copy) and return that folder; the pull copies workspace files it replaces under workspace/."""
    proj = home / "projects" / project_dir
    items = [proj / f"{session_id}.jsonl", proj / session_id, proj / "memory", home / "file-history" / session_id]
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
    elif ws.get("files") is not None and not force:
        edits = workspace.tree_edits(ws["root"], ws["files"])
        if edits:
            raise ApplyError(workspace.tree_problem(ws["root"], edits))


SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def pushed_copy(pushed: dict) -> dict:
    """What the cloud copy of a session pushed from here must be (see check_manifest): the session, project dir
    and workspace its push record (cli.write_push_record) names."""
    ws = pushed["workspace"]
    return {"session_id": pushed["session_id"], "project_dir": pushed["project_dir"],
            "workspace": {"kind": ws["kind"], "root": ws["root"]}}


def transfer_copy(transfer: dict) -> dict:
    """What the cloud copy of a transfer with no push record here (pushed from another machine) must be: the
    session and folder the transfer was created with, and the workspace a push of that folder from here would take.
    The project dir isn't known: Claude Code names it after the folder, but not always the same way (long names are
    shortened), so any plain name will do."""
    return {"session_id": transfer["session_id"], "project_dir": None, "workspace": workspace.locate(transfer["cwd"])}


def _plain_name(name) -> bool:
    return isinstance(name, str) and name not in ("", ".", "..") and "/" not in name and "\\" not in name


def check_manifest(manifest: dict, expect: dict) -> None:
    """Refuse a cloud copy whose manifest doesn't match expect (pushed_copy or transfer_copy).

    The VM writes the export's manifest, and pull writes where it says, so a compromised VM could otherwise point the
    workspace at, say, the home folder. expect's session_id None (a scheduled run's fork, named on the VM) only
    requires a session id; its project_dir None, a plain folder name under ~/.claude/projects; its workspace None,
    that the workspace is not applied."""
    want_sid = expect["session_id"]
    sid = manifest.get("session_id")
    if not (isinstance(sid, str) and SESSION_ID.fullmatch(sid)) or (want_sid is not None and sid != want_sid):
        raise ApplyError(f"the cloud copy is of a different session ({sid!r}) than {want_sid or 'a session id'}; "
                         "nothing was changed")
    project_dir, want_dir = manifest.get("project_dir"), expect["project_dir"]
    if not _plain_name(project_dir) or (want_dir is not None and project_dir != want_dir):
        raise ApplyError(f"the cloud copy is of a different project ({project_dir!r}) than "
                         f"{want_dir or 'a project folder'}; nothing was changed")
    for name in manifest.get("extra_transcripts", []):
        if not _plain_name(name) or not name.endswith(".jsonl"):
            raise ApplyError(f"the cloud copy names a transcript outside its project ({name!r}); nothing was changed")
    want_ws, ws = expect["workspace"], manifest.get("workspace") or {}
    if want_ws is not None and (ws.get("kind"), ws.get("root")) != (want_ws["kind"], want_ws["root"]):
        raise ApplyError(f"the cloud copy's workspace ({ws.get('kind')} at {ws.get('root')}) is not the one this "
                         f"session uses here ({want_ws['kind']} at {want_ws['root']}); nothing was changed")


def apply_export(bundle_dir, manifest: dict, *, expect: dict, home: Path | None = None, force: bool = False,
                 as_fork: bool = False, stash: bool = False, pushed_changes: str | None = None,
                 pushed_files: dict | None = None, accept_config: bool = False) -> dict:
    """Overwrite the local transcript, file-history, memory and workspace (unless expect has none) with the
    cloud copy.

    expect (pushed_copy or transfer_copy, never the manifest itself: the VM wrote it) is what the manifest must
    name, see check_manifest; its workspace None leaves the workspace alone. Every check runs before anything is
    written; the previous local state is backed up under
    ~/.claude/ctransfer-backups/<session>/<timestamp>/ and the old git HEAD under refs/ctransfer/backup/.
    pushed_changes is the fingerprint of the uncommitted work that was pushed (workspace.changes_digest): local
    changes matching it are replaced, not refused or stashed. For a non-git folder, pushed_files is the push's
    per-file fingerprints: files changed since are refused, or with force backed up under workspace/ and replaced.
    The workspace's untracked files (or a non-git folder's) are checked member by member and written only inside
    it, never into .git; in a git repo every untracked or ignored local file they replace is backed up under
    workspace/. New or changed config files (workspace.config_changes: .claude/, .mcp.json, CLAUDE.md, .envrc,
    .vscode/) are refused unless accept_config.
    Returns {"session_id", "backup", "transcript", "stash", "overwritten", "replaced"}; transcript is None when the
    cloud session never recorded one (a `ctransfer new` session nobody prompted), in which case only the workspace
    comes back. stash is {"root", "message"} of the git stash holding local changes set aside with stash, else None.
    overwritten lists the non-git folder's files changed since the push that force replaced; replaced, every local
    workspace file the pull replaced after copying it to the backup (overwritten included).
    """
    bundle_dir = Path(bundle_dir)
    home = Path(home or sessions.claude_home())
    check_manifest(manifest, expect)
    workspace_too = expect["workspace"] is not None
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
                                            pushed_changes=pushed_changes, pushed_files=pushed_files,
                                            accept_config=accept_config)
        if problems:
            raise ApplyError("; ".join(problems))

    overwritten = []
    if workspace_too and ws["kind"] == "tree" and force:
        overwritten = workspace.tree_conflicts(bundle_dir / "workspace", ws["root"], pushed_files)
    backup = _backup(home, sid, project_dir)
    stash_info, replaced = None, []
    if workspace_too:
        try:
            applied = workspace.apply_existing(ws, bundle_dir / "workspace", ws["root"], stash=stash, force=force,
                                               pushed_changes=pushed_changes, pushed_files=pushed_files,
                                               accept_config=accept_config, backup_dir=backup / "workspace")
        except workspace.WorkspaceError as e:
            raise ApplyError(str(e)) from e
        replaced = applied["replaced"]
        if applied["stash"]:
            stash_info = {"root": ws["root"], "message": applied["stash"]}

    result = {"session_id": sid, "backup": str(backup) if backup.exists() else None, "transcript": None,
              "stash": stash_info, "overwritten": overwritten, "replaced": replaced}
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
