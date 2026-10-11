"""ctransfer command line."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from . import api as api_mod
from . import agent, apply, bundle, net, sessions, workspace

POLL_SEC = 10
TURN_POLL_SEC = 1
TURN_GRACE_POLLS = 5  # how long to wait for the invoking turn to show up as busy (if it has one)


def _print(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def requested_session_id(args) -> str | None:
    """--session, or with --current the invoking session; None means 'discover from --cwd'."""
    if getattr(args, "current", False):
        session_id = sessions.current_session_id()
        if not session_id:
            raise SystemExit("--current: not running inside a Claude Code session")
        return session_id
    return args.session


def discover(args) -> sessions.SessionInfo:
    return sessions.discover(session_id=requested_session_id(args), cwd=args.cwd)


ACTIVE_STATES = ("queued", "pushing", "launching", "running")


def marker_dir() -> Path:
    """Progress of detached pushes, one file per session; the plugin's prompt guard reads them."""
    return api_mod.CONFIG_PATH.parent / "pushed"


def marker_path(session_id: str) -> Path:
    return marker_dir() / f"{session_id}.json"


def read_marker(session_id: str) -> dict | None:
    try:
        return json.loads(marker_path(session_id).read_text())
    except (OSError, ValueError):
        return None


def write_marker(sid: str, **fields) -> None:
    path = marker_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**(read_marker(sid) or {}), **fields, "updated_at": time.time()}
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def clear_markers(tid: str) -> None:
    for f in marker_dir().glob("*.json"):
        try:
            if json.loads(f.read_text()).get("transfer_id") == tid:
                f.unlink()
        except (OSError, ValueError):
            continue


def wait_turn_end(session_id: str, *, timeout_polls: int = 3600, sleep=None) -> None:
    """Block until the session's current turn ends. /ctransfer starts this during its own turn, so first
    give the turn a moment to show as busy, then wait for it to stop being busy."""
    sleep = sleep or time.sleep

    def status():
        return (sessions.find_live(session_id) or {}).get("status") or "stopped"

    for _ in range(TURN_GRACE_POLLS):
        if status() in ("busy", "stopped"):
            break
        sleep(TURN_POLL_SEC)
    for _ in range(timeout_polls):
        if status() != "busy":
            return
        sleep(TURN_POLL_SEC)
    raise SystemExit(f"session {session_id} is still mid-turn; gave up waiting")


def wait_for(api, tid: str, states: set[str], timeout: int, poll: float = POLL_SEC, sleep=time.sleep) -> dict:
    deadline = time.time() + timeout
    last = None
    while True:
        item = api.call("GET", f"/sessions/{tid}")
        if item["state"] != last:
            _print(f"  state: {item['state']}")
            last = item["state"]
        if item["state"] in states:
            return item
        if item["state"] in ("failed", "terminated"):
            raise SystemExit(f"transfer {tid} ended in state {item['state']} ({item.get('failure', '')})")
        if time.time() > deadline:
            raise SystemExit(f"timed out waiting for {tid} to reach {sorted(states)}")
        sleep(poll)


def keys_dir() -> Path:
    return api_mod.CONFIG_PATH.parent / "keys"


def report_bundle(path: Path, manifest: dict) -> None:
    """What a bundle carries and what it left behind."""
    for s in manifest["skipped"]:
        _print(f"  skipped (looks like a credential): {s}")
    if any(s.startswith("mcp:") for s in manifest["skipped"]):
        _print("  (an MCP server with a literal token in its env or headers stays behind: store the token with "
               "`ctransfer secret set NAME` and write ${NAME} in its config instead)")
    if manifest.get("mcp_servers"):
        _print(f"MCP servers: {', '.join(manifest['mcp_servers'])}")
    _print(f"bundle: {path.stat().st_size / 1e6:.1f} MB, {manifest['record_count']} records, "
           f"claude {manifest['claude_version']}")


def build_push_bundle(path: Path, info: sessions.SessionInfo, *, home: Path, file_history: bool = True) -> dict:
    """A schedule's template: the session, user config, workspace and the agent. Returns its manifest."""
    manifest = bundle.build(path, kind="push", home=home, session_id=info.session_id, project_dir=info.project_dir,
                            cwd=info.cwd, include_config=True, include_agent=True, include_file_history=file_history)
    report_bundle(path, manifest)
    return manifest


