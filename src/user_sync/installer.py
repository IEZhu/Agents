"""The installers' sync step and unattended setup from the environment (#171).

``scripts/init_repo.sh`` and ``scripts/init_repo.bat`` run ``python -m src.user_sync installer``
after the client configuration, with ``--yes`` when they run with ``--yes`` (``install.sh`` always
passes it), and ``installer --summary`` in their final summary. The step never fails setup, and by
itself it changes nothing: no settings, no key, nothing in the library's ``.git``.

* Sync already set up: it says so and asks nothing.
* ``AGENTS_USER_SYNC_REPO`` (``OWNER/NAME`` on GitHub) or ``AGENTS_USER_SYNC_REMOTE`` (an SSH URL)
  set: ``from_env`` sets sync up without a question, also under ``--yes``.
* git 2.32 or newer or ``ssh-keygen`` missing, or a ``.git`` in the library that sync did not
  create: it says why and asks nothing. That ``.git`` is only read, never changed.
* ``--yes`` (or ``AGENTS_ASSUME_YES``) or no terminal: it asks nothing.
* Otherwise it asks once. Yes opens the daemon's settings page, whose Sync page sets sync up, on
  macOS when the daemon answers and serves that page (``settings_page``); otherwise it starts the
  terminal wizard (``src.user_sync.wizard``). After the wizard started sync on a machine without the
  daemon, it offers the scheduled run (``src.user_sync.schedule``), default yes.

The variables count only as the command's process environment gives them, never from the
installation's ``.env``: otherwise every update would continue a setup. The command line takes
them before it reads ``.env`` (``take_environment``), and it takes ``AGENTS_GITHUB_TOKEN`` out of
its own environment for every command, so that no child process it starts inherits the token
(``forget_dotenv`` drops what ``.env`` added). Only the step and ``setup --from-env`` receive the
token.

``from_env`` (also ``python -m src.user_sync setup --from-env``) takes the commit identity from
``AGENTS_USER_SYNC_NAME`` and ``AGENTS_USER_SYNC_EMAIL`` and the machine label from
``AGENTS_USER_SYNC_LABEL``. For a repository on GitHub the token is checked with GitHub and kept in
the OS secret store through ``GitHubAccount.complete_sign_in``, and the deploy key is added through
the API; without a token or a connected account, the public key is printed to add by hand. Setup
then checks access and privacy, shows the preview and starts sync, except that a join with
conflicts stops at "confirmation needed" with the commands to review and confirm it. Once sync has
started without the daemon, it enables the scheduled run. Run again with the same variables, it
continues a setup that stopped, and it changes nothing once sync has started. A setup it made
itself that never started may be replaced by a run with another remote (a marker in the sync state
directory records it); every other setup, and a ``.git`` the library holds while sync has no
settings here, stays as it is, with the steps that clear the way.

Its own text is ASCII, since a Windows pipe may not encode anything else; names it repeats from the
library or the remote may not be, so the command line replaces what its output cannot encode.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from typing import Callable, Iterable, Mapping, MutableMapping
from urllib.request import ProxyHandler, Request, build_opener

from src.user_sync import gitcmd, keys
from src.user_sync.engine import (MANAGED_KEY, Settings, SyncError, Syncer, hosted_repository,
                                  installation_root, validate_identity)
from src.user_sync.github import GitHubError
from src.user_sync.gitcmd import Remote, RemoteError, parse_remote

REPO = "AGENTS_USER_SYNC_REPO"
REMOTE = "AGENTS_USER_SYNC_REMOTE"
NAME = "AGENTS_USER_SYNC_NAME"
EMAIL = "AGENTS_USER_SYNC_EMAIL"
LABEL = "AGENTS_USER_SYNC_LABEL"
TOKEN = "AGENTS_GITHUB_TOKEN"
ASSUME_YES = "AGENTS_ASSUME_YES"
VARIABLES = (REPO, REMOTE, NAME, EMAIL, LABEL)
PROMPT = "  Set up sync between your machines now? [y/N]: "
MARKER = "user-sync-from-env.json"  # in the sync state directory: a setup that from_env made
SYNC_PAGE = b"/ui/api/sync"  # the settings page's script calls it only where the Sync page exists
# Loopback calls never go through a proxy from http_proxy: the daemon's bearer token stays here.
_LOOPBACK = build_opener(ProxyHandler({}))
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}")
_STORES = {"macos-keychain": "the macOS Keychain", "secret-service": "the Secret Service",
           "windows-credential-manager": "the Windows Credential Manager",
           "file": "a private file in the sync state directory"}

Ask = Callable[[str], str]
Say = Callable[[str], None]


# --- the environment ----------------------------------------------------------------------


def take_environment(environ: MutableMapping[str, str] | None = None) -> dict:
    """The sync variables and ``AGENTS_ASSUME_YES`` as the process environment holds them.

    Call it before ``.env`` is read. ``AGENTS_GITHUB_TOKEN`` is taken out of the environment, so
    that no child process inherits it, and returned with the rest under its name.
    """
    environ = os.environ if environ is None else environ
    values = {name: environ[name] for name in (*VARIABLES, ASSUME_YES) if isinstance(environ.get(name), str)}
    token = environ.pop(TOKEN, None)
    if isinstance(token, str):
        values[TOKEN] = token
    return values


def forget_dotenv(values: Mapping[str, str], environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Remove the sync variables and the token that ``.env`` added after ``take_environment``.

    Returns their names, to say that ``.env`` does not count for them.
    """
    environ = os.environ if environ is None else environ
    ignored = []
    for name in (*VARIABLES, TOKEN):
        if name in environ and (name == TOKEN or name not in values):
            if (environ.pop(name) or "").strip():
                ignored.append(name)
    return ignored


