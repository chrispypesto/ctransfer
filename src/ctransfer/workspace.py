"""Capture a workspace (git repo or plain directory) and restore it elsewhere."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import posixpath
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

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


# Characters HFS+ (macOS) ignores in file names, so ".g\u200cit" is ".git" there.
_IGNORED_IN_NAMES = dict.fromkeys([*range(0x200C, 0x2010), *range(0x202A, 0x202F), *range(0x206A, 0x2070), 0xFEFF])
# Paths that make Claude Code or other tools run commands in a project (hooks, MCP servers, tasks, direnv) or tell
# Claude what to do: pull applies new or changed ones only with --accept-config. Folders, then file names.
CONFIG_DIRS = (".claude", ".vscode")
CONFIG_FILES = (".mcp.json", "claude.md", "claude.local.md", ".envrc")


def _name_key(part: str) -> str:
    """A path component as a case-insensitive file system (macOS, Windows) sees it: case folded, without the
    characters HFS+ ignores or the trailing dots and spaces Windows drops."""
    return part.translate(_IGNORED_IN_NAMES).casefold().rstrip(". ")


def is_git_path(path: str) -> bool:
    """Whether path is in (or is) a .git folder: git's own files (config, hooks), which can run commands."""
    return any(_name_key(part) in (".git", "git~1") for part in path.split("/"))


def is_config_path(path: str) -> bool:
    """Whether path configures Claude Code or a tool that runs commands in the folder (see CONFIG_DIRS)."""
    keys = [_name_key(part) for part in path.split("/") if part]
    return any(key in CONFIG_DIRS for key in keys) or bool(keys) and keys[-1] in CONFIG_FILES


def is_secret_name(path: str) -> bool:
    name = os.path.basename(path)
    if any(fnmatch.fnmatch(name, p) for p in SECRET_ALLOW):
        return False
    return any(fnmatch.fnmatch(name, p) for p in SECRET_PATTERNS)


def git_bytes(root, *args: str, env_extra: dict[str, str] | None = None) -> bytes:
    env = {**os.environ, **(env_extra or {})}
    if GIT_EXTRA_CONFIG:
        env["GIT_CONFIG_COUNT"] = str(len(GIT_EXTRA_CONFIG))
        for i, (k, v) in enumerate(GIT_EXTRA_CONFIG.items()):
            env[f"GIT_CONFIG_KEY_{i}"] = k
            env[f"GIT_CONFIG_VALUE_{i}"] = v
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, env=env)
    if p.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)}: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout


def git(root, *args: str, env_extra: dict[str, str] | None = None) -> str:
    return git_bytes(root, *args, env_extra=env_extra).decode(errors="replace")


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
    where = locate(cwd)
    if where["kind"] == "git":
        return _capture_git(Path(where["root"]), out)
    return _capture_tree(Path(where["root"]), out)


def locate(cwd) -> dict:
    """The workspace capture takes for cwd: {"kind": "git", "root": its repo's top level} for a git repo with a
    commit, else {"kind": "tree", "root": cwd}."""
    root = _toplevel(cwd)
    if root and _try_git(root, "rev-parse", "--verify", "-q", "HEAD"):
        return {"kind": "git", "root": str(root)}
    return {"kind": "tree", "root": str(cwd)}


