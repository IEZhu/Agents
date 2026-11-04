"""Installer migration must not overwrite unrelated or user-edited instructions."""
import importlib
import os
from pathlib import Path
import stat
import sys
import threading

import pytest


@pytest.fixture
def helpers(monkeypatch):
    helper_dir = Path(__file__).resolve().parents[1] / "scripts" / "_helpers"
    monkeypatch.syspath_prepend(str(helper_dir))
    injector = importlib.import_module("inject_claude_md")
    memory = importlib.import_module("migrate_routing_memory")
    return injector, memory


def test_managed_replacement_preserves_outside_bytes_and_backup(tmp_path, helpers):
    injector, _ = helpers
    target, source = tmp_path / "CLAUDE.md", tmp_path / "protocol.md"
    prefix, suffix = b"# Personal rules\r\nUse Spanish.\r\n\r\n", b"\r\n\r\nKeep this\r\n"
    old = prefix + injector.MARKER_BEGIN.encode() + b"\nold\n" + injector.MARKER_END.encode() + suffix
    target.write_bytes(old)
    source.write_text("new protocol\n", encoding="utf-8")

    assert injector.inject(target, source)
    result = target.read_bytes()
    assert result.startswith(prefix)
    assert result.endswith(suffix)
    assert b"new protocol" in result
    backups = list(tmp_path.glob("CLAUDE.md.backup.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == old
    assert not injector.inject(target, source)
    assert list(tmp_path.glob("CLAUDE.md.backup.*")) == backups


def test_legacy_markers_migrate_without_duplicate_section(tmp_path, helpers):
    injector, _ = helpers
    target, source = tmp_path / "CLAUDE.md", tmp_path / "protocol.md"
    target.write_text(f"Before\n{injector.LEGACY_MARKER_BEGIN}\nold\n{injector.LEGACY_MARKER_END}\nAfter", encoding="utf-8")
    source.write_text("protocol 2", encoding="utf-8")
    injector.inject(target, source)
    result = target.read_text(encoding="utf-8")
    assert result.count(injector.MARKER_BEGIN) == 1
    assert injector.LEGACY_MARKER_BEGIN not in result
    assert result.startswith("Before\n") and result.endswith("\nAfter")


@pytest.mark.parametrize("shape", ["begin_only", "end_only", "reversed", "duplicate", "mixed"])
def test_invalid_markers_leave_entire_file_untouched(tmp_path, helpers, shape):
    injector, _ = helpers
    begin, end = injector.MARKER_BEGIN, injector.MARKER_END
    contents = {
        "begin_only": begin, "end_only": end, "reversed": end + "\n" + begin,
        "duplicate": begin + "\n" + begin + "\n" + end,
        "mixed": begin + "\n" + end + "\n" + injector.LEGACY_MARKER_BEGIN,
    }[shape]
    target, source = tmp_path / "CLAUDE.md", tmp_path / "protocol.md"
    target.write_text(contents, encoding="utf-8")
    source.write_text("protocol 2", encoding="utf-8")
    with pytest.raises(ValueError, match="marker"):
        injector.inject(target, source)
    assert target.read_text(encoding="utf-8") == contents
    assert not list(tmp_path.glob("*.backup.*"))


def test_append_preserves_user_instructions_without_final_newline(tmp_path, helpers):
    injector, _ = helpers
    target, source = tmp_path / "CLAUDE.md", tmp_path / "protocol.md"
    target.write_text("User instructions", encoding="utf-8")
    source.write_text("protocol 2\n", encoding="utf-8")
    injector.inject(target, source)
    assert target.read_text(encoding="utf-8").startswith("User instructions\n" + injector.MARKER_BEGIN)


@pytest.mark.parametrize("legacy", ["memory-routing-v1.md", "memory-routing-v2.md", "memory-routing-v3.md", "memory-routing-v4.md",
                                    "memory-routing-v5.md", "memory-routing-v6.md", "memory-routing-v7.md", "memory-routing-v8.md", "memory-routing-v9.md", "memory-routing-v10.md", "memory-routing-v11.md", "memory-routing-v12.md", "memory-routing-v13.md", "memory-routing-v14.md", "memory-routing-v15.md", "memory-routing-v16.md", "memory-routing-v17.md", "memory-routing-v18.md", "memory-routing-v19.md", "memory-routing-v20.md", "memory-routing-v21.md", "memory-routing-v22.md", "memory-routing-v23.md"])
def test_known_memory_and_index_migrate_with_backups(tmp_path, helpers, legacy):
    """Check known reminder/index migration, original-byte backups, and repeat-run no-ops."""
    _, memory = helpers
    reminder, index = tmp_path / memory.FILENAME, tmp_path / "MEMORY.md"
    original = (memory.TEMPLATES / "legacy" / legacy).read_bytes()
    reminder.write_bytes(original)
    old_index = b"# My index\r\n" + memory.LEGACY_INDEX_ENTRIES[0].encode() + b"\r\nOther note\r\n"
    index.write_bytes(old_index)
    assert memory.migrate(tmp_path)
    assert reminder.read_bytes() == (memory.TEMPLATES / "memory-routing.md").read_bytes()
    assert index.read_bytes() == b"# My index\r\n" + memory.INDEX_ENTRY.encode() + b"\r\nOther note\r\n"
    assert next(tmp_path.glob(memory.FILENAME + ".backup.*")).read_bytes() == original
    assert next(tmp_path.glob("MEMORY.md.backup.*")).read_bytes() == old_index
    backups = list(tmp_path.glob("*.backup.*"))
    assert not memory.migrate(tmp_path)
    assert list(tmp_path.glob("*.backup.*")) == backups


def test_legacy_v2_reminder_with_current_index_updates_only_the_reminder(tmp_path, helpers):
    _, memory = helpers
    reminder, index = tmp_path / memory.FILENAME, tmp_path / "MEMORY.md"
    reminder.write_bytes((memory.TEMPLATES / "legacy" / "memory-routing-v2.md").read_bytes())
    index.write_text("# My index\n" + memory.INDEX_ENTRY + "\n", encoding="utf-8")
    assert memory.migrate(tmp_path)
    assert reminder.read_bytes() == (memory.TEMPLATES / "memory-routing.md").read_bytes()
    assert index.read_text(encoding="utf-8") == "# My index\n" + memory.INDEX_ENTRY + "\n"
    assert not list(tmp_path.glob("MEMORY.md.backup.*"))


def test_current_reminder_has_no_version_fallback():
    root = Path(__file__).resolve().parents[1]
    templates = root / "scripts" / "templates"
    current = (templates / "memory-routing.md").read_text(encoding="utf-8")
    assert "version 1" not in current.lower() and "version 2" not in current.lower()
    assert all(current != legacy.read_text(encoding="utf-8")
               for legacy in (templates / "legacy").glob("memory-routing-*.md"))
    assert not (templates / "routing-protocol-v1.md").exists()


def test_user_edited_memory_is_preserved_with_actionable_warning(tmp_path, helpers, capsys):
    _, memory = helpers
    reminder, index = tmp_path / memory.FILENAME, tmp_path / "MEMORY.md"
    custom = (memory.TEMPLATES / "legacy" / "memory-routing-v1.md").read_bytes() + b"\nMy exception\n"
    reminder.write_bytes(custom)
    index.write_text(memory.LEGACY_INDEX_ENTRIES[0] + "\n", encoding="utf-8")
    assert not memory.migrate(tmp_path)
    assert reminder.read_bytes() == custom
    assert index.read_text(encoding="utf-8") == memory.LEGACY_INDEX_ENTRIES[0] + "\n"
    warning = capsys.readouterr().err
    assert str(reminder) in warning and "Manually" in warning and "keep/switch" in warning
    assert not list(tmp_path.glob("*.backup.*"))


@pytest.mark.parametrize("suffix", [" — my custom note", "\n" + "duplicate feedback_agents_core_routing.md"])
def test_user_edited_or_duplicate_index_entry_is_preserved(tmp_path, helpers, capsys, suffix):
    _, memory = helpers
    reminder, index = tmp_path / memory.FILENAME, tmp_path / "MEMORY.md"
    reminder.write_bytes((memory.TEMPLATES / "legacy" / "memory-routing-v1.md").read_bytes())
    custom_index = memory.LEGACY_INDEX_ENTRIES[0] + suffix + "\n"
    index.write_text(custom_index, encoding="utf-8")
    assert memory.migrate(tmp_path)
    assert index.read_text(encoding="utf-8") == custom_index
    assert str(index) in capsys.readouterr().err
    assert not list(tmp_path.glob("MEMORY.md.backup.*"))


def test_windows_missing_memory_is_not_created(tmp_path, helpers):
    _, memory = helpers
    directory = tmp_path / "missing"
    assert not memory.migrate(directory, existing_only=True)
    assert not directory.exists()


def test_unix_first_install_creates_memory_and_preserves_existing_index(tmp_path, helpers):
    _, memory = helpers
    index = tmp_path / "MEMORY.md"
    index.write_bytes(b"[My note](my-note.md)")
    assert memory.migrate(tmp_path)
    assert index.read_text(encoding="utf-8") == "[My note](my-note.md)\n" + memory.INDEX_ENTRY + "\n"
    assert (tmp_path / memory.FILENAME).read_bytes() == (memory.TEMPLATES / "memory-routing.md").read_bytes()


def test_symlink_target_is_not_replaced(tmp_path, helpers):
    injector, _ = helpers
    original, link, source = tmp_path / "personal.md", tmp_path / "CLAUDE.md", tmp_path / "protocol.md"
    original.write_text("My instructions", encoding="utf-8")
    try:
        link.symlink_to(original)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Creating symlinks requires Windows developer mode or privilege")
        raise
    source.write_text("protocol 2", encoding="utf-8")
    with pytest.raises(ValueError, match="symlink"):
        injector.inject(link, source)
    assert original.read_text(encoding="utf-8") == "My instructions"
    assert link.is_symlink()


def test_checkout_managed_section_matches_core_template(helpers):
    injector, _ = helpers
    root = Path(__file__).resolve().parents[1]
    templates = root / "scripts" / "templates"
    original = (root / "CLAUDE.md").read_bytes()
    begin, end = injector.MARKER_BEGIN.encode(), injector.MARKER_END.encode()
    assert original.count(begin) == original.count(end) == 1
    _, managed = original.split(begin, 1)
    section, after = managed.split(end, 1)
    assert section.strip() == (templates / "routing-protocol-core.md").read_bytes().strip()
    assert b"Before answering ANY user query" not in original
    assert b"default to version 1" not in section
    assert b"## Repository notes" in after


def test_installers_always_use_core_template():
    root = Path(__file__).resolve().parents[1]
    for name in ("init_repo.sh", "init_repo.bat"):
        script = (root / "scripts" / name).read_text(encoding="utf-8")
        assert "routing-protocol-core.md" in script
        assert "routing-protocol-v1.md" not in script
        assert "AGENTS_PERSONA_PROTOCOL" not in script and "PERSONA_PROTOCOL" not in script
        assert "--protocol" not in script


def test_instruction_backups_are_bounded_and_contain_recent_versions(tmp_path, helpers, monkeypatch):
    injector, _ = helpers
    target = tmp_path / "AGENTS.md"
    ticks = iter(range(1_790_000_000_000_000_000, 1_790_000_000_000_000_010))
    monkeypatch.setattr(injector.time, "time_ns", lambda: next(ticks))
    for version in range(8):
        assert injector.write_with_backup(target, f"version {version}".encode())
    backups = sorted(tmp_path.glob("AGENTS.md.backup.*"))
    assert len(backups) == 3
    assert [backup.read_bytes() for backup in backups] == [b"version 4", b"version 5", b"version 6"]
    assert target.read_bytes() == b"version 7"
    assert not injector.write_with_backup(target, b"version 7")
    assert sorted(tmp_path.glob("AGENTS.md.backup.*")) == backups


def test_fixed_clock_keeps_distinct_backups_of_recent_versions(tmp_path, helpers, monkeypatch):
    injector, _ = helpers
    target = tmp_path / "AGENTS.md"
    monkeypatch.setattr(injector.time, "time_ns", lambda: 1_790_000_000_000_000_000)
    for version in range(8):
        assert injector.write_with_backup(target, f"version {version}".encode())
        backups = sorted(tmp_path.glob("AGENTS.md.backup.*"))
        expected = [f"version {previous}".encode() for previous in range(max(0, version - 3), version)]
        assert [backup.read_bytes() for backup in backups] == expected
        assert all(len(backup.name.rsplit(".", 1)[1]) == 19 for backup in backups)
    assert target.read_bytes() == b"version 7"


@pytest.mark.parametrize("collision", [False, True])
def test_failed_backup_copy_removes_partial_snapshot_and_preserves_old_backups(
    tmp_path, helpers, monkeypatch, collision,
):
    injector, _ = helpers
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"original")
    tick = 1_790_000_000_000_000_000
    monkeypatch.setattr(injector.time, "time_ns", lambda: tick)
    # More than the retention limit proves a failed backup does not prune either.
    timestamps = list(range(tick - 4, tick)) + ([tick] if collision else [])
    originals = {}
    for timestamp in timestamps:
        backup = target.with_name(f"{target.name}.backup.{timestamp}")
        originals[backup] = str(timestamp).encode()
        backup.write_bytes(originals[backup])

    def fail_copy(descriptor, content, backup, info):
        with os.fdopen(descriptor, "wb") as partial:
            partial.write(b"partial snapshot")
        raise OSError("backup copy failed")

    monkeypatch.setattr(injector, "_snapshot", fail_copy)
    with pytest.raises(OSError, match="backup copy failed"):
        injector.write_with_backup(target, b"replacement")
    assert target.read_bytes() == b"original"
    assert {backup: backup.read_bytes() for backup in tmp_path.glob("*.backup.*")} == originals
    assert not list(tmp_path.glob(".CLAUDE.md.*"))


def test_unchanged_update_prunes_legacy_backups_and_preserves_user_copies(tmp_path, helpers):
    injector, _ = helpers
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"current")
    timestamps = ["1775755471", "1779628138", "1790458484", "1790458484900485000", "1790458485"]
    for timestamp in timestamps:
        target.with_name(target.name + ".backup." + timestamp).write_bytes(timestamp.encode())
    manual = target.with_name(target.name + ".backup.before-my-edits")
    manual.write_bytes(b"personal snapshot")
    directory = target.with_name(target.name + ".backup.1775755470")
    directory.mkdir()
    assert not injector.write_with_backup(target, b"current")
    for timestamp in timestamps[:2]:
        assert not target.with_name(target.name + ".backup." + timestamp).exists()
    for timestamp in timestamps[2:]:
        assert target.with_name(target.name + ".backup." + timestamp).read_bytes() == timestamp.encode()
    assert manual.read_bytes() == b"personal snapshot"
    assert directory.is_dir()