def push(api, info: sessions.SessionInfo, *, home: Path, remote_control: bool = False, file_history: bool = True,
         prompt: str | None = None, wait: bool = True, keys: Path | None = None, sleep=time.sleep,
         on_launch=None) -> dict:
    """Send a session to a new cloud VM; with a prompt, the VM's claude resumes straight into that turn."""
    def build(path: Path) -> dict:
        return bundle.build(path, kind="push", home=home, session_id=info.session_id, project_dir=info.project_dir,
                            cwd=info.cwd, include_config=True, include_agent=True, include_file_history=file_history,
                            extra={"prompt": prompt} if prompt else None)

    resume_as = sessions.model_args(info.transcript)  # ["--model", model] plus ["--effort", level] if recorded
    if resume_as:
        effort = f" at {resume_as[3]} effort" if len(resume_as) == 4 else ""
        _print(f"the session continues on {resume_as[1]}{effort}, as it last ran")
    return _send(api, info.session_id, info.cwd, build, remote_control=remote_control, wait=wait, keys=keys,
                 sleep=sleep, on_launch=on_launch)


def local_claude_version() -> str | None:
    """The laptop's Claude Code version (`claude --version` prints e.g. "2.1.288 (Claude Code)")."""
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"\s*(\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def new_session(api, cwd: str, *, home: Path, session_id: str | None = None, prompt: str | None = None,
                remote_control: bool = False, wait: bool = True, keys: Path | None = None, sleep=time.sleep,
                on_launch=None) -> dict:
    """Start a fresh session on a new cloud VM, in a copy of the project at cwd, optionally with a first prompt."""
    cwd = str(Path(cwd).resolve())
    session_id = session_id or str(uuid.uuid4())

    def build(path: Path) -> dict:
        return bundle.build(path, kind="new", home=home, session_id=session_id,
                            project_dir=sessions.encode_project_dir(cwd), cwd=cwd, include_config=True,
                            include_agent=True, claude_version=local_claude_version(),
                            extra={"prompt": prompt} if prompt else None)

    return _send(api, session_id, cwd, build, remote_control=remote_control, wait=wait, keys=keys, sleep=sleep,
                 on_launch=on_launch)


def _send(api, session_id: str, cwd: str, build, *, remote_control: bool, wait: bool, keys: Path | None, sleep,
          on_launch) -> dict:
    """Build a bundle with build(path), upload it to a new transfer, launch its VM and (with wait) wait for running."""
    keys = keys or keys_dir()
    with tempfile.TemporaryDirectory(prefix="ctransfer-push-") as tmp:
        key = Path(tmp) / "key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ctransfer", "-f", str(key)], check=True)
        path = Path(tmp) / "push.tgz"
        manifest = build(path)
        report_bundle(path, manifest)
        created = api.call("POST", "/sessions", {"session_id": session_id, "cwd": cwd,
                                                 "claude_version": manifest["claude_version"], "remote_control": remote_control,
                                                 "ssh_pubkey": key.with_suffix(".pub").read_text().strip()})
        tid = created["transfer_id"]
        keys.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.move(key, keys / tid)
        shutil.move(key.with_suffix(".pub"), keys / f"{tid}.pub")
        net.post_file(created["upload"], path)
    api.call("POST", f"/sessions/{tid}/launch")
    _print(f"launched transfer {tid}")
    if on_launch:
        on_launch(tid)
    return wait_for(api, tid, {"running"}, timeout=25 * 60, sleep=sleep) if wait else api.call("GET", f"/sessions/{tid}")


def pull(api, tid: str, *, home: Path, force: bool = False, as_fork: bool = False, stash: bool = False,
         sleep=time.sleep) -> dict:
    item = api.call("GET", f"/sessions/{tid}")
    if item["state"] != "exported":
        if item["state"] in ("running", "export_requested"):
            api.call("POST", f"/sessions/{tid}/export")
            _print("export requested; the VM finishes its current turn first")
        item = wait_for(api, tid, {"exported"}, timeout=50 * 60, sleep=sleep)
    with tempfile.TemporaryDirectory(prefix="ctransfer-pull-") as tmp:
        path = Path(tmp) / "export.tgz"
        net.get_file(item["download_url"], path)
        manifest = bundle.extract(path, Path(tmp) / "x")
        result = apply.apply_export(Path(tmp) / "x", manifest, home=home, force=force, as_fork=as_fork, stash=stash)
    api.call("DELETE", f"/sessions/{tid}")  # the server already tore the VM down at export; this is idempotent
    return result


def cmd_pull(args) -> None:
    result = pull(api_mod.Api.from_config(), args.transfer_id, home=sessions.claude_home(), force=args.force,
                  as_fork=args.as_fork, stash=args.stash)
    clear_markers(args.transfer_id)
    _report_pull(result)