def _plain_mode(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """An archived file without setuid, setgid or sticky bits, which extraction refuses."""
    info.mode &= 0o777
    return info


def _capture_git(root: Path, out: Path) -> dict:
    # Fingerprint first: a file edited while the capture runs then only makes pull see a mismatch and refuse,
    # never a match that would let pull replace an edit the bundle doesn't have.
    changes = changes_digest(root)
    head = git(root, "rev-parse", "HEAD").strip()
    branch = _try_git(root, "symbolic-ref", "--short", "-q", "HEAD")
    git(root, "bundle", "create", "-q", str(out / "repo.bundle"), "--all")

    changed = _split_z(git(root, "diff", "--name-only", "-z", "HEAD"))
    skipped = [c for c in changed if is_secret_name(c)]
    pathspec = ["--", "."] + [f":(exclude,literal){c}" for c in skipped]
    (out / "dirty.diff").write_bytes(git_bytes(root, "diff", "--binary", "HEAD", *pathspec))

    # Never git's own files (a nested repo's .git): pull refuses them, as they can run commands.
    untracked = [u for u in _split_z(git(root, "ls-files", "--others", "--exclude-standard", "-z")) if not is_git_path(u)]
    skipped += [u for u in untracked if is_secret_name(u)]
    with tarfile.open(out / "untracked.tar", "w") as tf:
        for u in untracked:
            if not is_secret_name(u):
                tf.add(root / u, arcname=u, recursive=False, filter=_plain_mode)

    remotes = {name: git(root, "remote", "get-url", name).strip() for name in git(root, "remote").split()}
    # Who commits here (from the repo's or the user's git config), so commits made on the VM carry the same author.
    identity = {key: value for key in ("name", "email") if (value := _try_git(root, "config", f"user.{key}"))}
    return {"kind": "git", "root": str(root), "head": head, "branch": branch, "remotes": remotes, "identity": identity,
            "skipped": sorted(skipped), "changes": changes}


def _capture_tree(root: Path, out: Path) -> dict:
    skipped = []
    files = {}
    with tarfile.open(out / "tree.tar", "w") as tf:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in TREE_EXCLUDE_DIRS and not is_git_path(d)]
            for name in filenames:
                if is_git_path(name):  # a worktree's .git file: never sent, pull refuses it
                    continue
                full = Path(dirpath) / name
                rel = str(full.relative_to(root))
                if is_secret_name(name):
                    skipped.append(rel)
                    continue
                # Only files and symlinks: a pipe or socket carries nothing (opening a FIFO would block) and pull
                # refuses special files.
                if not (full.is_symlink() or full.is_file()):
                    continue
                # Fingerprint before archiving: a file edited in between then only makes pull see a mismatch and
                # refuse, never a match that would let pull overwrite an edit the archive doesn't have.
                files[rel] = _fingerprint(full)
                tf.add(full, arcname=rel, recursive=False, filter=_plain_mode)
    return {"kind": "tree", "root": str(root), "skipped": sorted(skipped), "files": files}


def _fingerprint(path: Path) -> str:
    """sha256 of a file's bytes, or of its target for a symlink."""
    if path.is_symlink():
        return hashlib.sha256(os.readlink(path).encode()).hexdigest()
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _member_fingerprint(tf: tarfile.TarFile, member: tarfile.TarInfo) -> str:
    """_fingerprint of a file as archived."""
    if member.issym():
        return hashlib.sha256(member.linkname.encode()).hexdigest()
    h = hashlib.sha256()
    f = tf.extractfile(member)
    if f is not None:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _local_fingerprint(root: Path, rel: str) -> str | None:
    """_fingerprint of root/rel, or None when there is no file (or symlink) there."""
    path = root / rel
    if not path.is_symlink() and not path.is_file():
        return None
    return _fingerprint(path)


def tree_edits(root, pushed_files: dict) -> list[str]:
    """Files of a pushed non-git folder that were changed here after the push (pushed_files is the push's
    {path: fingerprint}). Pulling would overwrite them; deleted ones are left out, pulling only brings them back."""
    root = Path(root)
    edits = []
    for rel, pushed in sorted(pushed_files.items()):
        local = _local_fingerprint(root, rel)
        if local is not None and local != pushed:
            edits.append(rel)
    return edits


def tree_conflicts(src, root, pushed_files: dict | None) -> list[str]:
    """Local files that extracting the captured non-git folder at src over root would overwrite and lose: the
    archive has a different version, and the local one isn't the pushed version the cloud copy grew out of (or
    nothing is known about what was pushed)."""
    src, root = Path(src), Path(root)
    tar_path = src / "tree.tar"
    if not tar_path.exists():
        return []
    conflicts = []
    with tarfile.open(tar_path) as tf:
        for member in tf:
            # A hard link (a second path to a file already archived) is written over a local file with its
            # target's bytes, so it counts like a file.
            if not (member.isfile() or member.islnk() or member.issym()):
                continue
            # A member extraction refuses (one that would land outside root, say) is never extracted, so it
            # overwrites nothing here; check_existing refuses the whole pull for it.
            name, why = _member_name(member)
            if why:
                continue
            local = _local_fingerprint(root, name)
            if local is None or local == _member_fingerprint(tf, member):
                continue
            if pushed_files is not None and pushed_files.get(name) == local:
                continue
            conflicts.append(name)
    return sorted(conflicts)