def test_failed_atomic_write_keeps_original_and_existing_backups(tmp_path, helpers, monkeypatch):
    injector, _ = helpers
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"original")
    backups = []
    for timestamp in range(1_770_000_000, 1_770_000_004):
        backup = target.with_name(target.name + ".backup." + str(timestamp))
        backup.write_bytes(b"older snapshot")
        backups.append(backup)

    def fail_replace(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(injector.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        injector.write_with_backup(target, b"replacement")
    assert target.read_bytes() == b"original"
    assert all(backup.read_bytes() == b"older snapshot" for backup in backups)
    assert any(backup.read_bytes() == b"original" for backup in tmp_path.glob("*.backup.*"))
    assert not list(tmp_path.glob(".CLAUDE.md.*"))


def test_backup_cleanup_failure_is_reported_after_successful_write(tmp_path, helpers, monkeypatch, capsys):
    injector, _ = helpers
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"original")
    for timestamp in range(1_770_000_000, 1_770_000_004):
        target.with_name(target.name + ".backup." + str(timestamp)).write_bytes(b"older")
    original_unlink = injector._unlink

    def fail_backup_unlink(name, *args, **kwargs):
        if ".backup." in name:
            raise PermissionError("backup is read-only")
        return original_unlink(name, *args, **kwargs)

    monkeypatch.setattr(injector, "_unlink", fail_backup_unlink)
    assert injector.write_with_backup(target, b"updated")
    assert target.read_bytes() == b"updated"
    assert "Could not prune instruction backups" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: mode bits and umask")
def test_backup_never_holds_the_bytes_at_the_process_umask(tmp_path, helpers, monkeypatch):
    """A snapshot takes the original's mode after the bytes, so it is created at 0o600.

    Created at the umask, the snapshot of a private instruction file was readable by
    everyone for as long as the copy into it took, because its mode arrived last.
    """
    injector, _ = helpers
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"ANTHROPIC_API_KEY=secret")
    target.chmod(0o600)
    # _take_mode runs on a snapshot that already holds every byte, which is the
    # widest moment of the window, and the recording lets the write continue.
    widest = []
    original_take_mode = injector._take_mode

    def record(descriptor, path, info):
        widest.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
        return original_take_mode(descriptor, path, info)

    monkeypatch.setattr(injector, "_take_mode", record)
    previous_umask = os.umask(0)
    try:
        backup = injector.copy_backup(target)
    finally:
        os.umask(previous_umask)

    assert widest == [0o600], "the snapshot held the bytes at a wider mode"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert backup.read_bytes() == b"ANTHROPIC_API_KEY=secret"


