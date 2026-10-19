"""Register standalone Agents-Core in a detected Codex profile (Python 3.11+)."""
import argparse
import copy
import os
from pathlib import Path
import stat
import sys
import tempfile
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts._helpers.backup_file import backup_file
from scripts._helpers.install_codex_instructions import detect_codex_home
from src.daemon.clients import replace_toml_server

SERVER = "Agents-Core"


class ConfigurationError(ValueError):
    """A content-free diagnostic safe to show without exposing TOML values."""


def configure(home: Path, python: Path, server: Path, *, ld_library_path: str = "") -> str:
    """Preserve existing transports; update standalone paths without losing options."""
    target = home.expanduser().absolute() / "config.toml"
    if target.is_symlink():
        raise ConfigurationError("Codex config.toml is a symlink; use a regular file before retrying")
    exists = target.exists()
    original = target.read_bytes() if exists else b""
    document = tomllib.loads(original.decode("utf-8"))
    servers = document.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcp_servers must be a table")
    old = servers.get(SERVER, {})
    if not isinstance(old, dict):
        raise ValueError("Agents-Core must be a table")
    arguments = old.get("args", [])
    bridge = (isinstance(arguments, list) and arguments
              and str(arguments[0]).replace("\\", "/").endswith("bridge/stdio.mjs"))
    if old.get("url") or bridge:
        return f"Codex MCP existing HTTP/bridge connection preserved: {target}"
    python, server = python.expanduser().absolute(), server.expanduser().absolute()
    if not python.is_file() or not server.is_file():
        raise ConfigurationError("The interpreter or server file is missing; check installation paths")
    entry = {**old, "command": str(python),
             "args": [str(server)]}
    if ld_library_path:
        environment = dict(old.get("env", {}))
        existing = environment.get("LD_LIBRARY_PATH", "")
        if not isinstance(existing, str):
            raise ConfigurationError("LD_LIBRARY_PATH must be a string")
        if ld_library_path not in existing.split(":"):
            environment["LD_LIBRARY_PATH"] = f"{ld_library_path}:{existing}" if existing else ld_library_path
        entry["env"] = environment
    if old == entry:
        return f"Codex MCP already current: {target}"
    try:
        updated = replace_toml_server(original.decode("utf-8"), SERVER, entry).encode("utf-8")
    except (ValueError, TypeError):
        raise ConfigurationError("Unsupported TOML layout; convert MCP definitions to table syntax") from None
    expected = copy.deepcopy(document)
    expected.setdefault("mcp_servers", {})[SERVER] = entry
    if tomllib.loads(updated.decode("utf-8")) != expected:
        raise ConfigurationError("Cannot safely preserve this TOML layout; use table syntax")
    target.parent.mkdir(parents=True, exist_ok=True)
    if exists:
        backup_file(target)  # A failed backup must prevent replacement.
    descriptor, name = tempfile.mkstemp(prefix=".config.toml.", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(updated)
        if exists:
            temporary.chmod(stat.S_IMODE(target.stat().st_mode))
        if target.is_symlink() or target.exists() != exists or (exists and target.read_bytes() != original):
            raise ConfigurationError("Codex config changed during setup; rerun the installer")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return f"Codex MCP configured: {target}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("python", type=Path)
    parser.add_argument("server", type=Path)
    parser.add_argument("--ld-library-path", default="", help="NixOS native library directory")
    args = parser.parse_args(argv)
    try:
        home = detect_codex_home()
        if home is None:
            print("Codex not detected; MCP registration skipped.")
            return 2  # Installers must not count an absent client as configured.
        print(configure(home, args.python, args.server, ld_library_path=args.ld_library_path))
    except (OSError, ValueError, TypeError) as error:
        # TOML parser errors can contain user values; report no configuration content.
        detail = str(error) if isinstance(error, ConfigurationError) else "Check config.toml syntax, table layout, paths and permissions"
        print(f"ERROR: Could not configure Codex MCP ({type(error).__name__}). "
              f"{detail}. Existing configuration was preserved.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
