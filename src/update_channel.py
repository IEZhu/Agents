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

An update switches branches only when the checked-out branch is the previous setting
(``AGENTS_AUTO_UPDATE_PREVIOUS_BRANCH``, recorded by ``set_branch``) and the branch is
the ``.env`` setting itself: a value exported in the environment that differs from
``.env`` never switches, it only steers the fast-forwards of a checkout already on that
branch. A checkout on any other branch, and one whose setting was edited by hand without
a previous record, is left alone (local development); ``channel set`` records the switch.
A switch to a branch that lacks commits of HEAD would go back to older code (a
downgrade, for example ``dev`` to ``main`` before a release): it needs the recorded
confirmation. Release merge commits do not count (``drops_commits``), so ``main`` to
``dev`` after a release is no downgrade.

Stdlib, ``python-dotenv``, ``src.env_file`` and ``src.executables`` only: ``src.self_update`` imports
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

from src import executables
from src import env_file as bindings

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
# `--no-write-fetch-head` (git 2.29): the downgrade check's fetch must not move FETCH_HEAD,
# which an updater reads right after its own fetch.
NO_WRITE_FETCH_HEAD = (2, 29)
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
    """Which branch switches an update may make; see the module docstring.

    *setting* is the ``.env`` setting (or the default): a target that differs from it
    came from the environment and switches nothing. None skips that check.
    """

    previous: str | None = None
    allow_downgrade: str | None = None
    setting: str | None = None

    def may_switch(self, current: str | None, target: str) -> bool:
        """Whether a checkout on *current* follows the channel and may move to *target*."""
        if not current or current in (target, "HEAD"):  # `rev-parse --abbrev-ref` names a detached HEAD "HEAD"
            return False
        if self.setting is not None and target != self.setting:
            return False
        return current == self.previous

    def downgrade_confirmed(self, branch: str, head: str) -> bool:
        """Whether the owner confirmed going back to *branch* from exactly *head*."""
        return bool(head) and self.allow_downgrade == f"{branch}@{head}"

    def reason(self, current: str | None, target: str) -> str:
        """Why a checkout on *current* is not switched to *target* (``may_switch`` is False)."""
        if self.setting is not None and target != self.setting:
            return (f"{BRANCH_KEY}={target} is exported and overrides .env ({self.setting}); an exported value "
                    f"never switches branches: save it with `channel set {target}`")
        if not current or current == "HEAD":
            return f"the checkout is on a detached HEAD, not {target}: local development"
        if self.previous is None:
            return (f"checked-out branch is {current}, not {target}, and no previous setting is recorded (a "
                    f"hand-edited .env): local development; to switch it, run `channel set {current}` and then "
                    f"`channel set {target}`")
        return (f"checked-out branch is {current}, not {target} or the previous setting {self.previous}: local "
                "development")


def policy_for(root, environ=None) -> Policy:
    """The switch policy of *root*'s ``.env`` under *environ* (default ``os.environ``, as a stdio server has it)."""
    return read(root, environ).policy


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
    setting: str = DEFAULTS[BRANCH_KEY]  # the branch .env names (or the default), whatever the environment says

    @property
    def overridden(self) -> bool:
        """An exported branch differs from the ``.env`` setting: it switches nothing."""
        return self.branch != self.setting

    @property
    def policy(self) -> Policy:
        return Policy(previous=self.previous, allow_downgrade=self.allow_downgrade, setting=self.setting)

    def override_note(self) -> str | None:
        if not self.overridden:
            return None
        return (f"{BRANCH_KEY}={self.branch} is exported in this shell and overrides .env ({self.setting}) for "
                "this command; an exported value never switches branches")


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
                    allow_downgrade=values.get(DOWNGRADE_KEY), sources=sources, revision=revision(stored),
                    setting=stored.get(BRANCH_KEY) or DEFAULTS[BRANCH_KEY])


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


