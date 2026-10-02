"""ctransfer command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from . import api as api_mod
from . import bundle, net, sessions

POLL_SEC = 10


def _print(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


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


def push(api, info: sessions.SessionInfo, *, home: Path, remote_control: bool = False, file_history: bool = True,
         wait: bool = True, sleep=time.sleep) -> dict:
    with tempfile.TemporaryDirectory(prefix="ctransfer-push-") as tmp:
        path = Path(tmp) / "push.tgz"
        manifest = bundle.build(path, kind="push", home=home, session_id=info.session_id, project_dir=info.project_dir,
                                cwd=info.cwd, include_config=True, include_agent=True, include_file_history=file_history)
        for s in manifest["skipped"]:
            _print(f"  skipped (looks like a credential): {s}")
        _print(f"bundle: {path.stat().st_size / 1e6:.1f} MB, {manifest['record_count']} records, "
               f"claude {manifest['claude_version']}")
        created = api.call("POST", "/sessions", {"session_id": info.session_id, "cwd": info.cwd,
                                                 "claude_version": manifest["claude_version"], "remote_control": remote_control})
        net.put_file(created["upload_url"], path)
    tid = created["transfer_id"]
    api.call("POST", f"/sessions/{tid}/launch")
    _print(f"launched transfer {tid}")
    return wait_for(api, tid, {"running"}, timeout=25 * 60, sleep=sleep) if wait else api.call("GET", f"/sessions/{tid}")


def cmd_configure(args) -> None:
    cfg = api_mod.load_config()
    for key in ("api_url", "region", "profile"):
        if getattr(args, key):
            cfg[key] = getattr(args, key)
    api_mod.save_config(cfg)
    print(json.dumps(cfg, indent=2))


def cmd_discover(args) -> None:
    info = sessions.discover(session_id=args.session, cwd=args.cwd)
    print(json.dumps({"session_id": info.session_id, "cwd": info.cwd, "status": info.status,
                      "transcript": str(info.transcript), "pid": (info.live or {}).get("pid")}, indent=2))


def cmd_bundle(args) -> None:
    info = sessions.discover(session_id=args.session, cwd=args.cwd)
    m = bundle.build(args.out, kind="push", home=sessions.claude_home(), session_id=info.session_id,
                     project_dir=info.project_dir, cwd=info.cwd, include_config=True, include_agent=True)
    print(json.dumps(m, indent=2))


def cmd_push(args) -> None:
    info = sessions.discover(session_id=args.session, cwd=args.cwd)
    if info.status == "busy" and not args.allow_busy:
        raise SystemExit(f"session {info.session_id} is mid-turn; wait for it to go idle (or pass --allow-busy)")
    item = push(api_mod.Api.from_config(), info, home=sessions.claude_home(), remote_control=args.remote_control,
                file_history=not args.no_file_history, wait=not args.no_wait)
    tid = item["transfer_id"]
    _print(f"\nsession {info.session_id} is running in the cloud as transfer {tid}")
    if info.live:
        _print("exit the local session now (/exit): continuing locally will diverge from the cloud copy")
    _print(f"attach:   ctransfer ssh {tid}\nbring back: ctransfer pull {tid}")


def cmd_ls(args) -> None:
    for s in api_mod.Api.from_config().call("GET", "/sessions")["sessions"]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("created_at", 0)))
        print(f"{s['transfer_id']}  {s['state']:<17} {s.get('claude_status', '-'):<8} {when}  {s['cwd']}")


def cmd_status(args) -> None:
    item = api_mod.Api.from_config().call("GET", f"/sessions/{args.transfer_id}")
    item.pop("download_url", None)
    print(json.dumps(item, indent=2))


def cmd_ssh(args) -> None:
    api = api_mod.Api.from_config()
    item = api.call("GET", f"/sessions/{args.transfer_id}")
    if not item.get("instance_name"):
        raise SystemExit("transfer has no instance")
    details = api.lightsail().get_instance_access_details(instanceName=item["instance_name"], protocol="ssh")["accessDetails"]
    keydir = Path(tempfile.mkdtemp(prefix="ctransfer-ssh-"))
    key = keydir / "key"
    key.write_text(details["privateKey"])
    key.chmod(0o600)
    (keydir / "key-cert.pub").write_text(details["certKey"])
    remote = "tmux attach -t claude || bash -l" if not args.command else args.command
    cmd = ["ssh", "-i", str(key), "-o", f"CertificateFile={keydir / 'key-cert.pub'}", "-o", "StrictHostKeyChecking=accept-new",
           "-t", f"{details['username']}@{details['ipAddress']}", remote]
    os.execvp("ssh", cmd)


def cmd_rm(args) -> None:
    print(json.dumps(api_mod.Api.from_config().call("DELETE", f"/sessions/{args.transfer_id}"), indent=2))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ctransfer", description="Move a Claude Code session to a cloud VM and back.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("configure", help="save control-plane settings")
    p.add_argument("--api-url")
    p.add_argument("--region")
    p.add_argument("--profile", help="AWS profile used to sign requests")
    p.set_defaults(fn=cmd_configure)

    def session_args(p):
        p.add_argument("--session", help="session id (default: newest session for --cwd)")
        p.add_argument("--cwd", help="project directory (default: current directory)")

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
    p.set_defaults(fn=cmd_push)

    p = sub.add_parser("ls", help="list transfers")
    p.set_defaults(fn=cmd_ls)

    for name, fn, help_ in (("status", cmd_status, "show one transfer"), ("ssh", cmd_ssh, "attach to the VM's claude tmux session"),
                            ("rm", cmd_rm, "delete a transfer's VM")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("transfer_id")
        if name == "ssh":
            p.add_argument("command", nargs="?", help="run this instead of attaching to tmux")
        p.set_defaults(fn=fn)
    return ap


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (LookupError, net.HttpError) as e:
        raise SystemExit(f"error: {e}")


if __name__ == "__main__":
    main()
