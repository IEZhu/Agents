"""The branch an installation follows, its update channel (#381).

One setting per installation: ``AGENTS_AUTO_UPDATE_BRANCH`` in its ``.env``. Both
updaters read it: the stdio servers load ``.env`` into their environment, and the
shared service's controller (``python -m src.daemon update``, ``auto-update run``,
``auto-update status``, ``channel``) reads only the update keys (``KEYS``) from
``.env`` with ``dotenv_values``, without loading the rest of ``.env`` into its own
environment (#279). A variable exported in the controller's environment still
overrides the file for that one command; ``show`` names where the value came from.

``set_branch`` is the one writer, for the CLI (``python -m src.daemon channel set``,
or ``python -m src.update_channel set`` on an installation without the service) and
for the web UI, which passes the ``revision`` it showed for optimistic concurrency.
It records the previous setting (``AGENTS_AUTO_UPDATE_PREVIOUS_BRANCH``) and, for a
confirmed downgrade, the confirmation (``AGENTS_AUTO_UPDATE_ALLOW_DOWNGRADE``) bound
to the branch and the HEAD it was given for. Saving changes no code: the next update
(``update``, a scheduled run, or a stdio server's next idle start) applies it.

An update switches branches when the setting differs from the checked-out branch and
that branch is a channel branch (``main``, ``dev`` or the previous setting); a
checkout on any other branch is local development and stays untouched. A switch to a
branch that does not contain HEAD would go back to older code (a downgrade, for
example ``dev`` to ``main`` before a release): it needs the recorded confirmation.

Stdlib, ``python-dotenv`` and ``src.executables`` only: ``src.self_update`` imports
this module inside the stdio server's exclusive installation lease.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from src import executables

ENABLED_KEY = "AGENTS_AUTO_UPDATE"
REMOTE_KEY = "AGENTS_AUTO_UPDATE_REMOTE"
BRANCH_KEY = "AGENTS_AUTO_UPDATE_BRANCH"
PREVIOUS_KEY = "AGENTS_AUTO_UPDATE_PREVIOUS_BRANCH"
DOWNGRADE_KEY = "AGENTS_AUTO_UPDATE_ALLOW_DOWNGRADE"
KEYS = (ENABLED_KEY, REMOTE_KEY, BRANCH_KEY, PREVIOUS_KEY, DOWNGRADE_KEY)
DEFAULTS = {ENABLED_KEY: "1", REMOTE_KEY: "origin", BRANCH_KEY: "main"}
# The channels the project publishes: main takes releases, dev every merged pull request.
STANDARD_BRANCHES = ("main", "dev")
GIT_TIMEOUT = 60
INSTALL_ROOT = Path(__file__).resolve().parents[1]
LOCK_NAME = "data/.channel.lock"
# Branch names the setting accepts, on top of `git check-ref-format --branch`: written into .env
# unquoted, so no quote, space, `#`, `$` or `@` (the downgrade confirmation is `<branch>@<sha>`).
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_SHA = re.compile(r"[0-9a-f]{40}")
SOURCE_ENVIRONMENT, SOURCE_FILE, SOURCE_DEFAULT = "environment", ".env", "default"


class ChannelError(RuntimeError):
    """A refused change; ``code`` is one of invalid, unknown_branch, unreachable, downgrade, conflict."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def is_safe_arg(value: str | None) -> bool:
    """A non-empty token git cannot mistake for an option (as ``self_update._is_safe_arg``)."""
    return bool(value) and not value.startswith("-")


def plausible_branch(name: str | None) -> bool:
    """The local part of the branch name check: safe for git's command line and for ``.env``."""
    return bool(name) and is_safe_arg(name) and _NAME.fullmatch(name) is not None


@dataclass(frozen=True)
class Policy:
    """Which branch switches an update may make; see the module docstring."""

    previous: str | None = None
    allow_downgrade: str | None = None

    def may_switch(self, current: str | None, target: str) -> bool:
        """Whether a checkout on *current* follows the channel and may move to *target*."""
        if not current or current in (target, "HEAD"):  # `rev-parse --abbrev-ref` names a detached HEAD "HEAD"
            return False
        return current in STANDARD_BRANCHES or current == self.previous

    def downgrade_confirmed(self, branch: str, head: str) -> bool:
        """Whether the owner confirmed going back to *branch* from exactly *head*."""
        return bool(head) and self.allow_downgrade == f"{branch}@{head}"