def drops_commits(git, head: str, target: str) -> bool | None:
    """Whether moving *head* to *target* goes back: *head* has non-merge commits *target* lacks.

    The one downgrade test of a switch, for ``set_branch`` and every updater (*git* runs
    ``git <args>`` in the installation and returns the completed process). A release merges
    dev into main with a merge commit that never reaches dev, so a merge commit alone is not
    a downgrade: ``main`` to ``dev`` after a release switches without a confirmation. None
    when git cannot tell; callers treat that as a downgrade or a failed check.
    """
    if head == target:
        return False
    counted = git("rev-list", "--count", "--no-merges", f"{target}..{head}")
    try:
        return int(counted.stdout.strip()) > 0 if counted.returncode == 0 else None
    except ValueError:
        return None


def worktree_on(git, branch: str) -> str | None:
    """The worktree that has *branch* checked out, if any; a switch to it would fail at the checkout.

    Callers ask while the installation is on another branch, so any worktree listed with
    *branch* is another one. None also when git cannot list them.
    """
    listed = git("worktree", "list", "--porcelain")
    if listed.returncode:
        return None
    path = None
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == "branch refs/heads/" + branch:
            return path or "?"
    return None


def in_use_reason(branch: str, path: str) -> str:
    return (f"{branch} is checked out in another worktree ({path}); the switch waits until that worktree "
            "leaves it")