def _report_pull(result: dict) -> None:
    if result["backup"]:
        _print(f"previous local state backed up to {result['backup']}")
    if result["transcript"] is None:
        _print("pulled the workspace; the cloud session was never prompted, so there is no conversation to resume")
    else:
        _print(f"pulled. continue with:  ctransfer resume {result['session_id']}")


RETURN_NOTE = (
    "This session was just brought back from a cloud VM to the user's laptop by ctransfer. The transfer is "
    "complete: you are running locally again, in the original project directory, which now holds the work done "
    "on the VM. Any earlier request in this conversation to pull or bring the session back has already been "
    "carried out; do not repeat it.")


def cmd_resume(args) -> None:
    """claude --resume on the model and effort the session last ran with, told it is back from the cloud (the
    counterpart of the VM's arrival note)."""
    transcript = sessions.find_transcript(args.session_id)
    if not transcript:
        raise SystemExit(f"no local transcript for session {args.session_id}; pull it first")
    os.execvp("claude", ["claude", "--resume", args.session_id, *sessions.model_args(transcript),
                         "--append-system-prompt", RETURN_NOTE])


def cmd_login(args) -> None:
    _print(f"By signing in you agree to the Terms of Use ({api_mod.TERMS_URL}) and the Privacy Policy "
           f"({api_mod.PRIVACY_URL}).")
    out = api_mod.login(api_mod.Api.from_config(signed_in=False), notify=_print)
    cfg = api_mod.load_config()
    cfg["token"] = out["token"]
    api_mod.save_config(cfg)
    _print(f"signed in as {out['login']}")


def cmd_logout(args) -> None:
    cfg = api_mod.load_config()
    cfg.pop("token", None)
    api_mod.save_config(cfg)
    _print("signed out")


def cmd_account_delete(args) -> None:
    api = api_mod.Api.from_config()
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("account deletion needs confirmation: run it in a terminal, or add --yes")
        _print("This permanently deletes your ctransfer account: every cloud session and its VM (running ones are "
               "stopped without being brought back), your schedules, your Claude token and secrets. Sessions on "
               "this machine are not touched.")
        if input("Type 'delete' to confirm: ").strip() != "delete":
            raise SystemExit("not deleted")
    out = api.call("DELETE", "/me")
    for local in (keys_dir(), marker_dir()):  # ssh keys and push progress of sessions that no longer exist
        shutil.rmtree(local, ignore_errors=True)
    cfg = api_mod.load_config()
    cfg.pop("token", None)
    api_mod.save_config(cfg)
    _print(f"Your ctransfer account is deleted: {out['vms_stopped']} VM(s) stopped, {out['sessions']} cloud "
           f"session(s) and {out['schedules']} schedule(s) removed. You are signed out.")


def cmd_token(args) -> None:
    api = api_mod.Api.from_config()
    if args.action == "rm":
        api.call("DELETE", "/me/claude-token")
        return
    token = _read_secret("token from `claude setup-token`: ")
    api.call("PUT", "/me/claude-token", {"token": token.strip()})
    _print("Claude token saved; your VMs will run on it")


def _read_secret(prompt: str) -> str:
    """A secret from stdin when piped, else typed at a hidden prompt (never a command-line argument, which shell
    history and process listings keep)."""
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass(prompt)
    return sys.stdin.read()


def cmd_secret_set(args) -> None:
    value = _read_secret(f"value for {args.name}: ").strip()
    if not value:
        raise SystemExit("empty value; to remove a secret use `ctransfer secret rm`")
    api_mod.Api.from_config().call("PUT", f"/me/secrets/{args.name}", {"value": value})
    _print(f"{args.name} saved. VMs launched from now on get it as an environment variable "
           f"(MCP server configs can use ${{{args.name}}}).")
    if args.name in agent.GITHUB_TOKEN_NAMES:
        _print("git and gh on those VMs use it for github.com (pushes, pull requests).")


def cmd_secret_ls(args) -> None:
    for s in api_mod.Api.from_config().call("GET", "/me/secrets")["secrets"]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["updated_at"])) if s.get("updated_at") else ""
        print(f"{s['name']:<32} {when}")


def cmd_secret_rm(args) -> None:
    api_mod.Api.from_config().call("DELETE", f"/me/secrets/{args.name}")
    _print(f"{args.name} removed; VMs launched from now on won't get it (running ones keep their copy)")


