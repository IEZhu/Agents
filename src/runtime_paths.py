"""Bind daemon content paths before importing the engine or application.

Ordinary starts use the running source tree. A private service configuration may
name that source as ``runtime_root`` while retaining its installed content and
lease identity in ``installation``. There is deliberately no environment override.
"""
from pathlib import Path
import sys


_RUNTIME_ROOT = Path(__file__).resolve().parents[1]
_installation_root = None
_PATH_CONSUMERS = frozenset(("src.server", "src.daemon.app", "src.daemon.overview",
                            "src.daemon.autodev_ui", "src.schedules.config", "src.schedules.cli"))


def runtime_root() -> Path:
    """The source tree this process actually imported."""
    return _RUNTIME_ROOT


def installation_root() -> Path:
    """The installed content tree, defaulting to the running source tree."""
    return _installation_root if _installation_root is not None else _RUNTIME_ROOT


def _absolute_root(value, name) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip() or not Path(value).is_absolute():
        raise RuntimeError(f"Service {name} must be an absolute path")
    return Path(value).resolve()


def configure_runtime_paths(installation, *, runtime_root=None) -> None:
    """Accept one source-bound service identity before engine/application imports.

    Without an explicit runtime root the installation must still be the source
    tree, as before. A declared root must match the code being executed, so a
    copied configuration cannot silently run a different source tree.
    """
    global _installation_root
    root = _absolute_root(installation, "installation")
    source = root if runtime_root is None else _absolute_root(runtime_root, "runtime_root")
    if source != _RUNTIME_ROOT:
        raise RuntimeError("Service configuration points at a different runtime source")
    if _installation_root is not None and _installation_root != root:
        raise RuntimeError("Runtime paths are already configured for a different installation")
    if any(name == "src.engine" or name.startswith("src.engine.") or
           name in _PATH_CONSUMERS for name in sys.modules):
        raise RuntimeError("Runtime paths must be configured before engine/application imports")
    _installation_root = root
