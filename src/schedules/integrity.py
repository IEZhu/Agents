"""The post-session check of a one-off repository's ``.git`` (#347).

docs/autodev-sandbox.md, open item "Moving a parent of a protected path": the sandbox denies the
session writes to ``.git/config`` and ``.git/hooks`` of its one-off repository, but whether
sandbox-runtime also denies renaming ``.git`` itself (and changing the files under the new name)
is not documented. Until the owner's live check settles it, the runner checks after each session,
outside the sandbox and without git, that ``.git`` is the same directory and its configuration,
hooks and redirect files are unchanged. Any change means a sandbox write path the probes did not
cover: the run imports nothing more, starts no further stage, ends ``refused`` with `reason`,
notifies the owner and turns the schedule off (#347 open question Q-git-integrity, default
adopted). The runner never runs git in a one-off repository afterwards (`src.schedules.repos`), so
a changed ``.git/config`` cannot execute in the runner.

A snapshot is plain JSON data (the run record may keep it). `git_snapshot` is taken after `repos.TrustedRepository.create_session_repository` and before the
session starts; `git_changes` compares it with a second snapshot after the session ended. A
snapshot records, through one descriptor of ``.git`` opened without following a link (so a
directory swapped in meanwhile cannot mix two views):

* ``.git`` itself: a directory (device, inode and birth time), or what replaced it: a file (a
  ``gitdir:`` redirector, its SHA-256), a symbolic link, nothing;
* ``config``: kind, mode, size and SHA-256 (a link is recorded as a link, never followed);
* ``hooks``: kind, and each entry's name, kind, mode, size and SHA-256 (at most `MAX_HOOKS`);
* `WATCHED`: ``info/grafts``, ``objects/info/alternates`` and ``commondir``, which redirect what
  git reads, each with the directories on its way (a directory replaced by a link counts).

Files are read without following links and without blocking, at most `MAX_HASHED` bytes each.
Normal work (commits, branches, bundles, the index) changes none of these.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from typing import Any

WATCHED = ("info/grafts", "objects/info/alternates", "commondir")
MAX_HOOKS = 256
MAX_HASHED = 16 * 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "link"
    return "other"


def _digest(descriptor: int) -> dict:
    """SHA-256 and size of an open regular file, at most `MAX_HASHED` bytes read."""
    digest, size = hashlib.sha256(), 0
    while chunk := os.read(descriptor, 1024 * 1024):
        size += len(chunk)
        if size > MAX_HASHED:
            return {"too_large": True}
        digest.update(chunk)
    return {"sha256": digest.hexdigest()}


def _entry(directory: int, name: str) -> dict:
    """What ``name`` in the open ``directory`` is, never through a link."""
    try:
        status = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return {"kind": "missing"}
    except OSError as error:
        return {"kind": "unreadable", "errno": error.errno}
    found: dict[str, Any] = {"kind": _kind(status.st_mode), "mode": stat.S_IMODE(status.st_mode)}
    if found["kind"] == "file":
        found["size"] = status.st_size
        try:
            descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory)
        except OSError as error:
            return {**found, "unreadable": error.errno}
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return {**found, "kind": "other"}
            found.update(_digest(descriptor))
        finally:
            os.close(descriptor)
    elif found["kind"] == "link":
        try:
            found["target"] = hashlib.sha256(os.fsencode(os.readlink(name, dir_fd=directory))).hexdigest()
        except OSError as error:
            found["unreadable"] = error.errno
    elif found["kind"] == "directory":
        found["inode"] = [status.st_dev, status.st_ino]
    return found


def _lookup(directory: int, relative: str) -> dict:
    """The entry at ``relative`` below the open ``directory``: each directory on the way is opened
    without following a link; a missing one makes the entry missing, and one that is something
    else (a link, a file) ends the walk as ``unreachable``, which is recorded."""
    parts = relative.split("/")
    current, opened = directory, []
    try:
        for index, part in enumerate(parts[:-1]):
            entry = _entry(current, part)
            if entry["kind"] == "missing":
                return {"kind": "missing"}
            if entry["kind"] != "directory":
                return {"kind": "unreachable", "at": "/".join(parts[:index + 1]), "found": entry}
            try:
                current = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            except OSError as error:
                return {"kind": "unreachable", "at": "/".join(parts[:index + 1]), "errno": error.errno}
            opened.append(current)
        return _entry(current, parts[-1])
    finally:
        for descriptor in opened:
            os.close(descriptor)


def _hooks(directory: int) -> dict:
    entry = _entry(directory, "hooks")
    if entry["kind"] != "directory":
        return entry
    try:
        descriptor = os.open("hooks", _DIRECTORY_FLAGS, dir_fd=directory)
    except OSError as error:
        return {"kind": "unreadable", "errno": error.errno}
    try:
        names = sorted(os.listdir(descriptor))
        entries = {name: _entry(descriptor, name) for name in names[:MAX_HOOKS]}
    finally:
        os.close(descriptor)
    return {"kind": "directory", "entries": entries, "count": len(names)}


def git_snapshot(repository: str | os.PathLike[str]) -> dict:
    """The state of ``<repository>/.git`` that `git_changes` compares (see the module docstring)."""
    root = Path(repository)
    try:
        descriptor = os.open(root / ".git", _DIRECTORY_FLAGS)
    except OSError:
        parent = os.open(root, _DIRECTORY_FLAGS)
        try:
            entry = _entry(parent, ".git")
        finally:
            os.close(parent)
        # A directory that cannot be opened is no state to compare against.
        return {"git": {"kind": "unopenable"} if entry["kind"] == "directory" else entry}
    try:
        status = os.fstat(descriptor)
        git = {"kind": "directory", "inode": [status.st_dev, status.st_ino],
               "birth": getattr(status, "st_birthtime", None), "mode": stat.S_IMODE(status.st_mode)}
        return {"git": git, "config": _entry(descriptor, "config"), "hooks": _hooks(descriptor),
                "watched": {name: _lookup(descriptor, name) for name in WATCHED}}
    finally:
        os.close(descriptor)


def git_changes(before: dict, after: dict) -> list[str]:
    """What changed in ``.git`` between two snapshots, one short item each; empty when nothing did."""
    old, new = before["git"], after["git"]
    if old["kind"] != new["kind"]:
        what = {"file": "a file (a gitdir redirector)", "link": "a symbolic link", "missing": "gone"}
        return [f".git is {what.get(new['kind'], new['kind'])} now"]
    if old["kind"] != "directory":
        return [] if old == new else [".git changed"]
    changes = []
    if (old["inode"], old["birth"]) != (new["inode"], new["birth"]):
        changes.append(".git is another directory (it was replaced)")
    if before["config"] != after["config"]:
        changes.append(".git/config changed")
    changes += _hook_changes(before["hooks"], after["hooks"])
    for name in WATCHED:
        was, now = before["watched"][name], after["watched"][name]
        if was == now:
            continue
        if now["kind"] == "unreachable":
            changes.append(f".git/{now['at']} is not a directory now (on the way to {name})")
        else:
            state = "appeared" if was["kind"] == "missing" else "is gone" if now["kind"] == "missing" else "changed"
            changes.append(f".git/{name} {state}")
    return changes


def _hook_changes(old: dict, new: dict) -> list[str]:
    if old == new:
        return []
    if old["kind"] != "directory" or new["kind"] != "directory":
        return [f".git/hooks changed ({old['kind']} -> {new['kind']})"]
    before, after = old["entries"], new["entries"]
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(name for name in set(before) & set(after) if before[name] != after[name])
    parts = [f"{label} {', '.join(names[:5])}" for label, names in (("added", added), ("removed", removed),
                                                                      ("changed", changed)) if names]
    if new["count"] != len(after) or old["count"] != len(before):
        parts.append(f"{new['count']} entries")
    return [".git/hooks changed: " + "; ".join(parts or ["entries differ"])]


def reason(changes: list[str]) -> str:
    """The run record's reason for a refused run (#347)."""
    return "sandbox: the one-off repository's .git changed (" + "; ".join(changes[:10]) + ")"