def tree_problem(root, paths: list[str]) -> str:
    """Why a pull over the non-git folder at root stops: the files that changed here since the push."""
    shown = ", ".join(paths[:10]) + (f" and {len(paths) - 10} more" if len(paths) > 10 else "")
    return (f"files in {root} changed here since the push, and pulling would overwrite them: {shown}. Nothing was "
            "changed. Pass --force to pull anyway: those files are backed up first, and the pull says where")


class _Entry(NamedTuple):
    """An archive member extraction will write: kind "dir", "file" (data from member; a hard link's is its
    target's) or "symlink" (to target)."""
    name: str
    kind: str
    mode: int
    member: tarfile.TarInfo
    target: str = ""


def _member_name(member: tarfile.TarInfo) -> tuple[str, str | None]:
    """A member's path inside the folder, and why extraction refuses it (None if it doesn't)."""
    name = member.name.rstrip("/") if member.isdir() else member.name
    parts = name.split("/")
    if not name or name.startswith("/") or "\0" in name or any(p in ("", ".", "..") for p in parts):
        return name, "its path leads outside the folder"
    if is_git_path(name):
        return name, "it is inside a .git folder, git's own files, which can run commands"
    return name, None


def _link_target(name: str, target: str) -> tuple[str | None, str | None]:
    """Where a symlink at name pointing at target leads, as a path inside the folder (None if outside it), and
    why extraction refuses it whatever it leads to."""
    if not target or "\0" in target:
        return None, "it is a link without a valid target"
    if target.startswith("/"):
        return None, None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    if resolved == ".." or resolved.startswith("../"):
        return None, None
    if is_git_path(resolved):
        return resolved, "it is a link into a .git folder, git's own files, which can run commands"
    return resolved, None


def _entries(tf: tarfile.TarFile) -> tuple[list[_Entry], list[str]]:
    """The members of an archive of untracked files or of a non-git folder, checked one by one (not trusting
    tarfile's own filters): plain files, folders, hard links to earlier files of the archive and symlinks only, at
    plain paths inside the folder, outside .git, without setuid, setgid or sticky bits. Returns the entries and why
    each refused member is refused."""
    entries: dict[str, _Entry] = {}
    links: set[str] = set()
    refused = []
    for member in tf.getmembers():
        name, why = _member_name(member)
        entry = None
        if why is None and member.mode & 0o7000:
            why = "it has a setuid, setgid or sticky bit"
        if why is None and name in entries:
            why = "it is in the archive twice"
        if why is None and any(posixpath.join(*name.split("/")[:i]) in links for i in range(1, name.count("/") + 1)):
            why = "it would be written through a link in the archive"
        if why is None:
            # Plain permissions only: executable or not.
            mode = 0o755 if member.isdir() or member.mode & 0o111 else 0o644
            if member.isdir():
                entry = _Entry(name, "dir", mode, member)
            elif member.isreg():
                entry = _Entry(name, "file", mode, member)
            elif member.islnk():
                target = entries.get(member.linkname)
                if target is None or target.kind != "file":
                    why = "it is a hard link to something that isn't a file of the archive"
                else:
                    entry = _Entry(name, "file", target.mode, target.member)
            elif member.issym():
                _, why = _link_target(name, member.linkname)
                entry = None if why else _Entry(name, "symlink", 0o777, member, member.linkname)
                links.add(name)
            else:
                why = "it is a device, pipe or other special file"
        if why:
            refused.append(f"{name} ({why})")
        elif entry is not None:
            entries[name] = entry
    return list(entries.values()), refused


def _inside(path: Path, root_real: Path) -> bool:
    """Whether path, with every link along it followed, is root_real or inside it."""
    real = Path(os.path.realpath(path))
    return real == root_real or root_real in real.parents


