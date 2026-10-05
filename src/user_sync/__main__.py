"""Command line for user library sync, usable without the daemon.

    python -m src.user_sync status | run | preview | check | conflicts | pause | resume | disconnect
    python -m src.user_sync setup --remote git@github.com:me/agents-library.git --name … --email …
    python -m src.user_sync start --confirm <preview hash>
    python -m src.user_sync resolve <conflict id> keep|mine|dismiss
    python -m src.user_sync scope [--exclude GROUP] [--include GROUP] [--allow-secret PATH] …
    python -m src.user_sync configure [--fetch-minutes N] [--[no-]ask-new-repositories]

Every command accepts ``--json`` for machine-readable output. The exit code is 0 unless sync
needs attention, a command failed, or the arguments were wrong (2).

``--state DIR`` and ``--library DIR`` (before the command) name the private state directory and
the library explicitly; scheduled runs pass both, so they never depend on the scheduler's
environment. The installation's ``.env`` is read first, as the MCP servers read it. While a command
runs it holds the installation's shared session lease, so an update never replaces the code under it.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import sys

from src.file_lock import file_lock
from src.user_sync.engine import SyncError, Syncer, installation_root


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
    setup = command("setup", "configure the remote, identity and this machine's key")
    setup.add_argument("--remote", required=True, help="git@host:owner/repo, ssh://… or https://…")
    setup.add_argument("--name", required=True, help="commit author name")
    setup.add_argument("--email", required=True, help="commit author email")
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
    return parser


def _execute(syncer: Syncer, arguments) -> dict | list:
    name = arguments.command
    if name == "status":
        return syncer.status()
    if name == "run":
        return syncer.run(force=arguments.force, confirm=arguments.confirm)
    if name == "setup":
        return syncer.setup(remote=arguments.remote, name=arguments.name, email=arguments.email,
                            label=arguments.label, branch=arguments.branch,
                            ask_new_repositories=arguments.ask_new_repositories,
                            trust_host_key=arguments.trust_host_key,
                            confirm_private=arguments.confirm_private)
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
    for key in ("remote", "label", "last_success", "retry_at", "pending", "conflicts", "head"):
        value = result.get(key)
        if value not in (None, [], 0, "") and not isinstance(value, list):
            print(f"  {key}: {value}")
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
    try:
        with file_lock(installation_root() / "data" / ".sessions.lock", shared=True, blocking=False):
            yield
    except BlockingIOError:
        raise SyncError("busy", "an update of Agents-Core is being installed; try again shortly",
                        state="busy") from None


def _failure(error: Exception) -> dict:
    """Every expected failure as a reason, never a traceback."""
    from src.flows import FlowError
    from src.user_sync.gitcmd import GitError, RemoteError
    from src.user_sync.keys import SSHKeyError
    from src.user_sync.scope import ScopeError
    if isinstance(error, SyncError):
        return {"status": error.state, "reason": error.reason, "message": error.message}
    reason = {ScopeError: "scopes_invalid", SSHKeyError: "ssh", GitError: "git_error",
              RemoteError: "unknown_remote", FlowError: "invalid"}.get(type(error), "library_unreadable")
    return {"status": "attention", "reason": reason, "message": str(error)}


def main(argv=None) -> int:
    arguments = _parser().parse_args(argv)
    _load_env()
    from src.flows import FlowError
    from src.user_sync.gitcmd import GitError, RemoteError
    from src.user_sync.keys import SSHKeyError
    from src.user_sync.scope import ScopeError
    try:
        with _session_lease():
            result = _execute(Syncer(arguments.library, arguments.state), arguments)
    except (SyncError, ScopeError, SSHKeyError, GitError, RemoteError, FlowError, OSError) as error:
        result = _failure(error)
        failed = result["status"] != "busy"
    else:
        status = result.get("state") or result.get("status") if isinstance(result, dict) else "ok"
        failed = status in ("attention", "error")
    if arguments.json:
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        print()
    else:
        _print(result, arguments.command)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
