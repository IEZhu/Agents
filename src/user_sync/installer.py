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
* ``--yes`` or no terminal: it asks nothing.
* Otherwise it asks once. Yes opens the daemon's settings page (``python -m src.daemon flows-ui``),
  whose Sync page sets sync up, on macOS when the daemon is installed; elsewhere, or when the page
  cannot be opened, it starts the terminal wizard (``src.user_sync.wizard``).

``from_env`` (also ``python -m src.user_sync setup --from-env``) takes the commit identity from
``AGENTS_USER_SYNC_NAME`` and ``AGENTS_USER_SYNC_EMAIL`` and the machine label from
``AGENTS_USER_SYNC_LABEL``. ``AGENTS_GITHUB_TOKEN`` is removed from the process environment before
anything runs (``take_token``), so no child process inherits it; for a repository on GitHub it is
checked with GitHub and kept in the OS secret store through ``GitHubAccount.complete_sign_in``, and
the deploy key is added through the API. Without a token or a connected account, the public key is
printed to add by hand. Setup then checks access and privacy, shows the preview and starts sync,
except that a join with conflicts stops at "confirmation needed" with the commands to review and
confirm it. Run again with the same variables, it continues a setup that stopped, and it changes
nothing once sync has started. It never touches a ``.git`` that the library holds while sync has no
settings here, also one left from an earlier sync: it stops and points to the wizard.

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
from typing import Callable, MutableMapping

from src.user_sync import gitcmd, keys
from src.user_sync.engine import (MANAGED_KEY, Settings, SyncError, Syncer, hosted_repository,
                                  installation_root, validate_identity)
from src.user_sync.gitcmd import Remote, RemoteError, parse_remote

REPO = "AGENTS_USER_SYNC_REPO"
REMOTE = "AGENTS_USER_SYNC_REMOTE"
NAME = "AGENTS_USER_SYNC_NAME"
EMAIL = "AGENTS_USER_SYNC_EMAIL"
LABEL = "AGENTS_USER_SYNC_LABEL"
TOKEN = "AGENTS_GITHUB_TOKEN"
PROMPT = "  Set up sync between your machines now? [y/N]: "
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}")
_STORES = {"macos-keychain": "the macOS Keychain", "secret-service": "the Secret Service",
           "windows-credential-manager": "the Windows Credential Manager",
           "file": "a private file in the sync state directory"}

Ask = Callable[[str], str]
Say = Callable[[str], None]
Environ = MutableMapping[str, str]


# --- the environment ----------------------------------------------------------------------


def _value(environ: Environ, name: str) -> str | None:
    value = environ.get(name)
    if not isinstance(value, str):
        return None
    return value.strip() or None


def requested(environ: Environ | None = None) -> bool:
    """Whether the environment asks for setup: ``AGENTS_USER_SYNC_REPO`` or ``_REMOTE`` is set."""
    environ = os.environ if environ is None else environ
    return bool(_value(environ, REPO) or _value(environ, REMOTE))


def take_token(environ: Environ | None = None) -> str | None:
    """``AGENTS_GITHUB_TOKEN``, removed from the environment so that no child process inherits it."""
    environ = os.environ if environ is None else environ
    value = environ.pop(TOKEN, None)
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _token_in_dotenv() -> bool:
    """Whether the installation's ``.env`` holds a GitHub token, which it should not keep."""
    try:
        from dotenv import dotenv_values
        return bool((dotenv_values(installation_root() / ".env").get(TOKEN) or "").strip())
    except Exception:  # no python-dotenv, or an unreadable file: nothing to warn about
        return False


# --- this machine -------------------------------------------------------------------------


def _python() -> str:
    """The interpreter as typed in the installation root: ``.venv/bin/python`` for the installers."""
    executable = Path(os.path.abspath(sys.executable))  # not resolved: the venv's link is the point
    try:
        return str(executable.relative_to(installation_root()))
    except ValueError:
        return str(executable)


def _quote(word: str) -> str:
    if os.name == "nt":
        return f'"{word}"' if any(ch in word for ch in ' &()^%!;,=') else word
    return shlex.quote(word)


def command(*arguments: str) -> str:
    """``<python> -m <arguments>``, to run in the installation root."""
    return " ".join(_quote(word) for word in (_python(), "-m", *arguments))


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


