"""VM-side agent.

`restore` (run once by cloud-init as root): unpack a pushed bundle into the login user's
~/.claude and the original cwd path, install the pinned Claude Code version, start
`claude --resume <id>` (or, for a `new` bundle, a fresh `claude --session-id <id> [prompt]`)
in tmux, and install a systemd unit for `run`.

`run` (systemd): heartbeat the control plane with the session's busy/idle status; when an
export is requested, wait for a turn boundary, stop claude, bundle the transcript + workspace
and upload it.

Scheduled runs (boot["run"] set, see `ctransfer cron`) start no interactive session: one headless
`claude -p` turn on a fork of the pushed session, after which the agent reports "done" and the
control plane asks for the export, which carries the fork's transcript and the run's result.

Tasks (boot["run"]["repo"] or ["continue"] set) are headless runs too, with no laptop bundle: a task's first step
clones a GitHub repo at a ref and starts a fresh session under the control plane's session id; each follow-up boots
from the previous step's export and resumes that same session (no fork), so its transcript and workspace, uncommitted
changes included, carry on from step to step.

Standard library only: this file ships inside every bundle and runs on a stock Ubuntu image.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import bundle, mcp, net, sessions, workspace

STATE_PATH = Path("/var/lib/ctransfer/state.json")
AGENT_DIR = Path("/opt/ctransfer/agent")
TMUX_SESSION = "claude"
# In tmux's status bar for whoever attaches with `ctransfer ssh`, most of whom have never used tmux.
DETACH_HINT = " Ctrl-b then d: detach, claude keeps running "
HEARTBEAT_SEC = 60
EXPORT_WAIT_SEC = 1800
RESULT_TEXT_MAX = 8000  # chars of the run's final message reported to the control plane
# What this agent does beyond pushes, new sessions and scheduled runs. The control plane ships this package to task
# VMs itself and reads FEATURES to tell whether it can.
FEATURES = ("task",)
CLONE_TIMEOUT_SEC = 900

UNIT = """[Unit]
Description=ctransfer session agent
After=network-online.target
Wants=network-online.target

[Service]
Environment=PYTHONPATH={agent_dir}
ExecStart=/usr/bin/python3 -m ctransfer.agent run --state {state}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


def _merge_tree(src: Path, dst: Path) -> None:
    for dirpath, _, filenames in os.walk(src):
        for name in filenames:
            f = Path(dirpath) / name
            target = dst / f.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)


def _as_user(user: str | None, cmd: str) -> list[str]:
    return ["sudo", "-u", user, "-H", "bash", "-lc", cmd] if user else ["bash", "-lc", cmd]


def _first_missing_ancestor(path: Path) -> Path | None:
    missing = None
    for p in [path, *path.parents]:
        if p.exists():
            break
        missing = p
    return missing


def _write_global_config(path: Path, trusted: list[str], carried_mcp: dict, root_prefix: str = "") -> None:
    cfg = json.loads(path.read_text()) if path.exists() else {}
    cfg["hasCompletedOnboarding"] = True
    mcp.install(cfg, carried_mcp, root_prefix)
    projects = cfg.setdefault("projects", {})
    for p in trusted:
        projects.setdefault(p, {})["hasTrustDialogAccepted"] = True
    path.write_text(json.dumps(cfg, indent=2))


