"""The terminal setup wizard: ``python -m src.user_sync setup`` without ``--remote`` or ``--github``.

It follows the web wizard (#168): GitHub sign-in with a device code, opening the browser when it
can, or a manual SSH URL for another host; the repository; the commit identity; the machine label;
the access check; the preview; and, after an explicit "yes", the start. The identity is asked for
and kept in sync's own settings, never read from the user's git configuration. Nothing is uploaded
before the owner types "yes" under the preview.

``ask`` and ``say`` stand for ``input`` and ``print``; tests pass scripted ones.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Callable
import webbrowser

from src.user_sync import github as github_api
from src.user_sync.engine import SyncError, Syncer, default_label, validate_identity
from src.user_sync.gitcmd import RemoteError, parse_remote

PREVIEW_LINES = 20
_KINDS = {
    "push": "upload this library to the empty repository",
    "join": "join the library in the repository; both sides' flows are kept",
    "fast_forward": "download the library from the repository",
    "merge": "merge this library with the repository's",
    "none": "nothing to transfer",
}

Ask = Callable[[str], str]
Say = Callable[[str], None]


class Cancelled(Exception):
    """The owner stopped the wizard; nothing was uploaded."""


def open_in_browser(url: str) -> bool:
    """Open ``url`` in a graphical browser; True when one took it.

    Never a console browser (lynx, w3m, links, ``www-browser``): ``webbrowser`` waits for it in this
    terminal, so device-flow polling would not start while the code expires. So on Linux and other
    POSIX systems without ``DISPLAY`` or ``WAYLAND_DISPLAY`` (an SSH session to a server) nothing
    opens, and a browser that ``webbrowser`` runs and waits for is skipped. The caller has printed
    the link anyway.
    """
    if sys.platform not in ("darwin", "win32") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        browser = webbrowser.get()
    except webbrowser.Error:
        return False
    if type(browser) is webbrowser.GenericBrowser or (isinstance(browser, webbrowser.UnixBrowser)
                                                      and not browser.background):
        return False
    return bool(browser.open(url))


def run(syncer: Syncer, *, ask: Ask = input, say: Say = print,
        open_browser: Callable[[str], object] = open_in_browser,
        sleep: Callable[[float], None] = time.sleep) -> dict:
    """Set up and start sync interactively; returns the result as the other commands do."""
    existing = syncer.settings()
    if existing is not None and existing.started:
        say(f"Sync is already set up with {existing.remote}. See status, or disconnect first to set it up again.")
        return {"status": "already_set_up", "remote": existing.remote,
                "message": "sync is already set up; nothing changed"}
    say("Sync keeps your personal flow library the same on your machines through a private git repository.")
    (repository, owner), remote = _where(syncer, ask, say, open_browser, sleep)
    name, email, label = _identity(ask, say, existing)
    if repository:
        result = _with_host_key(lambda trust: _setup_github(syncer, repository, owner, name, email, label, trust,
                                                            ask, say), ask, say)
        for step in result.get("steps", []):
            say(f"  - {step}")
    else:
        result = _with_host_key(lambda trust: syncer.setup(
            remote=remote, name=name, email=email, label=label, trust_host_key=trust), ask, say)
        if result.get("public_key"):
            say("Add this public key to the repository as a deploy key with write access:")
            say(f"  {result['public_key']}")
            ask("Press Enter when it is added: ")
    _check(syncer, ask, say)
    refusal = _confirm_private(syncer, ask, say, name, email, label)
    if refusal:
        return refusal
    preview = syncer.preview()
    for line in describe_preview(preview):
        say(line)
    if ask("Start sync with exactly this preview? Type yes to start: ").strip().lower() != "yes":
        say("Sync was not started. Run the wizard again, or: "
            f"python -m src.user_sync start --confirm {preview['hash']}")
        return {"status": "attention", "reason": "confirmation_needed", "message": "sync was not started",
                "hash": preview["hash"]}
    result = syncer.start(preview["hash"])
    if result.get("status") == "synced":
        say(f"Sync started: sent {len(result.get('sent', []))} and received {len(result.get('received', []))} files.")
    return result


def describe_preview(preview: dict) -> list[str]:
    """The preview for a terminal: transfers, conflicts, scanner hits and repository groups."""
    lines = [f"Preview: {_KINDS.get(preview.get('kind'), preview.get('kind'))}"]
    for key, title in (("upload", "Upload"), ("download", "Download"), ("delete_local", "Delete on this machine"),
                       ("remove_from_remote", "Remove from the repository")):
        lines += _listed(title, [_entry(item) for item in preview.get(key, [])])
    lines += _listed("Conflicts: the repository's version stays, this machine's is kept in history",
                     [f"{_entry(item)}: {item.get('kind')}" for item in preview.get("conflicts", [])])
    lines += _listed("Not uploaded, possible credentials (allow one with scope --allow-secret)",
                     [f"{item['path']} ({item.get('pattern')})" for item in preview.get("blocked", [])])
    lines += _listed("Waiting for approval on this machine",
                     list(preview.get("pending_groups", [])) + list(preview.get("pending_files", [])))
    lines += _listed("Repository groups", [
        f"{group['group']}: {group.get('origin') or 'no origin'}, {group.get('files')} files"
        + (" (new)" if group.get("new") else "") for group in preview.get("repository_groups", [])])
    if preview.get("upload_bytes"):
        lines.append(f"Upload size: {preview['upload_bytes']} bytes")
    return lines


def _listed(title: str, items: list[str]) -> list[str]:
    if not items:
        return []
    shown = [f"  {item}" for item in items[:PREVIEW_LINES]]
    if len(items) > PREVIEW_LINES:
        shown.append(f"  and {len(items) - PREVIEW_LINES} more")
    return [f"{title} ({len(items)}):", *shown]


def _entry(item) -> str:
    if not isinstance(item, dict):
        return str(item)
    return f"{item['flow']}  {item['path']}" if item.get("flow") else str(item.get("path"))


# --- steps ----------------------------------------------------------------------------------


def _where(syncer: Syncer, ask: Ask, say: Say, open_browser, sleep) -> tuple[tuple, str | None]:
    """``((owner/name, confirmed owner), None)`` on GitHub, ``((None, None), url)`` on another host."""
    choice = _choice(ask, say, "Where is the library's repository? [g] GitHub, signing in with a code; "
                               "[m] another host, by its SSH URL. Choose [g]: ", ("g", "m"), "g")
    if choice == "g":
        try:
            account = syncer.github_account()
            _sign_in(account, say, open_browser, sleep)
            return _choose_repository(syncer.github_client(), account.status().get("login") or "", ask, say), None
        except github_api.GitHubError as error:
            say(f"GitHub cannot be used: {error.message}")
            if ask("Use an SSH URL instead? Type yes to continue: ").strip().lower() != "yes":
                raise Cancelled from None
    url = _ask_until(ask, say, "SSH URL of the repository (git@host:owner/name.git): ",
                     lambda text: parse_remote(text, allow_file=syncer.allow_file_remote).url)
    return (None, None), url


def _setup_github(syncer: Syncer, repository: str, owner: str | None, name: str, email: str, label: str,
                  trust: str | None, ask: Ask, say: Say) -> dict:
    """``setup_github``; when this machine's key is still on the repository an earlier setup chose,
    offer to remove it there (GitHub accepts a key on one repository only)."""
    try:
        return syncer.setup_github(repository, name=name, email=email, label=label, trust_host_key=trust,
                                   confirm_owner=owner)
    except SyncError as error:
        if error.reason != "key_in_use" or not error.details.get("previous"):
            raise
        previous = error.details["previous"]
        say(f"This machine's deploy key is still on {previous}, which an earlier setup chose.")
        if ask(f"Remove it from {previous} and use {repository}? Type yes to continue: ").strip().lower() != "yes":
            raise Cancelled from None
        return syncer.setup_github(repository, name=name, email=email, label=label, trust_host_key=trust,
                                   confirm_owner=owner, move_key=True)


def _sign_in(account: github_api.GitHubAccount, say: Say, open_browser, sleep) -> None:
    status = account.status()
    if status["connected"] and not status["reconnect_needed"]:
        say(f"Signed in to {status['host']} as {status['login']}.")
        return
    if status["reconnect_needed"]:
        say("GitHub refused the stored authorization; sign in again.")
    device = account.start_sign_in()
    say(f"Open {device.verification_uri} and enter the code {device.user_code}")
    try:
        open_browser(device.verification_uri)
    except Exception:  # no browser here: the code above is enough
        pass
    say(f"Waiting for GitHub; the code expires in {max(device.expires_in // 60, 1)} minutes.")
    status = account.wait_for_sign_in(device, sleep=sleep)
    say(f"Signed in to {status['host']} as {status['login']}.")
    if status.get("warning"):
        say(f"Note: {status['warning']}")


def _choose_repository(client: github_api.GitHubClient, login: str, ask: Ask,
                       say: Say) -> tuple[str, str | None]:
    """``(owner/name, confirmed owner)``: a new private repository, a library found on the account
    (the default when there is one, since a second machine joins it), or another repository by name."""
    scan = client.libraries()
    libraries = list(scan.libraries)
    other = len(libraries) + 2
    default = "2" if libraries else "1"
    say("Repository for the library:")
    say("  1. create a new private repository")
    for number, info in enumerate(libraries, 2):
        say(f"  {number}. {info.full_name} (holds a library)")
    say(f"  {other}. another private repository, by OWNER/NAME")
    if scan.truncated:
        say(f"  (only the {scan.checked} most recently pushed repositories were searched)")
    while True:
        answer = ask(f"Choose a number [{default}]: ").strip() or default
        if answer == "1":
            name = ask(f"Name of the new repository [{github_api.DEFAULT_REPO_NAME}]: ").strip() \
                or github_api.DEFAULT_REPO_NAME
            try:
                info = client.create_library(name)
            except github_api.GitHubError as error:
                if error.code not in ("exists", "invalid"):
                    raise
                say(error.message)
                continue
            say(f"Created the private repository {info.full_name}.")
            return info.full_name, None
        if answer.isdigit() and 2 <= int(answer) < other:
            return libraries[int(answer) - 2].full_name, None
        if answer == str(other):
            chosen = _existing_private(client, ask("OWNER/NAME: ").strip(), login, ask, say)
            if chosen:
                return chosen
            continue
        say("Choose one of the numbers above.")


def _existing_private(client: github_api.GitHubClient, full_name: str, login: str, ask: Ask,
                      say: Say) -> tuple[str, str | None] | None:
    """A private repository by name; one of another owner only after the owner's name is typed."""
    try:
        info = client.repository(full_name)
    except github_api.GitHubError as error:
        if error.code not in ("not_found", "invalid"):
            raise
        say(error.message)
        return None
    verdict = github_api.privacy(info)
    if verdict != "private":
        say(f"{info.full_name} is {verdict}; sync uses only a private repository.")
        return None
    if info.owner.lower() == login.lower():
        return info.full_name, None
    say(f"{info.full_name} belongs to {info.owner}, not to you: everyone who can read it there can read your library.")
    typed = ask(f"Type {info.owner} to use it anyway, or press Enter to choose again: ").strip()
    if typed.lower() != info.owner.lower():
        return None
    return info.full_name, info.owner


