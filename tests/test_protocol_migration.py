"""Installer migration must not overwrite unrelated or user-edited instructions."""
import importlib
from pathlib import Path

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
                                    "memory-routing-v5.md"])
def test_known_memory_and_index_migrate_with_backups(tmp_path, helpers, legacy):
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

    def fail_copy(source, destination):
        Path(destination).write_bytes(b"partial snapshot")
        raise OSError("backup copy failed")

    monkeypatch.setattr(injector.shutil, "copy2", fail_copy)
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

    def fail_replace(*args):
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
    original_unlink = Path.unlink

    def fail_backup_unlink(path, *args, **kwargs):
        if ".backup." in path.name:
            raise PermissionError("backup is read-only")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_backup_unlink)
    assert injector.write_with_backup(target, b"updated")
    assert target.read_bytes() == b"updated"
    assert "Could not prune instruction backups" in capsys.readouterr().err
