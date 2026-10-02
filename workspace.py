"""Capture a workspace (git repo or plain directory) and restore it elsewhere."""

from __future__ import annotations

import fnmatch
import os
import subprocess
import tarfile
import time
from pathlib import Path

SECRET_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "id_ecdsa*",
    "*.tfvars", ".credentials.json", ".netrc", ".npmrc", ".pypirc",
)
SECRET_ALLOW = ("*.example", "*.sample", "*.template")
TREE_EXCLUDE_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache"}

# Extra `git -c` settings for every git call; the VM agent runs as root on a repo owned by
# the login user and sets safe.directory here.
GIT_EXTRA_CONFIG: dict[str, str] = {}


class WorkspaceError(Exception):
    pass


def is_secret_name(path: str) -> bool:
    name = os.path.basename(path)
    if any(fnmatch.fnmatch(name, p) for p in SECRET_ALLOW):
        return False
    return any(fnmatch.fnmatch(name, p) for p in SECRET_PATTERNS)


def git_bytes(root, *args: str) -> bytes:
    env = dict(os.environ)
    if GIT_EXTRA_CONFIG:
        env["GIT_CONFIG_COUNT"] = str(len(GIT_EXTRA_CONFIG))
        for i, (k, v) in enumerate(GIT_EXTRA_CONFIG.items()):
            env[f"GIT_CONFIG_KEY_{i}"] = k
            env[f"GIT_CONFIG_VALUE_{i}"] = v
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, env=env)
    if p.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)}: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout


def git(root, *args: str) -> str:
    return git_bytes(root, *args).decode(errors="replace")


def _try_git(root, *args: str) -> str | None:
    try:
        return git(root, *args).strip()
    except WorkspaceError:
        return None


def _toplevel(path) -> Path | None:
    top = _try_git(path, "rev-parse", "--show-toplevel")
    return Path(top) if top else None


def _split_z(out: str) -> list[str]:
    return [p for p in out.split("\0") if p]


