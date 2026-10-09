"""Private service state, usable by the stdlib-only controller and bootstrap."""
from pathlib import Path
import hashlib
import json
import os
import tempfile


def state_dir(installation=None):
    """``AGENTS_SERVICE_DIR`` when set, else ``default_state_dir``."""
    override = os.environ.get("AGENTS_SERVICE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return default_state_dir(installation)


def default_state_dir(installation=None):
    """The service's private state: ``~/Library/Application Support/Agents-Core/<id>`` on macOS,
    ``%USERPROFILE%\\.agents-core\\<id>`` on Windows.

    Not ``%LOCALAPPDATA%`` on Windows: the Claude desktop app is an MSIX package, and every
    process it starts (Code sessions, their terminals, stdio servers) writes new files under
    AppData into the package's private copy, which the Task Scheduler task never sees.
    """
    root = Path(installation or Path(__file__).resolve().parents[2]).resolve()
    identity = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    if os.name == "nt":
        return Path(os.environ.get("USERPROFILE") or Path.home()) / ".agents-core" / identity
    return Path.home() / "Library/Application Support/Agents-Core" / identity


def private_dir(path):
    """``path`` created if needed and readable by this user only: mode 0700 on POSIX, a protected
    owner-only DACL on Windows (`src.daemon.acl`), which files created inside it inherit."""
    path = Path(path)
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():  # Path.is_junction: Python 3.12+
        raise ValueError("Service directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        from . import acl
        if not acl.owned_by_user_or_admins(path):  # its owner could rewrite the DACL at any time
            raise PermissionError("Service directory belongs to another user")
        # Restricting walks the whole tree, so only a directory that needs it is restricted.
        if not acl.is_private(path, inherited_below=True):
            acl.restrict(path)
        return path
    if path.stat().st_uid != os.getuid():
        raise PermissionError("Service directory belongs to another user")
    path.chmod(0o700)
    return path


def atomic_private(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Refusing to replace a symlink")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb" if isinstance(content, bytes) else "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":  # Windows cannot open a directory to fsync it
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_private(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default
