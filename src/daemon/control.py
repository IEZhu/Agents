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
from .state import state_dir, private_dir, read_json, write_json, atomic_private


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

    def request(self, path="/health", *, method="GET"):
        token = (self.directory / "token").read_text().strip()
        request = Request(f"http://127.0.0.1:{self.config['port']}{path}", method=method,
                          headers={"Authorization": "Bearer " + token})
        try:
            with urlopen(request, timeout=3) as response: return json.load(response)
        except HTTPError as error:
            try: return json.load(error)
            except (ValueError, OSError): raise RuntimeError(f"Service HTTP {error.code}") from None

    def status(self):
        if not self.config: return {"state": "not_installed"}
        try: return self.request()
        except (OSError, URLError):
            result = self.launchctl("print", self.target, check=False)
            return {"state": "starting" if result.returncode == 0 else "stopped",
                    "supervised": result.returncode == 0,
                    "maintenance": (self.directory / "maintenance.json").exists(),
                    "transaction": (self.directory / "transaction.json").exists()}

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

    def install(self, *, port=8765, python=None, node=None):
        private_dir(self.directory)
        root = Path(__file__).resolve().parents[2]
        with file_lock(self.directory / "control.lock", blocking=False), file_lock(root / "data/.sessions.lock", blocking=False):
            if self.config: raise RuntimeError("Service already installed; use start or explicit uninstall")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
            python = os.path.abspath(python or sys.executable)
            git = shutil.which("git")
            if not git: raise RuntimeError("An absolute Git executable is required")
            config = {"installation": str(root), "python": python, "node": node or shutil.which("node"),
                      "git": git, "port": port, "model": "intfloat/multilingual-e5-large",
                      "model_cache": str(Path(os.environ.get("FASTEMBED_CACHE_DIR", "~/.cache/fastembed")).expanduser()),
                      "path": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"), "autostart": True}
            cache = Path(config["model_cache"]) / "models--qdrant--multilingual-e5-large-onnx"
            reference = cache / "refs/main"
            if not reference.is_file():
                raise RuntimeError("The e5-large model must be cached before service installation")
            revision = reference.read_text().strip()
            model_path = cache / "snapshots" / revision
            if not model_path.is_dir():
                raise RuntimeError("The e5-large model must be cached before service installation")
            config.update(model_artifact=cache.name + ":" + revision, model_path=str(model_path))
            write_json(self.directory / "service.json", config)
            atomic_private(self.directory / "token", secrets.token_urlsafe(48) + "\n")
            self.config = config
            self.write_plist()
            write_json(root / "data/.shared-service.json", {"directory": str(self.directory)})
        return {"state": "installed", "directory": str(self.directory), "port": port}

    def write_plist(self, probation=None):
        arguments = [self.config["python"], "-m", "src.daemon", "--state", str(self.directory), "serve"]
        if probation: arguments += ["--probation", probation]
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
            if not status.get("inflight", 0) and not status.get("io_pending", 0): return
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
        with file_lock(self.directory / "control.lock", blocking=False):
            self._stop()
            self.plist.unlink(missing_ok=True)
            marker = Path(self.config["installation"]) / "data/.shared-service.json"
            if read_json(marker, {}).get("directory") == str(self.directory): marker.unlink()
            (self.directory / "service.json").unlink()
        return {"state": "uninstalled", "retained": "private backups, token, workspace registry and history indexes"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Shared Agents-Core service")
    parser.add_argument("--state", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install")
    install.add_argument("--port", type=int, default=8765)
    install.add_argument("--python"); install.add_argument("--node")
    serve = commands.add_parser("serve"); serve.add_argument("--probation")
    for command in ("start", "status", "stop", "restart", "uninstall", "update", "recover", "clear-cache", "audit"):
        commands.add_parser(command)
    workspace = commands.add_parser("workspace")
    workspace.add_argument("action", choices=["register", "list"]); workspace.add_argument("path", nargs="?")
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--workspace", type=Path)
    migrate.add_argument("--clients", default="codex,claude,cursor")
    restore = commands.add_parser("restore-clients"); restore.add_argument("backup", type=Path)
    token = commands.add_parser("token"); token.add_argument("action", choices=["rotate"])
    args = parser.parse_args(argv)
    controller = Controller(args.state)
    if args.command == "serve":
        from .bootstrap import serve
        serve(controller.directory, args.probation)
        return
    if args.command == "install": result = controller.install(port=args.port, python=args.python, node=args.node)
    elif args.command == "audit":
        from .audit import inventory
        result = inventory()
    elif args.command == "workspace":
        from .workspaces import WorkspaceRegistry
        registry = WorkspaceRegistry(controller.directory)
        if args.action == "register":
            if not args.path: parser.error("workspace register requires a path")
            result = {"workspace_id": registry.register(args.path)}
        else: result = read_json(registry.path, {})
    elif args.command == "migrate":
        from .clients import ClientMigration
        migration = ClientMigration(controller.directory)
        clients = args.clients.split(",")
        if "desktop" in clients: clients.append("claude-deny-desktop")
        if "claude" in clients and args.workspace and (args.workspace / ".mcp.json").exists(): clients.append("claude-project")
        changes = [migration.prepare(client, args.workspace) for client in dict.fromkeys(clients)]
        with file_lock(controller.directory / "control.lock", blocking=False):
            from .bootstrap import assert_service_safe
            assert_service_safe(controller.directory)
            result = {"backup": str(migration.apply(changes)), "files": [str(p) for p, _, _ in changes]}
    elif args.command == "restore-clients":
        from .clients import ClientMigration
        controller.stop()
        ClientMigration(controller.directory).restore(args.backup)
        result = {"state": "restored"}
    elif args.command in ("update", "recover"):
        from .update import offline_update, recover
        result = (recover if args.command == "recover" else offline_update)(controller)
    elif args.command == "clear-cache": result = controller.request("/admin/cache/clear", method="POST")
    elif args.command == "token":
        from .rotation import rotate_token
        result = rotate_token(controller)
    else: result = getattr(controller, args.command)()
    print(json.dumps(result, ensure_ascii=False, indent=2))
