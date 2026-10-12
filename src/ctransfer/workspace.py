"""Capture a workspace (git repo or plain directory) and restore it elsewhere."""

from __future__ import annotations

import fnmatch
import hashlib
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
    # Who commits here (from the repo's or the user's git config), so commits made on the VM carry the same author.
    identity = {key: value for key in ("name", "email") if (value := _try_git(root, "config", f"user.{key}"))}
    return {"kind": "git", "root": str(root), "head": head, "branch": branch, "remotes": remotes, "identity": identity,
            "skipped": sorted(skipped), "changes": changes_digest(root)}


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
    for key, value in (meta.get("identity") or {}).items():
        git(root, "config", f"user.{key}", value)
    _apply_dirty(root, src)


def changes_digest(root) -> str | None:
    """A fingerprint of the uncommitted work in a git repo: HEAD, the diff against it and the untracked files,
    leaving out untracked credential files (they never travel, and pull leaves them alone).

    Pull compares it with the one recorded at push to tell the changes that were pushed (which the cloud copy
    already has) from edits made afterwards. None when the repo holds work that never left this machine, so it
    must never be taken for pushed work: a modified tracked credential file, or an untracked nested git repo
    (listed as a directory, and carried only as an empty one).
    """
    root = Path(root)
    changed = _split_z(git(root, "diff", "--name-only", "-z", "HEAD"))
    if any(is_secret_name(c) for c in changed):
        return None
    untracked = _untracked(root)
    if any(u.endswith("/") or (root / u).is_dir() and not (root / u).is_symlink() for u in untracked):
        return None
    h = hashlib.sha256()
    h.update(git_bytes(root, "rev-parse", "HEAD"))
    h.update(git_bytes(root, "diff", "--binary", "HEAD"))
    for u in sorted(untracked):
        h.update(b"\0untracked\0" + u.encode() + b"\0")
        path = root / u
        h.update(hashlib.sha256(os.readlink(path).encode() if path.is_symlink() else path.read_bytes()).digest())
    return h.hexdigest()


def _untracked(root: Path) -> list[str]:
    """Untracked files the cloud copy can have, i.e. not ignored and not credentials."""
    return [u for u in _split_z(git(root, "ls-files", "--others", "--exclude-standard", "-z")) if not is_secret_name(u)]


def _uncommitted(root: Path) -> bool:
    """Whether the repo has changes beyond untracked credential files."""
    entries = _split_z(git(root, "status", "--porcelain", "-z", "--untracked-files=all"))
    return any(not (e.startswith("?? ") and is_secret_name(e[3:])) for e in entries)


