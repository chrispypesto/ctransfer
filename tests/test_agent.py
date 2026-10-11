import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ctransfer import agent, bundle, sessions
from tests.conftest import add_replies, run_git

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


@pytest.mark.parametrize("prompt", [None, "keep going, then fix the user's \"login\" bug in $HOME"])
def test_manual_restore_launches_claude_told_the_transfer_is_complete(laptop, tmp_path, monkeypatch, prompt):
    """`agent restore --manual` end to end. Only the external steps are intercepted: the Claude installer
    (network) and tmux; the generated launcher then runs for real against a stand-in claude binary.
    `ctransfer push <prompt>`: the resumed claude runs the prompt at once instead of waiting idle."""
    info = sessions.discover(session_id=laptop["sid"])
    last_ran_with = add_replies(laptop["transcript"], info.session_id, info.cwd)
    b = tmp_path / "push.tgz"
    bundle.build(b, kind="push", home=laptop["home"], session_id=info.session_id, project_dir=info.project_dir,
                 cwd=info.cwd, include_config=True, include_agent=True, extra={"prompt": prompt} if prompt else None)
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
    assert agent.DETACH_HINT.strip() in external[1]  # whoever attaches sees how to leave
    assert (Path(info.cwd) / "app.py").read_text() == "print('v2 uncommitted')\n"

    stub = vm_home / ".local" / "bin" / "claude"  # prints the argv the launcher passes to claude
    stub.parent.mkdir(parents=True)
    stub.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    stub.chmod(0o755)
    out = real_run(["bash", str(vm_home / ".ctransfer" / "launch.sh")], env={"HOME": str(vm_home), "PATH": "/usr/bin:/bin"},
                   capture_output=True, text=True, check=True).stdout
    argv = json.loads(out)
    assert argv[:6] == ["--resume", info.session_id, *last_ran_with]  # same model and effort as on the laptop
    note_at = argv.index("--append-system-prompt") + 1
    assert "transfer t1" in argv[note_at] and "complete" in argv[note_at] and "ctransfer pull t1" in argv[note_at]
    assert argv[note_at + 1:] == ([prompt] if prompt else [])


def test_manual_restore_of_a_new_session_starts_claude_fresh_with_the_prompt(laptop, tmp_path, monkeypatch):
    """`ctransfer new`: the bundle carries the project, config and memory but no transcript, and the VM starts
    claude under the pre-chosen session id with the first prompt instead of resuming."""
    cwd, sid = str(laptop["repo"]), "0b9e3c1a-5f8e-4c2a-9d71-3f2a6b8e1c44"
    prompt = "fix the user's \"login\" bug in $HOME"
    b = tmp_path / "new.tgz"
    manifest = bundle.build(b, kind="new", home=laptop["home"], session_id=sid,
                            project_dir=sessions.encode_project_dir(cwd), cwd=cwd, include_config=True,
                            include_agent=True, claude_version="2.1.300", extra={"prompt": prompt})
    assert manifest["claude_version"] == "2.1.300" and manifest["record_count"] == 0
    bundle.extract(b, tmp_path / "bundle")
    shutil.rmtree(laptop["repo"])
    vm_home = tmp_path / "vm" / "home"
    vm_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(vm_home))
    boot = tmp_path / "boot.json"
    boot.write_text(json.dumps({**BOOT, "agent_url": ""}))
    real_run = subprocess.run
    monkeypatch.setattr(agent.subprocess, "run",
                        lambda cmd, **kw: None if any(x in cmd[-1] for x in ("install.sh", "tmux ")) else real_run(cmd, **kw))
    agent.main(["restore", "--boot", str(boot), "--bundle-dir", str(tmp_path / "bundle"), "--user", "",
                "--state", str(tmp_path / "state.json"), "--manual"])

    proj = vm_home / ".claude" / "projects" / sessions.encode_project_dir(cwd)
    assert (proj / "memory" / "MEMORY.md").exists() and not list(proj.glob("*.jsonl"))
    assert (vm_home / ".claude" / "CLAUDE.md").read_text() == "global rules\n"
    assert (Path(cwd) / "app.py").read_text() == "print('v2 uncommitted')\n"

    stub = vm_home / ".local" / "bin" / "claude"
    stub.parent.mkdir(parents=True)
    stub.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    stub.chmod(0o755)
    out = real_run(["bash", str(vm_home / ".ctransfer" / "launch.sh")], env={"HOME": str(vm_home), "PATH": "/usr/bin:/bin"},
                   capture_output=True, text=True, check=True).stdout
    argv = json.loads(out)
    assert argv[:2] == ["--session-id", sid] and "--resume" not in argv
    note = argv[argv.index("--append-system-prompt") + 1]
    assert "started on a cloud VM" in note and "ctransfer pull t1" in note
    assert argv[-1] == prompt


