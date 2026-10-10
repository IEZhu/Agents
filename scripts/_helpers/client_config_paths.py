"""Print the Windows installer's effective client paths without loading the engine."""
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.client_paths import client_config_path, client_home  # noqa: E402


def main():
    paths = {
        "MCP_SETTINGS_FILE": client_config_path("cursor"),
        "CLAUDE_DESKTOP_CONFIG": client_config_path("desktop"),
        "CLAUDE_CODE_DIR": client_home("claude"),
        "CLAUDE_CODE_MCP": client_config_path("claude"),
        # Claude Code settings: where the desktop app's Agents-Core entry is denied (#231).
        "CLAUDE_CODE_SETTINGS": client_config_path("claude-deny-desktop"),
    }
    for name, path in paths.items():
        print(f"{name}={path}")


if __name__ == "__main__":
    main()
