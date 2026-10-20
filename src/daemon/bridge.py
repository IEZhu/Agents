"""Choose the installed stdio bridge without requiring Node."""
from pathlib import Path
import sys


def bridge_command(config, *, installation=None):
    root = Path(installation or config["installation"])
    node = config.get("node")
    if node and Path(node).is_absolute() and Path(node).is_file():
        return str(node), root / "bridge/stdio.mjs"
    python = config.get("python", sys.executable)
    if not python or not Path(python).is_absolute() or not Path(python).is_file():
        raise ValueError("An absolute Python executable is required for the stdio bridge")
    return str(python), root / "bridge/stdio.py"
