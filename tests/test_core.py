import json
import tarfile

import pytest

from ctransfer import apply, bundle, sessions, workspace
from tests.conftest import make_repo, run_git


def test_encode_project_dir_matches_claude():
    assert sessions.encode_project_dir("/Users/me/Desktop/my_project") == "-Users-me-Desktop-my-project"


def test_discover_prefers_live_session(laptop):
    info = sessions.discover(cwd=str(laptop["repo"]))
    assert info.session_id == laptop["sid"]
    assert info.status == "idle"
    assert info.cwd == str(laptop["repo"])
    assert info.project_dir == laptop["transcript"].parent.name


def test_discover_by_id_without_live_entry(laptop):
    for f in (laptop["home"] / "sessions").iterdir():
        f.unlink()
    info = sessions.discover(session_id=laptop["sid"])
    assert info.live is None and info.status == "stopped"
    assert info.cwd == str(laptop["repo"])


def test_secret_names():
    assert workspace.is_secret_name("a/b/.env")
    assert workspace.is_secret_name("id_rsa.pub")
    assert workspace.is_secret_name("prod.tfvars")
    assert not workspace.is_secret_name(".env.example")
    assert not workspace.is_secret_name("app.py")


def test_git_capture_restore_roundtrip(laptop, tmp_path):
    meta = workspace.capture(laptop["repo"], tmp_path / "ws")
    assert meta["kind"] == "git" and meta["branch"] == "main"
    assert ".env" in meta["skipped"]
    dest = tmp_path / "vm" / "proj"
    workspace.restore_fresh(meta, tmp_path / "ws", dest)
    assert (dest / "app.py").read_text() == "print('v2 uncommitted')\n"
    assert (dest / "notes.md").read_text() == "untracked notes\n"
    assert (dest / "data.bin").read_bytes() == bytes(range(256))
    assert not (dest / ".env").exists()
    assert run_git(dest, "rev-parse", "HEAD") == meta["head"]
    assert run_git(dest, "remote", "get-url", "origin") == "https://github.com/example/repo.git"


def test_restore_refuses_non_empty_target(laptop, tmp_path):
    meta = workspace.capture(laptop["repo"], tmp_path / "ws")
    with pytest.raises(workspace.WorkspaceError):
        workspace.restore_fresh(meta, tmp_path / "ws", laptop["repo"])