def policy_from(values) -> Policy:
    """The switch policy of a mapping holding the update keys (``os.environ`` of a stdio server)."""
    return Policy(previous=values.get(PREVIOUS_KEY) or None, allow_downgrade=values.get(DOWNGRADE_KEY) or None)


@dataclass(frozen=True)
class Settings:
    """The update keys in effect for one command, and where each came from."""

    enabled: bool
    remote: str
    branch: str
    previous: str | None
    allow_downgrade: str | None
    sources: dict = field(default_factory=dict)
    revision: str = ""

    @property
    def policy(self) -> Policy:
        return Policy(previous=self.previous, allow_downgrade=self.allow_downgrade)


def env_file(root) -> Path:
    return Path(root) / ".env"


def file_values(root) -> dict:
    """The update keys set in the installation's ``.env``; nothing else of it is read into the result."""
    path = env_file(root)
    if not path.is_file():
        return {}
    from dotenv import dotenv_values
    values = dotenv_values(path)
    return {key: values[key] for key in KEYS if values.get(key)}


def revision(values: dict) -> str:
    """A short digest of the channel keys in ``.env``, for a writer that must not overwrite a newer value."""
    channel = {key: values.get(key) for key in (REMOTE_KEY, BRANCH_KEY, PREVIOUS_KEY, DOWNGRADE_KEY)}
    return hashlib.sha256(json.dumps(channel, sort_keys=True).encode()).hexdigest()[:16]


def read(root, environ=None) -> Settings:
    """The settings the controller uses for *root*: *environ* (default ``os.environ``) over ``.env``.

    An empty value counts as unset. ``.env`` is read with ``dotenv_values``; nothing is
    added to *environ*.
    """
    environ = os.environ if environ is None else environ
    stored = file_values(root)
    values, sources = {}, {}
    for key in KEYS:
        if environ.get(key):
            values[key], sources[key] = environ[key], SOURCE_ENVIRONMENT
        elif stored.get(key):
            values[key], sources[key] = stored[key], SOURCE_FILE
        elif key in DEFAULTS:
            values[key], sources[key] = DEFAULTS[key], SOURCE_DEFAULT
    return Settings(enabled=values[ENABLED_KEY].lower() in ("1", "true"), remote=values[REMOTE_KEY],
                    branch=values[BRANCH_KEY], previous=values.get(PREVIOUS_KEY),
                    allow_downgrade=values.get(DOWNGRADE_KEY), sources=sources, revision=revision(stored))


def _git(root, *args, timeout=GIT_TIMEOUT):
    """Git on the installation without the user's configuration (#279) and without prompts."""
    return subprocess.run([executables.path("git"), *args], cwd=str(root), capture_output=True, text=True,
                          timeout=timeout, check=False, stdin=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                          env={**executables.git_environment(root), "GIT_TERMINAL_PROMPT": "0",
                               "GCM_INTERACTIVE": "false"})


def valid_branch(root, name: str | None) -> bool:
    """``plausible_branch`` and ``git check-ref-format --branch`` naming the same branch."""
    if not plausible_branch(name):
        return False
    try:
        checked = _git(root, "check-ref-format", "--branch", name)
    except (OSError, subprocess.SubprocessError):
        return False
    return checked.returncode == 0 and checked.stdout.strip() == name


def checked_out(root) -> str | None:
    """The checked-out branch; None on a detached HEAD or when git cannot tell."""
    try:
        found = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return found.stdout.strip() or None if found.returncode == 0 else None


