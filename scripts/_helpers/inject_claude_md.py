"""Replace only the marked routing section, preserving other user instructions."""
from collections.abc import Callable
import errno
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time

MARKER_BEGIN = "# >>> Agents-Core Routing Protocol (managed by init_repo) >>>"
MARKER_END = "# <<< Agents-Core Routing Protocol (managed by init_repo) <<<"
LEGACY_MARKER_BEGIN = "# >>> Agents-Core Routing Protocol (managed by init_repo.sh) >>>"
LEGACY_MARKER_END = "# <<< Agents-Core Routing Protocol (managed by init_repo.sh) <<<"
BACKUP_LIMIT = 3
TIMESTAMP_DIGITS = 19
# One name format for the writer and the pruner, so a name the writer reserves is
# always one the pruner rotates: ten digits are the seconds this helper wrote before
# it used nanoseconds, and nineteen or more are nanoseconds. Nineteen alone stopped
# at the range the writer can leave, because it starts after the newest existing
# snapshot, so a wider name read as a user's own and was never removed.
MANAGED_SUFFIX = re.compile(r"(?:[0-9]{10}|[0-9]{19,})")
# A snapshot of bytes this installer did not write is the only copy of them, so its
# name carries a marker MANAGED_SUFFIX does not match and the rotation never trims.
RETAINED_MARKER = "kept."
SYMLINK_REFUSAL = "Refusing to replace a symlink: {path}; edit its target manually"
# Windows os.open follows the C runtime's text mode, which would translate the line
# endings of an instruction file and stop a read at Ctrl-Z; tempfile.mkstemp, which
# this helper used before, adds the same flag for the same reason. Absent on POSIX.
BINARY = getattr(os, "O_BINARY", 0)
# Every step of one write runs against the parent directory opened once, so a
# directory swapped for a symlink after the caller resolved it cannot move the
# backup, the temporary file or the rename. Windows supports no dir_fd argument.
# os.rename stands for the rename this uses: os.replace takes src_dir_fd and
# dst_dir_fd through the same renameat implementation but is not registered in
# os.supports_dir_fd, so probing it would disable the pinning wherever it works.
# os.stat with follow_symlinks=False stands for the lstat, which some versions
# register and others do not although fstatat is the same call underneath.
PARENT_FD = (all(function in os.supports_dir_fd
                 for function in (os.open, os.rename, os.unlink, os.stat))
             and os.stat in os.supports_follow_symlinks
             and os.listdir in os.supports_fd)


def managed_timestamp(name: str, prefix: str) -> int | None:
    """Read a managed snapshot's timestamp in nanoseconds, or None for another name."""
    if not name.startswith(prefix):
        return None
    suffix = name[len(prefix):]
    if not MANAGED_SUFFIX.fullmatch(suffix):
        return None
    return int(suffix) * (10**9 if len(suffix) == 10 else 1)


def _open_parent(parent: Path) -> int | None:
    """Pin the directory the whole write works in, where the platform allows it."""
    if not PARENT_FD:
        return None
    return os.open(parent, os.O_RDONLY | os.O_DIRECTORY)


def _close(parent_fd: int | None) -> None:
    """Close a pinned directory, where one was opened at all."""
    if parent_fd is not None:
        os.close(parent_fd)


def _children(parent: Path, parent_fd: int | None) -> list[str]:
    """List the destination's directory, through the descriptor when pinned."""
    return os.listdir(parent if parent_fd is None else parent_fd)