def restore(boot: dict, bundle_dir: Path, *, home: Path, user: str | None = None, root_prefix: str = "",
            launch: bool = True) -> dict:
    """Unpack the bundle and (with launch) start claude. Returns the agent state to persist.

    root_prefix relocates the workspace (tests use it so the 'VM' doesn't collide with the laptop path).
    """
    if (boot.get("run") or {}).get("repo"):
        return _restore_task_start(boot, home=home, user=user, root_prefix=root_prefix, launch=launch)
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    sid, cwd = manifest["session_id"], manifest["cwd"]
    ws = manifest["workspace"]
    claude_dir = home / ".claude"

    _merge_tree(bundle_dir / "claude", claude_dir)
    vm_root = Path(root_prefix + ws["root"])
    vm_cwd = root_prefix + cwd
    created_top = _first_missing_ancestor(vm_root)
    workspace.restore_fresh(ws, bundle_dir / "workspace", vm_root)
    carried_mcp = json.loads((bundle_dir / "mcp.json").read_text()) if (bundle_dir / "mcp.json").exists() else {}
    _write_global_config(home / ".claude.json", sorted({vm_cwd, str(vm_root)}), carried_mcp, root_prefix)

    ssh_dir = None
    if boot.get("ssh_pubkey"):
        # The pusher's per-transfer key: what `ctransfer ssh` logs in with.
        ssh_dir = home / ".ssh"
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        with open(ssh_dir / "authorized_keys", "a") as f:
            f.write(boot["ssh_pubkey"].strip() + "\n")
        (ssh_dir / "authorized_keys").chmod(0o600)

    if user:
        for p in filter(None, [claude_dir, home / ".claude.json", created_top or vm_root, ssh_dir]):
            subprocess.run(["chown", "-R", f"{user}:{user}", str(p)], check=True)

    state = {
        "transfer_id": boot["transfer_id"], "session_id": sid, "agent_url": boot["agent_url"],
        "agent_token": boot["agent_token"], "cwd": cwd, "vm_cwd": vm_cwd, "root": ws["root"],
        "vm_root": str(vm_root), "project_dir": manifest["project_dir"], "claude_home": str(claude_dir),
        "user": user, "restored_at": time.time(), "new": manifest["kind"] == "new",
        "run": boot.get("run"),  # a scheduled run's task, else None
    }
    secrets = boot.get("secrets") or {}
    github = _setup_github(home, user, secrets)
    if launch:
        _write_env(home, user, boot["claude_token"], secrets)
        _install_claude(home, user, boot.get("claude_version"))
        if github:
            _install_gh()
        if boot.get("run"):
            # A task's follow-up carries on from exactly where the last step left the workspace.
            state["workspace_update"] = ("kept as the last step left it" if boot["run"].get("continue")
                                         else _update_workspace(state, ws))
            _launch_run(state, home, boot["run"])
        else:
            _launch(state, home, bool(boot.get("remote_control")), manifest.get("prompt"))
    return state


def _restore_task_start(boot: dict, *, home: Path, user: str | None, root_prefix: str, launch: bool) -> dict:
    """A task's first step, which has no bundle: clone the repo at the ref into run["repo"]["path"], then (with launch)
    one headless turn of a new session under the control plane's session id. A clone that fails is reported as the
    run's result, so the step ends at once instead of the VM timing out."""
    run, claude_dir = boot["run"], home / ".claude"
    repo = run["repo"]
    path = repo["path"]
    vm_root = Path(root_prefix + path)
    claude_dir.mkdir(exist_ok=True)
    state = {
        "transfer_id": boot["transfer_id"], "session_id": boot["session_id"], "agent_url": boot["agent_url"],
        "agent_token": boot["agent_token"], "cwd": path, "vm_cwd": str(vm_root), "root": path, "vm_root": str(vm_root),
        "project_dir": sessions.encode_project_dir(path), "claude_home": str(claude_dir), "user": user,
        "restored_at": time.time(), "new": True, "run": run,
    }
    secrets = boot.get("secrets") or {}
    _write_env(home, user, boot["claude_token"], secrets)  # git's credential helper reads the GitHub token from it
    github = _setup_github(home, user, secrets)
    created_top = _first_missing_ancestor(vm_root.parent)
    vm_root.parent.mkdir(parents=True, exist_ok=True)
    if user:
        subprocess.run(["chown", "-R", f"{user}:{user}", str(created_top or vm_root.parent)], check=True)
    problem = _clone(state, repo, github)
    if problem is None:
        for key, value in (run.get("identity") or {}).items():  # commits made on the VM carry the user's name
            subprocess.run(["git", "-C", str(vm_root), "config", f"user.{key}", value], check=True)
    else:
        vm_root.mkdir(parents=True, exist_ok=True)  # the export still needs a workspace to capture
    state["workspace_update"] = f"cloned {repo['name']}" + (f" at {repo['ref']}" if repo.get("ref") else "")
    _write_global_config(home / ".claude.json", [str(vm_root)], {}, root_prefix)
    if user:
        for p in (claude_dir, home / ".claude.json", vm_root):
            subprocess.run(["chown", "-R", f"{user}:{user}", str(p)], check=True)
    if problem is not None:
        _fail_run(home, user, problem)
    elif launch:
        _install_claude(home, user, boot.get("claude_version"))
        if github:
            _install_gh()
        _launch_run(state, home, run)
    return state


