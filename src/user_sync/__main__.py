"""Command line for user library sync, usable without the daemon.

    python -m src.user_sync status | run | preview | check | conflicts | pause | resume | disconnect
    python -m src.user_sync setup        # the wizard, in a terminal
    python -m src.user_sync setup --github OWNER/NAME --name … --email …
    python -m src.user_sync setup --remote git@github.com:me/agents-library.git --name … --email …
    python -m src.user_sync start --confirm <preview hash>
    python -m src.user_sync resolve <conflict id> keep|mine|dismiss
    python -m src.user_sync scope [--exclude GROUP] [--include GROUP] [--allow-secret PATH] …
    python -m src.user_sync configure [--fetch-minutes N] [--[no-]ask-new-repositories]
    python -m src.user_sync github login | status | logout | libraries | create [NAME] | add-key

Every command accepts ``--json`` for machine-readable output; prompts and sign-in codes then go
to stderr. The exit code is 0 unless sync needs attention, a command failed or was cancelled, or
the arguments were wrong (2).

``--state DIR`` and ``--library DIR`` (before the command) name the private state directory and
the library explicitly; scheduled runs pass both, so they never depend on the scheduler's
environment. The installation's ``.env`` is read first, as the MCP servers read it. While a command
runs it holds the installation's shared session lease, so an update never replaces the code under it.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import json
import sys
import time
from types import SimpleNamespace
import webbrowser

from src.file_lock import file_lock
from src.user_sync.engine import SyncError, Syncer, installation_root
from src.user_sync.github import DEFAULT_REPO_NAME, GitHubError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.user_sync",
                                     description="Sync the personal flow library between machines.")
    parser.add_argument("--state", metavar="DIR", help="private state directory (default: per installation)")
    parser.add_argument("--library", metavar="DIR", help="the library (default: flows/.user or AGENTS_USER_FLOWS_DIR)")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, help_text):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--json", action="store_true", help="print JSON")
        return sub

    command("status", "show the sync state")
    run = command("run", "run one sync cycle")
    run.add_argument("--force", action="store_true", help="ignore the retry delay after a network error")
    run.add_argument("--confirm", metavar="HASH", help="confirm a previewed join or rewritten remote")
    setup = command("setup", "configure the remote, identity and this machine's key; "
                             "without --remote or --github, the wizard")
    target = setup.add_mutually_exclusive_group()
    target.add_argument("--remote", help="git@host:owner/repo, ssh://… or https://…")
    target.add_argument("--github", metavar="OWNER/NAME",
                        help="a private repository of the connected GitHub account; adds this machine's deploy key")
    setup.add_argument("--name", help="commit author name (required with --remote or --github)")
    setup.add_argument("--email", help="commit author email (required with --remote or --github)")
    setup.add_argument("--label", help="this machine's label (default: platform and a random suffix)")
    setup.add_argument("--branch", default="main")
    setup.add_argument("--ask-new-repositories", action="store_true", default=None,
                       help="ask before uploading flows of a repository that is new to the library")
    setup.add_argument("--trust-host-key", metavar="SHA256:…", help="confirm the host key fingerprint")
    setup.add_argument("--confirm-private", action="store_true", default=None,
                       help="confirm the repository is private when the host cannot be checked")
    command("check", "check access to the remote")
    command("preview", "show what starting or confirming sync would upload and download")
    start = command("start", "start sync after reviewing the preview")
    start.add_argument("--confirm", metavar="HASH", required=True, help="the preview's hash")
    command("conflicts", "list conflicts")
    resolve = command("resolve", "resolve a conflict")
    resolve.add_argument("id")
    resolve.add_argument("action", choices=("keep", "mine", "dismiss"))
    scopes = command("scope", "show or change what syncs")
    for option, help_text in (("--exclude", "stop syncing a group (common, personas, history, "
                                            "components or repos/<key>)"),
                              ("--include", "sync a group again"), ("--exclude-file", "stop syncing a file"),
                              ("--include-file", "sync a file again"),
                              ("--allow-secret", "upload this file although the scanner flagged it"),
                              ("--approve", "upload a held group on this machine (repos/<key>, history…)"),
                              ("--approve-file", "upload a held file on this machine")):
        scopes.add_argument(option, action="append", default=[], metavar="VALUE", help=help_text)
    scopes.add_argument("--confirm", metavar="HASH", help="confirm a change that uploads more")
    configure = command("configure", "change the fetch interval or the ask-before-upload setting")
    configure.add_argument("--fetch-minutes", type=int, metavar="MINUTES", help="1-60")
    configure.add_argument("--ask-new-repositories", action=argparse.BooleanOptionalAction, default=None,
                           help="ask before uploading flows of a repository that is new to the library")
    command("pause", "pause sync on this machine")
    command("resume", "resume sync on this machine")
    command("disconnect", "stop syncing on this machine; files and .git stay")
    hub = commands.add_parser("github", help="the GitHub account: sign-in, the library repository, deploy keys")
    actions = hub.add_subparsers(dest="github_command", required=True)
    for name, help_text in (("login", "sign in with a device code"),
                            ("status", "show the connected account"),
                            ("logout", "forget the account on this machine (Forget account)"),
                            ("libraries", "list your private repositories that hold a library"),
                            ("create", "create a private repository for the library"),
                            ("add-key", "add this machine's deploy key to the sync repository again")):
        action = actions.add_parser(name, help=help_text)
        action.add_argument("--json", action="store_true", help="print JSON")
        if name == "create":
            action.add_argument("name", nargs="?", default=DEFAULT_REPO_NAME,
                                help=f"the repository's name (default: {DEFAULT_REPO_NAME})")
    parser.setup_parser = setup
    return parser


def _execute(syncer: Syncer, arguments, console: SimpleNamespace) -> dict | list:
    name = arguments.command
    if name == "status":
        return syncer.status()
    if name == "run":
        return syncer.run(force=arguments.force, confirm=arguments.confirm)
    if name == "setup" and arguments.github:
        return syncer.setup_github(arguments.github, name=arguments.name, email=arguments.email,
                                   label=arguments.label, branch=arguments.branch,
                                   ask_new_repositories=arguments.ask_new_repositories,
                                   trust_host_key=arguments.trust_host_key)
    if name == "setup" and not arguments.remote:
        from src.user_sync import wizard
        return wizard.run(syncer, ask=console.ask, say=console.say, open_browser=console.open_browser,
                          sleep=console.sleep)
    if name == "setup":
        return syncer.setup(remote=arguments.remote, name=arguments.name, email=arguments.email,
                            label=arguments.label, branch=arguments.branch,
                            ask_new_repositories=arguments.ask_new_repositories,
                            trust_host_key=arguments.trust_host_key,
                            confirm_private=arguments.confirm_private)
    if name == "github":
        return _github(syncer, arguments.github_command, arguments, console)
    if name == "check":
        return syncer.check()
    if name == "preview":
        return syncer.preview()
    if name == "start":
        return syncer.start(arguments.confirm)
    if name == "conflicts":
        return syncer.conflicts()
    if name == "resolve":
        return syncer.resolve(arguments.id, arguments.action)
    if name == "configure":
        return syncer.configure(fetch_minutes=arguments.fetch_minutes,
                                ask_new_repositories=arguments.ask_new_repositories)
    if name == "scope":
        changes = dict(exclude=arguments.exclude, include=arguments.include,
                       exclude_files=arguments.exclude_file, include_files=arguments.include_file,
                       allow_paths=arguments.allow_secret, approve=arguments.approve,
                       approve_files=arguments.approve_file)
        if any(changes.values()):
            return syncer.change_scopes(**changes, confirm=arguments.confirm)
        return syncer.scopes()
    return getattr(syncer, name)()


def _github(syncer: Syncer, action: str, arguments, console: SimpleNamespace) -> dict:
    """``github login | status | logout | libraries | create [NAME] | add-key``."""
    account = syncer.github_account()
    if action == "status":
        status = account.status()
        if status["reconnect_needed"]:
            return {**status, "status": "attention", "reason": "reconnect_needed",
                    "message": "GitHub refused the stored authorization; sign in again with github login"}
        return {**status, "status": "connected" if status["connected"] else "not_connected"}
    if action == "login":
        device = account.start_sign_in()
        console.say(f"Open {device.verification_uri} and enter the code {device.user_code}")
        try:
            console.open_browser(device.verification_uri)
        except Exception:  # no browser here: the code above is enough
            pass
        console.say(f"Waiting for GitHub; the code expires in {max(device.expires_in // 60, 1)} minutes.")
        return {**account.wait_for_sign_in(device, sleep=console.sleep), "status": "connected"}
    if action == "logout":
        result = account.forget()
        return {**result, "status": "forgotten",
                "message": "the GitHub authorization is deleted from this machine; revoke it on GitHub at "
                           f"{result['revoke_url']}"}
    if action == "libraries":
        scan = syncer.github_client().libraries()
        repositories = [{"full_name": info.full_name, "ssh_url": info.ssh_url, "archived": info.archived}
                        for info in scan.libraries]
        message = None if repositories else "no private repository of this account holds a library yet"
        return {"status": "ok", "repositories": repositories, "checked": scan.checked,
                "truncated": scan.truncated, "message": message}
    if action == "create":
        info = syncer.github_client().create_library(arguments.name)
        return {"status": "created", "repository": info.full_name, "ssh_url": info.ssh_url,
                "message": f"next: python -m src.user_sync setup --github {info.full_name} --name … --email …"}
    return syncer.add_deploy_key()  # add-key


def _print(result, command: str) -> None:
    if isinstance(result, list):  # conflicts
        if not result:
            print("No conflicts.")
        for record in result:
            kept = record.get("kept")
            print(f"{record['id']}  {record.get('flow') or record.get('path')}  {record.get('kind')}, "
                  f"kept the {kept} version, from {record.get('machine')}")
        return
    status = result.get("state") or result.get("status")
    line = f"{command}: {status}"
    if result.get("reason"):
        line += f" ({result['reason']})"
    print(line)
    if result.get("message"):
        print(f"  {result['message']}")
    for step in result.get("steps", []):
        print(f"  - {step}")
    for key in ("remote", "label", "last_success", "retry_at", "pending", "conflicts", "head",
                "login", "host", "storage", "repository", "ssh_url", "deploy_key", "revoke_url"):
        value = result.get(key)
        if value not in (None, [], 0, "") and not isinstance(value, list):
            print(f"  {key}: {value}")
    if result.get("warning"):
        print(f"  warning: {result['warning']}")
    for repository in result.get("repositories", []):
        print(f"  {repository['full_name']}  {repository['ssh_url']}")
    if result.get("truncated"):
        print(f"  only the {result['checked']} most recently pushed repositories were searched")
    if result.get("public_key"):
        print(f"  public key: {result['public_key']}")
    for fingerprint in result.get("fingerprints", []):
        print(f"  host key: {fingerprint}")
    for key in ("upload", "download", "remove_from_remote", "delete_local", "conflicts", "blocked", "held",
                "pending_groups", "pending_files", "sent", "received"):
        items = result.get(key)
        if isinstance(items, list) and items:
            print(f"  {key}:")
            for item in items:
                print(f"    {item.get('flow') or item.get('path') if isinstance(item, dict) else item}")
    for group in result.get("repository_groups", []):
        marker = " (new)" if group.get("new") else ""
        print(f"  repository {group['group']}: {group.get('origin')}, {group['files']} files{marker}")
    if result.get("hash"):
        print(f"  confirm with: --confirm {result['hash']}")


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(installation_root() / ".env", override=False)


@contextmanager
def _session_lease():
    """The shared lease stdio servers hold; an update activates only when nobody holds it."""
    with ExitStack() as stack:
        try:  # only taking the lease can mean an update; errors inside the command are its own
            stack.enter_context(file_lock(installation_root() / "data" / ".sessions.lock",
                                          shared=True, blocking=False))
        except BlockingIOError:
            raise SyncError("busy", "an update of Agents-Core is being installed; try again shortly",
                            state="busy") from None
        yield


def _failure(error: Exception) -> dict:
    """Every expected failure as a reason, never a traceback."""
    from src.flows import FlowError
    from src.user_sync.gitcmd import GitError, RemoteError
    from src.user_sync.keys import SSHKeyError
    from src.user_sync.scope import ScopeError
    from src.user_sync.wizard import Cancelled
    if isinstance(error, SyncError):
        return {"status": error.state, "reason": error.reason, "message": error.message, **error.details}
    if isinstance(error, GitHubError):
        return {"status": "attention", "reason": error.code, "message": error.message}
    if isinstance(error, (Cancelled, EOFError, KeyboardInterrupt)):
        return {"status": "cancelled", "reason": "cancelled", "message": "stopped; nothing was uploaded"}
    reasons = ((ScopeError, "scopes_invalid"), (SSHKeyError, "ssh"), (GitError, "git_error"),
               (RemoteError, "unknown_remote"), (FlowError, "invalid"))
    reason = next((name for kind, name in reasons if isinstance(error, kind)), "library_unreadable")
    return {"status": "attention", "reason": reason, "message": str(error)}


def _console(json_output: bool, ask=None, say=None, open_browser=None, sleep=None) -> SimpleNamespace:
    """Prompts and notices for the wizard and ``github login``; on stderr when stdout carries JSON."""
    stream = sys.stderr if json_output else sys.stdout

    def default_say(text: str) -> None:
        print(text, file=stream, flush=True)

    def default_ask(prompt: str) -> str:
        stream.write(prompt)
        stream.flush()
        return input()

    return SimpleNamespace(ask=ask or default_ask, say=say or default_say,
                           open_browser=open_browser or webbrowser.open, sleep=sleep or time.sleep)


def main(argv=None, *, ask=None, say=None, open_browser=None, sleep=None, interactive=None) -> int:
    """``ask``, ``say``, ``open_browser``, ``sleep`` and ``interactive`` (a terminal on stdin) are for tests."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "setup":
        if arguments.remote or arguments.github:
            if not (arguments.name and arguments.email):
                parser.setup_parser.error("--name and --email are required with --remote or --github")
        elif not (sys.stdin.isatty() if interactive is None else interactive):
            parser.setup_parser.print_usage(sys.stderr)
            print("setup needs --remote or --github, or a terminal for the wizard", file=sys.stderr)
            return 2
    _load_env()
    from src.flows import FlowError
    from src.user_sync.gitcmd import GitError, RemoteError
    from src.user_sync.keys import SSHKeyError
    from src.user_sync.scope import ScopeError
    from src.user_sync.wizard import Cancelled
    console = _console(arguments.json, ask, say, open_browser, sleep)
    interrupted = False
    try:
        with _session_lease():
            result = _execute(Syncer(arguments.library, arguments.state), arguments, console)
    except (SyncError, ScopeError, SSHKeyError, GitError, RemoteError, FlowError, GitHubError,
            Cancelled, EOFError, OSError) as error:
        result = _failure(error)
        failed = result["status"] != "busy"
    except KeyboardInterrupt as error:
        result, failed, interrupted = _failure(error), True, True
    else:
        status = result.get("state") or result.get("status") if isinstance(result, dict) else "ok"
        failed = status in ("attention", "error")
    if arguments.json:
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        print()
    else:
        command = arguments.command if arguments.command != "github" else f"github {arguments.github_command}"
        _print(result, command)
    return 130 if interrupted else 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
