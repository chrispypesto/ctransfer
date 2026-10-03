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


def keys_dir() -> Path:
    return api_mod.CONFIG_PATH.parent / "keys"


def push(api, info: sessions.SessionInfo, *, home: Path, remote_control: bool = False, file_history: bool = True,
         wait: bool = True, keys: Path | None = None, sleep=time.sleep) -> dict:
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
        net.put_file(created["upload_url"], path)
    api.call("POST", f"/sessions/{tid}/launch")
    _print(f"launched transfer {tid}")
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
    _report_pull(result)


def cmd_apply(args) -> None:
    with tempfile.TemporaryDirectory(prefix="ctransfer-apply-") as tmp:
        manifest = bundle.extract(args.bundle, tmp)
        _report_pull(apply.apply_export(tmp, manifest, force=args.force, as_fork=args.as_fork, stash=args.stash))


def _report_pull(result: dict) -> None:
    if result["backup"]:
        _print(f"previous local state backed up to {result['backup']}")
    _print(f"pulled. continue with:  claude --resume {result['session_id']}")


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
    where = "is running in the cloud" if item["state"] == "running" else f"is {item['state']} in the cloud"
    _print(f"\nsession {info.session_id} {where} as transfer {tid}")
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
    print(json.dumps(api_mod.Api.from_config().call("DELETE", f"/sessions/{args.transfer_id}"), indent=2))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ctransfer", description="Move a Claude Code session to a cloud VM and back.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("configure", help="save control-plane settings")
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
    except (LookupError, net.HttpError, apply.ApplyError, workspace.WorkspaceError, bundle.BundleError) as e:
        raise SystemExit(f"error: {e}")


if __name__ == "__main__":
    main()