def add_secret_parser(sub) -> None:
    secret = sub.add_parser(
        "secret", help="environment variables your VMs get, e.g. GITHUB_TOKEN for git push and gh",
        description="Secrets are stored encrypted by the control plane and given to each VM you launch afterwards, "
                    "as environment variables of its claude (and of MCP servers it starts). A GitHub token named "
                    "GH_TOKEN or GITHUB_TOKEN also sets up git and gh for github.com; prefer a fine-grained token "
                    "limited to the repos and permissions the VM needs.")
    ssub = secret.add_subparsers(dest="secret_cmd", required=True)
    p = ssub.add_parser("set", help="store a secret; the value is read from stdin or a hidden prompt")
    p.add_argument("name", help="environment variable name, e.g. GITHUB_TOKEN")
    p.set_defaults(fn=cmd_secret_set)
    ssub.add_parser("ls", help="list your secrets' names (values are never shown)").set_defaults(fn=cmd_secret_ls)
    p = ssub.add_parser("rm", help="delete a secret")
    p.add_argument("name")
    p.set_defaults(fn=cmd_secret_rm)


def prompt_arg(args) -> str | None:
    """The positional first prompt of push/new: its words joined with single spaces, None if empty."""
    return " ".join(args.prompt).strip() or None


def cmd_push(args) -> None:
    # The transcript may not exist until the invoking turn ends (/ctransfer as a session's first prompt),
    # so detached and --when-idle pushes resolve just the id up front and read the session afterwards.
    sid = requested_session_id(args) or discover(args).session_id
    if args.detach:
        return _detach_push(args, sid)
    track = (lambda **kw: write_marker(sid, **kw)) if args.track else (lambda **kw: None)
    try:
        if args.when_idle:
            _print(f"waiting for session {sid} to finish its turn")
            wait_turn_end(sid)
        info = sessions.discover(session_id=sid)
        _print(f"pushing session {sid} ({info.cwd})")
        if info.status == "busy" and not (args.allow_busy or args.when_idle):
            raise SystemExit(f"session {sid} is mid-turn; wait for it to go idle (or pass --allow-busy/--when-idle)")
        track(state="pushing")
        item = push(api_mod.Api.from_config(), info, home=sessions.claude_home(), remote_control=args.remote_control,
                    file_history=not args.no_file_history, prompt=prompt_arg(args), wait=not args.no_wait,
                    on_launch=lambda tid: track(state="launching", transfer_id=tid))
    except BaseException as e:
        track(state="failed", error=str(e) or type(e).__name__)
        raise
    tid = item["transfer_id"]
    track(state="running" if item["state"] == "running" else "launching", transfer_id=tid)
    where = "is running in the cloud" if item["state"] == "running" else f"is {item['state']} in the cloud"
    _print(f"\nsession {sid} {where} as transfer {tid}")
    if info.live:
        _print("exit the local session now (/exit): continuing locally will diverge from the cloud copy")
    _print(f"attach:   ctransfer ssh {tid}\nbring back: ctransfer pull {tid}")


def _detach_push(args, sid: str) -> None:
    """Re-run this push as a background worker (it outlives the caller's shell), logging to a file."""
    api_mod.Api.from_config()  # fail now, not after the wait, if the control plane isn't configured
    active = read_marker(sid)
    if active and active.get("state") in ACTIVE_STATES:
        raise SystemExit(f"a push of session {sid} is already {active['state']} (see ctransfer status --current)")
    log = log_path(sid)
    cmd = ["push", "--session", sid, "--track", *_flags(args, "remote_control", "no_file_history", "allow_busy",
                                                          "no_wait", "when_idle")]
    prompt = prompt_arg(args)
    if prompt:
        cmd += ["--", prompt]
    marker_path(sid).unlink(missing_ok=True)
    write_marker(sid, state="queued", session_id=sid, cwd=os.getcwd(), log=str(log))
    write_marker(sid, pid=_spawn_worker(cmd, log))
    when = "once its current turn ends" if args.when_idle else "now"
    print(f"queued push of session {sid}; it starts {when}.\nprogress: ctransfer status --current  (log: {log})")


def log_path(session_id: str) -> Path:
    return api_mod.CONFIG_PATH.parent / "logs" / f"{session_id}.log"


def _flags(args, *names: str) -> list[str]:
    return ["--" + name.replace("_", "-") for name in names if getattr(args, name)]


def _spawn_worker(cli_args: list[str], log: Path) -> int:
    """Run `ctransfer <cli_args>` in the background (it outlives the caller's shell), appending to log. Returns its pid."""
    cmd = [sys.executable, "-m", "ctransfer.cli", *cli_args]
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        out.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd[1:])}\n".encode())
        out.flush()
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
    return proc.pid


