"""Acquire leases and check recovery barriers before loading any engine code."""
from contextlib import ExitStack, nullcontext
from logging.handlers import RotatingFileHandler
from pathlib import Path
import logging
import os
import sys

from src.file_lock import file_lock
from src.startup import assert_installation_safe
from .state import private_dir, read_json, state_dir


def assert_service_safe(directory, *, probation=None):
    directory = Path(directory)
    maintenance = read_json(directory / "maintenance.json")
    transaction = read_json(directory / "transaction.json")
    if maintenance or transaction:
        admission = read_json(directory / "probation.json")
        if not probation or not admission or admission.get("nonce") != probation:
            raise RuntimeError("Service is in maintenance; use the controller to recover")
        (directory / "probation.json").unlink()


def serve(directory=None, probation=None):
    directory = private_dir(directory or state_dir())
    config = read_json(directory / "service.json")
    if not config:
        raise RuntimeError("Service is not installed")
    root = Path(config["installation"]).resolve()
    if root != Path(__file__).resolve().parents[2]:
        raise RuntimeError("Service configuration points at a different installation")
    handler = RotatingFileHandler(directory / "service.log", maxBytes=10 * 1024**2, backupCount=5)
    os.chmod(directory / "service.log", 0o600)
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from .diagnostics import LogStream
    sys.stdout = LogStream(logging.getLogger("stdout"), logging.INFO)
    sys.stderr = LogStream(logging.getLogger("stderr"), logging.ERROR)
    with ExitStack() as leases:
        # Controller operations and startup have one ordered admission gate.
        # The controller retains control.lock throughout probation. Its one-use
        # admission is the only startup that may pass that gate.
        admitted = bool(probation and read_json(directory / "probation.json", {}).get("nonce") == probation)
        with (nullcontext() if admitted else file_lock(directory / "control.lock", blocking=False)):
            assert_service_safe(directory, probation=probation)
            leases.enter_context(file_lock(directory / ".daemon.lock", blocking=False))
            leases.enter_context(file_lock(root / "data/.sessions.lock", shared=True, blocking=False))
            assert_installation_safe(str(root))
        token_path = directory / "token"
        if token_path.is_symlink() or token_path.stat().st_mode & 0o077:
            raise PermissionError("Token must be private (0600)")
        token = token_path.read_text().strip()
        # Immutable process configuration, set before application imports only.
        os.environ.update(AGENTS_SERVICE_DIR=str(directory), AGENTS_TRANSPORT="http",
                          AGENTS_ROUTER_DATA_DIR=str(directory / "router"),
                          AGENTS_AUTO_UPDATE="0", AGENTS_AUTO_UPDATE_ENABLED="0",
                          EMBEDDING_MODEL=config["model"],
                          AGENTS_MODEL_ARTIFACT=config["model_artifact"],
                          AGENTS_MODEL_PATH=config["model_path"],
                          FASTEMBED_CACHE_DIR=config["model_cache"],
                          PATH=config["path"])
        from dotenv import load_dotenv
        load_dotenv(root / ".env", override=False)
        from .app import create_app
        import uvicorn
        app = create_app(directory, token, config["port"])
        uvicorn.run(app, host="127.0.0.1", port=config["port"], workers=1,
                    access_log=False, log_config=None, timeout_graceful_shutdown=60)


if __name__ == "__main__":
    serve(*sys.argv[1:])
