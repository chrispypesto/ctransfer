import io
import json
import os
import socket
import tarfile
from pathlib import Path

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

    assert workspace.check_existing(back, tmp_path / "ws2", local) == []
    assert workspace.apply_existing(back, tmp_path / "ws2", local) is None
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

    problems = workspace.check_existing(back, tmp_path / "ws2", local)
    assert any("uncommitted" in p for p in problems)
    assert any("--force" in p for p in problems)
    with pytest.raises(workspace.WorkspaceError, match="--stash"):
        workspace.apply_existing(back, tmp_path / "ws2", local)
    assert (local / "app.py").read_text() == "dirty\n"

    stash = workspace.apply_existing(back, tmp_path / "ws2", local, stash=True, force=True)
    assert run_git(local, "rev-parse", "HEAD") == back["head"]
    assert stash in run_git(local, "stash", "list")


def test_pushed_uncommitted_changes_give_way_to_the_cloud_copy(tmp_path):
    local = make_repo(tmp_path / "local")
    (local / "app.py").write_text("pushed edit\n")
    (local / "scratch.txt").write_text("pushed, then deleted in the cloud\n")
    pushed = workspace.capture(local, tmp_path / "ws1")
    remote = tmp_path / "remote"
    workspace.restore_fresh(pushed, tmp_path / "ws1", remote)
    (remote / "app.py").write_text("cloud edit\n")
    (remote / "scratch.txt").unlink()
    back = workspace.capture(remote, tmp_path / "ws2")

    # Without the push's fingerprint the pushed edits look like local work; with it they are the cloud copy's past.
    assert workspace.check_existing(back, tmp_path / "ws2", local) != []
    assert workspace.check_existing(back, tmp_path / "ws2", local, pushed_changes=pushed["changes"]) == []
    (local / "app.py").write_text("edited again after the push\n")
    assert workspace.check_existing(back, tmp_path / "ws2", local, pushed_changes=pushed["changes"]) != []
    (local / "app.py").write_text("pushed edit\n")

    assert workspace.apply_existing(back, tmp_path / "ws2", local, pushed_changes=pushed["changes"]) is None
    assert (local / "app.py").read_text() == "cloud edit\n"
    assert not (local / "scratch.txt").exists()
    assert run_git(local, "stash", "list") == ""


def test_untracked_nested_repo_is_never_taken_for_pushed_work(tmp_path):
    local = make_repo(tmp_path / "local")
    make_repo(local / "vendor" / "lib")  # a clone inside the workspace, untracked: git lists it as "vendor/lib/"
    pushed = workspace.capture(local, tmp_path / "ws1")
    remote = tmp_path / "remote"
    workspace.restore_fresh(pushed, tmp_path / "ws1", remote)
    back = workspace.capture(remote, tmp_path / "ws2")

    assert pushed["changes"] is None
    assert workspace.check_existing(back, tmp_path / "ws2", local, pushed_changes=pushed["changes"]) != []
    assert (local / "vendor" / "lib" / "app.py").exists()


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
    assert workspace.check_existing(meta, tmp_path / "ws", local) == []
    (local / "scratch.txt").write_text("wip\n")
    assert workspace.apply_existing(meta, tmp_path / "ws", local, stash=True)
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


def _plain_folder_round(tmp_path, links=None):
    """A non-git folder sent to a VM (as `ctransfer new` does) and restored there. Returns (folder, push manifest,
    vm folder, export(): the VM's copy brought back as pull receives it)."""
    folder = tmp_path / "laptop" / "notes"
    folder.mkdir(parents=True)
    (folder / "plan.txt").write_text("v1\n")
    (folder / "todo.txt").write_text("todo v1\n")
    (folder / ".env").write_text("SECRET=1\n")
    for link, target in (links or {}).items():
        os.link(folder / target, folder / link)
    sid = "6f1d2c3b-0a9e-4b8c-8d7f-1e2a3b4c5d6e"
    project_dir = sessions.encode_project_dir(str(folder))
    home = tmp_path / "laptop" / ".claude"
    home.mkdir()
    pushed = bundle.build(tmp_path / "push.tgz", kind="new", home=home, session_id=sid, project_dir=project_dir,
                          cwd=str(folder))
    bundle.extract(tmp_path / "push.tgz", tmp_path / "pushed")
    vm_folder = tmp_path / "vm" / "notes"
    workspace.restore_fresh(pushed["workspace"], tmp_path / "pushed" / "workspace", vm_folder)

    def export():
        vm_home = tmp_path / "vm" / ".claude"
        vm_home.mkdir(exist_ok=True)
        bundle.build(tmp_path / "export.tgz", kind="export", home=vm_home, session_id=sid, project_dir=project_dir,
                     cwd=str(folder), workspace_dir=str(vm_folder), record_root=str(folder))
        return bundle.extract(tmp_path / "export.tgz", tmp_path / "x")

    return folder, home, pushed, vm_folder, export


def test_pull_into_a_plain_folder_replaces_only_files_unchanged_since_the_push(tmp_path):
    folder, home, pushed, vm_folder, export = _plain_folder_round(tmp_path)
    (vm_folder / "plan.txt").write_text("v2 from the vm\n")
    (vm_folder / "new.txt").write_text("made on the vm\n")
    (folder / ".DS_Store").write_text("finder junk\n")  # appeared locally, the cloud copy has no such file

    apply.check_before_export(pushed, home=home)
    result = apply.apply_export(tmp_path / "x", export(), home=home, pushed_files=pushed["workspace"]["files"])

    assert (folder / "plan.txt").read_text() == "v2 from the vm\n"
    assert (folder / "new.txt").read_text() == "made on the vm\n"
    assert (folder / ".DS_Store").read_text() == "finder junk\n"
    assert (folder / ".env").read_text() == "SECRET=1\n"
    assert result["overwritten"] == [] and result["backup"] is None