def _value(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if not isinstance(value, str):
        return None
    return value.strip() or None


def requested(environ: Mapping[str, str]) -> bool:
    """Whether the environment asks for setup: ``AGENTS_USER_SYNC_REPO`` or ``_REMOTE`` is set."""
    return bool(_value(environ, REPO) or _value(environ, REMOTE))


def assume_yes(environ: Mapping[str, str]) -> bool:
    """``AGENTS_ASSUME_YES`` as the installers read it: ``1``, ``true`` or ``yes``."""
    return environ.get(ASSUME_YES) in ("1", "true", "yes")


# --- this machine -------------------------------------------------------------------------


def _python() -> str:
    """The interpreter as typed in the installation root: ``.venv/bin/python`` for the installers."""
    executable = Path(os.path.abspath(sys.executable))  # not resolved: the venv's link is the point
    try:
        return str(executable.relative_to(installation_root()))
    except ValueError:
        return str(executable)


def _windows() -> bool:
    return os.name == "nt"


def _quote(word: str) -> str:
    if _windows():
        return f'"{word}"' if any(ch in word for ch in ' &()^%!;,=') else word
    return shlex.quote(word)


def command(*arguments: str) -> str:
    """How to run ``<python> -m <arguments>``, which works only in the installation's root.

    ``cd <root> && …`` on macOS and Linux; on Windows, where cmd and PowerShell differ in how
    they chain commands, ``in <root>, run …``.
    """
    words = " ".join(_quote(word) for word in (_python(), "-m", *arguments))
    if _windows():
        return f"in {installation_root()}, run {words}"
    return f"cd {_quote(str(installation_root()))} && {words}"


def daemon_directory() -> Path | None:
    """The state directory of the macOS daemon installed for this installation, or None.

    ``python -m src.daemon install`` records it in ``data/.shared-service.json``.
    """
    if sys.platform != "darwin":
        return None
    try:
        marker = json.loads((installation_root() / "data" / ".shared-service.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    directory = marker.get("directory") if isinstance(marker, dict) else None
    if isinstance(directory, str) and (Path(directory) / "service.json").is_file():
        return Path(directory)
    return None


def settings_page(directory: Path) -> str | None:
    """A one-use link to the daemon's settings page, or None unless that page can set sync up now.

    The daemon must answer ``/health`` as ready, and the page it serves must be one with the Sync
    page: its script calls ``/ui/api/sync``. The link is what ``python -m src.daemon flows-ui``
    asks for. Never raises: anything else means the terminal wizard.
    """
    try:
        from src.daemon.control import Controller
        controller = Controller(directory)
        port = controller.config.get("port")
        if not isinstance(port, int):
            return None
        health = controller.request("/health", timeout=3)
        if not isinstance(health, dict) or health.get("state") != "ready":
            return None
        with _LOOPBACK.open(Request(f"http://127.0.0.1:{port}/ui"), timeout=5) as response:
            page = response.read(8 * 1024 * 1024)
        if SYNC_PAGE not in page:
            return None
        answer = controller.request("/admin/ui/code", method="POST", timeout=5)
        url = answer.get("url") if isinstance(answer, dict) else None
        return url if isinstance(url, str) and url.startswith(f"http://127.0.0.1:{port}/") else None
    except Exception:  # not running, not answering, an older page: the terminal wizard instead
        return None


def missing_tools() -> list[str]:
    """What sync needs and this machine lacks: git 2.32 or newer (Git for Windows), ``ssh-keygen``."""
    missing = []
    wanted = ".".join(map(str, gitcmd.MIN_GIT_VERSION))
    version = gitcmd.git_version()
    if version is None:
        missing.append("Git for Windows (https://git-scm.com/download/win)" if os.name == "nt"
                       else f"git {wanted} or newer")
    elif version < gitcmd.MIN_GIT_VERSION:
        missing.append(f"git {wanted} or newer (this machine has {'.'.join(map(str, version))})")
    if shutil.which("ssh-keygen") is None:
        missing.append("ssh-keygen (the OpenSSH Client: Settings > System > Optional features)"
                       if os.name == "nt" else "ssh-keygen (OpenSSH)")
    return missing


def foreign_git(library: Path) -> bool:
    """Whether the library holds a ``.git`` that sync did not create. It is read, never changed."""
    dot_git = Path(library) / ".git"
    if not dot_git.exists() and not dot_git.is_symlink():
        return False
    if dot_git.is_symlink() or not dot_git.is_dir():
        return True
    environment = gitcmd.clean_environment()
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    try:
        # --file reads that one file without includes, takes no lock and writes nothing.
        result = subprocess.run(["git", "config", "--file", str(dot_git / "config"), "--get", MANAGED_KEY],
                                capture_output=True, text=True, timeout=gitcmd.LOCAL_TIMEOUT,
                                env=environment, stdin=subprocess.DEVNULL, **gitcmd.no_window())
    except (OSError, subprocess.SubprocessError):
        return True  # it cannot be told apart: leave it alone
    return result.stdout.strip() != "true"


def blocker(syncer: Syncer) -> SyncError | None:
    """Why sync cannot be set up on this machine as it is, or None."""
    missing = missing_tools()
    if missing:
        reason = "ssh" if all(item.startswith("ssh-keygen") for item in missing) else "git_too_old"
        return SyncError(reason, "it needs " + " and ".join(missing))
    if foreign_git(syncer.library):
        return SyncError("foreign_git", f"{syncer.library / '.git'} was not created by Agents-Core sync, and "
                                        "sync leaves it as it is; move it away to set up sync")
    return None


def background() -> dict:
    """How this machine syncs in the background: ``by`` is ``daemon``, ``schedule`` or None."""
    if daemon_directory() is not None:
        return {"by": "daemon"}
    try:
        from src.user_sync import schedule
        found = schedule.status()
    except Exception as error:  # the scheduler could not be asked: say so, change nothing
        return {"by": None, "error": _reason(error)}
    if found.get("scheduled"):
        return {"by": "schedule", "minutes": found.get("interval_minutes")}
    return {"by": None}


def enable_background(syncer: Syncer, settings: Settings) -> dict:
    """Schedule ``run`` every ``fetch_minutes`` for this machine, as ``schedule enable`` does (#168)."""
    from src.user_sync import schedule
    return schedule.enable(settings.fetch_minutes, state_dir=syncer.state_dir, library=syncer.library)


def _background_off(minutes: int) -> str:
    return (f"Without background sync, this machine sends and receives changes only in the cycle that follows "
            f"each save made through its stdio servers. To sync every {minutes} minutes: "
            + command("src.user_sync", "schedule", "enable"))


def _turn_background_on(syncer: Syncer, settings: Settings, say: Say) -> str | None:
    """Enable the scheduled run and say so; the backend's name, or None when it failed."""
    try:
        enabled = enable_background(syncer, settings)
    except Exception as error:  # sync works without it; the owner can turn it on later
        say(f"  Background sync could not be scheduled ({_reason(error)}). "
            + _background_off(settings.fetch_minutes))
        return None
    say(f"  Background sync is on: every {settings.fetch_minutes} minutes ({enabled.get('backend', 'scheduled')}). "
        "To turn it off: " + command("src.user_sync", "schedule", "disable"))
    return "schedule"


# --- a setup that from_env made -------------------------------------------------------------


def _marker(syncer: Syncer) -> Path:
    return syncer.state_dir / MARKER


def _mark(syncer: Syncer) -> None:
    """Record that from_env made the current setup: its remote and this machine's public key."""
    settings = syncer.settings()
    if settings is None:
        return
    path = _marker(syncer)
    path.write_text(json.dumps({"remote": settings.remote, "public_key": keys.public_key(syncer.state_dir)}) + "\n",
                    encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)


def _unmark(syncer: Syncer) -> None:
    try:
        _marker(syncer).unlink(missing_ok=True)
    except OSError:
        pass


def made_here(syncer: Syncer, settings: Settings) -> bool:
    """Whether from_env made this setup: the marker names its remote and this machine's key.

    ``disconnect`` deletes the key, so a marker it leaves behind never matches a later setup.
    """
    try:
        data = json.loads(_marker(syncer).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(data, dict) and data.get("remote") == settings.remote
            and data.get("public_key") == keys.public_key(syncer.state_dir))


# --- messages -------------------------------------------------------------------------------


def _status_command() -> str:
    return command("src.daemon", "user-sync", "status") if daemon_directory() else command("src.user_sync", "status")


def _reason(error: BaseException) -> str:
    message = getattr(error, "message", None)
    if isinstance(message, str) and message:
        return message
    if isinstance(error, (OSError, RuntimeError, ValueError)) and str(error):
        return str(error)
    return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__


def _display(remote: str, allow_file: bool) -> str:
    try:
        return parse_remote(remote, allow_file=allow_file).display
    except RemoteError:
        return remote


def _sync_state(syncer: Syncer) -> dict:
    """The last recorded state, read from the state file: nothing is locked, read or written in the library."""
    try:
        value = json.loads(syncer.state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def describe(syncer: Syncer, settings: Settings) -> str:
    """One line about sync that is set up."""
    state = _sync_state(syncer)
    if settings.paused:
        where = "paused"
    elif not settings.started:
        where = "set up but not started yet"
    elif state.get("state") in ("attention", "offline"):
        where = f"on, {state['state']} ({state.get('reason') or 'see its status'})"
    else:
        where = "on"
    return (f"Sync between machines is {where}: {_display(settings.remote, syncer.allow_file_remote)}, "
            f"this machine is {settings.label}. Status: {_status_command()}")


def _not_started(result: dict) -> dict:
    """A result after which sync has not started, as ``attention`` with a reason: the exit code is 1."""
    reason = result.get("reason") or result.get("status") or "not_started"
    message = result.get("message") or f"sync did not start ({reason})"
    return {**result, "status": "attention", "reason": reason, "message": message}


# --- the installers' step -----------------------------------------------------------------


def _guarded(say: Say, action: Callable[[], dict]) -> dict:
    """Run ``action``; a failure is reported, never raised, so that the installer goes on."""
    from src.user_sync.wizard import Cancelled
    try:
        return action()
    except (Cancelled, EOFError):
        say("  Sync setup was cancelled; nothing was uploaded.")
        return {"status": "cancelled", "reason": "cancelled", "message": "stopped; nothing was uploaded"}
    except KeyboardInterrupt:
        say("  Sync setup was interrupted.")
        return {"status": "cancelled", "reason": "cancelled", "message": "interrupted"}
    except Exception as error:  # the installer must go on whatever went wrong here
        message = _reason(error)
        say(f"  Sync was not set up: {message}")
        reason = getattr(error, "reason", None) or getattr(error, "code", None) or "error"
        return {"status": "attention", "reason": reason, "message": message}


def _ignored_note(ignored: Iterable[str], say: Say) -> None:
    names = list(ignored)
    if names:
        say(f"  Ignored in .env: {', '.join(names)}. Sync setup reads them only from the environment of the "
            "command that runs it; remove them from .env.")


def step(syncer: Syncer, *, assume_yes: bool, interactive: bool, ask: Ask, say: Say,
         run_wizard: Callable[[], dict], open_browser: Callable[[str], object] | None = None,
         environ: Mapping[str, str] | None = None, token: str | None = None,
         ignored: Iterable[str] = ()) -> dict:
    """The installers' sync step; see the module docstring. Never raises.

    ``environ`` holds the sync variables as the process environment gave them (``take_environment``)
    and ``token`` the GitHub token; ``ignored`` names what ``.env`` set and does not count.
    """
    environ = {} if environ is None else environ
    settings = syncer.settings()
    if settings is None:
        _unmark(syncer)  # a marker without a setup is left from one that was disconnected
    started = settings is not None and bool(settings.started)
    if requested(environ) and not started:
        variable = REPO if _value(environ, REPO) else REMOTE
        say(f"  Setting up sync between machines from {variable}; nothing is asked.")
        # from_env says what .env set and does not count
        return _guarded(say, lambda: from_env(syncer, say=say, environ=environ, token=token, ignored=ignored))
    _ignored_note(ignored, say)
    if token:
        say(f"  {TOKEN} was not used: " + ("sync is already set up." if requested(environ)
                                          else f"it counts only together with {REPO} or {REMOTE}."))
    if settings is not None:
        line = describe(syncer, settings)
        say(f"  {line}")
        if requested(environ):
            say(f"  {REPO} and {REMOTE} change nothing once sync has started.")
        return {"status": "set_up", "message": line}
    problem = blocker(syncer)
    if problem is not None:
        say(f"  Sync between machines is off: {problem.message}.")
        return {"status": "off", "reason": problem.reason, "message": problem.message}
    if assume_yes or not interactive:
        why = "--yes never asks" if assume_yes else "there is no terminal to ask in"
        say(f"  Sync between machines is off ({why}); the summary below says how to turn it on.")
        return {"status": "off", "reason": "not_asked"}
    try:
        answer = ask(PROMPT)
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        say("  Sync stays off; the summary below says how to turn it on later.")
        return {"status": "off", "reason": "declined"}
    daemon = daemon_directory()
    url = settings_page(daemon) if daemon is not None else None
    if url is not None:
        try:
            opened = bool((open_browser or _open_browser)(url))
        except Exception:  # no browser here: the link below is enough
            opened = False
        if opened:
            say("  Opened the settings page in your browser; its Sync page sets sync up.")
        else:
            say(f"  Open the settings page in a browser; its Sync page sets sync up: {url}")
            say("  (a one-use link, valid for 2 minutes; " + command("src.daemon", "flows-ui") + " makes another)")
        return {"status": "off", "reason": "settings_page"}
    return _after_wizard(syncer, _guarded(say, run_wizard), ask=ask, say=say)


def _open_browser(url: str) -> bool:
    import webbrowser
    return webbrowser.open(url)


def _after_wizard(syncer: Syncer, result: dict, *, ask: Ask, say: Say) -> dict:
    """Offer background sync once the wizard started sync; a setup that did not start is ``attention``."""
    settings = syncer.settings()
    if settings is None or not settings.started:
        if settings is not None and result.get("status") not in ("cancelled", "attention"):
            return _not_started(result)
        return result
    found = background()
    if found["by"] == "daemon":
        say("  The daemon syncs in the background while it runs; status: "
            + command("src.daemon", "user-sync", "status"))
        return {**result, "background": "daemon"}
    if found["by"] == "schedule":
        say(f"  Background sync already runs every {found.get('minutes')} minutes.")
        return {**result, "background": "schedule"}
    try:
        answer = ask(f"  Also sync every {settings.fetch_minutes} minutes in the background? [Y/n]: ")
    except (EOFError, KeyboardInterrupt):
        answer = "n"  # no answer at all changes nothing
    if answer.strip().lower() in ("", "y", "yes"):
        return {**result, "background": _turn_background_on(syncer, settings, say)}
    say(f"  {_background_off(settings.fetch_minutes)}")
    return {**result, "background": None}


def summary(syncer: Syncer, *, say: Say) -> dict:
    """The installer's final summary: sync's state, background sync and how to turn either on.

    Ends with an empty line when it printed anything. Never raises.
    """
    try:
        settings = syncer.settings()
        if settings is not None and settings.started:
            found = background()
            if found["by"] == "daemon":
                say("  Sync between machines is on; the daemon syncs in the background while it runs.")
            elif found["by"] == "schedule":
                say(f"  Sync between machines is on, in the background every {found.get('minutes')} minutes.")
            else:
                say(f"  Sync between machines is on. {_background_off(settings.fetch_minutes)}")
            say("")
            return {"status": "set_up", "background": found["by"]}
        if settings is not None:
            for line in pending_steps(syncer, settings):
                say(line)
            say("")
            return {"status": "pending"}
        problem = blocker(syncer)
        if problem is not None:
            say(f"  Sync between machines is off: {problem.message}.")
            say("")
            return {"status": "off", "reason": problem.reason}
        say("  Sync between machines is off. To turn it on:")
        if daemon_directory() is not None:
            say(f"    {command('src.daemon', 'flows-ui')}   (the Sync page of the settings)")
        say(f"    {command('src.user_sync', 'setup')}   (step by step in a terminal)")
        say(f"  Without questions: set {REPO} or {REMOTE}, {NAME} and {EMAIL} in the environment of "
            "that command with --from-env (docs/user-sync.md).")
        say("")
        return {"status": "off"}
    except Exception as error:  # a summary line is never worth a failed setup
        return {"status": "unknown", "message": _reason(error)}


def pending_steps(syncer: Syncer, settings: Settings) -> list[str]:
    """Where a setup that has not started stopped, and the next step for that reason.

    Reads the settings, the state file and sync's ``known_hosts``; changes nothing.
    """
    state = _sync_state(syncer)
    ours = made_here(syncer, settings)
    where = _display(settings.remote, syncer.allow_file_remote)
    wizard = command("src.user_sync", "setup")
    again = command("src.user_sync", "setup", "--from-env") if ours else wizard
    try:
        remote = parse_remote(settings.remote, allow_file=syncer.allow_file_remote)
    except RemoteError:
        remote = None
    lines = [f"  Sync between machines is set up for {where} but not started yet."]
    if remote is not None and remote.kind == "ssh" and not keys.trusted_keys(syncer.known_hosts, remote.host,
                                                                             remote.port):
        if remote.host in ("github.com", gitcmd.GITHUB_443_HOST):
            lines += ["  github.com's host keys, which setup reads from its API, could not be fetched. When "
                      "github.com can be reached, run setup again:", f"    {again}"]
        else:
            confirm = command("src.user_sync", "setup", "--from-env", "--trust-host-key", "SHA256:...")
            lines += [f"  The host key of {remote.host} is not confirmed yet. Compare the fingerprints setup "
                      "shows with the ones the host publishes, then confirm the matching one:",
                      f"    {confirm if ours else wizard}"]
    elif state.get("reason") == "public_repo":
        confirm = command("src.user_sync", "setup", "--from-env", "--confirm-private")
        lines += [f"  Sync could not check that the repository is private ({state.get('message')}). Make it "
                  "private, or confirm that only you can read it:", f"    {confirm if ours else wizard}"]
    elif state.get("state") == "offline":
        lines += [f"  The remote could not be reached ({state.get('message')}). When it can, run setup again:",
                  f"    {again}"]
    elif state.get("access") == "denied" or state.get("reason") == "auth":
        public = keys.public_key(syncer.state_dir)
        lines += [f"  The remote refuses this machine's key. Add it to {where} as a deploy key with write "
                  "access, then run setup again:", *([f"    {public}"] if public else []), f"    {again}"]
    elif state.get("access") == "ok":
        lines += ["  Access works. Review the preview, then start sync with the hash it prints:",
                  f"    {command('src.user_sync', 'preview')}",
                  f"    {command('src.user_sync', 'start', '--confirm', 'HASH')}"]
        ours = False  # nothing to run again with the variables
    else:
        lines += ["  To finish it:", f"    {again}"]
    if ours:
        lines.append("  Setup from the environment needs the same AGENTS_USER_SYNC_* variables again.")
    if daemon_directory() is not None:
        lines.append(f"  Or finish it on the Sync page of the settings: {command('src.daemon', 'flows-ui')}")
    return lines


# --- setup from the environment -----------------------------------------------------------


def _same_target(existing: Settings, repository: str | None, remote: str | None, host: str,
                 allow_file: bool) -> bool:
    if remote is not None and existing.remote == remote:
        return True
    try:
        current = parse_remote(existing.remote, allow_file=allow_file)
    except RemoteError:
        return False
    found = hosted_repository(current, host)
    return repository is not None and found is not None and found.lower() == repository.lower()


def _github_api(syncer: Syncer, token: str | None, say: Say) -> bool:
    """Sign in with ``token`` when there is one; True when the GitHub API can be used."""
    account = syncer.github_account()
    if token:
        try:
            status = account.complete_sign_in(token)  # GitHub checks it first; nothing is kept if refused
        except GitHubError as error:
            if error.code not in ("auth", "forbidden"):
                raise
            detail = f" ({error.message})" if error.code == "forbidden" else ""
            raise SyncError("auth", f"GitHub refused {TOKEN}{detail}: it is wrong or has expired. Give it a token "
                                    "with the repo scope, which a private repository needs, and run setup "
                                    "again") from None
        storage = _STORES.get(status.get("storage"), status.get("storage"))
        say(f"  Signed in to {status['host']} as {status['login']} with {TOKEN}; the token is kept in {storage}.")
        if status.get("warning"):
            say(f"  Note: {status['warning']}")
        return True
    status = account.status()
    if status["connected"] and not status["reconnect_needed"]:
        say(f"  Using the GitHub account {status['login']} on {status['host']}.")
        return True
    if status["reconnect_needed"]:
        say("  GitHub refused the stored authorization; the deploy key has to be added by hand.")
    return False


def _old_repository(syncer: Syncer, existing: Settings, host: str) -> str | None:
    """``owner/name`` of the replaced setup's repository when it is on the GitHub host, else None."""
    try:
        return hosted_repository(parse_remote(existing.remote, allow_file=syncer.allow_file_remote), host)
    except RemoteError:
        return None


def _new_key(syncer: Syncer, old: str, say: Say) -> None:
    """Delete this machine's key pair, so that setup makes a new one that GitHub does not know yet."""
    private = keys.key_path(syncer.state_dir)
    for path in (private, private.with_suffix(".pub")):
        path.unlink(missing_ok=True)
    say(f"  This machine gets a new key: GitHub accepts a key on one repository only, and {old} may still "
        "hold the old one as a deploy key; remove it there (Settings > Deploy keys).")


def _free_the_key(syncer: Syncer, old: str, new: str, api: bool, say: Say) -> None:
    """Before a replaced setup's key goes to ``new``: take it off ``old``, or make a new key pair.

    GitHub refuses a deploy key that another repository has. With the API, this machine's key is
    removed from the old repository; without it, or when that fails, the key pair is replaced.
    """
    public = keys.public_key(syncer.state_dir)
    if public is None or old.lower() == new.lower():
        return
    if api:
        try:
            client = syncer.github_client()
            found = client.find_deploy_key(old, public)
            if found is not None:
                client.delete_deploy_key(old, found.id)
                say(f"  Removed this machine's deploy key from {old}, so that {new} can have it.")
            return
        except (GitHubError, SyncError) as error:
            say(f"  This machine's deploy key could not be removed from {old}: {_reason(error)}")
    _new_key(syncer, old, say)


def _with_scope_hint(error: SyncError, token: str | None) -> SyncError:
    """``error``, saying that a token from the environment may lack the repo scope when GitHub showed
    no such repository."""
    if error.reason != "unknown_remote" or not token:
        return error
    return SyncError(error.reason, f"{error.message}. If it exists, {TOKEN} may lack the repo scope it needs "
                                   "to see a private repository")


def _require_private(syncer: Syncer, repository: str, token: str | None) -> None:
    """Refuse a repository that GitHub does not show, or that is not private, as setup_github would."""
    try:
        syncer.github_client().require_private(repository)
    except GitHubError as error:
        if error.code != "not_found":
            raise
        raise _with_scope_hint(SyncError("unknown_remote", f"{repository} does not exist, or this account cannot "
                                                           "see it"), token) from None


def _clear_the_way(syncer: Syncer) -> str:
    dot_git = syncer.library / ".git"
    move = f", move {dot_git} away" if dot_git.exists() or dot_git.is_symlink() else ""
    return (f"finish it with the wizard ({command('src.user_sync', 'setup')}), or remove it "
            f"({command('src.user_sync', 'disconnect')}){move} and run setup from the environment again")


def from_env(syncer: Syncer, *, say: Say, environ: Mapping[str, str] | None = None, token: str | None = None,
             ignored: Iterable[str] = (), branch: str = "main", trust_host_key: str | None = None,
             confirm_private: bool | None = None, ask_new_repositories: bool | None = None) -> dict:
    """Set sync up from ``AGENTS_USER_SYNC_*`` without a question; see the module docstring.

    ``environ`` holds the variables as the process environment gave them, ``token`` the GitHub
    token, both from ``take_environment``; ``ignored`` names what ``.env`` set. Raises
    ``SyncError`` (and ``GitHubError``) when setup cannot go on.
    """
    environ = {} if environ is None else environ
    ignored = list(ignored)
    _ignored_note(ignored, say)
    existing = syncer.settings()
    if existing is None:
        _unmark(syncer)
    elif existing.started:
        message = f"sync is already set up with {_display(existing.remote, syncer.allow_file_remote)}; nothing changed"
        say(f"  {message[0].upper()}{message[1:]}.")
        return {"status": "set_up", "message": message}
    repo, remote = _value(environ, REPO), _value(environ, REMOTE)
    if not (repo or remote):
        from_dotenv = [name for name in ignored if name in (REPO, REMOTE)]
        raise SyncError("not_requested", f"set {REPO} (OWNER/NAME of a GitHub repository) or {REMOTE} (an SSH URL) "
                                         "in the environment of this command"
                        + (" (.env does not count)" if from_dotenv else ""), state="off")
    if repo and remote:
        raise SyncError("invalid", f"set {REPO} or {REMOTE}, not both")
    if repo and not _REPOSITORY.fullmatch(repo):
        raise SyncError("invalid", f"{REPO} must be OWNER/NAME of a GitHub repository, such as me/agents-library")
    name, email, label = _value(environ, NAME), _value(environ, EMAIL), _value(environ, LABEL)
    if not (name and email):
        raise SyncError("identity", f"{NAME} and {EMAIL} are required: sync commits under them and never reads "
                                    "your git configuration")
    validate_identity(name, email, label or "machine")
    target: Remote | None = None
    if remote:
        try:
            target = parse_remote(remote, allow_file=syncer.allow_file_remote)
        except RemoteError as error:
            raise SyncError("unknown_remote", f"{REMOTE}: {error}") from None
        if target.kind == "https":  # no credentials reach git: a private repository needs this machine's key
            raise SyncError("unknown_remote", f"{REMOTE} must be an SSH URL, such as "
                                              "git@git.example.com:me/agents-library.git")
    host = syncer.github_account().host
    repository = repo or (hosted_repository(target, host) if target is not None else None)
    ours = existing is not None and made_here(syncer, existing)
    replacing = existing is not None and not _same_target(existing, repository, remote, host,
                                                          syncer.allow_file_remote)
    if replacing:
        if not ours:
            raise SyncError("connected", f"sync is set up for {_display(existing.remote, syncer.allow_file_remote)} "
                                         f"but not started, and not from the environment; {_clear_the_way(syncer)}")
        say(f"  Replacing the setup for {_display(existing.remote, syncer.allow_file_remote)}, which setup from "
            "the environment made and which never started.")
    problem = blocker(syncer)
    if problem is not None:
        raise problem
    dot_git = syncer.library / ".git"
    if existing is None and (dot_git.exists() or dot_git.is_symlink()):
        raise SyncError("existing_git", f"{dot_git} is left from an earlier sync, and setup without questions never "
                                        f"touches an existing .git; move it away and run setup from the environment "
                                        f"again, or set sync up with the wizard ({command('src.user_sync', 'setup')})")
    options = {"name": name, "email": email, "label": label, "branch": branch, "trust_host_key": trust_host_key,
               "ask_new_repositories": ask_new_repositories}
    mark = existing is None or ours
    old = _old_repository(syncer, existing, host) if replacing else None
    try:
        api = repository is not None and _github_api(syncer, token, say)
        if old is not None and repository is not None:
            if api:  # the new repository first: a mistyped name must not cost the old one its key
                _require_private(syncer, repository, token)
            _free_the_key(syncer, old, repository, api, say)
        if api:
            try:
                result = syncer.setup_github(repository, **options)
            except GitHubError as error:
                if error.code != "exists":
                    raise
                holder = f" ({old} had it last)" if old else ""
                raise SyncError("key_in_use", f"GitHub refuses this machine's key for {repository}: another "
                                              f"repository still has it as a deploy key{holder}. Remove it there "
                                              f"(Settings > Deploy keys) and run setup again, or "
                                              f"{_clear_the_way(syncer)}") from None
            except SyncError as error:
                raise _with_scope_hint(error, token) from None
            for line in result.get("steps", []):
                say(f"  - {line}")
            if confirm_private and result.get("status") != "host_key_unconfirmed":
                # GitHub's API normally answers; this keeps an owner's confirmation for when it cannot.
                syncer.setup(remote=syncer.settings().remote, confirm_private=True, **options)
            manual = False
        else:
            if token and repository is None:
                say(f"  {TOKEN} is used only for a repository on {host}; it was not stored.")
            url = remote or f"git@{host.split(':')[0]}:{repository}.git"
            result = syncer.setup(remote=url, confirm_private=confirm_private, **options)
            manual = True
    finally:
        # Setup saves the settings before it trusts the host, makes the repository and adds the key:
        # whatever stopped it after that, the next run may still replace this setup.
        if mark and syncer.settings() is not None:
            try:
                _mark(syncer)
            except OSError:
                pass
    where = repository or (target.display if target is not None else "the repository")
    if result.get("status") == "host_key_unconfirmed":
        say(f"  {result['host']} offers these host keys; compare them with the fingerprints the host publishes:")
        for fingerprint in result["fingerprints"]:
            say(f"    {fingerprint}")
        say("  Then confirm the matching one, with the same variables set: "
            + command("src.user_sync", "setup", "--from-env", "--trust-host-key", "SHA256:..."))
        return {"status": "attention", "reason": "host_key", "fingerprints": result["fingerprints"],
                "message": "confirm the host key, then run setup again"}
    return _check_and_start(syncer, say, where=where, manual=manual, mark=mark, public_key=result.get("public_key"))


def _check_and_start(syncer: Syncer, say: Say, *, where: str, manual: bool, mark: bool,
                     public_key: str | None) -> dict:
    try:
        checked = syncer.check()
    except SyncError as error:
        public = public_key or keys.public_key(syncer.state_dir)
        if not (manual and error.reason == "auth" and public):
            raise
        say(f"  Add this machine's public key to {where} as a deploy key with write access:")
        say(f"    {public}")
        say("  Then run setup from the environment again, with the same variables set: "
            + command("src.user_sync", "setup", "--from-env"))
        return {"status": "waiting_for_access", "reason": "auth", "public_key": public,
                "message": "add this machine's public key as a deploy key with write access, then run setup again"}
    if mark:
        _mark(syncer)  # the check may have moved the remote to port 443
    if checked.get("message"):
        say(f"  {checked['message']}")
    verdict = syncer.privacy()
    if verdict in ("public", "internal"):
        raise SyncError("public_repo", f"the repository is {verdict}; sync uses only a private repository")
    if verdict != "private" and not syncer.settings().private_confirmed:
        raise SyncError("public_repo", "sync could not check that the repository is private; if only you can "
                                       "read it, confirm that with the same variables set: "
                                       + command("src.user_sync", "setup", "--from-env", "--confirm-private"))
    from src.user_sync.wizard import describe_preview
    preview = syncer.preview()
    for line in describe_preview(preview):
        say(f"  {line}")
    if preview["conflicts"]:
        say(f"  Joining keeps {len(preview['conflicts'])} conflicts, so it waits for your confirmation; "
            "nothing was uploaded.")
        say(f"  Review them: {command('src.user_sync', 'preview')}")
        say(f"  Then start:  {command('src.user_sync', 'start', '--confirm', preview['hash'])}")
        return {"status": "attention", "reason": "confirmation_needed", "hash": preview["hash"],
                "conflicts": preview["conflicts"],
                "message": "joining keeps conflicts; review the preview and start sync with its hash"}
    result = syncer.start(preview["hash"])
    settings = syncer.settings()
    if settings is None or not settings.started:
        failed = _not_started(result)
        say(f"  Sync did not start: {failed['reason']}: {failed['message']}")
        return failed
    _unmark(syncer)
    say(f"  Sync started: sent {len(result.get('sent', []))} and received {len(result.get('received', []))} files.")
    if result.get("status") == "attention":
        say(f"  It needs attention: {result.get('reason')}: {result.get('message')}")
    found = background()
    if found["by"] == "daemon":
        say("  The daemon syncs in the background while it runs; status: "
            + command("src.daemon", "user-sync", "status"))
        return {**result, "background": "daemon"}
    if found["by"] == "schedule":
        say(f"  Background sync already runs every {found.get('minutes')} minutes.")
        return {**result, "background": "schedule"}
    return {**result, "background": _turn_background_on(syncer, settings, say)}