def cmd_new(args) -> None:
    if args.session_id:
        try:
            sid = str(uuid.UUID(args.session_id))
        except ValueError:
            raise SystemExit(f"--session-id must be a UUID, not {args.session_id!r}")
    else:
        sid = str(uuid.uuid4())
    cwd = str(Path(args.cwd or os.getcwd()).resolve())
    prompt = prompt_arg(args)
    if args.detach:
        api_mod.Api.from_config()  # fail now, in the caller's terminal, if not signed in
        log = log_path(sid)
        cmd = ["new", "--session-id", sid, "--cwd", cwd, *_flags(args, "remote_control", "no_wait"),
               *(["--", prompt] if prompt else [])]
        _spawn_worker(cmd, log)
        print(f"starting new cloud session {sid} for {cwd} in the background.\n"
              f"progress: {log}  (`ctransfer ls` shows its transfer once created)")
        return
    item = new_session(api_mod.Api.from_config(), cwd, home=sessions.claude_home(), session_id=sid, prompt=prompt,
                       remote_control=args.remote_control, wait=not args.no_wait)
    tid = item["transfer_id"]
    where = "is running in the cloud" if item["state"] == "running" else f"is {item['state']} in the cloud"
    _print(f"\nnew session {sid} {where} as transfer {tid}")
    _print(f"attach:   ctransfer ssh {tid}\nbring back: ctransfer pull {tid}")