def test_retained_backup_survives_the_rotation_of_later_writes(tmp_path, helpers):
    """A snapshot of content this installer did not write stays out of the rotation.

    Its backup is the only copy of those bytes, and three later managed writes used
    to push it out as the oldest snapshot.
    """
    injector, _ = helpers
    target = tmp_path / ".coderabbit.yaml"
    target.write_bytes(b"hand-written by the user")

    assert injector.write_with_backup(target, b"managed 0", retain_backup=True)
    for version in range(1, injector.BACKUP_LIMIT + 3):
        assert injector.write_with_backup(target, f"managed {version}".encode())

    snapshots = {path.name: path.read_bytes() for path in tmp_path.glob(".coderabbit.yaml.backup.*")}
    assert b"hand-written by the user" in snapshots.values()
    rotated = [name for name in snapshots if injector.RETAINED_MARKER not in name]
    assert len(rotated) == injector.BACKUP_LIMIT, "the managed rotation is no longer bounded"


def test_reserved_name_past_the_timestamp_range_stays_managed(tmp_path, helpers):
    """Every name copy_backup reserves is one prune_backups manages and rotates.

    The writer pads to nineteen digits but starts after the newest existing
    snapshot, so a name at the top of that range made it reserve a twenty-digit
    name the pruner read as a user's own and never removed.
    """
    injector, _ = helpers
    target = tmp_path / "AGENTS.md"
    target.write_bytes(b"original")
    prefix = target.name + ".backup."
    target.with_name(prefix + "9" * injector.TIMESTAMP_DIGITS).write_bytes(b"far future")

    backup = injector.copy_backup(target)

    assert injector.managed_timestamp(backup.name, prefix) is not None, \
        "the reserved name falls outside the managed format"
    for extra in range(injector.BACKUP_LIMIT + 2):
        target.with_name(f"{prefix}{1_790_000_000_000_000_000 + extra:019d}").write_bytes(b"older")
    injector.prune_backups(target)
    remaining = sorted(path.name for path in tmp_path.glob(prefix + "*"))
    assert len(remaining) == injector.BACKUP_LIMIT
    assert backup.name in remaining