def open_settings(directory: Path) -> str:
    """Open the daemon's settings page in the browser (``python -m src.daemon flows-ui``); its URL.

    Raises when the page cannot be opened, for example while the daemon is stopped.
    """
    result = subprocess.run([sys.executable, "-m", "src.daemon", "--state", str(directory), "flows-ui"],
                            cwd=installation_root(), capture_output=True, text=True, timeout=60,
                            stdin=subprocess.DEVNULL)
    try:
        answer = json.loads(result.stdout)
    except ValueError:
        answer = None
    answer = answer if isinstance(answer, dict) else {}
    if result.returncode == 0 and isinstance(answer.get("url"), str):
        return answer["url"]
    lines = result.stderr.strip().splitlines()
    raise RuntimeError(str(answer.get("error") or (lines[-1] if lines else f"exit code {result.returncode}")))


def _status_command() -> str:
    return command("src.daemon", "user-sync", "status") if daemon_directory() else command("src.user_sync", "status")


def _running_note() -> str:
    if daemon_directory():
        return "The daemon syncs the library while it runs; check it with: " + command("src.daemon", "user-sync",
                                                                                      "status")
    return ("Agents-Core's stdio servers sync after each save. To sync every few minutes as well: "
            + command("src.user_sync", "schedule", "enable"))


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


def step(syncer: Syncer, *, assume_yes: bool, interactive: bool, ask: Ask, say: Say,
         run_wizard: Callable[[], dict], environ: Environ | None = None) -> dict:
    """The installers' sync step; see the module docstring. Never raises."""
    environ = os.environ if environ is None else environ
    token = take_token(environ)
    settings = syncer.settings()
    started = settings is not None and bool(settings.started)
    if requested(environ) and not started:
        variable = REPO if _value(environ, REPO) else REMOTE
        say(f"  Setting up sync between machines from {variable}; nothing is asked.")
        return _guarded(say, lambda: from_env(syncer, say=say, token=token, environ=environ))
    if token:
        if requested(environ):  # and sync has started: there is nothing to sign in for
            say(f"  {TOKEN} was not used: sync is already set up.")
        else:
            say(f"  {TOKEN} is used only together with {REPO} or {REMOTE}; it was not stored.")
        if _token_in_dotenv():
            say(f"  Remove {TOKEN} from .env: a token does not belong there.")
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
    if daemon is not None:
        try:
            url = open_settings(daemon)
        except Exception as error:  # the page needs the running daemon
            say(f"  The settings page could not be opened ({_reason(error)}); it needs the running daemon. "
                "Setting up sync in this terminal instead.")
        else:
            say(f"  Opened the settings page in your browser: {url}")
            say("  Its Sync page sets sync up. The link works once, for 2 minutes; to open the page again: "
                + command("src.daemon", "flows-ui"))
            return {"status": "off", "reason": "settings_page", "url": url}
    result = _guarded(say, run_wizard)
    settings = syncer.settings()
    if settings is not None and settings.started:
        say(f"  {_running_note()}")
    return result


def summary(syncer: Syncer, *, say: Say) -> dict:
    """The installer's final summary: how to turn sync on, unless it runs; then an empty line. Never raises."""
    try:
        settings = syncer.settings()
        if settings is not None and settings.started:
            return {"status": "set_up"}
        if settings is None:
            problem = blocker(syncer)
            if problem is not None:
                say(f"  Sync between machines is off: {problem.message}.")
                say("")
                return {"status": "off", "reason": problem.reason}
            say(f"  Sync between machines is off. To turn it on, in {installation_root()}:")
        else:
            say(f"  Sync between machines is set up but not started yet. To finish it, in {installation_root()}:")
        if daemon_directory() is not None:
            say(f"    {command('src.daemon', 'flows-ui')}   (the Sync page of the settings)")
        say(f"    {command('src.user_sync', 'setup')}   (step by step in a terminal)")
        if settings is None:
            say(f"  Without questions: set {REPO} or {REMOTE}, {NAME} and {EMAIL}, then run setup again "
                "(docs/user-sync.md).")
        say("")
        return {"status": "off" if settings is None else "set_up"}
    except Exception as error:  # a summary line is never worth a failed setup
        return {"status": "unknown", "message": _reason(error)}


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
        status = account.complete_sign_in(token)  # GitHub checks it first; nothing is kept if refused
        storage = _STORES.get(status.get("storage"), status.get("storage"))
        say(f"  Signed in to {status['host']} as {status['login']} with {TOKEN}; the token is kept in {storage}.")
        if status.get("warning"):
            say(f"  Note: {status['warning']}")
        if _token_in_dotenv():
            say(f"  Remove {TOKEN} from .env: the token is in the secret store now.")
        return True
    status = account.status()
    if status["connected"] and not status["reconnect_needed"]:
        say(f"  Using the GitHub account {status['login']} on {status['host']}.")
        return True
    if status["reconnect_needed"]:
        say("  GitHub refused the stored authorization; the deploy key has to be added by hand.")
    return False


