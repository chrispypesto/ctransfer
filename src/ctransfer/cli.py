"""ctransfer command line."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import api as api_mod
from . import apply, bundle, net, sessions, workspace

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


def push(api, info: sessions.SessionInfo, *, home: Path, remote_control: bool = False, file_history: bool = True,
         wait: bool = True, keys: Path | None = None, sleep=time.sleep, on_launch=None) -> dict:
    keys = keys or keys_dir()
    with tempfile.TemporaryDirectory(prefix="ctransfer-push-") as tmp:
        key = Path(tmp) / "key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ctransfer", "-f", str(key)], check=True)
        path = Path(tmp) / "push.tgz"
        manifest = bundle.build(path, kind="push", home=home, session_id=info.session_id, project_dir=info.project_dir,
                                cwd=info.cwd, include_config=True, include_agent=True, include_file_history=file_history)
        for s in manifest["skipped"]:
            _print(f"  skipped (looks like a credential): {s}")
        _print(f"bundle: {path.stat().st_size / 1e6:.1f} MB, {manifest['record_count']} records, "
               f"claude {manifest['claude_version']}")
        created = api.call("POST", "/sessions", {"session_id": info.session_id, "cwd": info.cwd,
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


def cmd_apply(args) -> None:
    with tempfile.TemporaryDirectory(prefix="ctransfer-apply-") as tmp:
        manifest = bundle.extract(args.bundle, tmp)
        _report_pull(apply.apply_export(tmp, manifest, force=args.force, as_fork=args.as_fork, stash=args.stash))


def _report_pull(result: dict) -> None:
    if result["backup"]:
        _print(f"previous local state backed up to {result['backup']}")
    _print(f"pulled. continue with:  ctransfer resume {result['session_id']}")


RETURN_NOTE = (
    "This session was just brought back from a cloud VM to the user's laptop by ctransfer. The transfer is "
    "complete: you are running locally again, in the original project directory, which now holds the work done "
    "on the VM. Any earlier request in this conversation to pull or bring the session back has already been "
    "carried out; do not repeat it.")


def cmd_resume(args) -> None:
    """claude --resume, told the session is back from the cloud (the counterpart of the VM's arrival note)."""
    if not sessions.find_transcript(args.session_id):
        raise SystemExit(f"no local transcript for session {args.session_id}; pull it first")
    os.execvp("claude", ["claude", "--resume", args.session_id, "--append-system-prompt", RETURN_NOTE])


def cmd_configure(args) -> None:
    cfg = api_mod.load_config()
    for key in ("api_url",):
        if getattr(args, key):
            cfg[key] = getattr(args, key)
    api_mod.save_config(cfg)
    print(json.dumps(cfg, indent=2))


def cmd_login(args) -> None:
    out = api_mod.login(api_mod.Api.from_config(signed_in=False), notify=_print)
    cfg = api_mod.load_config()
    cfg["token"] = out["token"]
    api_mod.save_config(cfg)
    _print(f"signed in as {out['login']}")


def cmd_logout(args) -> None:
    cfg = api_mod.load_config()
    cfg.pop("token", None)
    api_mod.save_config(cfg)


def cmd_token(args) -> None:
    api = api_mod.Api.from_config()
    if args.action == "rm":
        api.call("DELETE", "/me/claude-token")
        return
    if sys.stdin.isatty():
        import getpass

        token = getpass.getpass("token from `claude setup-token`: ")
    else:
        token = sys.stdin.read()
    api.call("PUT", "/me/claude-token", {"token": token.strip()})
    _print("Claude token saved; your VMs will run on it")


def cmd_discover(args) -> None:
    info = discover(args)
    print(json.dumps({"session_id": info.session_id, "cwd": info.cwd, "status": info.status,
                      "transcript": str(info.transcript), "pid": (info.live or {}).get("pid")}, indent=2))


def cmd_bundle(args) -> None:
    info = discover(args)
    m = bundle.build(args.out, kind="push", home=sessions.claude_home(), session_id=info.session_id,
                     project_dir=info.project_dir, cwd=info.cwd, include_config=True, include_agent=True)
    print(json.dumps(m, indent=2))


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
        if info.status == "busy" and not (args.allow_busy or args.when_idle):
            raise SystemExit(f"session {sid} is mid-turn; wait for it to go idle (or pass --allow-busy/--when-idle)")
        track(state="pushing")
        item = push(api_mod.Api.from_config(), info, home=sessions.claude_home(), remote_control=args.remote_control,
                    file_history=not args.no_file_history, wait=not args.no_wait,
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
    log = api_mod.CONFIG_PATH.parent / "logs" / f"{sid}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "ctransfer.cli", "push", "--session", sid, "--track"]
    for flag in ("remote_control", "no_file_history", "allow_busy", "no_wait", "when_idle"):
        if getattr(args, flag):
            cmd.append("--" + flag.replace("_", "-"))
    marker_path(sid).unlink(missing_ok=True)
    write_marker(sid, state="queued", session_id=sid, cwd=os.getcwd(), log=str(log))
    with open(log, "ab") as out:
        out.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd[1:])}\n".encode())
        out.flush()
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
    write_marker(sid, pid=proc.pid)
    when = "once its current turn ends" if args.when_idle else "now"
    print(f"queued push of session {sid}; it starts {when}.\nprogress: ctransfer status --current  (log: {log})")


def cmd_ls(args) -> None:
    for s in api_mod.Api.from_config().call("GET", "/sessions")["sessions"]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("created_at", 0)))
        print(f"{s['transfer_id']}  {s['state']:<17} {s.get('claude_status', '-'):<8} {when}  {s['cwd']}")