def _plan(tf: tarfile.TarFile, entries: list[_Entry], root: Path, *, outside_links: bool) -> tuple[list[_Entry], list[str]]:
    """The entries that would change something at root (the others match what is there), and why any entry can't
    be written there: a path that leads outside root through a local link, a file where a folder is, a link pointing
    outside root (allowed with outside_links)."""
    root_real = Path(os.path.realpath(root))
    changes, problems = [], []
    for e in entries:
        dest = root / e.name
        if not _inside(dest.parent, root_real):
            problems.append(f"{e.name} (its path leads outside the folder through a link here)")
            continue
        is_dir = dest.is_dir() and not dest.is_symlink()
        if e.kind == "dir":
            if not dest.is_dir() and os.path.lexists(dest):
                problems.append(f"{e.name} (it is a folder, and here it is a file)")
            elif not os.path.lexists(dest):
                changes.append(e)
            continue
        if is_dir:
            problems.append(f"{e.name} (it is a file, and here it is a folder)")
            continue
        if e.kind == "file":
            if dest.is_symlink() or not dest.is_file() or _fingerprint(dest) != _member_fingerprint(tf, e.member):
                changes.append(e)
            continue
        if dest.is_symlink() and os.readlink(dest) == e.target:
            continue  # unchanged, even if it points outside the folder: it was there before
        inside, _ = _link_target(e.name, e.target)
        if not outside_links and (inside is None or not _inside(dest.parent / e.target, root_real)):
            problems.append(f"{e.name} (it is a link pointing outside the folder, to {e.target})")
            continue
        changes.append(e)
    return changes, problems


def extract_problems(tar_path, root, *, outside_links: bool = False) -> list[str]:
    """Why extracting the archive at tar_path over root (see _extract) would be refused; [] if it wouldn't."""
    tar_path = Path(tar_path)
    if not tar_path.exists():
        return []
    with tarfile.open(tar_path) as tf:
        entries, refused = _entries(tf)
        _, problems = _plan(tf, entries, Path(root), outside_links=outside_links)
    if not refused and not problems:
        return []
    listed = refused + problems
    shown = "; ".join(listed[:10]) + (f"; and {len(listed) - 10} more" if len(listed) > 10 else "")
    return [f"the cloud copy of {root} has files pull won't write there: {shown}. Nothing was changed"]


def _extract(tar_path: Path, root: Path, *, backup_dir: Path | None = None, back_up=None,
             outside_links: bool = False) -> list[str]:
    """Write the archive at tar_path over root: every member checked first (see _entries and _plan), refusing the
    whole archive with WorkspaceError before writing anything if one fails; the files read out to a staging folder;
    then each file or link that differs from root's put in place with a rename (folders first, links last), after
    copying the one it replaces to backup_dir (only those back_up(name) picks, default all). Returns the names of
    the local files backed up."""
    if not tar_path.exists():
        return []
    with tarfile.open(tar_path) as tf, tempfile.TemporaryDirectory(prefix="ctransfer-staging-") as staging:
        entries, refused = _entries(tf)
        changes, problems = _plan(tf, entries, root, outside_links=outside_links)
        if refused or problems:
            raise WorkspaceError(extract_problems(tar_path, root, outside_links=outside_links)[0])
        staged = {}
        for i, e in enumerate(changes):
            if e.kind == "file":
                staged[e.name] = Path(staging) / str(i)
                src = tf.extractfile(e.member)
                with open(staged[e.name], "wb") as out:
                    if src is not None:
                        shutil.copyfileobj(src, out)

        root_real = Path(os.path.realpath(root))
        backed_up = []
        order = {"dir": 0, "file": 1, "symlink": 2}
        for e in sorted(changes, key=lambda e: (order[e.kind], e.name.count("/"))):
            dest = root / e.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not _inside(dest.parent, root_real):  # a link made since the check above
                raise WorkspaceError(f"{e.name} would be written outside {root} through a link")
            if e.kind == "dir":
                dest.mkdir(mode=e.mode, exist_ok=True)
                continue
            if os.path.lexists(dest) and backup_dir is not None and (back_up is None or back_up(e.name)):
                (backup_dir / e.name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, backup_dir / e.name, follow_symlinks=False)
                backed_up.append(e.name)
            tmp = dest.with_name(f".{dest.name}.ctransfer-{os.getpid()}")
            if os.path.lexists(tmp):
                tmp.unlink()
            if e.kind == "file":
                shutil.copyfile(staged[e.name], tmp)
                os.chmod(tmp, e.mode)
            else:
                os.symlink(e.target, tmp)
            os.replace(tmp, dest)
    return sorted(backed_up)