def _identity(ask: Ask, say: Say, existing) -> tuple[str, str, str]:
    """The commit identity and machine label; defaults come only from sync's own earlier setup."""
    say("Sync commits under a name and email you choose here; they are not read from your git configuration.")

    def checked(field: str):
        def check(text: str) -> str:
            values = {"name": "Owner", "email": "owner@example.com", "label": "machine", field: text}
            validate_identity(values["name"], values["email"], values["label"])
            return text
        return check

    name = _ask_until(ask, say, _prompt("Your name for commits", existing.name if existing else None),
                      checked("name"), default=existing.name if existing else None)
    email = _ask_until(ask, say, _prompt("Your email for commits", existing.email if existing else None),
                       checked("email"), default=existing.email if existing else None)
    suggested = existing.label if existing else default_label()
    label = _ask_until(ask, say, _prompt("Label of this machine (letters, digits, dashes)", suggested),
                       checked("label"), default=suggested)
    return name, email, label


def _with_host_key(setup: Callable[[str | None], dict], ask: Ask, say: Say) -> dict:
    """Run ``setup``; for a host other than github.com, let the owner confirm its host key first."""
    result = setup(None)
    while result.get("status") == "host_key_unconfirmed":
        say(f"{result['host']} offers these host keys:")
        for fingerprint in result["fingerprints"]:
            say(f"  {fingerprint}")
        say("Compare them with the fingerprints the host publishes before you trust one.")
        answer = ask("Paste the matching fingerprint (empty to stop): ").strip()
        if not answer:
            raise Cancelled
        if answer not in result["fingerprints"]:
            say("That is not one of the fingerprints above.")
            continue
        result = setup(answer)
    return result


