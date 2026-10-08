"""VM-side agent.

`restore` (run once by cloud-init as root): unpack a pushed bundle into the login user's
~/.claude and the original cwd path, install the pinned Claude Code version, start
`claude --resume <id>` (or, for a `new` bundle, a fresh `claude --session-id <id> [prompt]`)
in tmux, and install a systemd unit for `run`.

`run` (systemd): heartbeat the control plane with the session's busy/idle status; when an
export is requested, wait for a turn boundary, stop claude, bundle the transcript + workspace
and upload it.

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

from . import bundle, net, sessions, workspace

STATE_PATH = Path("/var/lib/ctransfer/state.json")
AGENT_DIR = Path("/opt/ctransfer/agent")
TMUX_SESSION = "claude"
HEARTBEAT_SEC = 60
EXPORT_WAIT_SEC = 1800

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


def _write_global_config(path: Path, trusted: list[str]) -> None:
    cfg = json.loads(path.read_text()) if path.exists() else {}
    cfg["hasCompletedOnboarding"] = True
    projects = cfg.setdefault("projects", {})
    for p in trusted:
        projects.setdefault(p, {})["hasTrustDialogAccepted"] = True
    path.write_text(json.dumps(cfg, indent=2))


def restore(boot: dict, bundle_dir: Path, *, home: Path, user: str | None = None, root_prefix: str = "",
            launch: bool = True) -> dict:
    """Unpack the bundle and (with launch) start claude. Returns the agent state to persist.

    root_prefix relocates the workspace (tests use it so the 'VM' doesn't collide with the laptop path).
    """
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    sid, cwd = manifest["session_id"], manifest["cwd"]
    ws = manifest["workspace"]
    claude_dir = home / ".claude"

    _merge_tree(bundle_dir / "claude", claude_dir)
    vm_root = Path(root_prefix + ws["root"])
    vm_cwd = root_prefix + cwd
    created_top = _first_missing_ancestor(vm_root)
    workspace.restore_fresh(ws, bundle_dir / "workspace", vm_root)
    _write_global_config(home / ".claude.json", sorted({vm_cwd, str(vm_root)}))

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
    }
    if launch:
        _install_claude(user, boot.get("claude_version"))
        _launch(state, home, boot["claude_token"], bool(boot.get("remote_control")), manifest.get("prompt"))
    return state


def _install_claude(user: str | None, version: str | None) -> None:
    arg = version if version and version != "latest" else ""
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


def claude_args(state: dict, *, remote_control: bool = False, prompt: str | None = None) -> list[str]:
    """How the VM starts claude: resume the pushed session, or start the new one under its pre-chosen id."""
    if state["new"]:
        args = ["--session-id", state["session_id"], "--append-system-prompt", new_session_note(state["transfer_id"])]
    else:
        args = ["--resume", state["session_id"], "--append-system-prompt", arrival_note(state["transfer_id"])]
    if remote_control:
        args.append("--remote-control")
    return args + [prompt] if prompt else args


def _launch(state: dict, home: Path, claude_token: str, remote_control: bool, prompt: str | None = None) -> None:
    ctl = home / ".ctransfer"
    ctl.mkdir(mode=0o700, exist_ok=True)
    env_file = ctl / "env"
    env_file.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={claude_token}\n")
    env_file.chmod(0o600)
    args = claude_args(state, remote_control=remote_control, prompt=prompt)
    launcher = ctl / "launch.sh"
    launcher.write_text(
        "#!/bin/bash\nset -a; . \"$HOME/.ctransfer/env\"; set +a\n"
        f"cd {json.dumps(state['vm_cwd'])}\n"
        f"exec \"$HOME/.local/bin/claude\" {shlex.join(args)}\n"
    )
    launcher.chmod(0o700)
    if state["user"]:
        subprocess.run(["chown", "-R", f"{state['user']}:{state['user']}", str(ctl)], check=True)
    subprocess.run(_as_user(state["user"], f"tmux new-session -d -s {TMUX_SESSION} -x 220 -y 50 {launcher}"), check=True)


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
        home = Path(self.state["claude_home"])
        for s in sessions.live_sessions(home):
            if s.get("sessionId") == self.state["session_id"] or s.get("cwd") == self.state["vm_cwd"]:
                return s.get("status") or "idle"
        return "stopped"

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
        extras = tuple(p for p in proj.glob("*.jsonl")
                       if p.stem != st["session_id"] and p.stat().st_mtime > st["restored_at"])
        workspace.GIT_EXTRA_CONFIG["safe.directory"] = "*"
        with tempfile.TemporaryDirectory(prefix="ctransfer-export-") as tmp:
            out = Path(tmp) / "export.tgz"
            manifest = bundle.build(
                out, kind="export", home=home, session_id=st["session_id"], project_dir=st["project_dir"],
                cwd=st["cwd"], workspace_dir=st["vm_cwd"], record_root=st["root"], extra_transcripts=extras,
                extra={"transfer_id": st["transfer_id"]},
            )
            self.upload(upload, out)
        self.post("exported", {"export_key": export_key, "manifest": {k: manifest[k] for k in ("record_count", "last_uuid", "claude_version")}})
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
        args.boot.unlink()  # held the Claude token; it now lives only in the user's ~/.ctransfer/env
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
