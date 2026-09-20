"""Replace only the marked routing section, preserving other user instructions."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

MARKER_BEGIN = "# >>> Agents-Core Routing Protocol (managed by init_repo) >>>"
MARKER_END = "# <<< Agents-Core Routing Protocol (managed by init_repo) <<<"
LEGACY_MARKER_BEGIN = "# >>> Agents-Core Routing Protocol (managed by init_repo.sh) >>>"
LEGACY_MARKER_END = "# <<< Agents-Core Routing Protocol (managed by init_repo.sh) <<<"


def write_with_backup(path: Path, content: bytes) -> bool:
    """Write atomically; preserve the original bytes in a unique backup on change."""
    if path.exists() and path.read_bytes() == content:
        return False
    if path.is_symlink():
        raise ValueError(f"Refusing to replace a symlink: {path}; edit its target manually")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"{path.name}.backup.{time.time_ns()}")
        shutil.copy2(path, backup)
        print(f"Backup created: {backup}")
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def inject(md_path: Path, src_path: Path) -> bool:
    section = src_path.read_bytes().rstrip(b"\r\n")
    block = MARKER_BEGIN.encode() + b"\n\n" + section + b"\n\n" + MARKER_END.encode()
    content = md_path.read_bytes() if md_path.exists() else b""
    pairs = [(MARKER_BEGIN.encode(), MARKER_END.encode()),
             (LEGACY_MARKER_BEGIN.encode(), LEGACY_MARKER_END.encode())]
    present = [(begin, end) for begin, end in pairs if begin in content or end in content]
    if present:
        if len(present) != 1:
            raise ValueError(f"Mixed routing marker versions in {md_path}; fix markers manually")
        begin, end = present[0]
        if content.count(begin) != 1 or content.count(end) != 1:
            raise ValueError(f"Expected one complete routing marker pair in {md_path}; fix markers manually")
        start, stop = content.index(begin), content.index(end)
        if stop < start:
            raise ValueError(f"End marker precedes begin marker in {md_path}; fix markers manually")
        content = content[:start] + block + content[stop + len(end):]
    else:
        separator = b"" if not content or content.endswith(b"\n") else b"\n"
        content += separator + block + b"\n"
    return write_with_backup(md_path, content)


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <target_md_path> <source_md_path>", file=sys.stderr)
        return 1
    try:
        changed = inject(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("Routing section updated" if changed else "Routing section already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
