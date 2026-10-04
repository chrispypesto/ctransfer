import json
import shutil
import subprocess
import sys
from pathlib import Path

from ctransfer import agent, bundle, sessions

BOOT = {"transfer_id": "t1", "agent_url": "https://agent.example/", "agent_token": "tok", "claude_token": "sk-ant-oat-x"}


def push_and_restore(laptop, tmp_path):
    info = sessions.discover(session_id=laptop["sid"])
    b = tmp_path / "push.tgz"
    bundle.build(b, kind="push", home=laptop["home"], session_id=info.session_id, project_dir=info.project_dir,
                 cwd=info.cwd, include_config=True, include_agent=True)
    bundle.extract(b, tmp_path / "unpacked")
    vm_home = tmp_path / "vm" / "home" / "ubuntu"
    vm_home.mkdir(parents=True)
    state = agent.restore(BOOT, tmp_path / "unpacked", home=vm_home, root_prefix=str(tmp_path / "vm" / "fs"), launch=False)
    return info, vm_home, state


def test_restore_lays_out_claude_home_and_workspace(laptop, tmp_path):
    info, vm_home, state = push_and_restore(laptop, tmp_path)
    vm_claude = vm_home / ".claude"
    assert (vm_claude / "projects" / info.project_dir / f"{info.session_id}.jsonl").read_bytes() == laptop["transcript"].read_bytes()
    assert (vm_claude / "projects" / info.project_dir / "memory" / "MEMORY.md").exists()
    assert (vm_claude / "file-history" / info.session_id / "abc@v1").exists()
    assert (vm_claude / "CLAUDE.md").read_text() == "global rules\n"
    assert not (vm_claude / ".credentials.json").exists()

    vm_repo = tmp_path / "vm" / "fs" / str(laptop["repo"]).lstrip("/")
    assert state["vm_root"] == str(vm_repo)
    assert (vm_repo / "app.py").read_text() == "print('v2 uncommitted')\n"
    cfg = json.loads((vm_home / ".claude.json").read_text())
    assert cfg["hasCompletedOnboarding"] and cfg["projects"][str(vm_repo)]["hasTrustDialogAccepted"]
    assert "sk-ant" not in json.dumps(state)


def test_manual_restore_launches_claude_told_the_transfer_is_complete(laptop, tmp_path, monkeypatch):
    """`agent restore --manual` end to end. Only the external steps are intercepted: the Claude installer
    (network) and tmux; the generated launcher then runs for real against a stand-in claude binary."""
    info = sessions.discover(session_id=laptop["sid"])
    b = tmp_path / "push.tgz"
    bundle.build(b, kind="push", home=laptop["home"], session_id=info.session_id, project_dir=info.project_dir,
                 cwd=info.cwd, include_config=True, include_agent=True)
    bundle.extract(b, tmp_path / "bundle")
    shutil.rmtree(laptop["repo"])  # the VM is another machine: the project path starts out empty
    vm_home = tmp_path / "vm" / "home"
    vm_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(vm_home))  # --user "": restore into $HOME without sudo/chown
    boot = tmp_path / "boot.json"
    boot.write_text(json.dumps({**BOOT, "agent_url": ""}))
    external, real_run = [], subprocess.run

    def run(cmd, **kw):
        if any(x in cmd[-1] for x in ("install.sh", "tmux ")):
            return external.append(cmd[-1])
        return real_run(cmd, **kw)

    monkeypatch.setattr(agent.subprocess, "run", run)
    agent.main(["restore", "--boot", str(boot), "--bundle-dir", str(tmp_path / "bundle"), "--user", "",
                "--state", str(tmp_path / "state.json"), "--manual"])

    assert not boot.exists() and not (tmp_path / "bundle").exists()  # the token-bearing boot file is gone
    assert json.loads((tmp_path / "state.json").read_text())["session_id"] == info.session_id
    assert [c.split()[0] for c in external] == ["curl", "tmux"]  # no systemd agent in manual mode
    assert (Path(info.cwd) / "app.py").read_text() == "print('v2 uncommitted')\n"

    stub = vm_home / ".local" / "bin" / "claude"  # prints the argv the launcher passes to claude
    stub.parent.mkdir(parents=True)
    stub.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    stub.chmod(0o755)
    out = real_run(["bash", str(vm_home / ".ctransfer" / "launch.sh")], env={"HOME": str(vm_home), "PATH": "/usr/bin:/bin"},
                   capture_output=True, text=True, check=True).stdout
    argv = json.loads(out)
    assert argv[:2] == ["--resume", info.session_id]
    note = argv[argv.index("--append-system-prompt") + 1]
    assert "transfer t1" in note and "complete" in note and "ctransfer pull t1" in note