@pytest.mark.parametrize("take_the_name, needle", [
    (lambda path, outside: os.link(outside, path), "hard links"),
    pytest.param(lambda path, outside: os.mkfifo(path), "not a regular file",
                 marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: FIFOs")),
])
def test_destination_node_taken_after_a_callers_check_is_refused(
    tmp_path, helpers, take_the_name, needle,
):
    """The node the write opens is the node its own stat accepted or refused.

    A caller's check cannot speak for the write that follows it: a FIFO put at the
    name afterwards used to stall the read forever, and a second hard link used to
    make the backup a copy of whatever the other name holds.
    """
    injector, _ = helpers
    target = tmp_path / ".coderabbit.yaml"
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"TOKEN=abc123")
    # The caller checked a regular single-link file; this is the swap after it.
    take_the_name(target, outside)
    failure = {}

    def attempt():
        try:
            injector.write_with_backup(target, b"managed")
        except BaseException as error:
            # Whatever the write raises belongs in the assertions below, not in a
            # traceback this thread would print and then discard.
            failure["error"] = error

    # In a thread, because the defect is a read that never returns: a bare call
    # would hang the suite instead of failing this test.
    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout=30)

    assert not worker.is_alive(), "the write is still reading the destination"
    assert needle in str(failure.get("error"))
    assert not list(tmp_path.glob(".coderabbit.yaml.backup.*"))
    assert outside.read_bytes() == b"TOKEN=abc123"