def _create(name: str, parent: Path, parent_fd: int | None, mode: int) -> int:
    """Create a sibling of the destination exclusively, and return its descriptor."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | BINARY
    if parent_fd is None:
        return os.open(parent / name, flags, mode)
    return os.open(name, flags, mode, dir_fd=parent_fd)


def _unlink(name: str, parent: Path, parent_fd: int | None) -> None:
    """Remove a sibling of the destination, inside the pinned directory."""
    if parent_fd is None:
        os.unlink(parent / name)
    else:
        os.unlink(name, dir_fd=parent_fd)


def _node(name: str, parent: Path, parent_fd: int | None) -> os.stat_result:
    """Stat a sibling of the destination without following it, as lstat does."""
    if parent_fd is None:
        return os.lstat(parent / name)
    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    """Identify the bytes a write read, not only the node that held them.

    The inode alone is not enough in either direction. ext4 hands the number of a
    file just unlinked to the next file created in that directory, so a replaced
    destination can keep it, which is why the change time and the size are part of
    this; and a file rewritten in place keeps its number while its content, which
    the backup holds, is gone. Windows tunneling can preserve a recreated file's
    creation time instead, and there its file id is the part that changes.
    """
    return (info.st_dev, info.st_ino, info.st_ctime_ns, info.st_size)


def _unchanged(path: Path, parent_fd: int | None, info: os.stat_result | None) -> bool:
    """Say whether the name still holds the bytes this write read, or still holds none.

    A descriptor pins an inode, never a name, so a rename replaces whatever the name
    has come to hold. Comparing just before it narrows that window instead of
    closing it: POSIX has no rename that refuses an unexpected destination, and
    renameat2 is Linux-only and absent from os.
    """
    try:
        node = _node(path.name, path.parent, parent_fd)
    except (FileNotFoundError, NotADirectoryError):
        return info is None
    return info is not None and _identity(node) == _identity(info)


def _replace(source: str, destination: str, parent: Path, parent_fd: int | None) -> None:
    """Rename one sibling of the destination over another, inside one directory."""
    if parent_fd is None:
        os.replace(parent / source, parent / destination)
    else:
        os.replace(source, destination, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)


def _open_destination(path: Path, parent_fd: int | None) -> int | None:
    """Pin the destination node once, or return None when nothing holds the name.

    O_NOFOLLOW refuses a symlink in the call that would otherwise have followed it,
    and O_NONBLOCK returns instead of waiting for a writer when a FIFO has taken the
    name, so the node is refused by its own stat rather than by a read never
    returning. Both close the window between a caller's check and this write.
    """
    flags = (os.O_RDONLY | BINARY
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        if parent_fd is None:
            return os.open(path, flags)
        return os.open(path.name, flags, dir_fd=parent_fd)
    except (FileNotFoundError, NotADirectoryError):
        # Nothing holds the name, including when a file holds one of its parents:
        # path.exists() answered the same, and the mkdir of a write reports it.
        return None
    except OSError as exc:
        # ELOOP is the symlink refusal of O_NOFOLLOW; some BSDs report EMLINK.
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise ValueError(SYMLINK_REFUSAL.format(path=path)) from exc
        raise


def _refuse_unsuitable(path: Path, parent_fd: int | None, info: os.stat_result) -> None:
    """Refuse a node this write cannot replace safely, on the descriptor it holds."""
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(
            f"Refusing to replace {path}: it is not a regular file; remove it manually")
    # st_nlink is 0 on Windows for a handle opened without the link count, so only a
    # count above one is evidence that another name exists.
    if info.st_nlink > 1:
        raise ValueError(
            f"Refusing to replace {path}: it has {info.st_nlink} hard links, so the write "
            f"would break the other name and its backup would hold the content that name "
            f"shares; replace it with a single-link file manually")
    # Windows has no O_NOFOLLOW, so a symlink that took the name after the caller's
    # own check was followed, and these bytes would be its target's, copied into a
    # backup beside the name. lstat names the link itself, and a link's identity is
    # never its target's, so comparing them catches what the flag cannot.
    try:
        named = _node(path.name, path.parent, parent_fd)
    except OSError as exc:
        raise ValueError(
            f"Refusing to replace {path}: its name cannot be read back, so nothing "
            f"confirms it holds the file that was opened") from exc
    if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
        raise ValueError(
            f"Refusing to replace {path}: its name does not hold the file this read "
            f"opened, so those bytes may come from elsewhere. Edit the target directly "
            f"if the name is a symlink, or rerun once nothing else is writing there")


def _read_all(descriptor: int) -> bytes:
    """Read the pinned file whole, so the comparison and the backup see one state."""
    chunks = []
    while True:
        chunk = os.read(descriptor, 1 << 20)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _take_mode(descriptor: int, path: Path, info: os.stat_result) -> None:
    """Apply the original's mode to bytes already written, never before them."""
    os.chmod(descriptor if os.chmod in os.supports_fd else path, stat.S_IMODE(info.st_mode))


def _take_times(descriptor: int, path: Path, info: os.stat_result) -> None:
    """Keep the original's timestamps on its snapshot, as a copy of it would."""
    target = descriptor if os.utime in os.supports_fd else path
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))


