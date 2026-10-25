"""Journal artifacts must not read or import a linked private journal."""

import os

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