def _check(syncer: Syncer, ask: Ask, say: Say) -> dict:
    """Check access until it works; a refused key may just not be added yet."""
    while True:
        try:
            result = syncer.check()
        except SyncError as error:
            say(f"Access check: {error.message}")
            if error.reason != "auth":
                raise
            if ask("Press Enter to check again, or type q to stop: ").strip().lower() == "q":
                raise Cancelled from None
            continue
        if result.get("message"):
            say(result["message"])
        say("Access works; the repository is "
            + ("empty." if result.get("remote_state") == "empty" else "an Agents-Core library."))
        return result


def _confirm_private(syncer: Syncer, ask: Ask, say: Say, name: str, email: str, label: str) -> dict | None:
    """None when the repository is private or the owner confirms it; else the refusal to return."""
    verdict = syncer.privacy()
    settings = syncer.settings()
    if verdict in ("public", "internal"):
        say(f"The repository is {verdict}; sync uses only a private repository. Make it private and run setup again.")
        return {"status": "attention", "reason": "public_repo", "message": f"the repository is {verdict}"}
    if verdict == "unknown" and not settings.private_confirmed:
        say("Sync could not verify that the repository is private: an anonymous check over HTTPS gave no "
            "clear answer, and no GitHub account answered for it.")
        if ask("Is it a private repository, which only its owner and the people they invite can read? "
               "Type yes to confirm: ").strip().lower() != "yes":
            say("Sync was not started.")
            return {"status": "attention", "reason": "public_repo",
                    "message": "privacy was not confirmed; sync was not started"}
        syncer.setup(remote=settings.remote, name=name, email=email, label=label, branch=settings.branch,
                     confirm_private=True)
    return None


# --- prompts --------------------------------------------------------------------------------


def _prompt(text: str, default: str | None) -> str:
    return f"{text} [{default}]: " if default else f"{text}: "


def _choice(ask: Ask, say: Say, prompt: str, options: tuple[str, ...], default: str) -> str:
    while True:
        answer = ask(prompt).strip().lower() or default
        if answer in options:
            return answer
        say(f"Type one of: {', '.join(options)}.")


def _ask_until(ask: Ask, say: Say, prompt: str, check: Callable[[str], str], *, default: str | None = None) -> str:
    """Ask until ``check`` accepts the answer (an empty one takes ``default``); returns what it returns."""
    while True:
        answer = ask(prompt).strip() or (default or "")
        if not answer:
            say("An answer is required.")
            continue
        try:
            return check(answer)
        except (SyncError, RemoteError, ValueError) as error:
            say(getattr(error, "message", None) or str(error))