def _local_time(t: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "-"


def _next_steps(tid: str, state: str) -> str:
    """What the user can do with a transfer in this state, as commands to copy."""
    if state == "running":
        return f"attach: ctransfer ssh {tid}   bring back: ctransfer pull {tid}"
    if state in ("launching", "bootstrapping", "export_requested", "exported"):
        return f"bring back: ctransfer pull {tid}"
    return ""


def cmd_ls(args) -> None:
    transfers = api_mod.Api.from_config().call("GET", "/sessions")["sessions"]
    if not transfers:
        print("no cloud sessions. `ctransfer push` moves this one to the cloud; `ctransfer new` starts a fresh one.")
        return
    print(f"{'TRANSFER':<12}  {'STATE':<17} {'CLAUDE':<8} {'CREATED':<16}  PROJECT")
    for s in transfers:
        print(f"{s['transfer_id']:<12}  {s['state']:<17} {s.get('claude_status', '-'):<8} "
              f"{_local_time(s.get('created_at')):<16}  {s['cwd']}")


def cmd_status(args) -> None:
    if not args.transfer_id:
        return _status_current()
    item = api_mod.Api.from_config().call("GET", f"/sessions/{args.transfer_id}")
    state = item["state"]
    claude = f" (claude {item['claude_status']})" if state == "running" and item.get("claude_status") else ""
    lines = [f"transfer {item['transfer_id']}: {state}{claude}",
             f"  session  {item['session_id']}",
             f"  project  {item['cwd']}",
             f"  created  {_local_time(item.get('created_at'))}"]
    if item.get("failure"):
        lines.append(f"  failure  {item['failure']}")
    if item.get("export_reason") and item["export_reason"] != "user":
        lines.append(f"  exported by the service ({item['export_reason']}); your work is kept until you pull it")
    steps = _next_steps(item["transfer_id"], state)
    print("\n".join(lines + ([steps] if steps else [])))


def _status_current() -> None:
    """The detached push of the invoking session: its progress, the cloud state, and the log tail."""
    sid = sessions.current_session_id()
    if not sid:
        raise SystemExit("not running inside a Claude Code session; pass a transfer id")
    marker = read_marker(sid)
    if not marker:
        raise SystemExit(f"no ctransfer push recorded for session {sid}")
    lines = [f"push of session {sid}: {marker['state']}"]
    tid = marker.get("transfer_id")
    if tid:
        try:
            cloud = api_mod.Api.from_config().call("GET", f"/sessions/{tid}")["state"]
        except (SystemExit, Exception) as e:  # not signed in, network: the local progress still helps
            cloud = f"unknown ({e})"
        lines.append(f"  transfer  {tid} (in the cloud: {cloud})")
    if marker.get("error"):
        lines.append(f"  error     {marker['error']}")
    if tid and marker["state"] == "running":
        lines.append(_next_steps(tid, "running"))
    try:
        tail = Path(marker["log"]).read_text().splitlines()[-10:]
    except (KeyError, OSError):
        tail = []
    if tail:
        lines += ["", f"log ({marker['log']}):", *tail]
    print("\n".join(lines))


def cmd_ssh(args) -> None:
    api = api_mod.Api.from_config()
    item = api.call("GET", f"/sessions/{args.transfer_id}")
    ip = (item.get("instance") or {}).get("ip")
    if not ip:
        raise SystemExit(f"transfer has no running instance (state {item['state']})")
    # push generated this key and the VM's agent authorized its public half.
    key = keys_dir() / args.transfer_id
    if not key.exists():
        raise SystemExit(f"no key at {key}: ssh only works from the machine that pushed")
    known_hosts = api_mod.CONFIG_PATH.parent / "known_hosts"
    tid = args.transfer_id
    if args.command:
        remote = args.command
    else:
        _print(f"attaching to the claude of {tid}. To leave, press Ctrl-b then d: claude keeps running in the cloud.\n"
               f"Quitting claude there (/exit) stops it on the VM; bring the session back with: ctransfer pull {tid}")
        gone = f"claude is no longer running on this VM. Bring the session back with: ctransfer pull {tid}"
        remote = f"tmux attach -t claude || {{ echo {shlex.quote(gone)}; echo 'This is a shell on the VM; exit leaves.'; exec bash -l; }}"
    cmd = ["ssh", "-i", str(key), "-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=accept-new",
           "-t", f"ubuntu@{ip}", remote]
    os.execvp("ssh", cmd)


def cmd_usage(args) -> None:
    """This user's standing against their own allowance, and whether a launch would go through right now."""
    u = api_mod.Api.from_config().call("GET", "/me/usage")
    limits = u.get("limits") or {}

    def of(key, unit=""):
        return f" of {limits[key]:g}{unit}" if key in limits else unit

    lines = [f"your free tier this month ({u['month']}):",
             f"  VM-hours     {u['billed_hours'] + u['open_hours']}{of('monthly_vm_hours')} "
             f"({u['open_hours']} on VMs still running)",
             f"  transferred  {u['bytes'] / 1e6:.0f}{of('monthly_mb', ' MB')}"]
    if "launches_today" in u:  # older control planes report only the month's launches
        lines.append(f"  launches     {u['launches_today']} today{of('daily_launches', ' a day')}")
    else:
        lines.append(f"  launches     {u['launches']} this month")
    if "running_vms" in u:
        lines.append(f"  VMs running  {u['running_vms']}{of('vms')}")
    if "launch_refusal" in u:
        lines.append(f"you can't launch a VM now: {u['launch_refusal']}" if u["launch_refusal"]
                     else "you can launch a VM now")
    print("\n".join(lines))


def cmd_rm(args) -> None:
    out = api_mod.Api.from_config().call("DELETE", f"/sessions/{args.transfer_id}")
    clear_markers(args.transfer_id)
    if out["state"] == "exported":
        print(f"deleted the VM of {args.transfer_id}; its exported session can still be pulled: "
              f"ctransfer pull {args.transfer_id}")
    else:
        print(f"deleted {args.transfer_id}: its VM and any work done on it are gone")


# ---- cron: scheduled runs of a pushed session ---------------------------------------------------

def cron_add(api, info: sessions.SessionInfo, *, home: Path, schedule: str, prompt: str, timeout_min: int | None = None,
             permission_mode: str = "default", file_history: bool = False) -> dict:
    """Upload the session as the schedule's template: every run starts from this snapshot, in a fork."""
    with tempfile.TemporaryDirectory(prefix="ctransfer-cron-") as tmp:
        path = Path(tmp) / "template.tgz"
        manifest = build_push_bundle(path, info, home=home, file_history=file_history)
        created = api.call("POST", "/schedules", {
            "session_id": info.session_id, "cwd": info.cwd, "claude_version": manifest["claude_version"],
            "cron": schedule, "prompt": prompt, "timeout_min": timeout_min, "permission_mode": permission_mode})
        net.post_file(created.pop("upload"), path)
    return created


def _utc(t: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t)) if t else "-"


def cmd_cron_add(args) -> None:
    prompt = Path(args.prompt_file).read_text() if args.prompt_file else args.prompt
    if not prompt:
        raise SystemExit("give the task with --prompt or --prompt-file")
    info = discover(args)
    out = cron_add(api_mod.Api.from_config(), info, home=sessions.claude_home(), schedule=args.schedule,
                   prompt=prompt, timeout_min=args.timeout, permission_mode=args.permission_mode,
                   file_history=args.file_history)
    _print(f"schedule {out['schedule_id']}: session {info.session_id} runs '{out['cron']}' (UTC); "
           f"next run {_utc(out['next_run_at'])}\nrun now: ctransfer cron run {out['schedule_id']}   "
           f"results: ctransfer cron logs {out['schedule_id']}")