def _snapshot(descriptor: int, content: bytes, backup: Path, info: os.stat_result) -> None:
    """Fill a reserved snapshot, then give it the mode and times of the original."""
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
        output.flush()
        _take_mode(output.fileno(), backup, info)
        _take_times(output.fileno(), backup, info)


def _copy_backup(path: Path, parent_fd: int | None, content: bytes,
                 info: os.stat_result, retain: bool) -> Path:
    """Reserve a unique snapshot name, including when the clock has not advanced."""
    prefix = path.name + ".backup."
    marker = RETAINED_MARKER if retain else ""
    timestamp = time.time_ns()
    # Start after existing snapshots so pruning cannot make a frozen clock reuse
    # an older name and then discard the newest backup as the oldest snapshot.
    for name in _children(path.parent, parent_fd):
        previous = managed_timestamp(name, prefix)
        if previous is not None:
            timestamp = max(timestamp, previous + 1)
    while True:
        name = f"{prefix}{marker}{timestamp:0{TIMESTAMP_DIGITS}d}"
        try:
            # 0o600 until the bytes are in place: a snapshot created at the process
            # umask publishes a private instruction file's content while it is written,
            # because the original's mode arrives only after the copy.
            descriptor = _create(name, path.parent, parent_fd, 0o600)
        except FileExistsError:
            timestamp += 1
            continue
        break
    backup = path.with_name(name)
    try:
        _snapshot(descriptor, content, backup, info)
    except BaseException:
        try:
            _unlink(name, path.parent, parent_fd)
        except OSError as exc:
            print(f"WARNING: Could not remove incomplete backup {backup}: {exc}", file=sys.stderr)
        raise
    return backup


def _prune(path: Path, parent_fd: int | None) -> None:
    """Keep the latest managed snapshots; leave every other backup name untouched."""
    prefix = path.name + ".backup."
    try:
        snapshots = []
        for name in _children(path.parent, parent_fd):
            timestamp = managed_timestamp(name, prefix)
            if timestamp is None:
                continue
            try:
                # lstat, so a symlink or a directory wearing a managed name is left
                # where it is, and in the pinned directory rather than by a lookup.
                info = _node(name, path.parent, parent_fd)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            snapshots.append((timestamp, name))
        for _, name in sorted(snapshots, reverse=True)[BACKUP_LIMIT:]:
            _unlink(name, path.parent, parent_fd)
    except OSError as exc:
        print(f"WARNING: Could not prune instruction backups for {path}: {exc}", file=sys.stderr)


