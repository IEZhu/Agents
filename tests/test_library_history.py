"""Durable Git versions, guarded recovery and lossless legacy archive migration."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time
from types import SimpleNamespace

import pytest

from src import user_library
from src.file_lock import file_lock
from src.library_history import (ABANDONED_PREFIX, HISTORY_REF, INCOMING_PREFIX, PENDING_REF, PORTABLE_METADATA,
                                 RECEIVED_PREFIX, HistoryError, LibraryHistory)
from src.user_sync import scope, versions


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "library"
    with file_lock(root / ".lock"):
        result = LibraryHistory(root)
        result.ensure()
        yield result


def legacy(root, path, raw, stamp="20261001T000000000001Z", *, deleted=False):
    directory, filename = path.rsplit("/", 1)
    version = stamp + "-" + hashlib.sha256(raw).hexdigest()[:12] + ("-deleted" if deleted else "")
    target = root / ".history" / directory / Path(filename).stem / (version + ".md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return version, target


def portable(history, event, name, *, allow=(), origins=None):
    rules = scope.Rules(scope.Scopes(allow=frozenset(allow)), origins or {})
    publication = versions.publication([event], rules)
    assert len(publication.events) == 1
    tree = history._make_tree(None, publication.events[0][1])
    commit = history.git.text("commit-tree", tree, input=("portable " + name + "\n").encode())
    ref = INCOMING_PREFIX + name
    history.git.run("update-ref", ref, commit)
    return ref


def source_event(*, previous=b"# Old\n", content=b"# New\n", extra=None):
    path = "common/shared.md"
    return {"id": "git-" + "a" * 40, "path": path, "action": "save", "archive": False,
            "deleted": False, "time": "2026-10-01T00:00:00+00:00", "previous": previous,
            "content": content, "changes": {path: content, **(extra or {})}}


def test_install_initializes_only_private_ref_with_service_identity(history):
    assert history.git.text("config", "--get", "agents-sync.managed") == "true"
    assert history.git.text("symbolic-ref", "HEAD") == "refs/heads/main"
    assert history.git.text("log", "-1", "--format=%an <%ae>", HISTORY_REF) == "Agents-Core <agents-core@localhost>"
    assert not (history.root / ".git" / "index").exists()
    assert not (history.root / ".history").exists()
    old = history._rev(HISTORY_REF)
    assert LibraryHistory(history.root).ensure() == {"status": "ready", "imported": 0}
    assert history._rev(HISTORY_REF) == old


def test_all_versions_survive_restart_and_gc_without_worktree_archives(history):
    path = "common/mine.md"
    history.apply({path: b"# One\r\n"}, flow_path=path)
    first = history.apply({path: b"# Two\n"}, flow_path=path)
    second = history.apply({path: b"# Three\n"}, flow_path=path)
    deleted = history.apply({path: None}, action="delete", flow_path=path)
    history.git.run("gc", "--prune=now")
    reopened = LibraryHistory(history.root)
    reopened.ensure()
    assert [version["version"] for version in reopened.history(path)] == [deleted, second, first]
    assert [reopened.read(path, version) for version in (first, second, deleted)] == [
        b"# One\r\n", b"# Two\n", b"# Three\n"]
    assert reopened.history(path)[0]["deleted"]
    assert not (history.root / path).exists()
    assert not (history.root / ".history").exists()


def test_noop_and_persona_only_changes_do_not_archive_flow(history):
    path = "common/mine.md"
    history.apply({path: b"# One\n"}, flow_path=path)
    old = history._rev(HISTORY_REF)
    assert history.apply({path: b"# One\n"}, flow_path=path) is None
    assert history._rev(HISTORY_REF) == old
    history.apply({path: b"# One\n", "personas/common/mine.json": b"{}\n"}, flow_path=path)
    assert history.history(path) == []
    assert history.export_events()[-1]["path"] is None


def test_external_edits_are_captured_before_next_save(history):
    path = "common/mine.md"
    history.apply({path: b"# One\n"}, flow_path=path)
    (history.root / path).write_bytes(b"# Manual\n")
    history.apply({path: b"# Three\n"}, flow_path=path)
    versions = history.history(path)
    assert [history.read(path, entry["version"]) for entry in versions] == [b"# Manual\n", b"# One\n"]


def test_reconcile_captures_external_flow_updates_and_deletions(history):
    first, second = "common/first.md", "common/second.md"
    history.apply({first: b"# One\n"}, flow_path=first)
    history.apply({second: b"# Two\n"}, flow_path=second)
    (history.root / first).write_bytes(b"# Changed\n")
    (history.root / second).unlink()
    assert history.reconcile() == 2
    assert history.read(first, history.history(first)[0]["version"]) == b"# One\n"
    assert history.read(second, history.history(second)[0]["version"]) == b"# Two\n"
    assert history.history(second)[0]["deleted"]
    assert history.reconcile() == 0


def test_explicit_archive_is_not_an_external_edit_when_tree_reconciles(history):
    path = "common/mine.md"
    history.apply({path: b"# Current\n"}, flow_path=path)
    archived = history.record(path, b"# Archived elsewhere\n")
    history.reconcile()
    assert [item["version"] for item in history.history(path)] == [archived]
    assert history.read(path, archived) == b"# Archived elsewhere\n"
    assert (history.root / path).read_bytes() == b"# Current\n"


def test_archiving_multiple_flows_keeps_native_tip_and_external_versions_clean(history):
    first, second = "common/first.md", "common/second.md"
    history.apply({first: b"# First current\n"}, flow_path=first)
    history.apply({second: b"# Second current\n"}, flow_path=second)
    native = history._tree(history._rev(HISTORY_REF))
    history.record(first, b"# Excluded archive one\n")
    history.record(second, b"# Excluded archive two\n")
    assert history._tree(history._rev(HISTORY_REF)) == native
    assert history.reconcile() == 0
    (history.root / first).write_bytes(b"# Manual edit\n")
    history.reconcile()
    external = [event for event in history.export_events() if event["action"] == "external"]
    assert len(external) == 1
    assert external[0]["previous"] == b"# First current\n"


@pytest.mark.parametrize("failure", ["restore_commit", "publish"])
def test_archive_is_published_atomically_and_retry_preserves_alias(history, monkeypatch, failure):
    path = "common/mine.md"
    history.apply({path: b"# Current\n"}, flow_path=path)
    alias = "20261001T000000000001Z-" + hashlib.sha256(b"# Archive\n").hexdigest()[:12]
    head = history._rev(HISTORY_REF)
    native = history._tree(head)
    original_commit, original_run = history._commit, history.git.run
    with monkeypatch.context() as patch:
        if failure == "restore_commit":
            def fail_restore(changes, metadata, **options):
                if metadata["action"] == "baseline":
                    raise OSError("interrupted archive")
                return original_commit(changes, metadata, **options)
            patch.setattr(history, "_commit", fail_restore)
        else:
            def fail_publish(*arguments, **options):
                if arguments[:2] == ("update-ref", HISTORY_REF):
                    raise OSError("interrupted archive")
                return original_run(*arguments, **options)
            patch.setattr(history.git, "run", fail_publish)
        with pytest.raises(OSError, match="interrupted archive"):
            history.record(path, b"# Archive\n", version=alias)
    assert history._rev(HISTORY_REF) == head
    assert history._tree(history._rev(HISTORY_REF)) == native
    assert history.history(path) == []
    assert (history.root / path).read_bytes() == b"# Current\n"
    assert history.record(path, b"# Archive\n", version=alias) == alias
    assert history.read(path, alias) == b"# Archive\n"
    assert history._tree(history._rev(HISTORY_REF)) == native
    assert [event["id"] for event in history.export_events()].count(alias) == 1


def test_git_history_reads_allow_linked_worktree_but_mutations_still_refuse(history, tmp_path):
    path = "common/mine.md"
    history.apply({path: b"# One\n"}, flow_path=path)
    version = history.apply({path: b"# Two\n"}, flow_path=path)
    current = history.root / "common"
    current.rename(history.root / "original-common")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "mine.md").write_bytes(b"# Outside\n")
    current.symlink_to(outside, target_is_directory=True)
    reopened = LibraryHistory(history.root)
    assert reopened.read(path, version) == b"# One\n"
    assert reopened.history(path)[0]["version"] == version
    assert any(event["id"] == version for event in reopened.export_events())
    head = reopened._rev(HISTORY_REF)
    assert reopened.reconcile() == 0
    assert reopened._rev(HISTORY_REF) == head
    assert reopened._blob(reopened._tree(head)[path]) == b"# Two\n"
    with pytest.raises(HistoryError, match="symlink directory"):
        reopened.apply({path: b"# Write\n"}, flow_path=path)
    assert (outside / "mine.md").read_bytes() == b"# Outside\n"


def test_interrupted_multi_file_mutation_rolls_forward_after_restart(history, monkeypatch):
    path, overlay = "common/mine.md", "personas/common/mine.json"
    history.apply({path: b"# One\n", overlay: b"old\n"}, flow_path=path)
    original = user_library.atomic_write
    with monkeypatch.context() as patch:
        def fail_overlay(target, raw):
            if Path(target) == history.root / overlay:
                raise OSError("interrupted")
            original(target, raw)
        patch.setattr(user_library, "atomic_write", fail_overlay)
        with pytest.raises(OSError, match="interrupted"):
            history.apply({path: b"# Two\n", overlay: b"new\n"}, flow_path=path)
    pending = history._rev(PENDING_REF)
    assert pending
    assert (history.root / path).read_bytes() == b"# Two\n"
    assert (history.root / overlay).read_bytes() == b"old\n"
    reopened = LibraryHistory(history.root)
    reopened.ensure()
    assert reopened._rev(PENDING_REF) is None
    assert (history.root / overlay).read_bytes() == b"new\n"
    assert reopened.read(path, "git-" + pending) == b"# One\n"


def test_recovery_refuses_unexpected_external_bytes(history, monkeypatch):
    path, overlay = "common/mine.md", "personas/common/mine.json"
    history.apply({path: b"# One\n", overlay: b"old\n"}, flow_path=path)
    original = user_library.atomic_write
    with monkeypatch.context() as patch:
        def fail_overlay(target, raw):
            if Path(target) == history.root / overlay:
                raise OSError("interrupted")
            original(target, raw)
        patch.setattr(user_library, "atomic_write", fail_overlay)
        with pytest.raises(OSError):
            history.apply({path: b"# Two\n", overlay: b"new\n"}, flow_path=path)
    (history.root / path).write_bytes(b"# Another edit\n")
    with pytest.raises(HistoryError, match="changed externally"):
        LibraryHistory(history.root).ensure()
    assert (history.root / path).read_bytes() == b"# Another edit\n"
    assert (history.root / overlay).read_bytes() == b"old\n"
    assert history._rev(PENDING_REF)


def test_explicit_abandonment_keeps_disk_and_intent_durable_but_unpublished(history, monkeypatch):
    path, overlay = "common/mine.md", "personas/common/mine.json"
    history.apply({path: b"# One\n", overlay: b"old\n"}, flow_path=path)
    head = history._rev(HISTORY_REF)
    original = user_library.atomic_write
    with monkeypatch.context() as patch:
        def fail_overlay(target, raw):
            if Path(target) == history.root / overlay:
                raise OSError("interrupted")
            original(target, raw)
        patch.setattr(user_library, "atomic_write", fail_overlay)
        with pytest.raises(OSError):
            history.apply({path: b"# Two\n", overlay: b"new\n"}, flow_path=path)
    pending = history._rev(PENDING_REF)
    (history.root / path).write_bytes(b"# Manual edit\n")
    assert history.abandon_pending()
    assert history._rev(PENDING_REF) is None
    assert history._rev(HISTORY_REF) == head
    assert history._rev(ABANDONED_PREFIX + pending) == pending
    assert (history.root / path).read_bytes() == b"# Manual edit\n"
    assert (history.root / overlay).read_bytes() == b"old\n"
    history.git.run("gc", "--prune=now")
    reopened = LibraryHistory(history.root)
    reopened.ensure()
    proposal = reopened._tree(reopened._rev(ABANDONED_PREFIX + pending))
    assert reopened._blob(proposal[path]) == b"# Two\n"
    assert reopened._blob(proposal[overlay]) == b"new\n"
    assert all(event["commit"] != pending for event in reopened.events())
    assert all(event["commit"] != pending for event in reopened.export_events())
    assert not reopened.abandon_pending()
    reopened.reconcile()
    assert reopened.history(path)[0]["revision_prefix"] == hashlib.sha256(b"# One\n").hexdigest()[:12]
    assert (history.root / path).read_bytes() == b"# Manual edit\n"


@pytest.mark.parametrize("operation", ["ensure", "apply", "recover"])
def test_old_private_ref_locks_are_removed_under_library_lock_only(history, monkeypatch, operation):
    path = "common/mine.md"
    history.apply({path: b"# One\n"}, flow_path=path)
    if operation == "recover":
        original = user_library.atomic_write
        with monkeypatch.context() as patch:
            def fail_flow(target, raw):
                if Path(target) == history.root / path:
                    raise OSError("interrupted")
                original(target, raw)
            patch.setattr(user_library, "atomic_write", fail_flow)
            with pytest.raises(OSError, match="interrupted"):
                history.apply({path: b"# Two\n"}, flow_path=path)
        assert history._rev(PENDING_REF)
    directory = history.root / ".git/refs/agents-core"
    locks = [directory / name for name in ("history.lock", "history-pending.lock")]
    unrelated = directory / "portable" / "other.lock"
    unrelated.parent.mkdir(exist_ok=True)
    old = time.time() - 60
    for target in [*locks, unrelated]:
        target.write_bytes(b"crashed writer")
        os.utime(target, (old, old))
    reopened = LibraryHistory(history.root)
    if operation == "apply":
        reopened.apply({path: b"# Two\n"}, flow_path=path)
    elif operation == "recover":
        assert reopened.recover()
    else:
        reopened.ensure()
    assert all(not target.exists() for target in locks)
    assert unrelated.read_bytes() == b"crashed writer"
    assert (history.root / path).read_bytes() == (b"# One\n" if operation == "ensure" else b"# Two\n")
    assert reopened._rev(PENDING_REF) is None


@pytest.mark.parametrize("name", ["history.lock", "history-pending.lock"])
def test_fresh_private_ref_locks_are_retained(history, name):
    path = "common/mine.md"
    lock = history.root / ".git/refs/agents-core" / name
    lock.write_bytes(b"active writer")
    history.ensure()
    assert not history.recover()
    assert lock.read_bytes() == b"active writer"
    with pytest.raises(HistoryError, match="flow_history"):
        history.apply({path: b"# Proposed\n"}, flow_path=path)
    assert lock.read_bytes() == b"active writer"


def test_preflight_refuses_directory_before_any_mutation(history):
    path = "common/mine.md"
    history.apply({path: b"# One\n"}, flow_path=path)
    (history.root / "personas" / "broken.json").mkdir(parents=True)
    with pytest.raises(HistoryError, match="directory"):
        history.apply({path: b"# Two\n", "personas/broken.json": b"new"}, flow_path=path)
    assert (history.root / path).read_bytes() == b"# One\n"
    assert history._rev(PENDING_REF) is None


def test_symlink_deletion_unlinks_only_link(history, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside\n")
    target = history.root / "personas" / "mine.json"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)
    history.apply({"personas/mine.json": None}, action="persona")
    assert not target.is_symlink()
    assert outside.read_bytes() == b"outside\n"
    assert "personas/mine.json" not in history._tree(history._rev(HISTORY_REF))


def test_windows_junction_scan_and_ancestor_are_not_followed(history, monkeypatch):
    junction = history.root / "common" / "junction"
    junction.mkdir(parents=True)
    (junction / "outside.md").write_bytes(b"outside")
    original = Path.lstat
    def reparse(path, *args, **kwargs):
        if Path(path) == junction:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400,
                                   st_reparse_tag=0xA0000003)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", reparse)
    assert "common/junction/outside.md" not in history._scan()
    with pytest.raises(HistoryError, match="symlink directory"):
        history.apply({"common/junction/outside.md": b"changed"})
    with pytest.raises(HistoryError, match="reparse point"):
        history.apply({"common/junction": None})
    assert (junction / "outside.md").read_bytes() == b"outside"


@pytest.mark.parametrize("path", ["../outside.md", "/tmp/outside.md", "C:/outside.md", "common/con.md",
                                 "common/trailing. ", "common/a\n.md", "common/a\\b.md", ".git/config"])
def test_portable_path_validation(history, path):
    with pytest.raises(HistoryError, match="flow_invalid"):
        history.apply({path: b"contents"}, flow_path=path)


def test_foreign_git_is_refused_without_touching_it(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    before = (root / ".git" / "config").read_bytes()
    with file_lock(root / ".lock"), pytest.raises(HistoryError, match="not created by Agents-Core"):
        LibraryHistory(root).ensure()
    assert (root / ".git" / "config").read_bytes() == before
    assert not (root / ".agents-library.json").exists()
    assert not (root / ".git" / "agents-core-history").exists()


def test_repository_preflight_accepts_missing_root_without_creating_anything(tmp_path):
    root = tmp_path / "not-created"
    history = LibraryHistory(root)
    assert history.validate_repository() is None
    assert not root.exists()
    root.mkdir()
    assert history.validate_repository() is None
    assert list(root.iterdir()) == []


def test_repository_preflight_refuses_foreign_git_without_even_creating_library_lock(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(HistoryError, match="not created by Agents-Core"):
        LibraryHistory(root).validate_repository()
    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before
    assert {path.name for path in root.iterdir()} == {".git"}


def test_repository_preflight_requires_local_managed_flag(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    history = LibraryHistory(root)
    history.directory.mkdir()
    history.git.config.write_bytes(b"[agents-sync]\nmanaged = true\n")
    before = history.git.config.read_bytes(), (root / ".git/config").read_bytes()
    assert history.git.text("config", "--get", "agents-sync.managed") == "true"
    with pytest.raises(HistoryError, match="not created by Agents-Core"):
        history.validate_repository()
    assert (history.git.config.read_bytes(), (root / ".git/config").read_bytes()) == before
    assert {path.name for path in root.iterdir()} == {".git"}


@pytest.mark.parametrize("raw", [b"malformed", b"[]", b'{"format":true}', b'{"format":0}',
                                  b'{"format":999,"custom":"preserved"}'])
def test_repository_preflight_refuses_invalid_markers_without_writes(tmp_path, raw):
    root = tmp_path / "library"
    root.mkdir()
    marker = root / user_library.MARKER
    marker.write_bytes(raw)
    with pytest.raises(HistoryError, match="flow_history"):
        LibraryHistory(root).validate_repository()
    assert marker.read_bytes() == raw
    assert list(root.iterdir()) == [marker]


def test_repository_preflight_accepts_legacy_marker_without_upgrading_it(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    marker = root / user_library.MARKER
    raw = b'{"format":1,"custom":"preserved"}\n'
    marker.write_bytes(raw)
    LibraryHistory(root).validate_repository()
    assert marker.read_bytes() == raw
    assert list(root.iterdir()) == [marker]


@pytest.mark.parametrize("relative", [user_library.MARKER, ".gitignore", ".gitattributes", ".lock",
                                      ".git", ".git/config", ".git/agents-core-history",
                                      ".git/agents-core-history/gitconfig", ".git/agents-core-history/hooks"])
def test_repository_preflight_refuses_linked_support_paths_without_following_them(tmp_path, relative):
    root = tmp_path / "library"
    root.mkdir()
    if relative.startswith(".git/"):
        subprocess.run(["git", "init", "--quiet", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "--local", "agents-sync.managed", "true"], check=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside must stay unchanged\n")
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    target.symlink_to(outside)
    with pytest.raises(HistoryError, match="symlink or reparse point"):
        LibraryHistory(root).validate_repository()
    assert target.is_symlink()
    assert outside.read_bytes() == b"outside must stay unchanged\n"
    if relative == ".lock":
        assert (root / ".lock").is_symlink()
    else:
        assert not (root / ".lock").exists()
    if relative != user_library.MARKER:
        assert not (root / user_library.MARKER).exists()


@pytest.mark.parametrize("relative", [user_library.MARKER, ".gitignore", ".gitattributes", ".lock"])
def test_repository_preflight_refuses_directories_in_support_file_paths(tmp_path, relative):
    root = tmp_path / "library"
    (root / relative).mkdir(parents=True)
    with pytest.raises(HistoryError, match="invalid file type"):
        LibraryHistory(root).validate_repository()
    assert list(root.iterdir()) == [root / relative]


def test_cached_backend_revalidates_repository_ownership_before_writes(history):
    old = history._rev(HISTORY_REF)
    history.git.run("config", "--local", "agents-sync.managed", "false")
    before = (history.root / ".git/config").read_bytes()
    with pytest.raises(HistoryError, match="not created by Agents-Core"):
        history.ensure()
    assert (history.root / ".git/config").read_bytes() == before
    assert history._rev(HISTORY_REF) == old


def test_repository_preflight_refuses_file_as_library_root_without_mutation(tmp_path):
    root = tmp_path / "library"
    root.write_bytes(b"keep this file\n")
    with pytest.raises(HistoryError, match="root is not a directory"):
        LibraryHistory(root).validate_repository()
    assert root.read_bytes() == b"keep this file\n"


def test_newer_marker_is_preserved_before_git_is_created(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    marker = root / user_library.MARKER
    marker.write_bytes(b'{"format":999,"custom":"preserved"}\n')
    before = marker.read_bytes()
    with file_lock(root / ".lock"), pytest.raises(HistoryError, match="newer"):
        LibraryHistory(root).ensure()
    assert marker.read_bytes() == before
    assert not (root / ".git").exists()


def test_cached_backend_still_refuses_a_newer_marker_without_rewriting_it(history):
    marker = history.root / user_library.MARKER
    marker.write_bytes(b'{"format":999,"custom":"preserved"}\n')
    before, head = marker.read_bytes(), history._rev(HISTORY_REF)
    with pytest.raises(HistoryError, match="newer"):
        history.ensure()
    assert marker.read_bytes() == before
    assert history._rev(HISTORY_REF) == head


def test_legacy_import_preserves_aliases_bytes_and_current_tree(tmp_path):
    root = tmp_path / "library"
    path = "common/mine.md"
    (root / "common").mkdir(parents=True)
    (root / path).write_bytes(b"# Current\n")
    (root / user_library.MARKER).write_text('{"format":1,"custom":"retained"}\n')
    old, _ = legacy(root, path, b"# Old\r\n")
    deleted, _ = legacy(root, path, b"# Deleted\n", "20261002T000000000001Z", deleted=True)
    with file_lock(root / ".lock"):
        history = LibraryHistory(root)
        assert history.ensure() == {"status": "ready", "imported": 2}
        assert history.read(path, old) == b"# Old\r\n"
        assert history.read(path, deleted) == b"# Deleted\n"
        assert [entry["version"] for entry in history.history(path)] == [deleted, old]
        assert history._blob(history._tree(history._rev(HISTORY_REF))[path]) == b"# Current\n"
        marker = json.loads(history._blob(history._tree(history._rev(HISTORY_REF))[user_library.MARKER]))
        assert marker == {"format": 2, "custom": "retained"}
        assert not (root / ".history").exists()
        old_head = history._rev(HISTORY_REF)
        assert history.import_legacy() == {"status": "ready", "imported": 0}
        assert history._rev(HISTORY_REF) == old_head


def test_migration_retry_deduplicates_already_imported_ids(tmp_path, monkeypatch):
    root = tmp_path / "library"
    path = "common/mine.md"
    first, target1 = legacy(root, path, b"# One\n")
    second, target2 = legacy(root, path, b"# Two\n", "20261002T000000000001Z")
    with file_lock(root / ".lock"):
        history = LibraryHistory(root)
        original = history.record
        with monkeypatch.context() as patch:
            def fail_second(path, raw, **kwargs):
                if kwargs["version"] == second:
                    raise OSError("interrupted migration")
                return original(path, raw, **kwargs)
            patch.setattr(history, "record", fail_second)
            with pytest.raises(OSError, match="interrupted migration"):
                history.ensure()
        assert target1.exists() and target2.exists()
        assert history.ensure()["imported"] == 2
        assert [event["id"] for event in history.export_events()].count(first) == 1
        assert history.read(path, first) == b"# One\n"
        assert history.read(path, second) == b"# Two\n"


def test_bad_legacy_checksum_does_not_delete_archives(tmp_path):
    root = tmp_path / "library"
    _, target = legacy(root, "common/mine.md", b"# One\n")
    target.write_bytes(b"# Different\n")
    with file_lock(root / ".lock"), pytest.raises(HistoryError, match="checksum"):
        LibraryHistory(root).ensure()
    assert target.read_bytes() == b"# Different\n"


def test_portable_event_alias_reads_git_blobs_without_materializing(history):
    path = "common/mine.md"
    alias = "git-" + "a" * 40
    metadata = {"format": 1, "source_id": alias, "path": path, "action": "save",
                "time": "2026-10-03T00:00:00+00:00", "archive": False, "deleted": False,
                "previous_path": "previous/" + path, "content_path": "content/" + path,
                "changes": {path: "changes/" + path}, "version": alias}
    tree = history._make_tree(None, {PORTABLE_METADATA: json.dumps(metadata).encode(),
                                    "previous/" + path: b"# Remote One\n",
                                    "content/" + path: b"# Remote Two\n",
                                    "changes/" + path: b"# Remote Two\n"})
    commit = history.git.text("commit-tree", tree, input=b"portable history\n")
    history.git.run("update-ref", INCOMING_PREFIX + "other", commit)
    assert history.read(path, alias) == b"# Remote One\n"
    assert history.history(path)[0]["version"] == alias
    assert not (history.root / path).exists()
    assert all(event["id"] != alias for event in history.export_events())
    history.git.run("update-ref", RECEIVED_PREFIX + commit, commit)
    history.git.run("update-ref", "-d", INCOMING_PREFIX + "other")
    history.git.run("gc", "--prune=now")
    reopened = LibraryHistory(history.root)
    reopened.ensure()
    assert reopened.read(path, alias) == b"# Remote One\n"
    assert all(event["id"] != alias for event in reopened.export_events())


@pytest.mark.parametrize("allowed_first", [False, True])
def test_compatible_policy_copies_restore_previously_filtered_version(history, allowed_first):
    secret = b"ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n"
    event = source_event(previous=secret)
    arguments = [("restricted", ()), ("allowed", (hashlib.sha256(secret).hexdigest(),))]
    if allowed_first:
        arguments.reverse()
    refs = [portable(history, event, name, allow=allow) for name, allow in arguments]
    for order in (refs, list(reversed(refs))):
        combined = history.events(order)
        assert len(combined) == 1
        assert combined[0]["previous"] == secret
        assert combined[0]["content"] == b"# New\n"
    assert history.read(event["path"], event["id"]) == secret
    assert history.history(event["path"])[0]["version"] == event["id"]


def test_compatible_policy_copies_union_filtered_change_entries(history):
    secret = b"ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n"
    event = source_event(content=secret, extra={"common/other.md": b"# Other\n"})
    first = portable(history, event, "restricted")
    second = portable(history, event, "allowed", allow=(hashlib.sha256(secret).hexdigest(),))
    for refs in ([first, second], [second, first]):
        combined = history.events(refs)[0]
        assert combined["content"] == secret
        assert combined["changes"] == event["changes"]


@pytest.mark.parametrize("field", ["previous", "content", "changes"])
def test_conflicting_portable_copies_are_refused(history, field):
    original = source_event()
    conflicting = source_event()
    if field == "changes":
        conflicting["changes"] = {original["path"]: b"# Different\n"}
    else:
        conflicting[field] = b"# Different\n"
    first = portable(history, original, "first")
    second = portable(history, conflicting, "second")
    for refs in ([first, second], [second, first]):
        with pytest.raises(HistoryError, match="conflicting copies"):
            history.events(refs)


def test_native_transition_keeps_precedence_over_filtered_portable_copy(history):
    path = "common/shared.md"
    history.apply({path: b"# Old\n"}, flow_path=path)
    source = history.apply({path: b"# New\n"}, flow_path=path)
    event = next(item for item in history.export_events() if item["id"] == source)
    ref = portable(history, event, "copy")
    native = next(item for item in history.events([ref, HISTORY_REF]) if item["id"] == source)
    assert native["commit"] == source.removeprefix("git-")
    assert native["previous"] == b"# Old\n"
    conflicting = {**event, "previous": b"# Different\n"}
    portable(history, conflicting, "conflicting")
    with pytest.raises(HistoryError, match="conflicting copies"):
        history.events()


def test_native_imported_archive_matches_source_predecessor_and_keeps_precedence(history):
    event = source_event()
    ref = portable(history, event, "source")
    history.record(event["path"], event["previous"], version=event["id"])
    for refs in ([HISTORY_REF, ref], [ref, HISTORY_REF]):
        found = next(item for item in history.events(refs) if item["id"] == event["id"])
        assert found["archive"]
        assert found["content"] == b"# Old\n"
    assert history.read(event["path"], event["id"]) == b"# Old\n"
    conflicting = {**event, "previous": b"# Different\n"}
    portable(history, conflicting, "conflicting")
    with pytest.raises(HistoryError, match="conflicting copies"):
        history.events()


def test_native_baseline_keeps_legacy_metadata_over_sanitized_own_publication(history):
    path = "repos/sample/.repo.json"
    raw = b'{"origin":"github.com/owner/sample","path":"/local/private/clone"}\n'
    history.apply({path: raw}, action="baseline")
    event = history.export_events()[-1]
    ref = portable(history, event, "own", origins={"repos/sample": "github.com/owner/sample"})
    portable_event = history.events([ref])[0]
    assert json.loads(portable_event["changes"][path]) == {"origin": "github.com/owner/sample"}
    for refs in ([ref, HISTORY_REF], [HISTORY_REF, ref]):
        combined = next(item for item in history.events(refs) if item["id"] == event["id"])
        assert combined["commit"] == event["commit"]
        assert combined["changes"][path] == raw