def from_env(syncer: Syncer, *, say: Say, token: str | None = None, environ: Environ | None = None,
             branch: str = "main", trust_host_key: str | None = None, confirm_private: bool | None = None,
             ask_new_repositories: bool | None = None) -> dict:
    """Set sync up from ``AGENTS_USER_SYNC_*`` without a question; see the module docstring.

    ``token`` is the ``AGENTS_GITHUB_TOKEN`` that the caller took out of the environment
    (``take_token``). Raises ``SyncError`` (and ``GitHubError``) when setup cannot go on.
    """
    environ = os.environ if environ is None else environ
    repo, remote = _value(environ, REPO), _value(environ, REMOTE)
    if not (repo or remote):
        raise SyncError("not_requested", f"set {REPO} (OWNER/NAME of a GitHub repository) or {REMOTE} "
                                         "(an SSH URL)", state="off")
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
    existing = syncer.settings()
    if existing is not None and existing.started:
        message = f"sync is already set up with {_display(existing.remote, syncer.allow_file_remote)}; nothing changed"
        say(f"  {message[0].upper()}{message[1:]}.")
        return {"status": "set_up", "message": message}
    host = syncer.github_account().host
    repository = repo or (hosted_repository(target, host) if target is not None else None)
    if existing is not None and not _same_target(existing, repository, remote, host, syncer.allow_file_remote):
        raise SyncError("connected", f"sync is set up for {_display(existing.remote, syncer.allow_file_remote)} "
                                     f"but not started; finish that setup ({command('src.user_sync', 'setup')}) "
                                     f"or remove it ({command('src.user_sync', 'disconnect')}) first")
    problem = blocker(syncer)
    if problem is not None:
        raise problem
    dot_git = syncer.library / ".git"
    if existing is None and (dot_git.exists() or dot_git.is_symlink()):
        raise SyncError("existing_git", f"{dot_git} is left from an earlier sync, and setup without questions never "
                                        f"touches an existing .git; set sync up again with "
                                        f"{command('src.user_sync', 'setup')}, or move the .git away")
    options = {"name": name, "email": email, "label": label, "branch": branch, "trust_host_key": trust_host_key,
               "ask_new_repositories": ask_new_repositories}
    if repository is not None and _github_api(syncer, token, say):
        result = syncer.setup_github(repository, **options)
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
    where = repository or (target.display if target is not None else "the repository")
    if result.get("status") == "host_key_unconfirmed":
        say(f"  {result['host']} offers these host keys; compare them with the fingerprints the host publishes:")
        for fingerprint in result["fingerprints"]:
            say(f"    {fingerprint}")
        say("  Then confirm the matching one, with the same variables set: "
            + command("src.user_sync", "setup", "--from-env", "--trust-host-key", "SHA256:..."))
        return {"status": "attention", "reason": "host_key", "fingerprints": result["fingerprints"],
                "message": "confirm the host key, then run setup again"}
    return _check_and_start(syncer, say, where=where, manual=manual, public_key=result.get("public_key"))


def _check_and_start(syncer: Syncer, say: Say, *, where: str, manual: bool, public_key: str | None) -> dict:
    try:
        checked = syncer.check()
    except SyncError as error:
        public = public_key or keys.public_key(syncer.state_dir)
        if not (manual and error.reason == "auth" and public):
            raise
        say(f"  Add this machine's public key to {where} as a deploy key with write access:")
        say(f"    {public}")
        say("  Then run setup again with the same variables set, or: "
            + command("src.user_sync", "setup", "--from-env"))
        return {"status": "waiting_for_access", "reason": "auth", "public_key": public,
                "message": "add this machine's public key as a deploy key with write access, then run setup again"}
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
    if settings is not None and settings.started:
        say(f"  Sync started: sent {len(result.get('sent', []))} and received {len(result.get('received', []))} files.")
        if result.get("status") == "attention":
            say(f"  It needs attention: {result.get('reason')}: {result.get('message')}")
        say(f"  {_running_note()}")
    else:
        say(f"  Sync did not start: {result.get('reason')}: {result.get('message')}")
    return result
