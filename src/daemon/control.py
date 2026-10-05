"""Explicit macOS service control. No model or engine imports on ordinary commands."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.file_lock import file_lock
from src.model_migration import DEFAULT_MODEL, GENERATION
from .state import state_dir, private_dir, read_json, write_json, atomic_private


# Models fastembed downloads into its Hugging Face cache, by cache directory.
HF_CACHE_MODELS = {"intfloat/multilingual-e5-large": "models--qdrant--multilingual-e5-large-onnx"}


def pin_model(model, cache):
    """`model_path` and `model_artifact` of an already downloaded *model*; downloads nothing.

    A model with a plain-file copy (`embedding_prompts.LOCAL_COPIES`) pins that copy
    and its export revision, the value the standalone fingerprint uses. Models in
    `HF_CACHE_MODELS` pin their cached snapshot.
    """
    from src.engine.embedding_prompts import COMPLETE, local_copy, pinned_revision
    copy = local_copy(model, str(cache))
    if copy is not None:
        if not (Path(copy) / COMPLETE).is_file():
            raise RuntimeError(f"The {model} model must be downloaded before service installation")
        return {"model_artifact": pinned_revision(model), "model_path": copy}
    if model not in HF_CACHE_MODELS:
        raise RuntimeError(f"The service cannot pin {model}; use {DEFAULT_MODEL} or one of {sorted(HF_CACHE_MODELS)}")
    folder = Path(cache) / HF_CACHE_MODELS[model]
    reference = folder / "refs/main"
    revision = reference.read_text().strip() if reference.is_file() else ""
    if not revision or not (folder / "snapshots" / revision).is_dir():
        raise RuntimeError(f"The {model} model must be cached before service installation")
    return {"model_artifact": folder.name + ":" + revision, "model_path": str(folder / "snapshots" / revision)}


def switched_model_config(config):
    """*config* moved to the default model, downloading its plain-file copy when missing."""
    from src.engine.embedding_prompts import materialize
    materialize(DEFAULT_MODEL, config["model_cache"])
    return {**config, "model": DEFAULT_MODEL, "model_generation": GENERATION,
            **pin_model(DEFAULT_MODEL, config["model_cache"])}


class Controller:
    def __init__(self, directory=None):
        self.directory = Path(directory or state_dir())
        self.config = read_json(self.directory / "service.json", {})

    @property
    def label(self):
        return "local.agents-core." + self.directory.name

    @property
    def target(self): return f"gui/{os.getuid()}/{self.label}"

    @property
    def plist(self): return Path.home() / "Library/LaunchAgents" / (self.label + ".plist")

    def launchctl(self, *args, check=True):
        return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, check=check)

    def request(self, path="/health", *, method="GET", body=None, timeout=3, status=False):
        """The service's JSON answer; with ``status``, ``(HTTP status, JSON)``."""
        token = (self.directory / "token").read_text().strip()
        headers = {"Authorization": "Bearer " + token}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(f"http://127.0.0.1:{self.config['port']}{path}", data=data, method=method,
                          headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                value = json.load(response)
                return (response.status, value) if status else value
        except HTTPError as error:
            try: value = json.load(error)
            except (ValueError, OSError): raise RuntimeError(f"Service HTTP {error.code}") from None
            return (error.code, value) if status else value

    def status(self):
        if not self.config: return {"state": "not_installed"}
        try: return self.request()
        except (OSError, URLError):
            result = self.launchctl("print", self.target, check=False)
            return {"state": "starting" if result.returncode == 0 else "stopped",
                    "supervised": result.returncode == 0,
                    "maintenance": (self.directory / "maintenance.json").exists(),
                    "transaction": (self.directory / "transaction.json").exists()}

    def user_sync(self, command, arguments=None):
        """A user-sync command through the service, or through the engine here when it is down.

        Returns ``(failed, result)``; ``result["via"]`` says which ran it. Only a refused
        connection (nothing listens: the service is stopped) falls back; a slow or failing service
        does not, so an operation it may still finish never runs twice. The engine's sync lock
        keeps a direct run from overlapping the service's loop: one of them reports ``lock_held``.
        """
        from .sync_loop import LONG_REQUEST_TIMEOUT, REQUEST_TIMEOUTS, direct
        arguments = arguments or {}
        if self.config.get("port") and (self.directory / "token").is_file():
            method = "GET" if command == "status" else "POST"
            try:
                code, result = self.request("/admin/user-sync/" + command, method=method,
                                            body=None if method == "GET" else arguments, status=True,
                                            timeout=REQUEST_TIMEOUTS.get(command, LONG_REQUEST_TIMEOUT))
            except (OSError, RuntimeError, ValueError) as error:
                reason = error.reason if isinstance(error, URLError) else error
                if isinstance(reason, TimeoutError):
                    return True, {"status": "error", "reason": "timeout", "via": "service",
                                  "message": "the service did not answer in time; the operation may still finish there"}
                if isinstance(reason, (RuntimeError, ValueError)):  # an answer that is not the service's JSON
                    return True, {"status": "error", "reason": "service_error", "via": "service",
                                  "message": f"{type(reason).__name__}: {reason}"}
                if not isinstance(reason, ConnectionRefusedError):
                    return True, {"status": "error", "reason": "service_unreachable",
                                  "message": f"{type(reason).__name__}: {reason}", "via": "service"}
            else:
                return self._sync_failed(code, result), {**result, "via": "service"}
        code, result = direct(self.directory, self.config.get("installation") or Path(__file__).resolve().parents[2],
                              command, arguments)
        return self._sync_failed(code, result), {**result, "via": "direct"}

    @staticmethod
    def _sync_failed(code, result):
        """Exit 1 for an error or a state that needs attention; ``busy`` (another runner or an update
        holds the library) is not a failure, as in ``python -m src.user_sync``."""
        state = result.get("state") or result.get("status")  # status answers name it "state"
        if state == "busy":
            return False
        return code >= 400 or state in ("attention", "error")

    def sync_summary(self):
        """The short sync status when the service does not answer; never raises."""
        try:
            from .sync_loop import direct_summary
            return direct_summary(self.directory, self.config.get("installation") or Path(__file__).resolve().parents[2])
        except Exception as error:
            return {"state": "unknown", "reason": type(error).__name__}

    def wait_ready(self, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status()
            if status.get("state") == "ready":
                if status.get("install_root") != self.config["installation"]:
                    raise RuntimeError("Port belongs to a different installation")
                return status
            if status.get("state") == "failed": raise RuntimeError("Runtime warmup failed; inspect service.log")
            time.sleep(.2)
        raise TimeoutError("Service did not become ready within the warmup budget")

    def install(self, *, port=8765, python=None, node=None, model=None):
        private_dir(self.directory)
        root = Path(__file__).resolve().parents[2]
        with file_lock(self.directory / "control.lock", blocking=False), file_lock(root / "data/.sessions.lock", blocking=False):
            if self.config: raise RuntimeError("Service already installed; use start or explicit uninstall")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
            python = os.path.abspath(python or sys.executable)
            git = shutil.which("git")
            if not git: raise RuntimeError("An absolute Git executable is required")
            node = node or shutil.which("node")
            config = {"installation": str(root), "python": python, "node": os.path.abspath(node) if node else None,
                      "git": git, "port": port, "model": model or DEFAULT_MODEL,
                      "model_cache": str(Path(os.environ.get("FASTEMBED_CACHE_DIR", "").strip() or "~/.cache/fastembed").expanduser()),
                      "path": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"), "autostart": True,
                      "model_generation": GENERATION}
            config.update(pin_model(config["model"], config["model_cache"]))
            write_json(self.directory / "service.json", config)
            atomic_private(self.directory / "token", secrets.token_urlsafe(48) + "\n")
            self.config = config
            self.write_plist()
            write_json(root / "data/.shared-service.json", {"directory": str(self.directory)})
        return {"state": "installed", "directory": str(self.directory), "port": port,
                "scheduled_sync": stop_scheduled_sync(root), "user_sync": sync_note(self.directory)}

    def write_plist(self, probation=None):
        arguments = [self.config["python"], "-m", "src.daemon", "--state", str(self.directory), "serve"]
        # One argument: a URL-safe nonce may start with "-", which argparse would read as an option.
        if probation: arguments.append("--probation=" + probation)
        payload = {"Label": self.label, "ProgramArguments": arguments,
                   "WorkingDirectory": self.config["installation"],
                   "EnvironmentVariables": {"PATH": self.config["path"], "PYTHONUNBUFFERED": "1"},
                   "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
                   "ThrottleInterval": 15, "ProcessType": "Interactive",
                   "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
        atomic_private(self.plist, plistlib.dumps(payload).decode())

    def _start(self, probation=None):
        self.write_plist(probation)
        loaded = self.launchctl("print", self.target, check=False).returncode == 0
        if not loaded:
            self.launchctl("bootstrap", f"gui/{os.getuid()}", str(self.plist))
        else:
            self.launchctl("kickstart", self.target)

    def start(self):
        with file_lock(self.directory / "control.lock", blocking=False):
            from .bootstrap import assert_service_safe
            assert_service_safe(self.directory)
            self._start()
        return self.wait_ready()

    def _drain(self, timeout=60):
        try: self.request("/admin/drain", method="POST")
        except (OSError, URLError): return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.request()
            # Streams end on drain; waiting for them lets each client receive
            # its final chunk before launchd stops the process.
            if not any(status.get(key, 0) for key in ("inflight", "io_pending", "streams")): return
            time.sleep(.1)
        self.request("/admin/resume", method="POST")
        raise TimeoutError("Drain timed out; runtime resumed without killing active work")

    def _stop(self):
        self._drain()
        result = self.launchctl("bootout", self.target, check=False)
        if result.returncode and self.launchctl("print", self.target, check=False).returncode == 0:
            raise RuntimeError("launchd could not stop the service")
        deadline = time.monotonic() + 60
        while True:
            try:
                with file_lock(self.directory / ".daemon.lock", blocking=False): return
            except BlockingIOError:
                if time.monotonic() > deadline: raise TimeoutError("Daemon still holds its lease")
                time.sleep(.1)

    def stop(self):
        with file_lock(self.directory / "control.lock", blocking=False):
            self._stop()
        return {"state": "stopped"}

    def restart(self):
        self.stop()
        return self.start()

    def uninstall(self):
        from .autoupdate import disable
        disable(self)
        with file_lock(self.directory / "control.lock", blocking=False):
            self._stop()
            self.plist.unlink(missing_ok=True)
            marker = Path(self.config["installation"]) / "data/.shared-service.json"
            if read_json(marker, {}).get("directory") == str(self.directory): marker.unlink()
            (self.directory / "service.json").unlink()
        return {"state": "uninstalled", "retained": "private backups, token, workspace registry and history indexes",
                "user_sync": "the daemon no longer syncs the library; to keep syncing without it run "
                             "`python -m src.user_sync schedule enable`"}


def sync_note(directory):
    """What ``install`` says about user library sync (#171): set up, pending or off, and what to do.

    ``directory`` is the service's state directory, the one ``install --state`` chose: the service
    and the engine keep sync's settings in its ``user-sync``.
    """
    try:
        settings = read_json(Path(directory) / "user-sync" / "user-sync.json")
    except (OSError, ValueError):
        settings = None
    finish = ("run `python -m src.daemon start`, then `python -m src.daemon flows-ui` and open its Sync page, "
              "or `python -m src.user_sync setup` in a terminal")
    if isinstance(settings, dict) and settings.get("started"):
        return ("set up: the service runs the sync loop once it starts; "
                "`python -m src.daemon user-sync status` shows its state")
    if isinstance(settings, dict):
        return "pending: sync is set up but has not started; to finish it, " + finish
    return "off: to sync personal flows between machines, " + finish


def stop_scheduled_sync(installation=None):
    """Remove the scheduled sync run (#168): the daemon runs the sync loop itself, and a job from
    before the install would only compete with it for the sync lock.

    Returns ``removed``, ``none`` (nothing was scheduled) or ``failed: <reason>``; never raises.
    """
    try:
        from src.user_sync import schedule
        options = {"installation": installation} if installation else {}
        found = schedule.status(**options)
        # A plist that launchd has not loaded yet (``interval_minutes``) would load at the next login.
        if not (found.get("scheduled") or found.get("interval_minutes")):
            return "none"
        if schedule.disable(**options).get("scheduled"):
            return "failed: the scheduled run is still loaded"
        return "removed"
    except Exception as error:  # the sync lock still keeps the two from overlapping
        return f"failed: {error}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Shared Agents-Core service")
    parser.add_argument("--state", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install")
    install.add_argument("--port", type=int, default=8765)
    install.add_argument("--python"); install.add_argument("--node")
    install.add_argument("--model", help=f"embedding model (default {DEFAULT_MODEL}); it must already be downloaded")
    serve = commands.add_parser("serve"); serve.add_argument("--probation")
    for command in ("start", "status", "stop", "restart", "uninstall", "update", "recover", "clear-cache"):
        commands.add_parser(command)
    audit = commands.add_parser("audit")
    audit.add_argument("--workspace", type=Path)
    audit.add_argument("--client-config", action="append", default=[], metavar="CLIENT=PATH")
    flows_ui = commands.add_parser("flows-ui", help="open the local flow editor in a browser")
    flows_ui.add_argument("--no-open", action="store_true", help="print the one-use URL only")
    flows_ui.add_argument("--revoke", action="store_true", help="end every browser session of the editor")
    flows_ui.add_argument("--auto", choices=["on", "off"],
                          help="allow or refuse sign-in without a code for browsers of this OS user; "
                               "off also ends every session")
    workspace = commands.add_parser("workspace")
    workspace.add_argument("action", choices=["register", "list"]); workspace.add_argument("path", nargs="?")
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--workspace", type=Path)
    migrate.add_argument("--clients", default="codex,claude,cursor")
    migrate.add_argument("--client-config", action="append", default=[], metavar="CLIENT=PATH")
    restore = commands.add_parser("restore-clients"); restore.add_argument("backup", type=Path)
    token = commands.add_parser("token"); token.add_argument("action", choices=["rotate"])
    auto = commands.add_parser("auto-update", help="unattended updates from the tracked branch")
    auto.add_argument("action", choices=["enable", "disable", "status", "run"])
    auto.add_argument("--interval", type=int, help="seconds between checks (default 900)")
    auto.add_argument("--idle-seconds", type=int, help="apply only after this long without requests (default 120)")
    sync = commands.add_parser("user-sync", help="sync the personal flow library through the service; "
                                                 "uses the engine directly when the service is down")
    sync_commands = sync.add_subparsers(dest="sync_command", required=True)
    for name, text in (("status", "show the sync state and the service's loop"),
                       ("check", "check access to the remote"),
                       ("preview", "show what starting or confirming sync would upload and download"),
                       ("pause", "pause sync on this machine"), ("resume", "resume sync on this machine"),
                       ("disconnect", "stop syncing on this machine; files and .git stay")):
        sync_commands.add_parser(name, help=text)
    sync_run = sync_commands.add_parser("run", help="sync now, also during the retry delay after a network error")
    sync_run.add_argument("--confirm", metavar="HASH", help="confirm a previewed join or rewritten remote")
    sync_start = sync_commands.add_parser("start", help="start sync after reviewing the preview")
    sync_start.add_argument("--confirm", metavar="HASH", required=True, help="the preview's hash")
    sync_setup = sync_commands.add_parser("setup", help="configure the remote, identity and this machine's key")
    sync_setup.add_argument("--remote", required=True, help="git@host:owner/repo, ssh://… or https://…")
    sync_setup.add_argument("--name", required=True, help="commit author name")
    sync_setup.add_argument("--email", required=True, help="commit author email")
    sync_setup.add_argument("--label", help="this machine's label (default: platform and a random suffix)")
    sync_setup.add_argument("--branch", default="main")
    sync_setup.add_argument("--ask-new-repositories", action="store_true", default=None,
                            help="ask before uploading flows of a repository that is new to the library")
    sync_setup.add_argument("--trust-host-key", metavar="SHA256:…", help="confirm the host key fingerprint")
    sync_setup.add_argument("--confirm-private", action="store_true", default=None,
                            help="confirm the repository is private when the host cannot be checked")
    args = parser.parse_args(argv)
    overrides = []
    if args.command in ("audit", "migrate"):
        from src.client_paths import parse_client_configs, CLIENTS
        try:
            overrides = parse_client_configs(args.client_config, multiple=args.command == "audit")
        except ValueError as error:
            parser.error(str(error))
        if args.command == "migrate":
            clients = [client.strip() for client in args.clients.split(",")]
            if any(client not in CLIENTS for client in clients):
                parser.error("Unknown client in --clients")
            if "desktop" in clients: clients.append("claude-deny-desktop")
            if "claude" in clients and args.workspace and (args.workspace / ".mcp.json").exists(): clients.append("claude-project")
            if any(client not in clients for client, _ in overrides):
                parser.error("Each --client-config target must be selected by --clients")
    controller = Controller(args.state)
    exit_code = 0
    if args.command == "serve":
        from .bootstrap import serve
        serve(controller.directory, args.probation)
        return
    if args.command == "install": result = controller.install(port=args.port, python=args.python, node=args.node,
                                                                     model=args.model)
    elif args.command == "audit":
        from .audit import inventory
        result = inventory(workspace=args.workspace, client_configs=overrides, directory=controller.directory)
    elif args.command == "workspace":
        from .workspaces import WorkspaceRegistry
        registry = WorkspaceRegistry(controller.directory)
        if args.action == "register":
            if not args.path: parser.error("workspace register requires a path")
            result = {"workspace_id": registry.register(args.path)}
        else: result = read_json(registry.path, {})
    elif args.command == "migrate":
        from .clients import ClientMigration
        selected = dict(overrides)
        with file_lock(controller.directory / "control.lock", blocking=False):
            from .bootstrap import assert_service_safe
            assert_service_safe(controller.directory)
            migration = ClientMigration(controller.directory)
            changes = [migration.prepare(client, args.workspace, config_path=selected.get(client)) for client in dict.fromkeys(clients)]
            result = {"backup": str(migration.apply(changes)), "files": [str(p) for p, _, _ in changes]}
    elif args.command == "restore-clients":
        from .clients import ClientMigration
        from .bootstrap import assert_service_safe
        with file_lock(controller.directory / "control.lock", blocking=False):
            assert_service_safe(controller.directory)
            controller._stop()
            ClientMigration(controller.directory).restore(args.backup)
        result = {"state": "restored"}
    elif args.command in ("update", "recover"):
        from .update import offline_update, recover
        result = (recover if args.command == "recover" else offline_update)(controller)
    elif args.command == "clear-cache": result = controller.request("/admin/cache/clear", method="POST")
    elif args.command == "flows-ui" and (args.revoke or args.auto):
        from .flows_ui import auto_sign_in_enabled, replace_session_key, set_auto_sign_in
        try:
            with file_lock(controller.directory / "control.lock", blocking=False):
                state_dir = private_dir(controller.directory)
                if args.auto:
                    set_auto_sign_in(state_dir, args.auto == "on")
                if args.revoke:
                    replace_session_key(state_dir)
                auto = "on" if auto_sign_in_enabled(state_dir) else "off"
            result = {"auto_sign_in": auto}
            if args.revoke or args.auto == "off":
                result.update(state="revoked", note="every session ended; " + (
                    "browsers of this OS user sign in again by themselves" if auto == "on"
                    else "every browser needs a one-use code from flows-ui"))
        except BlockingIOError:
            result = {"state": "not_revoked" if args.revoke else "not_changed",
                      "error": "another control command is running; try again"}
    elif args.command == "flows-ui":
        result = controller.request("/admin/ui/code", method="POST")
        if "url" in result and not args.no_open:
            import webbrowser
            webbrowser.open(result["url"])
        result = {"url": result.get("url"), "error": result.get("error"),
                  "note": "one-use link, valid for 2 minutes; the browser then stays signed in for 30 days after its last visit"}
    elif args.command == "auto-update":
        from . import autoupdate
        if args.action == "enable":
            result = autoupdate.enable(controller, autoupdate.DEFAULT_INTERVAL if args.interval is None else args.interval,
                                       autoupdate.DEFAULT_IDLE_SECONDS if args.idle_seconds is None else args.idle_seconds)
        else:
            result = getattr(autoupdate, args.action)(controller)
    elif args.command == "token":
        from .rotation import rotate_token
        result = rotate_token(controller)
    elif args.command == "user-sync":
        failed, result = controller.user_sync(args.sync_command, _sync_arguments(args))
        exit_code = 1 if failed else 0
    else: result = getattr(controller, args.command)()
    if args.command == "status" and "user_sync" not in result:
        result["user_sync"] = controller.sync_summary()  # the service does not answer
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


def _sync_arguments(args):
    """The JSON arguments of a user-sync command, as /admin/user-sync takes them."""
    if args.sync_command == "setup":
        values = {"remote": args.remote, "name": args.name, "email": args.email, "label": args.label,
                  "branch": args.branch, "ask_new_repositories": args.ask_new_repositories,
                  "trust_host_key": args.trust_host_key, "confirm_private": args.confirm_private}
        return {key: value for key, value in values.items() if value is not None}
    if args.sync_command in ("run", "start") and args.confirm is not None:
        return {"confirm": args.confirm}
    return {}