def capture(cwd, out_dir) -> dict:
    """Write the workspace containing cwd into out_dir and return its metadata.

    Git repos travel as a bundle of all refs + a binary diff of uncommitted changes against
    HEAD + a tar of untracked files. Anything else travels as a plain tar. Files that look
    like credentials are left out and listed under "skipped".
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    root = _toplevel(cwd)
    if root and _try_git(root, "rev-parse", "--verify", "-q", "HEAD"):
        return _capture_git(root, out)
    return _capture_tree(Path(cwd), out)


def _capture_git(root: Path, out: Path) -> dict:
    head = git(root, "rev-parse", "HEAD").strip()
    branch = _try_git(root, "symbolic-ref", "--short", "-q", "HEAD")
    git(root, "bundle", "create", "-q", str(out / "repo.bundle"), "--all")

    changed = _split_z(git(root, "diff", "--name-only", "-z", "HEAD"))
    skipped = [c for c in changed if is_secret_name(c)]
    pathspec = ["--", "."] + [f":(exclude,literal){c}" for c in skipped]
    (out / "dirty.diff").write_bytes(git_bytes(root, "diff", "--binary", "HEAD", *pathspec))

    untracked = _split_z(git(root, "ls-files", "--others", "--exclude-standard", "-z"))
    skipped += [u for u in untracked if is_secret_name(u)]
    with tarfile.open(out / "untracked.tar", "w") as tf:
        for u in untracked:
            if not is_secret_name(u):
                tf.add(root / u, arcname=u, recursive=False)

    remotes = {name: git(root, "remote", "get-url", name).strip() for name in git(root, "remote").split()}
    return {"kind": "git", "root": str(root), "head": head, "branch": branch, "remotes": remotes, "skipped": sorted(skipped)}


def _capture_tree(root: Path, out: Path) -> dict:
    skipped = []
    with tarfile.open(out / "tree.tar", "w") as tf:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in TREE_EXCLUDE_DIRS]
            for name in filenames:
                full = Path(dirpath) / name
                rel = str(full.relative_to(root))
                if is_secret_name(name):
                    skipped.append(rel)
                    continue
                tf.add(full, arcname=rel, recursive=False)
    return {"kind": "tree", "root": str(root), "skipped": sorted(skipped)}


def _extract(tar_path: Path, dest: Path) -> None:
    if tar_path.exists():
        with tarfile.open(tar_path) as tf:
            tf.extractall(dest, filter="data")


def _apply_dirty(root: Path, src: Path) -> None:
    diff = src / "dirty.diff"
    if diff.exists() and diff.stat().st_size:
        git(root, "apply", "--binary", "--whitespace=nowarn", str(diff))
    _extract(src / "untracked.tar", root)


def restore_fresh(meta: dict, src, root) -> None:
    """Recreate a captured workspace at an empty (or missing) root."""
    # git runs with -C root, so a relative src would resolve against the wrong directory.
    src, root = Path(src).resolve(), Path(root)
    if root.exists() and any(root.iterdir()):
        raise WorkspaceError(f"{root} already exists and is not empty")
    root.mkdir(parents=True, exist_ok=True)
    if meta["kind"] == "tree":
        _extract(src / "tree.tar", root)
        return
    git(root, "init", "-q")
    git(root, "fetch", "-q", "--update-head-ok", str(src / "repo.bundle"),
        "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*", "+refs/remotes/*:refs/remotes/*")
    if meta.get("branch"):
        git(root, "symbolic-ref", "HEAD", f"refs/heads/{meta['branch']}")
        git(root, "reset", "-q", "--hard", meta["head"])
    else:
        git(root, "fetch", "-q", str(src / "repo.bundle"), "HEAD")
        git(root, "checkout", "-q", "--detach", meta["head"])
    for name, url in (meta.get("remotes") or {}).items():
        git(root, "remote", "add", name, url)
    _apply_dirty(root, src)


def apply_existing(meta: dict, src, root, *, stash: bool = False, force: bool = False, dry_run: bool = False) -> list[str]:
    """Bring an existing local workspace up to a captured one (the pull direction).

    Returns a list of problems. With dry_run nothing in the working tree changes (the bundle's
    refs are still fetched into refs/ctransfer/remote/*). Raises WorkspaceError on failure.
    """
    src, root = Path(src).resolve(), Path(root)
    if meta["kind"] == "tree":
        if not dry_run:
            root.mkdir(parents=True, exist_ok=True)
            _extract(src / "tree.tar", root)
        return []
    if _toplevel(root) is None:
        return [f"{root} is not a git repository"]
    problems = []
    bundle = str(src / "repo.bundle")
    git(root, "fetch", "-q", bundle, "+refs/heads/*:refs/ctransfer/remote/*")
    git(root, "fetch", "-q", bundle, "HEAD")
    target, branch = meta["head"], meta.get("branch")
    local_head = _try_git(root, "rev-parse", "--verify", "-q", "HEAD")

    # Untracked credential files never left this machine and the cloud copy won't touch them,
    # so they neither count as local changes nor get stashed.
    entries = _split_z(git(root, "status", "--porcelain", "-z", "--untracked-files=all"))
    secrets = [e[3:] for e in entries if e.startswith("?? ") and is_secret_name(e[3:])]
    dirty = len(entries) > len(secrets)
    if dirty and not stash:
        problems.append(f"{root} has uncommitted changes (commit them or pass --stash)")
    branch_exists = bool(branch and _try_git(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"))
    if branch_exists:
        tip = git(root, "rev-parse", f"refs/heads/{branch}").strip()
        if tip != target and _try_git(root, "merge-base", "--is-ancestor", tip, target) is None and not force:
            problems.append(f"local branch {branch} has commits the cloud copy lacks (pass --force to reset it)")
    if dry_run or problems:
        return problems

    if dirty:
        git(root, "stash", "push", "-q", "-u", "-m", f"ctransfer pull {int(time.time())}",
            "--", ".", *[f":(exclude,literal){s}" for s in secrets])
    if local_head:
        git(root, "update-ref", f"refs/ctransfer/backup/{int(time.time())}", local_head)
    if branch_exists:
        git(root, "checkout", "-q", branch)
        git(root, "reset", "-q", "--hard", target)  # fast-forward, or forced reset after the check above
    elif branch:
        git(root, "checkout", "-q", "-b", branch, target)
    else:
        git(root, "checkout", "-q", "--detach", target)
    _apply_dirty(root, src)
    return []
