import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ctransfer import agent, bundle, cli, sessions
from tests.conftest import add_replies, make_repo, run_git

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


def test_a_new_session_from_an_empty_claude_home_restores_with_a_claude_home_on_the_vm(tmp_path):
    """A fresh install has no ~/.claude files, so the bundle has no claude/ folder; the VM must still get ~/.claude
    (the restore chowns it to the VM user)."""
    repo = make_repo(tmp_path / "laptop" / "src" / "my_proj")
    empty_home = tmp_path / "laptop" / ".claude"
    empty_home.mkdir(parents=True)
    b = tmp_path / "new.tgz"
    bundle.build(b, kind="new", home=empty_home, session_id="0b9e3c1a-5f8e-4c2a-9d71-3f2a6b8e1c44",
                 project_dir=sessions.encode_project_dir(str(repo)), cwd=str(repo), include_config=True,
                 include_agent=True)
    bundle.extract(b, tmp_path / "bundle")
    assert not (tmp_path / "bundle" / "claude").exists()
    vm_home = tmp_path / "vm" / "home"
    vm_home.mkdir(parents=True)
    state = agent.restore(BOOT, tmp_path / "bundle", home=vm_home, root_prefix=str(tmp_path / "vm" / "fs"), launch=False)
    assert (vm_home / ".claude").is_dir() and state["claude_home"] == str(vm_home / ".claude")


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
    # The note (earlier runs' summaries) comes first in the run's message, then the task.
    assert argv[:5] == ["-p", "unattended run 3\n\nThe task:\ncheck the build; it's broken?", "--resume",
                        info.session_id, "--fork-session"]
    assert argv[5:9] == last_ran_with and argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--append-system-prompt" not in argv


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


TASK_SID = "7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"


def task_boot(run: dict) -> dict:
    return {**BOOT, "agent_url": "", "session_id": TASK_SID, "claude_version": "latest", "run": run}


def restore_on_vm(tmp_path: Path, monkeypatch, vm: str, boot: dict, bundle_dir: Path) -> tuple[Path, dict]:
    """`agent restore --manual` on a fresh VM (its home under tmp_path/vm); only the Claude installer, apt and tmux
    are intercepted. Returns (vm home, the agent's state)."""
    vm_home = tmp_path / vm / "home"
    vm_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(vm_home))
    boot_file = tmp_path / vm / "boot.json"
    boot_file.write_text(json.dumps(boot))
    bundle_dir.mkdir(parents=True, exist_ok=True)
    real_run = subprocess.run

    def intercept(cmd, **kw):
        if any(x in cmd[-1] for x in ("install.sh", "tmux ", "apt-get")):
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kw)

    monkeypatch.setattr(agent.subprocess, "run", intercept)
    agent.main(["restore", "--boot", str(boot_file), "--bundle-dir", str(bundle_dir), "--user", "",
                "--state", str(tmp_path / vm / "state.json"), "--manual"])
    monkeypatch.setattr(agent.subprocess, "run", real_run)
    return vm_home, json.loads((tmp_path / vm / "state.json").read_text())


def run_headless_turn(tmp_path: Path, vm_home: Path, state: dict, reply: str, *, limit_resets_at: int | None = None) -> list[str]:
    """Runs the launcher the agent wrote against a stand-in claude that appends a turn to the session's transcript
    and answers like `claude -p --output-format stream-json --verbose` (message lines as claude 2.1.296 writes them,
    the result last). limit_resets_at: claude stops on the usage limit instead. Returns the argv claude got; the
    environment it got is in tmp_path/env.json."""
    transcript = Path(state["claude_home"]) / "projects" / state["project_dir"] / f"{state['session_id']}.jsonl"
    sid = state["session_id"]
    if limit_resets_at is None:
        lines = [{"type": "system", "subtype": "init", "session_id": sid},
                 {"type": "rate_limit_event", "session_id": sid,
                  "rate_limit_info": {"status": "allowed", "resetsAt": 1, "rateLimitType": "five_hour"}},
                 {"type": "result", "subtype": "success", "session_id": sid, "result": reply, "is_error": False,
                  "num_turns": 2, "total_cost_usd": 0.31, "permission_denials": []}]
    else:
        lines = [{"type": "system", "subtype": "init", "session_id": sid},
                 {"type": "rate_limit_event", "session_id": sid, "rate_limit_info": {
                     "status": "rejected", "resetsAt": limit_resets_at, "rateLimitType": "five_hour"}},
                 {"type": "result", "subtype": "success", "session_id": sid, "result": reply, "is_error": True,
                  "num_turns": 1, "total_cost_usd": 0}]
    stub = vm_home / ".local" / "bin" / "claude"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(f"#!{sys.executable}\nimport json, pathlib, sys\n"
                    f"t = pathlib.Path({str(transcript)!r}); t.parent.mkdir(parents=True, exist_ok=True)\n"
                    f"with t.open('a') as f: f.write(json.dumps({{'type': 'user', 'sessionId': {sid!r}, "
                    "'message': {'content': sys.argv[2]}}) + '\\n')\n"
                    f"pathlib.Path({str(tmp_path / 'argv.json')!r}).write_text(json.dumps(sys.argv[1:]))\n"
                    f"import os; pathlib.Path({str(tmp_path / 'env.json')!r}).write_text(json.dumps(dict(os.environ)))\n"
                    f"for line in {lines!r}: print(json.dumps(line))\n"
                    f"sys.exit({1 if limit_resets_at else 0})\n")
    stub.chmod(0o755)
    run_launcher(tmp_path, vm_home)
    return json.loads((tmp_path / "argv.json").read_text())