def test_scheduled_run_forks_the_session_headlessly_and_reports_its_result(laptop, tmp_path, monkeypatch):
    """A cron run's restore: the branch fast-forwards from its upstream, then the launcher runs one headless,
    forked, time-limited turn (against a stand-in claude) whose JSON result the agent reports."""
    upstream = tmp_path / "upstream.git"
    run_git(tmp_path, "clone", "-q", "--bare", str(laptop["repo"]), str(upstream))
    run_git(laptop["repo"], "remote", "set-url", "origin", str(upstream))
    info = sessions.discover(session_id=laptop["sid"])
    last_ran_with = add_replies(laptop["transcript"], info.session_id, info.cwd)
    b = tmp_path / "push.tgz"
    bundle.build(b, kind="push", home=laptop["home"], session_id=info.session_id, project_dir=info.project_dir,
                 cwd=info.cwd, include_config=True, include_agent=True)
    bundle.extract(b, tmp_path / "bundle")
    other = tmp_path / "other"  # someone pushes to the upstream after the schedule was created
    run_git(tmp_path, "clone", "-q", str(upstream), str(other))
    (other / "later.txt").write_text("landed after the push\n")
    run_git(other, "add", "later.txt")
    run_git(other, "-c", "user.email=o@x", "-c", "user.name=o", "commit", "-q", "-m", "later")
    run_git(other, "push", "-q", "origin", "main")
    shutil.rmtree(laptop["repo"])

    vm_home = tmp_path / "vm" / "home"
    vm_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(vm_home))
    run = {"prompt": "check the build; it's broken?", "note": "unattended run 3", "timeout_sec": 600,
           "permission_mode": "acceptEdits"}
    boot = tmp_path / "boot.json"
    boot.write_text(json.dumps({**BOOT, "agent_url": "", "run": run}))
    external, real_run = [], subprocess.run

    def intercept(cmd, **kw):
        if any(x in cmd[-1] for x in ("install.sh", "tmux ")):
            return external.append(cmd[-1])
        return real_run(cmd, **kw)

    monkeypatch.setattr(agent.subprocess, "run", intercept)
    agent.main(["restore", "--boot", str(boot), "--bundle-dir", str(tmp_path / "bundle"), "--user", "",
                "--state", str(tmp_path / "state.json"), "--manual"])

    state = json.loads((tmp_path / "state.json").read_text())
    assert state["run"] == run and state["workspace_update"] == "fast-forwarded from origin/main"
    assert (Path(info.cwd) / "later.txt").exists()
    assert (Path(info.cwd) / "app.py").read_text() == "print('v2 uncommitted')\n"  # the pushed edits survive

    fork = "f0f0f0f0-0000-4000-8000-000000000001"
    stub = vm_home / ".local" / "bin" / "claude"  # writes the fork's transcript and claude's -p JSON reply
    stub.parent.mkdir(parents=True)
    stub.write_text(f"#!{sys.executable}\nimport json, pathlib, sys\n"
                    f"pathlib.Path({str(Path(state['claude_home']) / 'projects' / info.project_dir / (fork + '.jsonl'))!r})"
                    ".write_text('{}\\n')\n"
                    f"print(json.dumps({{'session_id': {fork!r}, 'result': 'build fixed', 'is_error': False, "
                    "'num_turns': 4, 'total_cost_usd': 0.12, 'permission_denials': [{}], 'argv': sys.argv[1:]}))\n")
    stub.chmod(0o755)
    shim = tmp_path / "coreutils" / "timeout"  # Ubuntu's coreutils timeout, which macOS lacks: drop the limits, run
    shim.parent.mkdir()
    shim.write_text('#!/bin/bash\nwhile [[ $1 == -* ]]; do shift; done\nshift\nexec "$@"\n')
    shim.chmod(0o755)
    real_run(["bash", str(vm_home / ".ctransfer" / "launch.sh")],
             env={"HOME": str(vm_home), "PATH": f"{shim.parent}:/usr/bin:/bin"}, check=True)

    vm_agent = agent.Agent(state)
    assert vm_agent.status() == "done"
    result = agent.run_result(vm_home)
    assert result == {"exit_code": 0, "session_id": fork, "is_error": False, "num_turns": 4, "total_cost_usd": 0.12,
                      "text": "build fixed", "permission_denials": 1}
    argv = json.loads((vm_home / ".ctransfer" / "run" / "result.json").read_text())["argv"]
    assert argv[:5] == ["-p", run["prompt"], "--resume", info.session_id, "--fork-session"]
    assert argv[5:9] == last_ran_with and argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--append-system-prompt") + 1] == "unattended run 3"