def _write(path: Path, parent_fd: int | None, content: bytes,
           info: os.stat_result | None) -> None:
    """Fill a sibling temporary file and rename it over the destination name."""
    while True:
        # Named here rather than by tempfile.mkstemp, which takes a directory path
        # and no descriptor, at the same exclusive 0o600 that mkstemp created: a
        # file this write creates keeps that mode, and a replacement takes the
        # mode of the file it replaces once it holds every byte.
        name = f".{path.name}.{secrets.token_hex(8)}"
        try:
            descriptor = _create(name, path.parent, parent_fd, 0o600)
        except FileExistsError:
            continue
        break
    temporary = path.with_name(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            if info is not None:
                _take_mode(output.fileno(), temporary, info)
        # Checked as late as the rename allows: another process can take the name or
        # rewrite the file while this write prepares, and the rename would then
        # replace bytes no backup holds. Refusing leaves them and this one's snapshot.
        if not _unchanged(path, parent_fd, info):
            raise ValueError(
                f"Refusing to replace {path}: it changed while this write prepared, so the "
                f"bytes it would replace are not the ones it read and backed up. The "
                f"destination is untouched and that snapshot is kept; rerun once nothing "
                f"else is writing there")
        _replace(name, path.name, path.parent, parent_fd)
    finally:
        try:
            _unlink(name, path.parent, parent_fd)
        except FileNotFoundError:
            pass


def prune_backups(path: Path) -> None:
    """Keep the latest managed snapshots; leave user-named backups untouched."""
    _prune(path, None)


def copy_backup(path: Path, retain: bool = False) -> Path:
    """Snapshot an existing file into a sibling whose name no other snapshot holds."""
    parent_fd = _open_parent(path.parent)
    try:
        descriptor = _open_destination(path, parent_fd)
        if descriptor is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
        try:
            info = os.fstat(descriptor)
            _refuse_unsuitable(path, parent_fd, info)
            return _copy_backup(path, parent_fd, _read_all(descriptor), info, retain)
        finally:
            os.close(descriptor)
    finally:
        _close(parent_fd)


def read_destination(path: Path) -> bytes | None:
    """Read a file this helper may write, or None when nothing holds the name.

    A caller that inspects a destination before writing it reads it this way, so a
    symlink, a FIFO or a second hard link is refused by the stat of the node that
    was opened rather than by a path lookup that may resolve elsewhere, and reading
    a FIFO cannot stall the run before the write refuses it.
    """
    if path.is_symlink():
        raise ValueError(SYMLINK_REFUSAL.format(path=path))
    try:
        parent_fd = _open_parent(path.parent)
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        descriptor = _open_destination(path, parent_fd)
        if descriptor is None:
            return None
        try:
            _refuse_unsuitable(path, parent_fd, os.fstat(descriptor))
            return _read_all(descriptor)
        finally:
            os.close(descriptor)
    finally:
        _close(parent_fd)


def write_with_backup(path: Path, content: bytes,
                      retain_backup: bool | Callable[[bytes], bool] = False) -> bool:
    """Write exact bytes atomically, retaining up to three managed backups.

    retain_backup keeps the snapshot out of that rotation, for content this installer
    did not write, whose backup is the only copy of it. A caller whose answer depends
    on the bytes being replaced passes a function of them, so that decision follows
    the read this write made and not one the caller made before it. That function
    runs before anything is written and may refuse the write by raising.
    """
    return _write_with_backup(path, lambda _current: content, retain_backup)


def render_with_backup(path: Path, render: Callable[[bytes], bytes],
                       retain_backup: bool | Callable[[bytes], bool] = False) -> bool:
    """Write bytes derived from the destination's own current content.

    render receives the bytes this write read through its own descriptor, empty when
    the file is absent, so the result cannot be composed from a node that took the
    name after the caller looked at it.
    """
    return _write_with_backup(path, render, retain_backup)


def _write_with_backup(path: Path, render: Callable[[bytes], bytes],
                       retain_backup: bool | Callable[[bytes], bool]) -> bool:
    """Write through one pinned parent and destination, then rotate the backups.

    The parent directory and the destination node are each opened once and every
    step runs against those descriptors, so neither a swapped parent directory nor
    another node at the destination can redirect the backup or the write after a
    caller's check. retain_backup keeps the snapshot out of the rotation, for
    content this installer did not write: that backup is its only copy, and three
    later writes would otherwise rotate it away.
    """
    if path.is_symlink():
        raise ValueError(SYMLINK_REFUSAL.format(path=path))
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_fd = _open_parent(path.parent)
    try:
        descriptor = _open_destination(path, parent_fd)
        try:
            current, info = None, None
            if descriptor is not None:
                info = os.fstat(descriptor)
                _refuse_unsuitable(path, parent_fd, info)
                current = _read_all(descriptor)
            content = render(current if current is not None else b"")
            if current == content:
                _prune(path, parent_fd)
                return False
            if descriptor is not None:
                retain = (retain_backup(current) if callable(retain_backup)
                          else retain_backup)
                print(f"Backup created: {_copy_backup(path, parent_fd, current, info, retain)}")
        finally:
            if descriptor is not None:
                os.close(descriptor)
        # Written after that descriptor is closed: Windows refuses to rename over a
        # file another handle holds. The pin has done its work by then, because the
        # bytes and the stat it answered for are the ones the backup holds.
        _write(path, parent_fd, content, info)
        _prune(path, parent_fd)
        return True
    finally:
        _close(parent_fd)


def render(content: bytes, section: bytes, md_path: Path) -> bytes:
    """Render the managed section without writes, preserving all surrounding bytes."""
    section = section.rstrip(b"\r\n")
    block = MARKER_BEGIN.encode() + b"\n\n" + section + b"\n\n" + MARKER_END.encode()
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
    return content


def inject(md_path: Path, src_path: Path) -> bool:
    section = src_path.read_bytes()
    # Render from the bytes the write itself reads: reading the target separately
    # here would stall on a FIFO at its name before the write could refuse it, and
    # would compose the result from whatever that lookup resolved to.
    return render_with_backup(md_path, lambda current: render(current, section, md_path))


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
