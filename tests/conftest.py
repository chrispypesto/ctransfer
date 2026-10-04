import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from ctransfer import api as api_mod
from ctransfer.sessions import encode_project_dir


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    """Keep ctransfer state out of the real ~/.config, and ignore the Claude session running the tests."""
    monkeypatch.setattr(api_mod, "CONFIG_PATH", tmp_path / "config" / "config.json")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def run_git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.email", "t@example.com")
    run_git(root, "config", "user.name", "t")
    (root / "app.py").write_text("print('v1')\n")
    (root / "data.bin").write_bytes(bytes(range(256)))
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    run_git(root, "remote", "add", "origin", "https://github.com/example/repo.git")
    return root


def record(session_id, cwd, **kw):
    return {"type": "user", "uuid": str(uuid.uuid4()), "sessionId": session_id, "cwd": cwd, "version": "2.1.287", **kw}


def write_transcript(home: Path, session_id: str, cwd: str, n: int = 3) -> Path:
    proj = home / "projects" / encode_project_dir(cwd)
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{session_id}.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps({"type": "mode", "mode": "normal", "sessionId": session_id}) + "\n")
        for i in range(n):
            f.write(json.dumps(record(session_id, cwd, message={"content": f"turn {i}"})) + "\n")
    return path


@pytest.fixture
def laptop(tmp_path, monkeypatch):
    """A fake ~/.claude with one session whose cwd is a dirty git repo."""
    home = tmp_path / "laptop" / ".claude"
    home.mkdir(parents=True)
    repo = make_repo(tmp_path / "laptop" / "src" / "my_proj")
    (repo / "app.py").write_text("print('v2 uncommitted')\n")
    (repo / "notes.md").write_text("untracked notes\n")
    (repo / ".env").write_text("SECRET=1\n")
    sid = str(uuid.uuid4())
    transcript = write_transcript(home, sid, str(repo))
    (transcript.parent / "memory").mkdir()
    (transcript.parent / "memory" / "MEMORY.md").write_text("- remembered\n")
    fh = home / "file-history" / sid
    fh.mkdir(parents=True)
    (fh / "abc@v1").write_text("old app.py\n")
    (home / "CLAUDE.md").write_text("global rules\n")
    (home / "settings.json").write_text("{}\n")
    (home / ".credentials.json").write_text('{"secret": true}')
    (home / "sessions").mkdir()
    (home / "sessions" / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": sid, "cwd": str(repo), "status": "idle", "updatedAt": 1}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return {"home": home, "repo": repo, "sid": sid, "transcript": transcript}