def cmd_cron_ls(args) -> None:
    for s in api_mod.Api.from_config().call("GET", "/schedules")["schedules"]:
        last = s.get("last_run") or {}
        print(f"{s['schedule_id']}  {s['cron']:<15} next {_utc(s.get('next_run_at'))}  "
              f"last {last.get('status', '-'):<9} {s['cwd']}  {s['prompt'][:50]!r}")


def cmd_cron_run(args) -> None:
    out = api_mod.Api.from_config().call("POST", f"/schedules/{args.schedule_id}/run")
    print(f"started run {out['run_id']} of schedule {out['schedule_id']}\n"
          f"results: ctransfer cron logs {out['schedule_id']}")


def _run_line(run: dict) -> str:
    result = run.get("result") or {}
    bits = [_utc(run.get("created_at")), run["status"], f"run {run['run_id']}"]
    if result.get("duration_ms"):
        bits.append(f"{result['duration_ms'] / 60000:.1f} min")
    if result.get("total_cost_usd") is not None:
        bits.append(f"${result['total_cost_usd']:.2f}")
    if result.get("permission_denials"):
        bits.append(f"{result['permission_denials']} permission denials")
    return "  ".join(bits)


def cmd_cron_logs(args) -> None:
    sched = api_mod.Api.from_config().call("GET", f"/schedules/{args.schedule_id}")
    print(f"schedule {sched['schedule_id']}  '{sched['cron']}' UTC  next {_utc(sched.get('next_run_at'))}\n"
          f"task: {sched['prompt']}\n")
    for run in sched["runs"][: args.n][::-1]:  # newest last, next to the prompt
        result = run.get("result") or {}
        print(_run_line(run))
        for line in filter(None, [run.get("error"), result.get("error"), result.get("workspace_update")]):
            print(f"  ({line})")
        if result.get("text"):
            print("  " + result["text"].replace("\n", "\n  "))
        print()


def cmd_cron_pull(args) -> None:
    """A run's forked transcript as a local session to inspect or continue; the workspace is left alone."""
    api = api_mod.Api.from_config()
    item = api.call("GET", f"/sessions/{args.run_id}")
    if item["state"] != "exported" or not item.get("download_url"):
        raise SystemExit(f"run {args.run_id} has no transcript to pull (state {item['state']})")
    with tempfile.TemporaryDirectory(prefix="ctransfer-pull-") as tmp:
        path = Path(tmp) / "export.tgz"
        net.get_file(item["download_url"], path)
        manifest = bundle.extract(path, Path(tmp) / "x")
        result = apply.apply_export(Path(tmp) / "x", manifest, home=sessions.claude_home(), workspace_too=False)
    _print(f"pulled run {args.run_id} as session {result['session_id']} (workspace untouched)\n"
           f"continue with:  claude --resume {result['session_id']}")


def cmd_cron_rm(args) -> None:
    out = api_mod.Api.from_config().call("DELETE", f"/schedules/{args.schedule_id}")
    stopped = f"; stopped its running run(s): {', '.join(out['runs_stopped'])}" if out["runs_stopped"] else ""
    print(f"deleted schedule {out['deleted']}{stopped}")