def _clone(state: dict, repo: dict, github: bool) -> str | None:
    """Clones repo["url"] into the state's vm_root and checks out repo["ref"] (a branch, tag or commit; none: the
    default branch). Returns why it failed, or None."""
    root = shlex.quote(state["vm_root"])
    cmd = f"GIT_TERMINAL_PROMPT=0 git clone --quiet {shlex.quote(repo['url'])} {root}"
    if repo.get("ref"):
        cmd += f" && git -C {root} checkout --quiet {shlex.quote(repo['ref'])}"
    try:
        out = subprocess.run(_as_user(state["user"], cmd), capture_output=True, text=True, timeout=CLONE_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return f"couldn't clone {repo['name']}: it took more than {CLONE_TIMEOUT_SEC // 60} minutes"
    if out.returncode == 0:
        return None
    reason = (out.stderr.strip().splitlines() or ["git failed"])[-1][:300]
    hint = "" if github else (" If the repository is private, store a GitHub token for your cloud sessions first "
                              "(`ctransfer secret set GH_TOKEN`).")
    return f"couldn't clone {repo['name']}" + (f" at {repo['ref']}" if repo.get("ref") else "") + f": {reason}.{hint}"


def _fail_run(home: Path, user: str | None, error: str) -> None:
    """Ends a run before claude starts: the error and exit code are its result, so the agent reports done."""
    out = run_dir(home)
    out.mkdir(parents=True, exist_ok=True)
    (out / "error").write_text(error)
    (out / "exit_code").write_text("1\n")
    if user:
        subprocess.run(["chown", "-R", f"{user}:{user}", str(home / ".ctransfer")], check=True)


def _write_env(home: Path, user: str | None, claude_token: str, secrets: dict[str, str]) -> None:
    """~/.ctransfer/env (0600): the user's secrets and the Claude token, as the launcher and git's credential helper
    source them. The control plane refuses secrets named like the token, so the token line is the only one."""
    ctl = home / ".ctransfer"
    ctl.mkdir(mode=0o700, exist_ok=True)
    env_file = ctl / "env"
    lines = [f"{name}={shlex.quote(value)}" for name, value in sorted(secrets.items())]
    lines.append(f"CLAUDE_CODE_OAUTH_TOKEN={shlex.quote(claude_token)}")
    env_file.write_text("\n".join(lines) + "\n")
    env_file.chmod(0o600)
    if user:
        subprocess.run(["chown", "-R", f"{user}:{user}", str(ctl)], check=True)


# The secret names gh reads a token from, in its order of preference; git's credential helper follows the same order.
GITHUB_TOKEN_NAMES = ("GH_TOKEN", "GITHUB_TOKEN")
GIT_CREDENTIAL_HELPER = """#!/bin/bash
# git credential helper written by ctransfer: answers for https://github.com with the GitHub token in ~/.ctransfer/env.
[ "$1" = get ] || exit 0
set -a; . {env}; set +a
echo username=x-access-token
echo "password=${{GH_TOKEN:-$GITHUB_TOKEN}}"
"""


def _setup_github(home: Path, user: str | None, secrets: dict[str, str]) -> bool:
    """With a GitHub token among the secrets, point git at it for github.com: a credential helper that reads the token
    from ~/.ctransfer/env, and SSH remote URLs rewritten to HTTPS so pushes go through the helper (the VM has no SSH
    key for GitHub). gh reads the same token from the environment claude runs in. Returns whether it was set up."""
    if not any(secrets.get(name) for name in GITHUB_TOKEN_NAMES):
        return False
    ctl = home / ".ctransfer"
    ctl.mkdir(mode=0o700, exist_ok=True)
    helper = ctl / "git-credential-github"
    helper.write_text(GIT_CREDENTIAL_HELPER.format(env=shlex.quote(str(ctl / "env"))))
    helper.chmod(0o700)
    gitconfig = home / ".gitconfig"
    subprocess.run(["git", "config", "--file", str(gitconfig), "credential.https://github.com.helper", str(helper)],
                   check=True)
    for ssh_form in ("git@github.com:", "ssh://git@github.com/"):
        subprocess.run(["git", "config", "--file", str(gitconfig), "--add", "url.https://github.com/.insteadOf", ssh_form],
                       check=True)
    if user:
        subprocess.run(["chown", "-R", f"{user}:{user}", str(ctl), str(gitconfig)], check=True)
    return True


def _install_gh() -> None:
    """The GitHub CLI, for opening and answering pull requests. Best effort: a session without it still runs."""
    # Skipped when already there, as on the control plane's VM image.
    cmd = "command -v gh >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -q gh"
    out = subprocess.run(_as_user(None, cmd), capture_output=True, text=True)
    if out.returncode:
        print(f"ctransfer agent: installing gh failed: {out.stderr.strip()[-500:]}", file=sys.stderr, flush=True)


def installed_claude_version(home: Path, user: str | None) -> str | None:
    """The version of the Claude CLI already in ~/.local/bin (the control plane's VM image has one), if any."""
    binary = home / ".local" / "bin" / "claude"
    if not binary.exists():
        return None
    out = subprocess.run(_as_user(user, f"{shlex.quote(str(binary))} --version"), capture_output=True, text=True)
    words = out.stdout.split()  # "2.1.296 (Claude Code)"
    return words[0] if out.returncode == 0 and words else None


def _install_claude(home: Path, user: str | None, version: str | None) -> None:
    """The session's Claude Code version; any installed one will do when the session's is unknown."""
    wanted = version if version and version != "latest" else ""
    installed = installed_claude_version(home, user)
    if installed and (installed == wanted or not wanted):
        return
    arg = wanted
    subprocess.run(_as_user(user, f"curl -fsSL https://claude.ai/install.sh | bash -s {arg}"), check=True)


def arrival_note(transfer_id: str) -> str:
    """Appended to the VM claude's system prompt: the transcript ends mid-handoff (often with the /ctransfer
    request itself), so tell the model the move already happened."""
    return (f"This session was moved from the user's laptop to a cloud VM by ctransfer (transfer {transfer_id}). "
            "The transfer is complete: you are now running on the VM, in a copy of the same project at the same path. "
            "Any earlier request in this conversation to transfer, push or /ctransfer the session has already been "
            "carried out; do not repeat it. To bring the session back, the user runs "
            f"`ctransfer pull {transfer_id}` on their laptop.")


def new_session_note(transfer_id: str) -> str:
    """Appended to the system prompt of a session started on the VM (`ctransfer new`)."""
    return (f"This session was started on a cloud VM by ctransfer (transfer {transfer_id}), in a copy of the user's "
            "project at the same path as on their laptop. Work done here stays on the VM until the user brings the "
            f"session back by running `ctransfer pull {transfer_id}` on their laptop.")


def transcript_path(state: dict) -> Path:
    return Path(state["claude_home"]) / "projects" / state["project_dir"] / f"{state['session_id']}.jsonl"


def claude_args(state: dict, *, remote_control: bool = False, prompt: str | None = None) -> list[str]:
    """How the VM starts claude: resume the pushed session on the model and effort it last ran with, or start the new
    one under its pre-chosen id."""
    if state["new"]:
        args = ["--session-id", state["session_id"], "--append-system-prompt", new_session_note(state["transfer_id"])]
    else:
        args = ["--resume", state["session_id"], *sessions.model_args(transcript_path(state)),
                "--append-system-prompt", arrival_note(state["transfer_id"])]
    if remote_control:
        args.append("--remote-control")
    return args + [prompt] if prompt else args


def _write_launcher(state: dict, home: Path, body: str) -> Path:
    """~/.ctransfer/launch.sh: exports ~/.ctransfer/env (the Claude token and the user's secrets, which MCP server
    configs can reference as ${NAME}), cds to the session's cwd, runs body."""
    launcher = home / ".ctransfer" / "launch.sh"
    launcher.write_text("#!/bin/bash\nset -a; . \"$HOME/.ctransfer/env\"; set +a\n"
                        f"cd {json.dumps(state['vm_cwd'])}\n{body}\n")
    launcher.chmod(0o700)
    return launcher


def _start_in_tmux(state: dict, home: Path, launcher: Path, *, status_hint: str | None = None) -> None:
    if state["user"]:
        subprocess.run(["chown", "-R", f"{state['user']}:{state['user']}", str(home / ".ctransfer")], check=True)
    cmd = f"tmux new-session -d -s {TMUX_SESSION} -x 220 -y 50 {launcher}"
    if status_hint:
        cmd += (f" \\; set-option -t {TMUX_SESSION} status-right {shlex.quote(status_hint)}"
                f" \\; set-option -t {TMUX_SESSION} status-right-length {len(status_hint)}")
    subprocess.run(_as_user(state["user"], cmd), check=True)


def _launch(state: dict, home: Path, remote_control: bool, prompt: str | None = None) -> None:
    args = claude_args(state, remote_control=remote_control, prompt=prompt)
    launcher = _write_launcher(state, home, f"exec \"$HOME/.local/bin/claude\" {shlex.join(args)}")
    _start_in_tmux(state, home, launcher, status_hint=DETACH_HINT)


def run_dir(home: Path) -> Path:
    """Where a scheduled run's claude writes its JSON result, stderr and exit code."""
    return home / ".ctransfer" / "run"


def run_prompt(run: dict) -> str:
    """The run's message to claude: the control plane's note (what this run is, summaries of earlier runs), then the
    task. The note goes in the message, not --append-system-prompt: on a resumed, forked session (claude 2.1.296)
    the run's claude had the note in its argv yet reported no previous-run summaries in its system prompt."""
    return f"{run['note']}\n\nThe task:\n{run['prompt']}"


def _launch_run(state: dict, home: Path, run: dict) -> None:
    """One headless turn. A scheduled run's is on a fork of the pushed session: --fork-session writes the turn to a
    new transcript, so the pushed one (every run's starting point) never grows. A task's first step starts its session
    under the given id; a follow-up resumes that same session, so the task's turns stay one conversation. A coreutils
    timeout bounds the run."""
    out = run_dir(home)
    out.mkdir(parents=True, exist_ok=True)
    if run.get("repo"):
        session = ["--session-id", state["session_id"]]
    else:
        session = ["--resume", state["session_id"], *([] if run.get("continue") else ["--fork-session"]),
                   *sessions.model_args(transcript_path(state))]
    claude = " ".join([
        f"timeout --kill-after=30 {int(run['timeout_sec'])}", "\"$HOME/.local/bin/claude\"",
        "-p", shlex.quote(run_prompt(run)), shlex.join(session),
        "--output-format", "stream-json", "--verbose", "--permission-mode", shlex.quote(run.get("permission_mode") or "default")])
    launcher = _write_launcher(state, home,
                               f"{claude} > \"$HOME/.ctransfer/run/result.json\" 2> \"$HOME/.ctransfer/run/stderr.log\"\n"
                               "echo $? > \"$HOME/.ctransfer/run/exit_code\"")
    _start_in_tmux(state, home, launcher)


def _update_workspace(state: dict, ws: dict) -> str:
    """Fast-forward the pushed branch from its remote (origin, else the first one), so a run sees what landed since
    the push. Best effort: without a GitHub token secret the VM has no git credentials, so private remotes stay at the
    pushed snapshot."""
    remotes = sorted(ws.get("remotes") or {}, key=lambda name: name != "origin")
    if ws.get("kind") != "git" or not ws.get("branch") or not remotes:
        return "not updated: no branch with a remote"
    cmd = (f"GIT_TERMINAL_PROMPT=0 git -C {shlex.quote(state['vm_root'])} pull --ff-only --quiet "
           f"{shlex.quote(remotes[0])} {shlex.quote(ws['branch'])}")
    try:
        out = subprocess.run(_as_user(state["user"], cmd), capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "not updated: git pull timed out"
    if out.returncode:
        return "not updated: " + (out.stderr.strip().splitlines() or ["git pull failed"])[-1][:200]
    return f"fast-forwarded from {remotes[0]}/{ws['branch']}"


def read_output(path: Path) -> tuple[dict, dict | None]:
    """claude -p's output: (its result message, the last rate-limit info it reported, if any). The run writes
    stream-json, one message per line, the result last; a single JSON object (--output-format json, from agents before
    stream-json) is read as the result."""
    reply, limit = {}, None
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return reply, limit
    for line in lines:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("type") == "rate_limit_event":
            limit = message.get("rate_limit_info") or None
        elif message.get("type") in ("result", None):
            reply = message
    return reply, limit


def run_result(home: Path) -> dict:
    """What a finished run reports: claude's result (trimmed), or why there is none. usage_limit is set when claude
    stopped on the account's usage limit: a rate-limit event with status rejected, and no successful result."""
    out = run_dir(home)
    code = (out / "exit_code").read_text().strip() if (out / "exit_code").exists() else ""
    result: dict = {"exit_code": int(code) if code.lstrip("-").isdigit() else None}
    reply, limit = read_output(out / "result.json")
    for key in ("session_id", "is_error", "subtype", "num_turns", "total_cost_usd", "duration_ms"):
        if key in reply:
            result[key] = reply[key]
    result["text"] = str(reply.get("result") or "")[:RESULT_TEXT_MAX]
    result["permission_denials"] = len(reply.get("permission_denials") or [])
    if (out / "error").exists():  # the run ended before claude started, e.g. its repo couldn't be cloned
        result["error"] = (out / "error").read_text()[:1000]
    if not reply or reply.get("is_error"):
        try:
            result["stderr"] = (out / "stderr.log").read_text()[-1000:]
        except OSError:
            pass
        if result["exit_code"] == 124:
            result["error"] = "timed out"
        if limit and limit.get("status") == "rejected":
            result["usage_limit"] = {"resets_at": limit.get("resetsAt"), "type": limit.get("rateLimitType")}
    return result


def install_service(state_path: Path) -> None:
    Path("/etc/systemd/system/ctransfer-agent.service").write_text(UNIT.format(agent_dir=AGENT_DIR, state=state_path))
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "ctransfer-agent.service"], check=True)