def run_launcher(tmp_path: Path, vm_home: Path) -> None:
    """Runs the launcher the agent wrote, as tmux would on the VM."""
    shim = tmp_path / "coreutils" / "timeout"  # Ubuntu's coreutils timeout, which macOS lacks: drop the limits, run
    if not shim.exists():
        shim.parent.mkdir()
        shim.write_text('#!/bin/bash\nwhile [[ $1 == -* ]]; do shift; done\nshift\nexec "$@"\n')
        shim.chmod(0o755)
    subprocess.run(["bash", str(vm_home / ".ctransfer" / "launch.sh")],
                   env={"HOME": str(vm_home), "PATH": f"{shim.parent}:/usr/bin:/bin"}, check=True)


def export_from_vm(tmp_path: Path, state: dict, name: str) -> tuple[Path, dict]:
    """The agent's export once the control plane asks for it. Returns (the uploaded bundle, what it reported)."""
    reported, uploaded = {}, tmp_path / f"{name}.tgz"
    vm_agent = agent.Agent(state, poster=lambda route, body: reported.update({route: body}) or {},
                           uploader=lambda form, path: shutil.copy(path, uploaded), stopper=lambda: None)
    assert vm_agent.status() == "done"
    vm_agent.export({}, "export-key")
    return uploaded, reported["exported"]


def test_a_task_clones_its_repo_at_the_ref_and_a_follow_up_resumes_the_same_session(tmp_path, monkeypatch):
    """A task's first step has no bundle: the VM clones the repo at the ref and starts a new session under the
    given id. A follow-up boots from that step's export and resumes the same session (no fork), with the workspace
    as the step left it, uncommitted changes included."""
    src = make_repo(tmp_path / "src" / "repo")
    run_git(src, "checkout", "-q", "-b", "feature")
    (src / "feature.txt").write_text("on the feature branch\n")
    run_git(src, "add", "feature.txt")
    run_git(src, "commit", "-q", "-m", "feature")
    upstream = tmp_path / "github" / "example" / "repo.git"  # stands in for https://github.com/example/repo.git
    run_git(tmp_path, "clone", "-q", "--bare", str(src), str(upstream))
    path = str(tmp_path / "vm-fs" / "repo")  # where the control plane puts the clone
    identity = {"name": "alice", "email": "1+alice@users.noreply.github.com"}
    first = {"prompt": "fix the bug", "note": "task note", "timeout_sec": 600, "permission_mode": "acceptEdits",
             "identity": identity, "repo": {"name": "example/repo", "url": str(upstream), "ref": "feature", "path": path}}
    assert "task" in agent.FEATURES

    vm_home, state = restore_on_vm(tmp_path, monkeypatch, "vm1", task_boot(first), tmp_path / "vm1" / "bundle")
    assert state["session_id"] == TASK_SID and state["cwd"] == path
    assert state["workspace_update"] == "cloned example/repo at feature"
    assert (Path(path) / "feature.txt").exists() and run_git(path, "branch", "--show-current") == "feature"
    assert run_git(path, "config", "user.email") == identity["email"]
    argv = run_headless_turn(tmp_path, vm_home, state, "fixed it")
    assert argv[:4] == ["-p", "task note\n\nThe task:\nfix the bug", "--session-id", TASK_SID]
    assert "--resume" not in argv and "--fork-session" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    (Path(path) / "app.py").write_text("print('fixed, not committed')\n")  # what claude left on the VM

    uploaded, report = export_from_vm(tmp_path, state, "step1")
    assert report["result"]["text"] == "fixed it" and report["result"]["session_id"] == TASK_SID
    assert report["result"]["total_cost_usd"] == 0.31 and "usage_limit" not in report["result"]
    assert argv[argv.index("--output-format") + 1:][:2] == ["stream-json", "--verbose"]
    assert report["result"]["workspace_update"] == "cloned example/repo at feature"

    shutil.rmtree(path)  # the follow-up runs on another VM
    bundle.extract(uploaded, tmp_path / "vm2" / "bundle")
    follow_up = {"prompt": "address the review", "note": "follow-up note", "timeout_sec": 600,
                 "permission_mode": "acceptEdits", "continue": True}
    vm_home, state = restore_on_vm(tmp_path, monkeypatch, "vm2", task_boot(follow_up), tmp_path / "vm2" / "bundle")
    assert state["session_id"] == TASK_SID and state["workspace_update"] == "kept as the last step left it"
    assert (Path(path) / "app.py").read_text() == "print('fixed, not committed')\n"
    assert run_git(path, "branch", "--show-current") == "feature" and run_git(path, "config", "user.name") == "alice"
    transcript = Path(state["claude_home"]) / "projects" / state["project_dir"] / f"{TASK_SID}.jsonl"
    assert "fix the bug" in transcript.read_text()  # the first turn came along
    argv = run_headless_turn(tmp_path, vm_home, state, "addressed")
    assert argv[:4] == ["-p", "follow-up note\n\nThe task:\naddress the review", "--resume", TASK_SID]
    assert "--fork-session" not in argv and "--session-id" not in argv
    _, report = export_from_vm(tmp_path, state, "step2")
    assert report["result"]["session_id"] == TASK_SID
    turns = [json.loads(line)["message"]["content"] for line in transcript.read_text().splitlines()]
    assert ["fix the bug" in t for t in turns] == [True, False] and "address the review" in turns[1]