def test_secrets_reach_claude_and_a_github_token_lets_git_and_gh_use_github(laptop, tmp_path, monkeypatch):
    """The user's stored secrets become claude's environment on the VM. A GitHub token also gets git pushing to
    github.com over HTTPS (SSH remotes included) with the laptop's commit identity, and gh installed."""
    run_git(laptop["repo"], "remote", "add", "fork", "git@github.com:me/repo.git")
    info = sessions.discover(session_id=laptop["sid"])
    b = tmp_path / "push.tgz"
    bundle.build(b, kind="push", home=laptop["home"], session_id=info.session_id, project_dir=info.project_dir,
                 cwd=info.cwd, include_config=True, include_agent=True)
    bundle.extract(b, tmp_path / "bundle")
    shutil.rmtree(laptop["repo"])
    vm_home = tmp_path / "vm" / "home"
    vm_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(vm_home))
    secrets = {"GITHUB_TOKEN": "github_pat_test", "ODD": "it's \"$quoted\" `and` spaced\nover lines"}
    boot = tmp_path / "boot.json"
    boot.write_text(json.dumps({**BOOT, "agent_url": "", "secrets": secrets}))
    external, real_run = [], subprocess.run

    def intercept(cmd, **kw):
        if any(x in cmd[-1] for x in ("install.sh", "tmux ", "apt-get")):
            external.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kw)

    monkeypatch.setattr(agent.subprocess, "run", intercept)
    agent.main(["restore", "--boot", str(boot), "--bundle-dir", str(tmp_path / "bundle"), "--user", "",
                "--state", str(tmp_path / "state.json"), "--manual"])
    assert any("apt-get install -y -q gh" in c for c in external)
    assert "github_pat_test" not in (tmp_path / "state.json").read_text()

    stub = vm_home / ".local" / "bin" / "claude"  # prints the secrets it sees in its environment
    stub.parent.mkdir(parents=True)
    stub.write_text(f"#!{sys.executable}\nimport json, os\n"
                    "print(json.dumps({k: os.environ.get(k) for k in ('GITHUB_TOKEN', 'ODD', 'CLAUDE_CODE_OAUTH_TOKEN')}))\n")
    stub.chmod(0o755)
    env = {"HOME": str(vm_home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    out = real_run(["bash", str(vm_home / ".ctransfer" / "launch.sh")], env=env, capture_output=True, text=True,
                   check=True).stdout
    assert json.loads(out) == {**secrets, "CLAUDE_CODE_OAUTH_TOKEN": BOOT["claude_token"]}

    repo = Path(info.cwd)
    fill = real_run(["git", "-C", str(repo), "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                    env=env, capture_output=True, text=True, check=True).stdout
    assert "username=x-access-token" in fill and "password=github_pat_test" in fill
    assert real_run(["git", "-C", str(repo), "remote", "get-url", "fork"], env=env, capture_output=True, text=True,
                    check=True).stdout.strip() == "https://github.com/me/repo.git"
    assert run_git(repo, "config", "user.email") == "t@example.com"


def test_without_a_github_token_git_is_left_alone(laptop, tmp_path):
    _, vm_home, state = push_and_restore(laptop, tmp_path)
    assert not (vm_home / ".gitconfig").exists()
    assert run_git(state["vm_root"], "config", "user.name") == "t"  # commits still carry the laptop's identity


def test_mcp_servers_follow_the_session_except_those_holding_literal_credentials(laptop, tmp_path):
    """User-scope servers and the project's local-scope ones land in the VM's ~/.claude.json at the same paths.
    One with a token written into its config stays on the laptop; one referencing a secret as ${NAME} goes."""
    repo = str(laptop["repo"])
    github = {"type": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
              "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}", "LOG_LEVEL": "debug"}}
    notion = {"type": "http", "url": "https://mcp.notion.com/mcp"}
    (laptop["home"] / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "me@example.com"},
        "mcpServers": {"github": github,
                       "leaky": {"type": "http", "url": "https://mcp.example.com", "headers": {"Authorization": "Bearer abc123"}}},
        "projects": {repo: {"mcpServers": {"notion": notion}, "enabledMcpjsonServers": ["db"], "allowedTools": []},
                     "/somewhere/else": {"mcpServers": {"other": notion}}},
    }))
    info, vm_home, state = push_and_restore(laptop, tmp_path)
    manifest = json.loads((tmp_path / "unpacked" / "manifest.json").read_text())
    assert manifest["mcp_servers"] == ["github (user)", "notion (local)"] and "mcp:user:leaky" in manifest["skipped"]
    assert b"abc123" not in subprocess.run(["tar", "-xzOf", str(tmp_path / "push.tgz")], capture_output=True).stdout

    cfg = json.loads((vm_home / ".claude.json").read_text())
    assert cfg["mcpServers"] == {"github": github}
    project = cfg["projects"][state["vm_root"]]
    assert project["mcpServers"] == {"notion": notion} and project["enabledMcpjsonServers"] == ["db"]
    assert project["hasTrustDialogAccepted"]
    assert "oauthAccount" not in cfg and not any(p.endswith("/somewhere/else") for p in cfg["projects"])


def test_a_claude_cli_already_on_the_vm_is_recognized(tmp_path):
    """A VM booted from an image with the CLI baked in skips installing the same version again."""
    home = tmp_path / "home"
    assert agent.installed_claude_version(home, None) is None
    binary = home / ".local" / "bin" / "claude"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\necho '2.1.296 (Claude Code)'\n")
    binary.chmod(0o755)
    assert agent.installed_claude_version(home, None) == "2.1.296"