def _apply_dirty(root: Path, src: Path, *, backup_dir: Path | None = None, outside_links: bool = False) -> list[str]:
    diff = src / "dirty.diff"
    if diff.exists() and diff.stat().st_size:
        git(root, "apply", "--binary", "--whitespace=nowarn", str(diff))
    return _extract(src / "untracked.tar", root, backup_dir=backup_dir, outside_links=outside_links)


def restore_fresh(meta: dict, src, root) -> None:
    """Recreate a captured workspace at an empty (or missing) root (on the VM). Links pointing outside it are
    restored as they were on the laptop; pull is the direction that refuses them."""
    # git runs with -C root, so a relative src would resolve against the wrong directory.
    src, root = Path(src).resolve(), Path(root)
    if root.exists() and any(root.iterdir()):
        raise WorkspaceError(f"{root} already exists and is not empty")
    root.mkdir(parents=True, exist_ok=True)
    if meta["kind"] == "tree":
        _extract(src / "tree.tar", root, outside_links=True)
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
    _apply_dirty(root, src, outside_links=True)


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
                   pushed_changes: str | None = None, pushed_files: dict | None = None,
                   accept_config: bool = False) -> list[str]:
    """check_local against a captured workspace: why apply_existing would refuse; [] if it wouldn't. Only the
    bundle's refs are fetched (into refs/ctransfer/remote/*); the working tree is not touched. A non-git folder
    is checked with tree_conflicts against pushed_files (the push's per-file fingerprints). Either way the archive
    of untracked files (or of the folder) must pass extract_problems, and new or changed config files (see
    config_changes) need accept_config."""
    src, root = Path(src).resolve(), Path(root)
    if meta["kind"] == "tree":
        conflicts = [] if force else tree_conflicts(src, root, pushed_files)
        problems = [tree_problem(root, conflicts)] if conflicts else []
        problems += extract_problems(src / "tree.tar", root)
    else:
        if _toplevel(root) is None:
            return [f"{root} is not a git repository"]
        _fetch(root, src)
        problems = check_local(root, branch=meta.get("branch"), target=meta["head"], stash=stash, force=force,
                               pushed_changes=pushed_changes)
        problems += extract_problems(src / "untracked.tar", root)
    if not accept_config and (changed := config_changes(meta, src, root, pushed_files=pushed_files)):
        problems.append(config_problem(changed))
    return problems


def config_problem(paths: list[str]) -> str:
    """Why a pull stops at new or changed config files (see config_changes)."""
    shown = ", ".join(paths[:10]) + (f" and {len(paths) - 10} more" if len(paths) > 10 else "")
    return (f"the cloud copy adds or changes files that can make Claude Code or your tools run commands on this "
            f"computer: {shown}. Nothing was changed. If you expect these changes (the session on the VM made "
            "them), pull again with --accept-config to apply them")


def _object_format(root: Path) -> str:
    return _try_git(root, "rev-parse", "--show-object-format") or "sha1"


def _blob_id(data: bytes, object_format: str) -> str:
    """The id git gives a blob of data (a file's bytes, or a symlink's target)."""
    return hashlib.new(object_format, b"blob %d\0" % len(data) + data).hexdigest()


def _local_blob(path: Path, object_format: str) -> str | None:
    """_blob_id of the file or symlink at path, None if there is neither."""
    if path.is_symlink():
        return _blob_id(os.fsencode(os.readlink(path)), object_format)
    if path.is_file():
        return _blob_id(path.read_bytes(), object_format)
    return None