def test_a_task_whose_repo_cannot_be_cloned_ends_at_once_saying_why(tmp_path, monkeypatch):
    path = str(tmp_path / "vm-fs" / "repo")
    run = {"prompt": "x", "note": "n", "timeout_sec": 600, "permission_mode": "default",
           "repo": {"name": "example/private", "url": str(tmp_path / "missing.git"), "ref": "main", "path": path}}
    vm_home, state = restore_on_vm(tmp_path, monkeypatch, "vm", task_boot(run), tmp_path / "vm" / "bundle")
    assert not (vm_home / ".ctransfer" / "launch.sh").exists()  # claude never starts
    _, report = export_from_vm(tmp_path, state, "step1")
    result = report["result"]
    assert result["exit_code"] == 1 and result["error"].startswith("couldn't clone example/private at main: ")
    assert "ctransfer secret set GH_TOKEN" in result["error"]  # no GitHub token stored: say how to add one


def repo_with_setup(root: Path, script: str) -> Path:
    src = make_repo(root)
    (src / ".ctransfer").mkdir()
    (src / ".ctransfer" / "setup.sh").write_text(script)
    run_git(src, "add", "-A")
    run_git(src, "commit", "-q", "-m", "setup script")
    return src


def test_a_repos_setup_script_runs_before_claude_with_the_secrets_and_hands_claude_its_environment(tmp_path, monkeypatch):
    """The repo's .ctransfer/setup.sh installs what it needs before claude starts: it runs in the repo root with the
    user's secrets, and what it appends to $CTRANSFER_SETUP_ENV (here a tool directory on PATH) reaches claude."""
    src = repo_with_setup(tmp_path / "src" / "repo", (
        'echo "installing tools, API key of ${#MY_API_KEY} chars"\n'
        'pwd > "$HOME/setup-cwd"\n'
        'mkdir -p "$HOME/tools/bin"\n'
        'echo \'PATH="$HOME/tools/bin:$PATH"\' >> "$CTRANSFER_SETUP_ENV"\n'
        'echo REPO_TOOLS=ready >> "$CTRANSFER_SETUP_ENV"\n'))
    path = str(tmp_path / "vm-fs" / "repo")
    run = {"prompt": "run the tests", "note": "n", "timeout_sec": 600, "permission_mode": "default",
           "repo": {"name": "example/repo", "url": str(src), "ref": "", "path": path}}
    boot = {**task_boot(run), "secrets": {"MY_API_KEY": "k-123"}}
    vm_home, state = restore_on_vm(tmp_path, monkeypatch, "vm", boot, tmp_path / "vm" / "bundle")
    run_headless_turn(tmp_path, vm_home, state, "tests pass")
    assert (vm_home / "setup-cwd").read_text().strip() == path
    env = json.loads((tmp_path / "env.json").read_text())
    assert env["PATH"].startswith(f"{vm_home}/tools/bin:") and env["REPO_TOOLS"] == "ready"
    assert env["MY_API_KEY"] == "k-123"
    _, report = export_from_vm(tmp_path, state, "step1")
    result = report["result"]
    assert result["exit_code"] == 0 and result["text"] == "tests pass" and "error" not in result
    assert result["setup"]["exit_code"] == 0 and result["setup"]["seconds"] >= 0
    assert result["setup"]["output"] == "installing tools, API key of 5 chars\n"