class Agent:
    def __init__(self, state: dict, *, poster=None, uploader=None, status_fn=None, stopper=None, sleep=time.sleep):
        self.state = state
        self.post = poster or self._post
        self.upload = uploader or net.post_file
        self.status = status_fn or self._claude_status
        self.stop_claude = stopper or self._stop_claude
        self.sleep = sleep

    def _post(self, route: str, body: dict) -> dict:
        url = f"{self.state['agent_url'].rstrip('/')}/agent/{self.state['transfer_id']}/{route}"
        return net.request_json(url, body=body, headers={"authorization": f"Bearer {self.state['agent_token']}"})

    def _claude_status(self) -> str:
        if self.state.get("run"):
            return self._run_status()
        home = Path(self.state["claude_home"])
        for s in sessions.live_sessions(home):
            if s.get("sessionId") == self.state["session_id"] or s.get("cwd") == self.state["vm_cwd"]:
                return s.get("status") or "idle"
        return "stopped"

    def _run_status(self) -> str:
        """busy while the headless run is going, then done (also when it died without writing an exit code)."""
        if (run_dir(Path(self.state["claude_home"]).parent) / "exit_code").exists():
            return "done"
        alive = subprocess.run(_as_user(self.state["user"], f"tmux has-session -t {TMUX_SESSION}"),
                               capture_output=True).returncode == 0
        return "busy" if alive else "done"

    def _stop_claude(self) -> None:
        subprocess.run(_as_user(self.state["user"], f"tmux kill-session -t {TMUX_SESSION}"), check=False)

    def tick(self) -> dict:
        resp = self.post("heartbeat", {"status": self.status()})
        if resp.get("export"):
            self.export(resp["upload"], resp["export_key"])
        return resp

    def export(self, upload: dict, export_key: str) -> dict:
        deadline = time.time() + EXPORT_WAIT_SEC
        while self.status() == "busy" and time.time() < deadline:
            self.sleep(5)
        self.stop_claude()
        st = self.state
        home = Path(st["claude_home"])
        proj = home / "projects" / st["project_dir"]
        session_id, report = st["session_id"], {}
        if st.get("run"):
            # The run's turn lives in its fork; export that, so a pull brings back the run, not the template.
            result = run_result(home.parent)
            report = {"result": {**result, "workspace_update": st.get("workspace_update", "")}}
            if result.get("session_id") and (proj / f"{result['session_id']}.jsonl").exists():
                session_id = result["session_id"]
        extras = tuple(p for p in proj.glob("*.jsonl")
                       if p.stem != session_id and p.stat().st_mtime > st["restored_at"])
        workspace.GIT_EXTRA_CONFIG["safe.directory"] = "*"
        with tempfile.TemporaryDirectory(prefix="ctransfer-export-") as tmp:
            out = Path(tmp) / "export.tgz"
            manifest = bundle.build(
                out, kind="export", home=home, session_id=session_id, project_dir=st["project_dir"],
                cwd=st["cwd"], workspace_dir=st["vm_cwd"], record_root=st["root"], extra_transcripts=extras,
                extra={"transfer_id": st["transfer_id"]},
            )
            self.upload(upload, out)
        self.post("exported", {"export_key": export_key, **report,
                               "manifest": {k: manifest[k] for k in ("record_count", "last_uuid", "claude_version")}})
        return manifest

    def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:  # keep heartbeating through transient network errors
                print(f"ctransfer agent: {e}", file=sys.stderr, flush=True)
            self.sleep(HEARTBEAT_SEC)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="ctransfer.agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("restore")
    r.add_argument("--boot", type=Path, required=True)
    r.add_argument("--bundle-dir", type=Path, required=True)
    r.add_argument("--user", default="ubuntu")
    r.add_argument("--state", type=Path, default=STATE_PATH)
    r.add_argument("--manual", action="store_true",
                   help="manual spike: no control plane, so skip the ready report and the systemd agent")
    run = sub.add_parser("run")
    run.add_argument("--state", type=Path, default=STATE_PATH)
    args = ap.parse_args(argv)

    if args.cmd == "restore":
        boot = json.loads(args.boot.read_text())
        home = Path(os.path.expanduser(f"~{args.user}"))
        state = restore(boot, args.bundle_dir, home=home, user=args.user)
        args.state.parent.mkdir(parents=True, exist_ok=True)
        args.state.write_text(json.dumps(state))
        args.state.chmod(0o600)
        args.boot.unlink()  # held the Claude token and secrets; they now live only in the user's ~/.ctransfer/env
        shutil.rmtree(args.bundle_dir, ignore_errors=True)
        if not args.manual:
            agent = Agent(state)
            # claude registers in ~/.claude/sessions a few seconds after tmux starts it.
            for _ in range(30):
                if agent.status() != "stopped":
                    break
                time.sleep(1)
            agent.post("ready", {"status": agent.status()})
            install_service(args.state)
    else:
        Agent(json.loads(args.state.read_text())).run()


if __name__ == "__main__":
    main()
