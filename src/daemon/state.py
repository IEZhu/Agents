"""Private service state, usable by the stdlib-only controller and bootstrap."""
from pathlib import Path
import hashlib
import json
import os
import tempfile


def state_dir(installation=None):
    override = os.environ.get("AGENTS_SERVICE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    root = Path(installation or Path(__file__).resolve().parents[2]).resolve()
    identity = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    return Path.home() / "Library/Application Support/Agents-Core" / identity


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Service directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
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