def test_a_failing_setup_script_ends_the_run_before_claude_starts_and_reports_its_output(tmp_path, monkeypatch):
    src = repo_with_setup(tmp_path / "src" / "repo", 'for i in $(seq 1 5000); do echo "step $i"; done\n'
                                                     'echo "conda: no space left on device" >&2\nexit 3\n')
    path = str(tmp_path / "vm-fs" / "repo")
    run = {"prompt": "run the tests", "note": "n", "timeout_sec": 600, "permission_mode": "default",
           "repo": {"name": "example/repo", "url": str(src), "ref": "", "path": path}}
    vm_home, state = restore_on_vm(tmp_path, monkeypatch, "vm", task_boot(run), tmp_path / "vm" / "bundle")
    stub = vm_home / ".local" / "bin" / "claude"
    stub.parent.mkdir(parents=True)
    stub.write_text(f"#!/bin/bash\ntouch {tmp_path / 'claude-ran'}\n")
    stub.chmod(0o755)
    run_launcher(tmp_path, vm_home)
    assert not (tmp_path / "claude-ran").exists()
    _, report = export_from_vm(tmp_path, state, "step1")
    result = report["result"]
    assert result["exit_code"] == 1
    assert result["error"].startswith("the repo's setup script .ctransfer/setup.sh failed with exit code 3 after ")
    assert result["error"].endswith(" s, so Claude didn't start")
    assert result["setup"]["exit_code"] == 3
    output = result["setup"]["output"]
    assert len(output) == agent.SETUP_OUTPUT_MAX and output.endswith("step 5000\nconda: no space left on device\n")
    # What `ctransfer task show` prints of it: the reason, then the end of the script's output.
    shown = cli._task_text({"task_id": "task1", "repo": "example/repo", "status": "error", "session_id": TASK_SID,
                            "permission_mode": "default", "steps": [{"step": 1, "status": "error", "prompt": "x",
                                                                     "result": result}]})
    assert f"  ({result['error']})\n  setup output (last lines):\n" in shown
    assert shown.endswith("    step 4999\n    step 5000\n    conda: no space left on device")


def test_a_run_that_stops_on_the_usage_limit_reports_when_the_limit_resets(tmp_path, monkeypatch):
    src = make_repo(tmp_path / "src" / "repo")
    path = str(tmp_path / "vm-fs" / "repo")
    run = {"prompt": "x", "note": "n", "timeout_sec": 600, "permission_mode": "default",
           "repo": {"name": "example/repo", "url": str(src), "ref": "", "path": path}}
    vm_home, state = restore_on_vm(tmp_path, monkeypatch, "vm", task_boot(run), tmp_path / "vm" / "bundle")
    assert state["workspace_update"] == "cloned example/repo"  # no ref: the default branch
    run_headless_turn(tmp_path, vm_home, state, "You've hit your limit", limit_resets_at=1_800_018_000)
    _, report = export_from_vm(tmp_path, state, "step1")
    assert report["result"]["usage_limit"] == {"resets_at": 1_800_018_000, "type": "five_hour"}
    assert report["result"]["is_error"] and report["result"]["text"] == "You've hit your limit"


def test_an_export_waiting_on_a_long_turn_keeps_heartbeating_and_uploads_with_the_latest_form(laptop, tmp_path):
    """Asked to export mid-turn, the agent waits for the turn to end; a turn can outlast the control plane's patience
    for a silent VM, so the wait must keep reporting the VM busy, not go quiet."""
    _, _, state = push_and_restore(laptop, tmp_path)
    waited = []  # seconds the agent slept while the turn ran
    turn_sec = 20 * 60
    posted, uploads = [], []

    def control_plane(route, body):  # the control plane's answers: keep exporting, with a fresh form each beat
        posted.append((route, body))
        return {"export": True, "upload": {"form": len(posted)}, "export_key": "export-key"}

    vm_agent = agent.Agent(state, poster=control_plane, uploader=lambda form, path: uploads.append(form),
                           status_fn=lambda: "busy" if sum(waited) < turn_sec else "idle",
                           stopper=lambda: None, sleep=waited.append)
    vm_agent.export({"form": 0}, "export-key")

    beats = [body["status"] for route, body in posted if route == "heartbeat"]
    assert beats == ["busy"] * (turn_sec // agent.HEARTBEAT_SEC - 1)  # one a minute while busy, none once idle
    assert [route for route, _ in posted][-1] == "exported"
    assert uploads == [{"form": len(beats)}]  # the last beat's form, not the one from before the wait
