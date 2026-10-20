"""Refresh owned shared-service connections with session-aware transports."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.daemon import registrations
from src.daemon.bootstrap import assert_service_safe
from src.daemon.clients import ClientMigration
from src.file_lock import file_lock


def refresh(server: Path, *, home=None):
    marker = server.resolve().parents[1] / "data/.shared-service.json"
    if not marker.exists():
        return {}
    saved = json.loads(marker.read_text())
    directory = saved.get("directory") if isinstance(saved, dict) else None
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        raise ValueError("Invalid shared-service marker")
    with file_lock(Path(directory) / "control.lock", blocking=False):
        assert_service_safe(directory)
        service = json.loads((Path(directory) / "service.json").read_text())
        installation = service.get("installation") if isinstance(service, dict) else None
        if (not isinstance(installation, str) or not Path(installation).is_absolute()
                or Path(installation).resolve() != server.resolve().parents[1]):
            raise ValueError("Shared service belongs to another installation")
        if not registrations.state(directory)["auto"]:
            return {"registrations": {"status": "skipped", "reason": "automatic upkeep disabled"}}
        ClientMigration(directory).stdio_bridge()
        # All known layers of a client change together; customised entries stop
        # that client's upgrade instead of producing mixed HTTP/stdio layers.
        return registrations.upgrade(directory, home=home)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("Usage: ensure_workspace_bindings.py /absolute/src/server.py", file=sys.stderr)
        return 1
    try:
        report = refresh(Path(argv[0]))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        print("ERROR: Could not refresh workspace bindings; check shared-service state and permissions.", file=sys.stderr)
        return 1
    for client, result in report.items():
        print(f"Workspace binding {client}: {result['status']}")
    return int(any(result["status"] == "failed" for result in report.values()))


if __name__ == "__main__":
    raise SystemExit(main())