def test_apply_existing_fast_forwards_and_applies_dirty(tmp_path):
    local = make_repo(tmp_path / "local")
    meta = workspace.capture(local, tmp_path / "ws1")
    remote = tmp_path / "remote"
    workspace.restore_fresh(meta, tmp_path / "ws1", remote)
    run_git(remote, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-am", "remote work", "--allow-empty")
    (remote / "app.py").write_text("print('remote dirty')\n")
    (remote / "new.txt").write_text("new\n")
    back = workspace.capture(remote, tmp_path / "ws2")

    assert workspace.apply_existing(back, tmp_path / "ws2", local, dry_run=True) == []
    assert workspace.apply_existing(back, tmp_path / "ws2", local) == []
    assert run_git(local, "rev-parse", "HEAD") == back["head"]
    assert (local / "app.py").read_text() == "print('remote dirty')\n"
    assert (local / "new.txt").read_text() == "new\n"
    assert run_git(local, "for-each-ref", "refs/ctransfer/backup")


def test_apply_existing_detects_dirty_and_divergence(tmp_path):
    local = make_repo(tmp_path / "local")
    meta = workspace.capture(local, tmp_path / "ws1")
    remote = tmp_path / "remote"
    workspace.restore_fresh(meta, tmp_path / "ws1", remote)
    run_git(remote, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "remote")
    back = workspace.capture(remote, tmp_path / "ws2")
    run_git(local, "commit", "-q", "--allow-empty", "-m", "local diverges")
    (local / "app.py").write_text("dirty\n")

    problems = workspace.apply_existing(back, tmp_path / "ws2", local)
    assert any("uncommitted" in p for p in problems)
    assert any("--force" in p for p in problems)
    assert (local / "app.py").read_text() == "dirty\n"

    assert workspace.apply_existing(back, tmp_path / "ws2", local, stash=True, force=True) == []
    assert run_git(local, "rev-parse", "HEAD") == back["head"]
    assert "ctransfer pull" in run_git(local, "stash", "list")


def test_tree_capture_for_non_git_dir(tmp_path):
    d = tmp_path / "plain"
    (d / "node_modules").mkdir(parents=True)
    (d / "node_modules" / "x.js").write_text("x")
    (d / "a.txt").write_text("a")
    (d / "key.pem").write_text("k")
    meta = workspace.capture(d, tmp_path / "ws")
    assert meta["kind"] == "tree" and meta["skipped"] == ["key.pem"]
    workspace.restore_fresh(meta, tmp_path / "ws", tmp_path / "out")
    assert (tmp_path / "out" / "a.txt").read_text() == "a"
    assert not (tmp_path / "out" / "node_modules").exists()


def test_push_bundle_contents(laptop, tmp_path):
    info = sessions.discover(session_id=laptop["sid"])
    out = tmp_path / "b.tgz"
    m = bundle.build(out, kind="push", home=laptop["home"], session_id=info.session_id, project_dir=info.project_dir,
                     cwd=info.cwd, include_config=True, include_agent=True)
    names = tarfile.open(out).getnames()
    proj = f"claude/projects/{info.project_dir}"
    assert f"{proj}/{info.session_id}.jsonl" in names
    assert f"{proj}/memory/MEMORY.md" in names
    assert f"claude/file-history/{info.session_id}/abc@v1" in names
    assert "claude/CLAUDE.md" in names and "agent/ctransfer/agent.py" in names
    assert not any(n.endswith(".credentials.json") or n.endswith(".env") for n in names)
    assert m["record_count"] == 3 and m["claude_version"] == "2.1.287"

    extracted = bundle.extract(out, tmp_path / "x")
    assert extracted == json.loads((tmp_path / "x" / "manifest.json").read_text())


def test_untracked_secrets_are_neither_dirty_nor_stashed(tmp_path):
    local = make_repo(tmp_path / "local")
    meta = workspace.capture(local, tmp_path / "ws")
    (local / ".env").write_text("SECRET=1\n")
    assert workspace.apply_existing(meta, tmp_path / "ws", local, dry_run=True) == []
    (local / "scratch.txt").write_text("wip\n")
    assert workspace.apply_existing(meta, tmp_path / "ws", local, stash=True) == []
    assert not (local / "scratch.txt").exists()
    assert (local / ".env").read_text() == "SECRET=1\n"


def test_restore_accepts_relative_bundle_dir(laptop, tmp_path, monkeypatch):
    meta = workspace.capture(laptop["repo"], tmp_path / "ws")
    monkeypatch.chdir(tmp_path)
    workspace.restore_fresh(meta, "ws", tmp_path / "elsewhere" / "proj")
    assert (tmp_path / "elsewhere" / "proj" / "notes.md").exists()


def test_is_prefix():
    assert apply.is_prefix(["a", "b"], ["a", "b", "c"])
    assert apply.is_prefix([], ["a"])
    assert not apply.is_prefix(["a", "x"], ["a", "b", "c"])
    assert not apply.is_prefix(["a", "b", "c"], ["a", "b"])


def test_export_of_a_never_prompted_session_pulls_only_the_workspace(tmp_path):
    """A `ctransfer new` session nobody prompted has no transcript: pull brings back the VM's workspace and
    writes no transcript."""
    laptop_repo = make_repo(tmp_path / "laptop" / "proj")
    vm_repo = tmp_path / "vm" / "proj"
    run_git(tmp_path, "clone", "-q", str(laptop_repo), str(vm_repo))
    (vm_repo / "app.py").write_text("print('edited on the vm')\n")
    vm_home, laptop_home = tmp_path / "vm" / ".claude", tmp_path / "laptop" / ".claude"
    vm_home.mkdir()
    sid = "6f1d2c3b-0a9e-4b8c-8d7f-1e2a3b4c5d6e"
    out = tmp_path / "export.tgz"
    m = bundle.build(out, kind="export", home=vm_home, session_id=sid, project_dir=sessions.encode_project_dir(str(laptop_repo)),
                     cwd=str(laptop_repo), workspace_dir=str(vm_repo), record_root=str(laptop_repo))
    assert m["record_count"] == 0
    manifest = bundle.extract(out, tmp_path / "x")

    result = apply.apply_export(tmp_path / "x", manifest, home=laptop_home)
    assert result["transcript"] is None and result["session_id"] == sid
    assert (laptop_repo / "app.py").read_text() == "print('edited on the vm')\n"
    assert not (laptop_home / "projects").exists()


def test_push_bundle_still_requires_a_transcript(tmp_path):
    with pytest.raises(bundle.BundleError, match="transcript not found"):
        bundle.build(tmp_path / "b.tgz", kind="push", home=tmp_path, session_id="s", project_dir="p", cwd=str(tmp_path))