def add_cron_parser(sub, session_args) -> None:
    cron = sub.add_parser("cron", help="run a task on a schedule, each time in a fresh fork of a pushed session")
    csub = cron.add_subparsers(dest="cron_cmd", required=True)

    p = csub.add_parser("add", help="snapshot a session and run a task on it on a cron schedule (UTC)")
    session_args(p)
    p.add_argument("--schedule", required=True, help="5-field cron expression in UTC, at most hourly, e.g. '0 9 * * 1-5'")
    p.add_argument("--prompt", help="the task each run performs")
    p.add_argument("--prompt-file", help="read the task from this file")
    p.add_argument("--timeout", type=int, help="minutes a run may take (default: the free tier's maximum)")
    p.add_argument("--permission-mode", default="default", choices=("default", "acceptEdits", "plan", "bypassPermissions"),
                   help="claude --permission-mode for runs (default: the session's own settings apply)")
    p.add_argument("--file-history", action="store_true", help="include /rewind checkpoints in the snapshot")
    p.set_defaults(fn=cmd_cron_add)

    csub.add_parser("ls", help="list schedules").set_defaults(fn=cmd_cron_ls)
    for name, fn, help_ in (("run", cmd_cron_run, "start a run now"), ("rm", cmd_cron_rm, "delete a schedule")):
        p = csub.add_parser(name, help=help_)
        p.add_argument("schedule_id")
        p.set_defaults(fn=fn)

    p = csub.add_parser("logs", help="recent runs and their results")
    p.add_argument("schedule_id")
    p.add_argument("-n", type=int, default=5, help="how many runs (default 5)")
    p.set_defaults(fn=cmd_cron_logs)

    p = csub.add_parser("pull", help="bring a run's transcript back as a new local session")
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_cron_pull)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ctransfer", description="Move a Claude Code session to a cloud VM and back.")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    # Listed in the order a user meets them: set up, send work out, check on it, bring it back.

    sub.add_parser("login", help="sign in with GitHub").set_defaults(fn=cmd_login)

    p = sub.add_parser("token", help="store (set, read from stdin) or delete (rm) the Claude token your VMs run on")
    p.add_argument("action", choices=("set", "rm"))
    p.set_defaults(fn=cmd_token)
    add_secret_parser(sub)

    def session_args(p):
        p.add_argument("--session", help="session id (default: newest session for --cwd)")
        p.add_argument("--cwd", help="project directory (default: current directory)")
        p.add_argument("--current", action="store_true", help="the Claude Code session running this command")

    p = sub.add_parser("push", help="move a session to a new cloud VM")
    session_args(p)
    p.add_argument("--remote-control", action="store_true", help="start claude with --remote-control on the VM")
    p.add_argument("--no-file-history", action="store_true", help="skip /rewind checkpoints")
    p.add_argument("--allow-busy", action="store_true", help="push even if the session is mid-turn")
    p.add_argument("--no-wait", action="store_true", help="return once the VM is launching")
    p.add_argument("--when-idle", action="store_true", help="wait for the session's current turn to end, then push")
    p.add_argument("--detach", action="store_true", help="push from a background worker; log under ~/.config/ctransfer/logs")
    p.add_argument("--track", action="store_true", help=argparse.SUPPRESS)  # set on the detached worker
    p.add_argument("prompt", nargs="*", help="prompt the VM's claude runs as soon as it resumes (default: wait idle)")
    p.set_defaults(fn=cmd_push)

    p = sub.add_parser("new", help="start a new session on a cloud VM, in a copy of this project")
    p.add_argument("prompt", nargs="*", help="first prompt for the new session (default: start idle)")
    p.add_argument("--cwd", help="project directory to copy (default: current directory)")
    p.add_argument("--session-id", help="UUID for the new session (default: random)")
    p.add_argument("--remote-control", action="store_true", help="start claude with --remote-control on the VM")
    p.add_argument("--no-wait", action="store_true", help="return once the VM is launching")
    p.add_argument("--detach", action="store_true", help="start from a background worker; log under ~/.config/ctransfer/logs")
    p.set_defaults(fn=cmd_new)

    sub.add_parser("ls", help="list your cloud sessions").set_defaults(fn=cmd_ls)

    p = sub.add_parser("status", help="one cloud session in detail (no id: this Claude Code session's push)")
    p.add_argument("transfer_id", nargs="?")
    p.add_argument("--current", action="store_true", help="the Claude Code session running this command (default)")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("ssh", help="attach to a cloud session's claude (Ctrl-b then d detaches; it keeps running)")
    p.add_argument("transfer_id")
    p.add_argument("command", nargs="?", help="run this on the VM instead of attaching")
    p.set_defaults(fn=cmd_ssh)

    p = sub.add_parser("pull", help="bring a cloud session back, overwriting the local copy")
    p.add_argument("transfer_id")
    p.add_argument("--force", action="store_true", help="overwrite local transcript/branch even if they diverged")
    p.add_argument("--as-fork", action="store_true", help="write the cloud transcript under a new session id")
    p.add_argument("--stash", action="store_true", help="stash local uncommitted changes instead of refusing")
    p.set_defaults(fn=cmd_pull)

    p = sub.add_parser("resume", help="continue a pulled session locally (claude --resume, told it is back)")
    p.add_argument("session_id")
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("rm", help="delete a cloud session and its VM without bringing it back")
    p.add_argument("transfer_id")
    p.set_defaults(fn=cmd_rm)

    add_cron_parser(sub, session_args)

    sub.add_parser("usage", help="your free-tier usage this month, and whether you can launch now").set_defaults(fn=cmd_usage)
    sub.add_parser("logout", help="forget the saved sign-in").set_defaults(fn=cmd_logout)

    account = sub.add_parser("account", help="delete your ctransfer account and everything stored for it")
    asub = account.add_subparsers(dest="account_cmd", required=True)
    p = asub.add_parser("delete", help="delete your account, cloud sessions, VMs, schedules, Claude token and secrets")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(fn=cmd_account_delete)
    return ap


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (LookupError, net.HttpError, apply.ApplyError, workspace.WorkspaceError, bundle.BundleError) as e:
        raise SystemExit(f"error: {e}")


if __name__ == "__main__":
    main()