def test_pull_into_a_plain_folder_refuses_a_new_local_file_the_cloud_copy_also_made(tmp_path):
    """A file created on both sides after the push can't be caught before the export (nothing about it was
    pushed); the cloud copy shows the clash, and pull refuses before writing anything."""
    folder, home, pushed, vm_folder, export = _plain_folder_round(tmp_path)
    (vm_folder / "plan.txt").write_text("v2 from the vm\n")
    (vm_folder / "draft.txt").write_text("vm draft\n")
    (folder / "draft.txt").write_text("my draft\n")

    apply.check_before_export(pushed, home=home)
    manifest = export()
    with pytest.raises(apply.ApplyError, match=r"changed here since the push.*: draft\.txt\. .*--force"):
        apply.apply_export(tmp_path / "x", manifest, home=home, pushed_files=pushed["workspace"]["files"])
    assert (folder / "draft.txt").read_text() == "my draft\n"
    assert (folder / "plan.txt").read_text() == "v1\n"

    result = apply.apply_export(tmp_path / "x", manifest, home=home, force=True,
                                pushed_files=pushed["workspace"]["files"])
    assert result["overwritten"] == ["draft.txt"]
    assert (folder / "draft.txt").read_text() == "vm draft\n"
    assert (folder / "plan.txt").read_text() == "v2 from the vm\n"
    assert (Path(result["backup"]) / "workspace" / "draft.txt").read_text() == "my draft\n"


def test_pull_into_a_plain_folder_pushed_from_elsewhere_refuses_any_local_difference(tmp_path):
    """Without this machine's push fingerprints nothing local is known to be what the cloud copy grew out of."""
    folder, home, pushed, vm_folder, export = _plain_folder_round(tmp_path)
    (vm_folder / "plan.txt").write_text("v2 from the vm\n")
    with pytest.raises(apply.ApplyError, match=r"plan\.txt"):
        apply.apply_export(tmp_path / "x", export(), home=home)
    assert (folder / "plan.txt").read_text() == "v1\n"


def test_pull_into_a_plain_folder_refuses_local_edits_before_the_export(tmp_path):
    folder, home, pushed, vm_folder, export = _plain_folder_round(tmp_path)
    (folder / "plan.txt").write_text("edited here after the push\n")

    with pytest.raises(apply.ApplyError, match=r": plan\.txt\. Nothing was changed\. .*--force"):
        apply.check_before_export(pushed, home=home)
    apply.check_before_export(pushed, home=home, force=True)

    # The VM left plan.txt alone: the cloud copy is still the pushed version, and the local edit is still newer.
    with pytest.raises(apply.ApplyError, match=r"plan\.txt"):
        apply.apply_export(tmp_path / "x", export(), home=home, pushed_files=pushed["workspace"]["files"])
    assert (folder / "plan.txt").read_text() == "edited here after the push\n"


def test_push_of_a_plain_folder_with_a_fifo_and_a_socket(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "a.txt").write_text("a")
    os.mkfifo(folder / "pipe")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(folder / "s.sock"))
    try:
        meta = workspace.capture(folder, tmp_path / "ws")
    finally:
        sock.close()
    assert set(meta["files"]) == {"a.txt"}


def test_pull_ignores_archive_entries_outside_the_folder(tmp_path):
    """Only what extraction would write inside the folder is compared (and backed up with --force)."""
    folder, outside, src = tmp_path / "notes", tmp_path / "outside.txt", tmp_path / "ws"
    folder.mkdir()
    src.mkdir()
    outside.write_text("not part of the folder\n")
    with tarfile.open(src / "tree.tar", "w") as tf:
        info = tarfile.TarInfo("../outside.txt")
        info.size = 5
        tf.addfile(info, io.BytesIO(b"other"))
    assert workspace.tree_conflicts(src, folder, {}) == []


def test_pull_into_a_plain_folder_backs_up_an_edited_hard_link(tmp_path):
    """Of two hard-linked files, the archive holds the second as a link to the first; an editor that saves by
    rename breaks the link locally, and that file's edit must not be lost."""
    folder, home, pushed, vm_folder, export = _plain_folder_round(tmp_path, links={"b.txt": "plan.txt"})
    manifest = export()
    with tarfile.open(tmp_path / "x" / "workspace" / "tree.tar") as tf:
        [link] = [m.name for m in tf if m.islnk()]
    (folder / "edit.tmp").write_text("edited here\n")
    os.replace(folder / "edit.tmp", folder / link)
    with pytest.raises(apply.ApplyError, match=rf": {link}\."):
        apply.apply_export(tmp_path / "x", manifest, home=home, pushed_files=pushed["workspace"]["files"])

    result = apply.apply_export(tmp_path / "x", manifest, home=home, force=True,
                                pushed_files=pushed["workspace"]["files"])
    assert result["overwritten"] == [link]
    assert (Path(result["backup"]) / "workspace" / link).read_text() == "edited here\n"
    assert (folder / link).read_text() == "v1\n"