def test_parent_swapped_after_it_was_opened_cannot_move_the_write(tmp_path, helpers, monkeypatch):
    """Every step of a write runs against the parent directory opened once.

    A caller that resolved the parent to prove it is inside the target could have it
    replaced by a symlink afterwards, which moved the write, its backup and its
    temporary file out of that directory.
    """
    if os.rename not in os.supports_dir_fd:
        pytest.skip("this platform supports no dir_fd argument")
    injector, _ = helpers
    # Asserted, not skipped on: a capability probe that asks for one function this
    # interpreter does not register turned the pinning off everywhere, and a skip
    # here would have reported that as nothing to check.
    assert injector.PARENT_FD, "the write does not pin the parent on a platform that can"
    inside, elsewhere = tmp_path / "inside", tmp_path / "elsewhere"
    inside.mkdir()
    elsewhere.mkdir()
    target = inside / "AGENTS.md"
    target.write_bytes(b"original")
    original_write = injector._write

    def swap_then_write(path, parent_fd, content, info):
        inside.rename(tmp_path / "moved")
        inside.symlink_to(elsewhere)
        return original_write(path, parent_fd, content, info)

    monkeypatch.setattr(injector, "_write", swap_then_write)
    assert injector.write_with_backup(target, b"managed")

    assert (tmp_path / "moved" / "AGENTS.md").read_bytes() == b"managed"
    assert not list(elsewhere.iterdir()), "the write landed outside the opened directory"