def _git_version(root) -> tuple:
    try:
        found = _git(root, "version", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ()
    match = re.search(r"(\d+)\.(\d+)", found.stdout)
    return (int(match[1]), int(match[2])) if match else ()


def is_downgrade(root, remote: str, branch: str, head: str, tip: str) -> bool | None:
    """``drops_commits`` for *head* and *remote*'s *tip* of *branch*; None when it cannot be checked.

    A tip that is not local yet is fetched without touching FETCH_HEAD (git 2.29 or later);
    with an older git the check is unavailable (None) rather than disturb an updater that
    reads FETCH_HEAD after its own fetch.
    """
    if head == tip:
        return False
    if _git(root, "cat-file", "-e", tip + "^{commit}").returncode:
        if _git_version(root) < NO_WRITE_FETCH_HEAD:
            return None
        fetched = _git(root, "fetch", "--quiet", "--no-write-fetch-head", remote, "refs/heads/" + branch)
        if fetched.returncode:
            raise ChannelError("unreachable", "fetch failed: " + fetched.stderr.strip()[-300:])
    dropped = drops_commits(lambda *args: _git(root, *args), head, tip)
    if dropped is None:
        raise ChannelError("unreachable", "cannot compare HEAD with " + tip[:12])
    return dropped


# What the writer puts into .env: a branch name, or a downgrade confirmation `<branch>@<40-hex>`.
def _writable(value) -> bool:
    if value is None:
        return True
    name, at, sha = value.partition("@")
    return plausible_branch(name) and (not at or _SHA.fullmatch(sha) is not None)


def rewrite_env(path: Path, updates: dict) -> None:
    """Set (a value) or remove (None) the keys of *updates* in the ``.env`` at *path*, atomically.

    Works on python-dotenv's bindings (``src.env_file``): the first binding of a key keeps its
    place, form and inline comment, later ones go, and every other binding (a multi-line
    quoted value among them) and the line endings stay as they are. Refuses
    (``ChannelError``) a value that is not a branch name or ``<branch>@<sha>``, so data never
    turns into a new assignment. A missing file is created private; an existing one keeps its
    permission bits, and a symlinked one its link (the target is replaced).
    """
    refused = [key for key, value in updates.items() if not _writable(value)]
    if refused:
        raise ChannelError("invalid", "refusing to write an unsafe value for " + ", ".join(refused))
    bindings.rewrite(path, updates, parse=bindings.dotenv_parse)


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
    return "the updaters leave this checkout alone: " + settings.policy.reason(current, settings.branch)


def summary(root, environ=None) -> dict:
    """Configured and checked-out branch without network access, for status pages."""
    settings = read(root, environ)
    current = checked_out(root)
    found = {"configured_branch": settings.branch, "source": settings.sources[BRANCH_KEY],
             "checked_out_branch": current, "action": plan(settings, current), "revision": settings.revision}
    if found["action"] == "local":
        found["reason"] = settings.policy.reason(current, settings.branch)
    if settings.overridden:
        found["note"] = settings.override_note()
    return found


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
        result["note"] = settings.override_note() or (
            f"{BRANCH_KEY} is exported in this shell with the value {env_file(root)} holds")
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
    The previous setting is recorded only when it is a plausible branch name; a downgrade
    that git cannot check (older than 2.29, tip not fetched) is saved with a warning, and
    the updaters check it again before they switch. Returns the saved setting with its new
    ``revision`` and any ``warnings``.
    """
    from src.file_lock import file_lock
    root = Path(root)
    if not valid_branch(root, branch):
        raise ChannelError("invalid", f"unsafe or invalid branch name: {branch!r}")
    settings = read(root, environ)
    current, head = checked_out(root), _head(root)
    stored = file_values(root)
    # The policy the next update has once this is saved: the setting this replaces becomes the previous one.
    policy = Policy(previous=_previous_after(stored, branch), setting=branch)
    switches = policy.may_switch(current, branch)
    tip, downgrade = None, False
    if verify:
        tip = remote_tip(root, settings.remote, branch)
        if switches and head:
            downgrade = is_downgrade(root, settings.remote, branch, head, tip)
    if downgrade and not allow_downgrade:
        raise ChannelError("downgrade", (
            f"{settings.remote}/{branch} lacks commits of HEAD {head[:12]} on {current or 'this checkout'}: "
            "switching would go back to older code, and state or .env changes that newer code made may not be "
            "read by it; confirm with --allow-downgrade"))
    confirmed = switches and head and allow_downgrade and downgrade is not False
    with file_lock(root / LOCK_NAME):
        stored = file_values(root)
        if expected_revision is not None and expected_revision != revision(stored):
            raise ChannelError("conflict", "the update channel changed since it was read; reload and try again")
        before = stored.get(BRANCH_KEY) or DEFAULTS[BRANCH_KEY]
        updates = {BRANCH_KEY: branch, DOWNGRADE_KEY: f"{branch}@{head}" if confirmed else None}
        if before != branch and plausible_branch(before):
            updates[PREVIOUS_KEY] = before
        rewrite_env(env_file(root), updates)
        saved = read(root, environ)
    result = {"branch": branch, "previous_setting": before, "checked_out": current, "remote_tip": tip,
              "downgrade": bool(downgrade), "revision": saved.revision, "next": _next_step(saved, current)}
    warnings = []
    if branch not in STANDARD_BRANCHES:
        warnings.append(f"{branch} is not main or dev: feature branches get rewritten and then stop "
                        "fast-forwarding")
    if downgrade is None:
        warnings.append(f"git could not check whether {branch} goes back to older code (git 2.29 or later "
                        "fetches it without disturbing a running update); the update refuses a downgrade "
                        "until it is confirmed with --allow-downgrade")
    if current != branch and not saved.policy.may_switch(current, branch) and not saved.overridden:
        warnings.append("the updaters leave this checkout alone: " + saved.policy.reason(current, branch))
    if switches:
        elsewhere = worktree_on(lambda *args: _git(root, *args), branch)
        if elsewhere:
            warnings.append(in_use_reason(branch, elsewhere))
    if saved.overridden:
        warnings.append(f"{BRANCH_KEY}={saved.branch} is exported in this shell and overrides the saved "
                        "value for commands started from it; it never switches branches")
    if warnings:
        result["warnings"] = warnings
    return result


def _previous_after(stored: dict, branch: str) -> str | None:
    """The previous setting once *branch* is saved over *stored*."""
    before = stored.get(BRANCH_KEY) or DEFAULTS[BRANCH_KEY]
    if before != branch and plausible_branch(before):
        return before
    return stored.get(PREVIOUS_KEY)


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