def cmd_status(args) -> None:
    if not args.transfer_id:
        return _status_current()
    item = api_mod.Api.from_config().call("GET", f"/sessions/{args.transfer_id}")
    item.pop("download_url", None)
    print(json.dumps(item, indent=2))


def _status_current() -> None:
    """The detached push of the invoking session: its marker, the cloud state, and the log tail."""
    sid = sessions.current_session_id()
    if not sid:
        raise SystemExit("not running inside a Claude Code session; pass a transfer id")
    marker = read_marker(sid)
    if not marker:
        raise SystemExit(f"no ctransfer push recorded for session {sid}")
    if marker.get("transfer_id"):
        try:
            marker["cloud_state"] = api_mod.Api.from_config().call("GET", f"/sessions/{marker['transfer_id']}")["state"]
        except (SystemExit, Exception) as e:  # not signed in, network: the marker still helps
            marker["cloud_state"] = f"unknown ({e})"
    print(json.dumps(marker, indent=2))
    try:
        tail = Path(marker["log"]).read_text().splitlines()[-10:]
    except (KeyError, OSError):
        tail = []
    if tail:
        print("\n".join(["", "log:", *tail]))


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
    remote = "tmux attach -t claude || bash -l" if not args.command else args.command
    cmd = ["ssh", "-i", str(key), "-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=accept-new",
           "-t", f"ubuntu@{ip}", remote]
    os.execvp("ssh", cmd)


def cmd_usage(args) -> None:
    u = api_mod.Api.from_config().call("GET", "/me/usage")
    limits = u.get("limits") or {}

    def of(key):
        return f" of {limits[key]}" if key in limits else ""
    print(f"{u['month']}: {u['billed_hours'] + u['open_hours']}{of('monthly_vm_hours')} VM-hours "
          f"({u['open_hours']} on running VMs), {u['launches']} launches, {u['bytes'] / 1e6:.0f}{of('monthly_mb')} MB transferred")


def cmd_rm(args) -> None:
    out = api_mod.Api.from_config().call("DELETE", f"/sessions/{args.transfer_id}")
    clear_markers(args.transfer_id)
    print(json.dumps(out, indent=2))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ctransfer", description="Move a Claude Code session to a cloud VM and back.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("configure", help="point at another control plane (default: the hosted one)")
    p.add_argument("--api-url")
    p.set_defaults(fn=cmd_configure)

    sub.add_parser("login", help="sign in with GitHub").set_defaults(fn=cmd_login)
    sub.add_parser("logout", help="forget the saved sign-in").set_defaults(fn=cmd_logout)

    p = sub.add_parser("token", help="store (set, read from stdin) or delete (rm) the Claude token your VMs run on")
    p.add_argument("action", choices=("set", "rm"))
    p.set_defaults(fn=cmd_token)

    def session_args(p):
        p.add_argument("--session", help="session id (default: newest session for --cwd)")
        p.add_argument("--cwd", help="project directory (default: current directory)")
        p.add_argument("--current", action="store_true", help="the Claude Code session running this command")

    p = sub.add_parser("discover", help="show which local session would be pushed")
    session_args(p)
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("bundle", help="write a push bundle to a file (manual spike / debugging)")
    session_args(p)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_bundle)

    p = sub.add_parser("push", help="send a session to a new cloud VM")
    session_args(p)
    p.add_argument("--remote-control", action="store_true", help="start claude with --remote-control on the VM")
    p.add_argument("--no-file-history", action="store_true", help="skip /rewind checkpoints")
    p.add_argument("--allow-busy", action="store_true", help="push even if the session is mid-turn")
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--when-idle", action="store_true", help="wait for the session's current turn to end, then push")
    p.add_argument("--detach", action="store_true", help="push from a background worker; log under ~/.config/ctransfer/logs")
    p.add_argument("--track", action="store_true", help=argparse.SUPPRESS)  # set on the detached worker
    p.set_defaults(fn=cmd_push)

    def overwrite_args(p):
        p.add_argument("--force", action="store_true", help="overwrite local transcript/branch even if they diverged")
        p.add_argument("--as-fork", action="store_true", help="write the cloud transcript under a new session id")
        p.add_argument("--stash", action="store_true", help="stash local uncommitted changes instead of refusing")

    p = sub.add_parser("pull", help="bring a cloud session back, overwriting the local copy")
    p.add_argument("transfer_id")
    overwrite_args(p)
    p.set_defaults(fn=cmd_pull)

    p = sub.add_parser("apply", help="apply an export bundle file locally (manual spike)")
    p.add_argument("bundle")
    overwrite_args(p)
    p.set_defaults(fn=cmd_apply)

    sub.add_parser("usage", help="this month's VM-hours and transfer volume").set_defaults(fn=cmd_usage)

    p = sub.add_parser("resume", help="continue a pulled session locally (claude --resume, told it is back)")
    p.add_argument("session_id")
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("ls", help="list transfers")
    p.set_defaults(fn=cmd_ls)

    for name, fn, help_ in (("status", cmd_status, "show one transfer"), ("ssh", cmd_ssh, "attach to the VM's claude tmux session"),
                            ("rm", cmd_rm, "delete a transfer's VM")):
        p = sub.add_parser(name, help=help_)
        if name == "status":  # no id: the push of the invoking Claude Code session
            p.add_argument("transfer_id", nargs="?")
            p.add_argument("--current", action="store_true", help="the Claude Code session running this command (default)")
        else:
            p.add_argument("transfer_id")
        if name == "ssh":
            p.add_argument("command", nargs="?", help="run this instead of attaching to tmux")
        p.set_defaults(fn=fn)
    return ap


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (LookupError, net.HttpError, apply.ApplyError, workspace.WorkspaceError, bundle.BundleError) as e:
        raise SystemExit(f"error: {e}")


if __name__ == "__main__":
    main()
