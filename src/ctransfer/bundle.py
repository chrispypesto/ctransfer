"""Build and unpack transfer bundles.

Layout of a bundle (.tgz):
    manifest.json
    claude/projects/<project-dir>/<session-id>.jsonl   transcript (plus memory/ and subagent dirs); none in a
                                                       `new` bundle, or in an export of a never-prompted session
    claude/file-history/<session-id>/                  pre-edit file copies for /rewind
    claude/CLAUDE.md, settings.json, skills/, ...      user config (push only)
    mcp.json                                           MCP servers from ~/.claude.json, see mcp.collect (push only)
    workspace/                                         see workspace.capture
    agent/ctransfer/*.py                               VM agent source (push only)
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import tarfile
import tempfile
import time
from pathlib import Path

from . import mcp, sessions, workspace

FORMAT = 1
CONFIG_ITEMS = ("CLAUDE.md", "settings.json", "skills", "agents", "commands", "output-styles")
# Synced from the claude.ai account on the VM itself; no need to carry it.
CONFIG_EXCLUDE = {"skills/synced"}
AGENT_MODULES = ("__init__.py", "sessions.py", "workspace.py", "bundle.py", "net.py", "mcp.py", "agent.py")
# Never shipped, whatever directory they turn up in.
FORBIDDEN_NAMES = {".credentials.json", ".claude.json"}


class BundleError(Exception):
    pass


def _copy(src: Path, dst: Path, skipped: list[str]) -> None:
    """Copy a file or tree, leaving out anything that looks like a credential."""
    if src.is_dir():
        for dirpath, _, filenames in os.walk(src):
            for name in filenames:
                f = Path(dirpath) / name
                _copy(f, dst / f.relative_to(src), skipped)
    elif src.is_file():
        if workspace.is_secret_name(src.name) or src.name in FORBIDDEN_NAMES:
            skipped.append(str(src))
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def build(
    out_path,
    *,
    kind: str,
    home: Path,
    session_id: str,
    project_dir: str,
    cwd: str,
    workspace_dir: str | None = None,
    record_root: str | None = None,
    extra_transcripts: tuple[Path, ...] = (),
    include_config: bool = False,
    include_agent: bool = False,
    include_file_history: bool = True,
    claude_version: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Write a bundle for one session to out_path and return its manifest.

    kind is push (an existing session), new (a session the VM starts fresh, so no transcript yet) or export.
    claude_version is recorded when there is no transcript to read it from.
    workspace_dir is where the workspace is read from (defaults to cwd); record_root, if given,
    replaces the workspace root recorded in the manifest (the VM uses it to report the laptop path).
    """
    home = Path(home)
    proj = home / "projects" / project_dir
    transcript = proj / f"{session_id}.jsonl"
    if kind == "push" and not transcript.is_file():
        raise BundleError(f"transcript not found: {transcript}")
    skipped: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ctransfer-") as tmp:
        stage = Path(tmp)
        dst_proj = stage / "claude" / "projects" / project_dir
        _copy(transcript, dst_proj / transcript.name, skipped)
        for t in extra_transcripts:
            _copy(t, dst_proj / t.name, skipped)
        _copy(proj / "memory", dst_proj / "memory", skipped)
        _copy(proj / session_id, dst_proj / session_id, skipped)  # subagent transcripts, tool-result spill files
        if include_file_history:
            _copy(home / "file-history" / session_id, stage / "claude" / "file-history" / session_id, skipped)
        if include_config:
            for item in CONFIG_ITEMS:
                src = home / item
                if src.is_dir():
                    for child in src.iterdir():
                        if f"{item}/{child.name}" not in CONFIG_EXCLUDE:
                            _copy(child, stage / "claude" / item / child.name, skipped)
                else:
                    _copy(src, stage / "claude" / item, skipped)
        if include_agent:
            pkg = Path(__file__).parent
            agent_dir = stage / "agent" / "ctransfer"
            agent_dir.mkdir(parents=True)
            for mod in AGENT_MODULES:
                shutil.copy2(pkg / mod, agent_dir / mod)

        ws = workspace.capture(workspace_dir or cwd, stage / "workspace")
        if record_root:
            ws["root"] = record_root
        carried_mcp = mcp.collect(sessions.global_config_path(), [cwd, ws["root"]], skipped) if include_config else {}
        if carried_mcp:
            (stage / "mcp.json").write_text(json.dumps(carried_mcp, indent=2))
        _assert_clean(stage)

        has_transcript = transcript.is_file()
        uuids = sessions.record_uuids(transcript) if has_transcript else []
        manifest = {
            "format": FORMAT,
            "kind": kind,
            "session_id": session_id,
            "cwd": cwd,
            "project_dir": project_dir,
            "claude_version": (sessions.transcript_version(transcript) if has_transcript else None) or claude_version,
            "record_count": len(uuids),
            "last_uuid": uuids[-1] if uuids else None,
            "extra_transcripts": [t.name for t in extra_transcripts],
            "mcp_servers": mcp.names(carried_mcp),
            "workspace": ws,
            "skipped": sorted(skipped + [f"workspace:{s}" for s in ws.get("skipped", [])]),
            "created_at": int(time.time()),
            "host": platform.node(),
            **(extra or {}),
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2))
        with tarfile.open(out_path, "w:gz") as tf:
            for child in sorted(stage.iterdir()):
                tf.add(child, arcname=child.name)
    return manifest


def _assert_clean(stage: Path) -> None:
    for dirpath, _, filenames in os.walk(stage / "claude"):
        bad = FORBIDDEN_NAMES.intersection(filenames)
        if bad:
            raise BundleError(f"refusing to bundle credential files: {sorted(bad)} in {dirpath}")


def extract(bundle_path, dest) -> dict:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle_path) as tf:
        tf.extractall(dest, filter="data")
    manifest = json.loads((dest / "manifest.json").read_text())
    if manifest.get("format") != FORMAT:
        raise BundleError(f"unsupported bundle format {manifest.get('format')}")
    return manifest