def _tree_blobs(out: bytes, id_field: int) -> dict[str, str]:
    """{path: blob id} from `git ls-files -s -z` (id_field 1) or `git ls-tree -r -z` (id_field 2) output, leaving
    out submodules."""
    blobs = {}
    for rec in out.split(b"\0"):
        if not rec:
            continue
        info, path = rec.split(b"\t", 1)
        fields = info.split(b" ")
        if fields[0] != b"160000":
            blobs[os.fsdecode(path)] = fields[id_field].decode()
    return blobs


def _cloud_tracked(root: Path, src: Path, target: str) -> dict[str, str]:
    """{path: blob id} of every file git tracks in the cloud copy (commit target plus its uncommitted diff), read
    into a throwaway index: the working tree and the repo's own index are untouched. target must be fetched."""
    with tempfile.TemporaryDirectory(prefix="ctransfer-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(root, "read-tree", target, env_extra=env)
        diff = src / "dirty.diff"
        if diff.exists() and diff.stat().st_size:
            git(root, "apply", "--cached", "--binary", "--whitespace=nowarn", str(diff), env_extra=env)
        return _tree_blobs(git_bytes(root, "ls-files", "-s", "-z", env_extra=env), 1)


def _archived_blobs(tar_path: Path, object_format: str, pick) -> dict[str, str]:
    """{path: blob id} of the files and symlinks in the archive at tar_path whose path pick(path) picks."""
    blobs = {}
    if not tar_path.exists():
        return blobs
    with tarfile.open(tar_path) as tf:
        for member in tf:
            if not pick(member.name):
                continue
            if member.issym():
                blobs[member.name] = _blob_id(os.fsencode(member.linkname), object_format)
            elif member.isfile() or member.islnk():
                f = tf.extractfile(member)
                blobs[member.name] = _blob_id(f.read() if f else b"", object_format)
    return blobs


def config_changes(meta: dict, src, root, *, pushed_files: dict | None = None) -> list[str]:
    """Config files (is_config_path) that pulling the captured workspace at src over root would add or change.

    A cloud version the user already had doesn't count: in a git repo, the one in the working tree or in the
    local HEAD; in a non-git folder, the one there or the pushed one (pushed_files). A git repo's bundle must be
    fetched (check_existing does that)."""
    src, root = Path(src).resolve(), Path(root)
    if meta["kind"] == "tree":
        changed = []
        tar_path = src / "tree.tar"
        if tar_path.exists():
            with tarfile.open(tar_path) as tf:
                for member in tf:
                    if not (member.isfile() or member.islnk() or member.issym()) or not is_config_path(member.name):
                        continue
                    cloud = _member_fingerprint(tf, member)
                    if cloud not in (_local_fingerprint(root, member.name), (pushed_files or {}).get(member.name)):
                        changed.append(member.name)
        return sorted(set(changed))
    object_format = _object_format(root)
    cloud = {path: blob for path, blob in _cloud_tracked(root, src, meta["head"]).items() if is_config_path(path)}
    cloud.update(_archived_blobs(src / "untracked.tar", object_format, is_config_path))
    head = {}
    if _try_git(root, "rev-parse", "--verify", "-q", "HEAD"):
        head = _tree_blobs(git_bytes(root, "ls-tree", "-r", "-z", "HEAD"), 2)
    return sorted(path for path, blob in cloud.items()
                  if blob not in (_local_blob(root / path, object_format), head.get(path)))


def _checkout_clobbers(root: Path, cloud: dict[str, str], object_format: str) -> list[str]:
    """Local untracked or ignored files that checking out the cloud copy (cloud: _cloud_tracked) would overwrite
    with different content: git replaces ignored files without asking."""
    others = _split_z(git(root, "ls-files", "--others", "--directory", "-z"))
    files = {o for o in others if not o.endswith("/")}
    dirs = tuple(o for o in others if o.endswith("/"))
    clobbered = []
    for path, blob in cloud.items():
        if path in files or dirs and path.startswith(dirs):
            local = _local_blob(root / path, object_format)
            if local is not None and local != blob:
                clobbered.append(path)
    return sorted(clobbered)


def _cloud_copy_has(root: Path, src: Path, target: str, path: str) -> bool:
    """Whether the captured workspace at src (HEAD target) has the file at path with the same bytes as root has,
    either untracked or committed."""
    local = root / path
    if local.is_symlink():
        untracked = src / "untracked.tar"
        if not untracked.exists():
            return False
        with tarfile.open(untracked) as tf:
            try:
                member = tf.getmember(path)
            except KeyError:
                return False
            return member.issym() and member.linkname == os.readlink(local)
    if not local.is_file():
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
                   pushed_changes: str | None = None, pushed_files: dict | None = None,
                   accept_config: bool = False, backup_dir: Path | None = None) -> dict:
    """Bring an existing local workspace up to a captured one (the pull direction).

    Uncommitted changes that are exactly what was pushed (pushed_changes, see changes_digest) are replaced by the
    cloud copy, which grew out of them. Newer ones are stashed with stash, else (like a diverged branch without
    force) it raises WorkspaceError and changes nothing. So does any check_existing problem: an archive member
    extraction refuses, or new or changed config files without accept_config.
    A non-git folder is extracted over root; local files changed since the push (see tree_conflicts) make it raise
    unless force (then they are copied to backup_dir first). In a git repo, every untracked or ignored local file
    the cloud copy replaces is copied to backup_dir first.
    Returns {"stash": the stash's message when one was made, else None, "replaced": the files copied to
    backup_dir}.
    """
    src, root = Path(src).resolve(), Path(root)
    problems = check_existing(meta, src, root, stash=stash, force=force, pushed_changes=pushed_changes,
                              pushed_files=pushed_files, accept_config=accept_config)
    if problems:
        raise WorkspaceError("; ".join(problems))
    if meta["kind"] == "tree":
        conflicts = set(tree_conflicts(src, root, pushed_files))
        root.mkdir(parents=True, exist_ok=True)
        replaced = _extract(src / "tree.tar", root, backup_dir=backup_dir, back_up=conflicts.__contains__)
        return {"stash": None, "replaced": replaced}
    target, branch = meta["head"], meta.get("branch")
    state = _local_state(root, branch=branch, target=target, pushed_changes=pushed_changes)

    local_head = _try_git(root, "rev-parse", "--verify", "-q", "HEAD")
    stashed = None
    if state["as_pushed"]:
        git(root, "reset", "-q", "--hard")
        for u in _untracked(root):
            if not _cloud_copy_has(root, src, target, u):  # left in place: extracting it again changes nothing
                (root / u).unlink()
    elif state["new_changes"]:
        # Untracked credential files never left this machine and the cloud copy won't touch them, and untracked
        # files the cloud copy brings back unchanged would only be in the way when the stash is popped: both stay.
        keep = [u for u in _split_z(git(root, "ls-files", "--others", "--exclude-standard", "-z"))
                if is_secret_name(u) or _cloud_copy_has(root, src, target, u)]
        stashed = f"ctransfer pull {int(time.time())}"
        git(root, "stash", "push", "-q", "-u", "-m", stashed, "--", ".", *[f":(exclude,literal){k}" for k in keep])
    # Untracked or ignored local files the checkout (or the uncommitted diff) would replace: git overwrites
    # ignored ones without asking, and the diff can't add a file that is already there. Saved, then moved aside.
    replaced = _checkout_clobbers(root, _cloud_tracked(root, src, target), _object_format(root))
    for path in replaced:
        if backup_dir is not None:
            (backup_dir / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / path, backup_dir / path, follow_symlinks=False)
        (root / path).unlink()
    if local_head:
        git(root, "update-ref", f"refs/ctransfer/backup/{int(time.time())}", local_head)
    if state["branch_exists"]:
        git(root, "checkout", "-q", branch)
        git(root, "reset", "-q", "--hard", target)  # fast-forward, or forced reset after the check above
    elif branch:
        git(root, "checkout", "-q", "-b", branch, target)
    else:
        git(root, "checkout", "-q", "--detach", target)
    replaced += _apply_dirty(root, src, backup_dir=backup_dir)
    return {"stash": stashed, "replaced": sorted(set(replaced))}