def test_every_file_open_asks_for_binary_mode(tmp_path, helpers, monkeypatch):
    """Windows os.open follows the C runtime's text mode, which would rewrite bytes.

    tempfile.mkstemp, which this helper used before, sets O_BINARY for that reason.
    Without it `os.read` of the destination translates CRLF to LF, and the backup
    then holds the translated bytes, which is what the written file is compared to.

    O_BINARY exists only on Windows, so elsewhere a bit stands in for it and the
    wrapper strips it before the real open, because 0x8000 means something else
    there: O_EVTONLY on macOS, O_LARGEFILE on Linux. Where the flag is real it is
    passed through, since stripping it is exactly the text mode this asserts against.
    """
    injector, _ = helpers
    real_flag = getattr(os, "O_BINARY", 0)
    stand_in = real_flag or 0x8000
    monkeypatch.setattr(injector, "BINARY", stand_in)
    requested = []
    real_open = os.open

    def record(path, flags, *rest, **kwargs):
        requested.append(flags)
        return real_open(path, flags if real_flag else flags & ~stand_in, *rest, **kwargs)

    monkeypatch.setattr(injector.os, "open", record)
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"first\r\nsecond\r\n")

    assert injector.write_with_backup(target, b"third\r\nfourth\r\n")

    directory = getattr(os, "O_DIRECTORY", 0)
    files = [flags for flags in requested if not (directory and flags & directory)]
    # The read of the destination, the backup and the temporary file: a directory
    # descriptor needs no translation mode, and Windows opens none.
    assert len(files) == 3
    assert all(flags & stand_in for flags in files)
    assert target.read_bytes() == b"third\r\nfourth\r\n"
    backup, = tmp_path.glob("CLAUDE.md.backup.*")
    assert backup.read_bytes() == b"first\r\nsecond\r\n"