def _local_state(root: Path, *, branch: str | None, target: str, pushed_changes: str | None) -> dict:
    """What pulling a cloud copy (branch at commit target) over the repo at root would run into."""
    uncommitted = _uncommitted(root)
    as_pushed = uncommitted and pushed_changes is not None and changes_digest(root) == pushed_changes
    branch_exists = bool(branch and _try_git(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"))
    diverged = False
    if branch_exists:
        tip = git(root, "rev-parse", f"refs/heads/{branch}").strip()
        diverged = tip != target and _try_git(root, "merge-base", "--is-ancestor", tip, target) is None
    return {"new_changes": uncommitted and not as_pushed, "as_pushed": as_pushed, "branch_exists": branch_exists,
            "diverged": diverged}


def _problems(root: Path, state: dict, *, branch: str | None, stash: bool, force: bool) -> list[str]:
    problems = []
    if state["new_changes"] and not stash:
        problems.append(f"{root} has uncommitted changes the cloud copy doesn't have (commit them, or pass --stash "
                        "to set them aside as a git stash)")
    if state["diverged"] and not force:
        problems.append(f"local branch {branch} has commits the cloud copy lacks (pass --force to reset it)")
    return problems


def check_local(root, *, branch: str | None, target: str, stash: bool = False, force: bool = False,
                pushed_changes: str | None = None) -> list[str]:
    """Why a pull of a cloud copy that grew out of commit target on branch could not go over the repo at root;
    [] if it could. Uncommitted changes that fingerprint as pushed_changes (see changes_digest) don't count:
    the cloud copy already has them. Nothing is changed."""
    root = Path(root)
    if _toplevel(root) is None:
        return [f"{root} is not a git repository"]
    state = _local_state(root, branch=branch, target=target, pushed_changes=pushed_changes)
    return _problems(root, state, branch=branch, stash=stash, force=force)


def check_existing(meta: dict, src, root, *, stash: bool = False, force: bool = False,
                   pushed_changes: str | None = None) -> list[str]:
    """check_local against a captured workspace: why apply_existing would refuse; [] if it wouldn't. Only the
    bundle's refs are fetched (into refs/ctransfer/remote/*); the working tree is not touched."""
    if meta["kind"] == "tree":
        return []
    src, root = Path(src).resolve(), Path(root)
    if _toplevel(root) is None:
        return [f"{root} is not a git repository"]
    _fetch(root, src)
    return check_local(root, branch=meta.get("branch"), target=meta["head"], stash=stash, force=force,
                       pushed_changes=pushed_changes)


def _cloud_copy_has(root: Path, src: Path, target: str, path: str) -> bool:
    """Whether the captured workspace at src (HEAD target) has the file at path with the same bytes as root has,
    either untracked or committed."""
    local = root / path
    if local.is_symlink() or not local.is_file():
        return False
    data = local.read_bytes()
    untracked = src / "untracked.tar"
    if untracked.exists():
        with tarfile.open(untracked) as tf:
            try:
                member = tf.extractfile(path)
            except KeyError:
                member = None
            if member is not None and member.read() == data:
                return True
    try:
        return git_bytes(root, "cat-file", "blob", f"{target}:{path}") == data
    except WorkspaceError:
        return False


def _fetch(root: Path, src: Path) -> None:
    bundle = str(src / "repo.bundle")
    git(root, "fetch", "-q", bundle, "+refs/heads/*:refs/ctransfer/remote/*")
    git(root, "fetch", "-q", bundle, "HEAD")


def apply_existing(meta: dict, src, root, *, stash: bool = False, force: bool = False,
                   pushed_changes: str | None = None) -> str | None:
    """Bring an existing local workspace up to a captured one (the pull direction).

    Uncommitted changes that are exactly what was pushed (pushed_changes, see changes_digest) are replaced by the
    cloud copy, which grew out of them. Newer ones are stashed with stash, else (like a diverged branch without
    force) it raises WorkspaceError and changes nothing. Returns the stash's message when one was made.
    """
    src, root = Path(src).resolve(), Path(root)
    if meta["kind"] == "tree":
        root.mkdir(parents=True, exist_ok=True)
        _extract(src / "tree.tar", root)
        return None
    if _toplevel(root) is None:
        raise WorkspaceError(f"{root} is not a git repository")
    _fetch(root, src)
    target, branch = meta["head"], meta.get("branch")
    state = _local_state(root, branch=branch, target=target, pushed_changes=pushed_changes)
    problems = _problems(root, state, branch=branch, stash=stash, force=force)
    if problems:
        raise WorkspaceError("; ".join(problems))

    local_head = _try_git(root, "rev-parse", "--verify", "-q", "HEAD")
    stashed = None
    if state["as_pushed"]:
        git(root, "reset", "-q", "--hard")
        for u in _untracked(root):
            (root / u).unlink()
    elif state["new_changes"]:
        # Untracked credential files never left this machine and the cloud copy won't touch them, and untracked
        # files the cloud copy brings back unchanged would only be in the way when the stash is popped: both stay.
        keep = [u for u in _split_z(git(root, "ls-files", "--others", "--exclude-standard", "-z"))
                if is_secret_name(u) or _cloud_copy_has(root, src, target, u)]
        stashed = f"ctransfer pull {int(time.time())}"
        git(root, "stash", "push", "-q", "-u", "-m", stashed, "--", ".", *[f":(exclude,literal){k}" for k in keep])
    if local_head:
        git(root, "update-ref", f"refs/ctransfer/backup/{int(time.time())}", local_head)
    if state["branch_exists"]:
        git(root, "checkout", "-q", branch)
        git(root, "reset", "-q", "--hard", target)  # fast-forward, or forced reset after the check above
    elif branch:
        git(root, "checkout", "-q", "-b", branch, target)
    else:
        git(root, "checkout", "-q", "--detach", target)
    _apply_dirty(root, src)
    return stashed
