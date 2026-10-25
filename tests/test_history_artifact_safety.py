"""Journal artifacts must not read or import a linked private journal."""

import os
import stat
import sys
from types import SimpleNamespace

import pytest

from src.memory import history


def link_or_skip(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable")
        raise


@pytest.fixture
def journals(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_machine_history", lambda _: None)
    selected, private = tmp_path / "selected", tmp_path / "private"
    selected.mkdir()
    private.mkdir()
    target = private / "history.md"
    history.HistoryWriter(str(target)).append_entry("PRIVATE_MARKER", "action", "outcome")
    return selected / "history.md", target


@pytest.mark.parametrize("artifact", ["history.md", "history.md.rotating", "history/2026-01.md"])
@pytest.mark.parametrize("read", ["journal_entries", "git_export"])
def test_linked_journal_artifacts_fail_closed(journals, artifact, read):
    path, target = journals
    linked = path.parent / artifact
    linked.parent.mkdir(exist_ok=True)
    link_or_skip(linked, target)
    with pytest.raises(OSError):
        if read == "git_export":
            history.journal_blocks(str(path))
        else:
            history.HistoryReader(str(path)).journal_entries()


def test_recent_read_rejects_pending_link_without_sync(journals):
    path, target = journals
    link_or_skip(path.with_name(path.name + ".rotating"), target)
    with pytest.raises(OSError):
        history.HistoryReader(str(path)).read_recent()


@pytest.mark.parametrize("target_kind", ["inside", "missing", "directory"])
def test_pending_must_be_a_regular_file_even_without_an_escape(journals, target_kind):
    path, _ = journals
    pending = path.with_name(path.name + ".rotating")
    if target_kind == "directory":
        pending.mkdir()
    else:
        target = path.with_name("other.md")
        if target_kind == "inside":
            target.write_text("local")
        link_or_skip(pending, target)
    with pytest.raises(OSError):
        history.HistoryReader(str(path)).read_recent()


def test_cached_regular_file_cannot_be_retargeted_to_link(journals):
    path, target = journals
    history.HistoryWriter(str(path)).append_entry("local", "action", "outcome")
    assert history.parsed_file(str(path))[1][0].intent == "local"
    path.unlink()
    link_or_skip(path, target)
    with pytest.raises(OSError):
        history.parsed_file(str(path))


def test_leaf_swap_between_inspection_and_open_is_not_read(journals, monkeypatch):
    path, target = journals
    path.write_text("local")
    # Confirm symlink privilege before injecting a race into the open boundary.
    probe = path.with_name("probe")
    link_or_skip(probe, target)
    probe.unlink()
    real_open = os.open
    swapped = False

    def replace_then_open(filename, flags, *args, **kwargs):
        nonlocal swapped
        if os.path.basename(os.fspath(filename)) == path.name and not swapped:
            swapped = True
            path.unlink()
            path.symlink_to(target)
        return real_open(filename, flags, *args, **kwargs)

    monkeypatch.setattr(history.os, "open", replace_then_open)
    with pytest.raises(OSError):
        history.parsed_file(str(path))
    assert swapped


def test_keyed_acceptance_refuses_pending_link_before_commit(journals):
    path, target = journals
    link_or_skip(path.with_name(path.name + ".rotating"), target)
    before = target.read_bytes()
    with pytest.raises(OSError):
        history.HistoryWriter(str(path)).append_entry("local", "action", "outcome", idempotency_key="one")
    assert not path.exists()
    assert target.read_bytes() == before


def test_rotation_does_not_import_linked_pending(journals):
    path, target = journals
    pending = path.with_name(path.name + ".rotating")
    link_or_skip(pending, target)
    before = target.read_bytes()
    with pytest.raises(OSError):
        history.HistoryWriter(str(path))._merge_pending_locked(str(pending))
    assert not (path.parent / "history").exists()
    assert pending.is_symlink() and target.read_bytes() == before


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipes")
def test_pending_fifo_is_rejected_before_read(journals):
    path, _ = journals
    os.mkfifo(path.with_name(path.name + ".rotating"))
    with pytest.raises(OSError):
        history.HistoryReader(str(path)).read_recent()


def test_archive_directory_link_never_exports_private_entries(journals):
    path, target = journals
    target.rename(target.with_name("2026-01.md"))
    link_or_skip(path.parent / "history", target.parent, directory=True)
    with pytest.raises(OSError):
        history.journal_blocks(str(path))


def test_nested_archive_cannot_follow_an_earlier_directory_link(journals):
    path, target = journals
    nested = target.parent / "nested"
    nested.mkdir()
    target.rename(nested / "2026-01.md")
    link_or_skip(path.parent / "custom", target.parent, directory=True)
    archive = path.parent / "custom" / "nested"
    with pytest.raises(OSError):
        history.HistoryReader(str(path), str(archive)).journal_entries()
    with pytest.raises(OSError):
        history.journal_blocks(str(path), str(archive))


def test_regular_external_archive_remains_supported(journals):
    path, target = journals
    target.rename(target.with_name("2026-01.md"))
    entries = history.HistoryReader(str(path), str(target.parent)).journal_entries()
    assert [entry.intent for entry in entries] == ["PRIVATE_MARKER"]


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX descriptor-relative directory open")
def test_archive_directory_swap_cannot_redirect_the_open(journals, monkeypatch):
    path, target = journals
    directory = path.parent / "history"
    directory.mkdir()
    (directory / "2026-01.md").write_text("local")
    target.rename(target.with_name("2026-01.md"))
    real_open = os.open

    def swapped(filename, flags, *args, **kwargs):
        if filename == "history":
            directory.rename(path.parent / "original")
            directory.symlink_to(target.parent, target_is_directory=True)
        return real_open(filename, flags, *args, **kwargs)

    monkeypatch.setattr(history.os, "open", swapped)
    with pytest.raises(OSError):
        history.HistoryReader(str(path)).journal_entries()


def test_failed_unlink_does_not_truncate_a_retargeted_pending(journals, monkeypatch):
    path, target = journals
    pending = path.with_name(path.name + ".rotating")
    link_or_skip(pending, target)
    before = target.read_bytes()

    def denied(_):
        raise PermissionError("pending busy")

    monkeypatch.setattr(history.os, "unlink", denied)
    history.HistoryWriter._discard_pending(str(pending))
    assert target.read_bytes() == before


def test_no_nofollow_fallback_rejects_wrong_open_handle_path(journals, monkeypatch):
    path, target = journals
    path.write_text("local")
    monkeypatch.delattr(history.os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(history, "_windows_final_path", lambda _: str(target))
    with pytest.raises(PermissionError, match="escaped"):
        history.parsed_file(str(path))


def test_workspace_preflight_rejects_pending_escape(journals):
    from src.daemon.workspaces import ClientContext, WorkspaceError

    path, target = journals
    link_or_skip(path.with_name(path.name + ".rotating"), target)
    context = ClientContext("test", "stdio", root=path.parent)
    with pytest.raises(WorkspaceError, match="escapes"):
        context.require_root()


@pytest.mark.parametrize("plain,extended", [
    ("C:\\Work\\history.md", "\\\\?\\C:\\Work\\history.md"),
    ("\\\\Host\\Share\\history.md", "\\\\?\\UNC\\Host\\Share\\history.md"),
])
def test_windows_handle_path_normalization_preserves_dos_and_unc(plain, extended):
    assert history._normalized_windows_path(plain) == history._normalized_windows_path(extended)


def test_windows_regular_reparse_file_survives_missing_fstat_tag(journals, monkeypatch):
    path, _ = journals
    path.write_bytes(b"cloud-backed journal")
    real_lstat, real_fstat = os.lstat, os.fstat

    def shaped(info, tag):
        values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        return SimpleNamespace(**{**values, "st_file_attributes": 0x420, "st_reparse_tag": tag})

    def lstat(filename, *args, **kwargs):
        info = real_lstat(filename, *args, **kwargs)
        return shaped(info, 0x9000601A) if stat.S_ISREG(info.st_mode) else info

    def fstat(fd):
        info = real_fstat(fd)
        return shaped(info, 0) if stat.S_ISREG(info.st_mode) else info

    monkeypatch.delattr(history.os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(history.os, "lstat", lstat)
    monkeypatch.setattr(history.os, "fstat", fstat)
    monkeypatch.setattr(history, "_windows_final_path", lambda _: history._normalized_windows_path(str(path)))
    with history._open_regular(str(path), "rb") as stream:
        assert stream.read() == b"cloud-backed journal"


def test_regular_history_file_does_not_disable_read_or_export(journals):
    path, _ = journals
    history.HistoryWriter(str(path)).append_entry("local", "action", "outcome")
    (path.parent / "history").write_text("project release history")
    reader = history.HistoryReader(str(path))
    assert [e.intent for e in reader.journal_entries()] == ["local"]
    assert [e.intent for e, _ in history.journal_blocks(str(path))] == ["local"]
    assert [e.intent for e in reader.read_merged(history.MachineHistory("local"))] == ["local"]
    # Keep keyed acceptance fail-closed when its receipt archive cannot be enumerated.
    with pytest.raises(NotADirectoryError):
        history.HistoryWriter(str(path)).append_entry("next", "action", "outcome", idempotency_key="one")


def test_failed_unlink_empties_regular_pending_and_keeps_delete_usable(journals, monkeypatch):
    path, _ = journals
    pending = path.with_name(path.name + ".rotating")
    real_unlink = os.unlink

    def refuse_pending(filename, *args, **kwargs):
        if os.fspath(filename) == str(pending):
            raise PermissionError("pending busy")
        return real_unlink(filename, *args, **kwargs)

    monkeypatch.setattr(history.os, "unlink", refuse_pending)
    result = history.HistoryWriter(str(path), rotation_kb=0).append_entry("local", "action", "outcome")
    assert pending.stat().st_size == 0
    reader = history.HistoryReader(str(path))
    assert reader.read_recent() == []
    entry, = reader.journal_entries()
    assert entry.id == result["entry_id"]
    history.delete_entry(str(path), entry.timestamp[:7], entry.id, entry.timestamp)
    assert reader.journal_entries() == []


def test_delete_reports_unsafe_pending_without_following_it(journals):
    path, target = journals
    receipt = history.HistoryWriter(str(path)).append_entry("local", "action", "outcome")
    before = target.read_bytes()
    link_or_skip(path.with_name(path.name + ".rotating"), target)
    with pytest.raises(PermissionError):
        history.delete_entry(str(path), None, receipt["entry_id"], receipt["timestamp"])
    assert target.read_bytes() == before


@pytest.mark.parametrize("warm", [False, True])
def test_statistics_refuse_linked_pending_even_with_cached_answers(journals, warm):
    from src.memory.history_stats import FileSummaries, _workspace_answers

    path, target = journals
    pending = path.with_name(path.name + ".rotating")
    summaries = FileSummaries()
    if warm:
        pending.write_bytes(target.read_bytes())
        assert summaries.answers(str(pending))
        pending.unlink()
    link_or_skip(pending, target)
    with pytest.raises(OSError):
        list(_workspace_answers(path.parent, summaries, "2000-01"))


def test_ui_raw_source_refuses_an_in_workspace_link(journals, monkeypatch):
    from src.daemon.history_ui import HistoryUI

    path, target = journals
    inside = path.parent / "other.md"
    inside.write_bytes(target.read_bytes())
    link_or_skip(path, inside)
    ui = HistoryUI(SimpleNamespace())
    monkeypatch.setattr(ui, "_root", lambda _: path.parent)
    with pytest.raises(OSError):
        ui.source("workspace", "current")


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_no_sync_semantic_index_refuses_unsafe_journal(journals, monkeypatch, kind):
    path, target = journals
    if kind == "symlink":
        link_or_skip(path, target)
    elif kind == "directory":
        path.mkdir()
    elif hasattr(os, "mkfifo"):
        os.mkfifo(path)
    else:
        pytest.skip("POSIX named pipes")
    data_dir = path.parent / "index"
    data_dir.mkdir()
    fake_store = SimpleNamespace(count=lambda: 0)
    monkeypatch.setitem(sys.modules, "src.engine.vector_store", SimpleNamespace(NumpyVectorStore=lambda **_: fake_store))
    monkeypatch.setitem(sys.modules, "src.engine.embedder", SimpleNamespace(model_fingerprint=lambda: "test"))
    store = history.HistoryStore(str(path), str(data_dir))
    parsed = []
    monkeypatch.setattr(store, "_refresh", lambda *args, **kwargs: parsed.extend(kwargs["load"]()))
    with pytest.raises(OSError):
        store._ensure(None)
    assert not parsed