def test_destination_is_closed_before_the_rename(tmp_path, helpers, monkeypatch):
    """Windows refuses to rename over a file another handle still holds.

    The descriptor pins the node for the comparison and the backup, and by the
    rename it has done that work, so holding it open only breaks the write there.
    """
    injector, _ = helpers
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"original")
    held, closed, open_at_rename = [], [], []
    real_open_destination, real_close, real_write = (
        injector._open_destination, injector.os.close, injector._write)

    def remember(path, parent_fd):
        descriptor = real_open_destination(path, parent_fd)
        held.append(descriptor)
        return descriptor

    def record_close(descriptor):
        closed.append(descriptor)
        return real_close(descriptor)

    def check_then_write(path, parent_fd, content, info):
        open_at_rename.extend(one for one in held if one is not None and one not in closed)
        return real_write(path, parent_fd, content, info)

    monkeypatch.setattr(injector, "_open_destination", remember)
    monkeypatch.setattr(injector.os, "close", record_close)
    monkeypatch.setattr(injector, "_write", check_then_write)

    assert injector.write_with_backup(target, b"managed")

    assert held and held[0] is not None, "the existing destination was not opened"
    assert not open_at_rename, "the destination is still open when the rename runs"
    assert target.read_bytes() == b"managed"


def test_a_node_that_took_the_name_before_the_rename_is_refused(tmp_path, helpers, monkeypatch):
    """The rename replaces a name, and a descriptor pins an inode rather than one.

    Another process can take the destination's name while this write prepares its
    backup, and the rename would then replace a file whose bytes no snapshot holds.
    """
    injector, _ = helpers
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(b"original")
    intruder = b"another process put this here"
    original_write = injector._write

    def take_the_name_then_write(path, parent_fd, content, info):
        # After the backup, before the rename: a different regular file, so only its
        # identity tells the write that the name is no longer the one it read.
        path.unlink()
        path.write_bytes(intruder)
        return original_write(path, parent_fd, content, info)

    monkeypatch.setattr(injector, "_write", take_the_name_then_write)

    with pytest.raises(ValueError, match="took the name"):
        injector.write_with_backup(target, b"managed")

    assert target.read_bytes() == intruder, "the file that took the name was replaced"
    backup, = tmp_path.glob("CLAUDE.md.backup.*")
    assert backup.read_bytes() == b"original"
    assert not list(tmp_path.glob(".CLAUDE.md.*")), "the temporary file is left behind"


def test_a_file_created_under_a_new_destination_is_refused(tmp_path, helpers, monkeypatch):
    """A write that found no file refuses one that appeared while it prepared.

    Replacing it would destroy bytes this run never read and never backed up.
    """
    injector, _ = helpers
    target = tmp_path / "AGENTS.md"
    intruder = b"another process created this"
    original_write = injector._write

    def create_then_write(path, parent_fd, content, info):
        path.write_bytes(intruder)
        return original_write(path, parent_fd, content, info)

    monkeypatch.setattr(injector, "_write", create_then_write)

    with pytest.raises(ValueError, match="took the name"):
        injector.write_with_backup(target, b"managed")

    assert target.read_bytes() == intruder
    assert not list(tmp_path.glob("AGENTS.md.backup.*")), "a file it never read was snapshotted"