def _head(root) -> str | None:
    try:
        found = _git(root, "rev-parse", "--verify", "--quiet", "HEAD", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return found.stdout.strip() or None if found.returncode == 0 else None


def remote_tip(root, remote: str, branch: str) -> str:
    """The commit *branch* points at on *remote*; ``ChannelError`` when it does not exist or cannot be read."""
    if not (is_safe_arg(remote) and plausible_branch(branch)):
        raise ChannelError("invalid", f"unsafe remote or branch name: {remote!r} {branch!r}")
    try:
        listed = _git(root, "ls-remote", "--quiet", "--exit-code", remote, "refs/heads/" + branch)
    except (OSError, subprocess.SubprocessError) as error:
        raise ChannelError("unreachable", f"cannot read {remote}: {error}") from error
    if listed.returncode == 2:
        raise ChannelError("unknown_branch", f"branch {branch!r} does not exist on {remote}")
    if listed.returncode:
        raise ChannelError("unreachable", f"cannot read {remote}: "
                                          + (listed.stderr.strip()[-300:] or f"exit code {listed.returncode}"))
    tip = listed.stdout.split()[0] if listed.stdout.split() else ""
    if not _SHA.fullmatch(tip):
        raise ChannelError("unreachable", f"unexpected answer from {remote}: {listed.stdout[:200]!r}")
    return tip


def is_downgrade(root, remote: str, branch: str, head: str, tip: str) -> bool:
    """Whether moving *head* to *tip* goes back: *head* is not an ancestor of *tip*. Fetches when needed."""
    if head == tip:
        return False
    if _git(root, "cat-file", "-e", tip + "^{commit}").returncode:
        fetched = _git(root, "fetch", "--quiet", remote, branch)
        if fetched.returncode:
            raise ChannelError("unreachable", "fetch failed: " + fetched.stderr.strip()[-300:])
    ancestor = _git(root, "merge-base", "--is-ancestor", head, tip)
    if ancestor.returncode not in (0, 1):
        raise ChannelError("unreachable", "cannot compare HEAD with " + tip[:12] + ": " + ancestor.stderr.strip()[-300:])
    return ancestor.returncode == 1


def _assignment(key):
    return re.compile(r"^(?P<lead>\s*(?:export\s+)?" + re.escape(key) +
                      r"\s*=[ \t]*)(?P<value>'[^']*'|\"[^\"]*\"|[^\s#]*)(?P<rest>.*)$", re.S)


def rewrite_env(path: Path, updates: dict) -> None:
    """Set (a value) or remove (None) the keys of *updates* in the ``.env`` at *path*, atomically.

    The first assignment of a key keeps its place and its inline comment; later ones go.
    Other lines stay as they are. A missing file is created private; an existing one keeps
    its permission bits, and a symlinked one its link (the target is replaced).
    """
    path = Path(os.path.realpath(path))
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    patterns = {key: _assignment(key) for key in updates}
    seen, kept = set(), []
    for line in lines:
        key = next((key for key, pattern in patterns.items() if pattern.match(line)), None)
        if key is None:
            kept.append(line)
            continue
        if key in seen or updates[key] is None:
            continue
        seen.add(key)
        match = patterns[key].match(line)
        kept.append(match["lead"] + updates[key] + match["rest"])
    if kept and not kept[-1].endswith("\n"):
        kept[-1] += "\n"
    kept += [f"{key}={value}\n" for key, value in updates.items() if value is not None and key not in seen]
    mode = path.stat().st_mode & 0o7777 if path.exists() else 0o600
    descriptor, staging = tempfile.mkstemp(prefix=".env.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("".join(kept))
        os.chmod(staging, mode)
        os.replace(staging, path)
    except BaseException:
        Path(staging).unlink(missing_ok=True)
        raise


def plan(settings: Settings, current: str | None) -> str:
    """What the next update does with this checkout: follow, switch, or leave it (local development)."""
    if current == settings.branch:
        return "follow"
    if settings.policy.may_switch(current, settings.branch):
        return "switch"
    return "local"


def _next_step(settings, current):
    action = plan(settings, current)
    if action == "follow":
        return f"the updaters fast-forward {settings.branch}"
    if action == "switch":
        return (f"the next update switches from {current} to {settings.branch} "
                "(`python -m src.daemon update`, a scheduled run, or a stdio server's next idle start)")
    return (f"the checkout is on {current or 'a detached HEAD'}, not a channel branch: the updaters leave it "
            "alone (local development)")


def summary(root, environ=None) -> dict:
    """Configured and checked-out branch without network access, for status pages."""
    settings = read(root, environ)
    current = checked_out(root)
    return {"configured_branch": settings.branch, "source": settings.sources[BRANCH_KEY],
            "checked_out_branch": current, "action": plan(settings, current), "revision": settings.revision}


def show(root, *, environ=None, state_dir=None, network=True) -> dict:
    """Everything ``channel show`` prints: the setting and its source, the checkout, HEAD, the remote tip
    (with *network*) and the last update results of the service (*state_dir*) and of the stdio updater."""
    root = Path(root)
    settings = read(root, environ)
    current = checked_out(root)
    result = {"installation": str(root), "branch": settings.branch, "source": settings.sources[BRANCH_KEY],
              "remote": settings.remote, "checked_out": current, "head": _head(root),
              "previous": settings.previous, "downgrade_confirmed": settings.allow_downgrade,
              "next": _next_step(settings, current), "revision": settings.revision}
    if settings.sources[BRANCH_KEY] == SOURCE_ENVIRONMENT:
        result["note"] = f"{BRANCH_KEY} is exported in this shell and overrides {env_file(root)} for this command"
    if network:
        try:
            result["remote_tip"] = remote_tip(root, settings.remote, settings.branch)
        except ChannelError as error:
            result["remote_tip"], result["remote_error"] = None, str(error)
    last = {}
    if state_dir is not None:
        last["service"] = _read_json(Path(state_dir) / "auto-update.json")
    last["stdio"] = _read_json(root / "data" / ".last_update.json")
    result["last_update"] = last
    return result


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def set_branch(root, branch: str, *, allow_downgrade: bool = False, expected_revision: str | None = None,
               environ=None, verify: bool = True) -> dict:
    """Save *branch* as the installation's update branch; the one writer of the setting.

    Refuses (``ChannelError``) an unsafe or invalid name, a branch the remote does not have
    (with *verify*), a downgrade without *allow_downgrade*, and, when *expected_revision* is
    given, a setting that changed since that revision was read. The check against the remote
    runs before the lock; the revision check and the write run under ``data/.channel.lock``.
    Returns the saved setting with its new ``revision`` and any ``warnings``.
    """
    from src.file_lock import file_lock
    root = Path(root)
    if not valid_branch(root, branch):
        raise ChannelError("invalid", f"unsafe or invalid branch name: {branch!r}")
    settings = read(root, environ)
    current, head = checked_out(root), _head(root)
    # The checkout the next update would switch: a channel branch, counting the setting this replaces.
    switches = Policy(previous=file_values(root).get(BRANCH_KEY) or DEFAULTS[BRANCH_KEY]).may_switch(current, branch)
    tip, downgrade = None, False
    if verify:
        tip = remote_tip(root, settings.remote, branch)
        if switches and head:
            downgrade = is_downgrade(root, settings.remote, branch, head, tip)
    if downgrade and not allow_downgrade:
        raise ChannelError("downgrade", (
            f"{settings.remote}/{branch} does not contain HEAD {head[:12]} of {current or 'this checkout'}: "
            "switching would go back to older code, and state or .env changes that newer code made may not be "
            "read by it; confirm with --allow-downgrade"))
    with file_lock(root / LOCK_NAME):
        stored = file_values(root)
        if expected_revision is not None and expected_revision != revision(stored):
            raise ChannelError("conflict", "the update channel changed since it was read; reload and try again")
        before = stored.get(BRANCH_KEY) or DEFAULTS[BRANCH_KEY]
        updates = {BRANCH_KEY: branch, DOWNGRADE_KEY: f"{branch}@{head}" if downgrade else None}
        if before != branch:
            updates[PREVIOUS_KEY] = before
        rewrite_env(env_file(root), updates)
        saved = read(root, environ)
    result = {"branch": branch, "previous_setting": before, "checked_out": current, "remote_tip": tip,
              "downgrade": downgrade, "revision": saved.revision, "next": _next_step(saved, current)}
    warnings = []
    if branch not in STANDARD_BRANCHES:
        warnings.append(f"{branch} is not main or dev: feature branches get rewritten and then stop "
                        "fast-forwarding")
    if current != branch and not switches:
        warnings.append(f"this checkout is on {current or 'a detached HEAD'}, not a channel branch: the updaters "
                        "leave it alone until main, dev or the previous setting is checked out")
    if saved.branch != branch:
        warnings.append(f"{BRANCH_KEY}={saved.branch} is exported in this shell and overrides the saved "
                        "value for commands started from it")
    if warnings:
        result["warnings"] = warnings
    return result


def main(argv=None) -> int:
    """``python -m src.update_channel show|set`` for this installation, with or without the service."""
    parser = argparse.ArgumentParser(description="The branch this Agents-Core installation follows (#381)")
    parser.add_argument("--root", type=Path, default=INSTALL_ROOT, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("show", help="configured and checked-out branch, HEAD, remote tip, last update")
    chosen = commands.add_parser("set", help="follow another branch from the next update on")
    chosen.add_argument("branch")
    chosen.add_argument("--allow-downgrade", action="store_true",
                        help="confirm a switch to a branch that does not contain the installed code")
    args = parser.parse_args(argv)
    try:
        if args.action == "show":
            result = show(args.root)
        else:
            result = set_branch(args.root, args.branch, allow_downgrade=args.allow_downgrade)
    except ChannelError as error:
        print(json.dumps({"state": "refused", "code": error.code, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
