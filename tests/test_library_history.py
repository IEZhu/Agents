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
from src.library_history import (ABANDONED_PREFIX, ACCEPTED_PREFIX, HISTORY_REF, INCOMING_PREFIX, LEGACY_SUPPORT_MANIFEST,
                                 LEGACY_SUPPORT_REF, PENDING_REF, PORTABLE_METADATA,
                                 RECEIVED_PREFIX, HistoryError, LibraryHistory)
from src.user_sync import gitcmd, scope, versions


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
    locks = [directory / name for name in ("history.lock", "history-pending.lock", "legacy-support.lock")]
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


def test_fresh_history_requests_batch_objects_and_never_rewrite_unchanged_config(history, monkeypatch):
    expected = {}
    for number in range(40):
        path = "common/mine.md" if number % 2 else "repos/sample/other.md"
        raw = f"# Revision {number}\r\n".encode() + b"\0\xff"
        commit = history._commit({path: raw}, {"action": "save", "path": path, "paths": [path],
            "archive": False, "deleted": False, "time": history._now()})
        expected["git-" + commit] = raw
    config = history.git.config.read_bytes()
    original, calls = gitcmd.subprocess.run, []
    def counted(*arguments, **options):
        calls.append(arguments[0])
        return original(*arguments, **options)
    def refuse_config_write(*arguments, **options):
        pytest.fail("an unchanged history request must not rewrite support files")
    monkeypatch.setattr(user_library, "atomic_write", refuse_config_write)
    monkeypatch.setattr(gitcmd.subprocess, "run", counted)
    for _ in range(2):
        calls.clear()
        events = LibraryHistory(history.root).events()
        assert {item["id"]: item["content"] for item in events if item["id"] in expected} == expected
        assert len(calls) <= 12, calls
        assert not any("ls-tree" in arguments for arguments in calls)
        assert not any("cat-file" in arguments and "blob" in arguments for arguments in calls)
        assert any("--local" in arguments and "config" in arguments for arguments in calls)
    assert history.git.config.read_bytes() == config


def test_batch_reader_observes_new_private_and_received_refs_without_shared_content_cache(history):
    reader = LibraryHistory(history.root)
    initial = reader.events()
    path = "common/mine.md"
    commit = history._commit({path: b"# New local\r\n"}, {"action": "save", "path": path, "paths": [path],
        "archive": False, "deleted": False, "time": history._now()})
    event = source_event(previous=b"# Received before\r\n")
    incoming = portable(history, event, "received")
    tip = history._rev(incoming)
    history.git.run("update-ref", RECEIVED_PREFIX + tip, tip)
    history.git.run("update-ref", "-d", incoming)
    observed = {item["id"]: item for item in reader.events()}
    assert len(observed) == len(initial) + 2
    assert observed["git-" + commit]["content"] == b"# New local\r\n"
    assert observed[event["id"]]["previous"] == b"# Received before\r\n"
    assert all(item["id"] != event["id"] for item in reader.export_events())
    history.git.run("config", "--local", "agents-sync.managed", "false")
    with pytest.raises(HistoryError, match="not created by Agents-Core"):
        reader.events()


def test_batched_history_rejects_invalid_utf8_tree_entries_with_typed_error(history):
    blob = history.git.text("hash-object", "-w", "--stdin", input=b"# Bytes\n")
    raw = b"100644 invalid-\xff\0" + bytes.fromhex(blob)
    tree = history.git.text("hash-object", "-w", "-t", "tree", "--stdin", input=raw)
    commit = history.git.text("commit-tree", tree, input=b"unknown incoming commit\n")
    history.git.run("update-ref", INCOMING_PREFIX + "invalid", commit)
    with pytest.raises(HistoryError, match="invalid entry"):
        LibraryHistory(history.root).events()


def saved_support(history):
    commit = history._rev(LEGACY_SUPPORT_REF)
    assert commit
    tree = history._tree(commit)
    manifest = json.loads(history._blob(tree[LEGACY_SUPPORT_MANIFEST]))
    return {item["path"]: history._blob(tree[item["stored"]]) for item in manifest["files"]}


def test_legacy_editor_and_os_leftovers_are_preserved_exactly_and_never_exported(tmp_path):
    root = tmp_path / "library"
    alias, _ = legacy(root, "common/mine.md", b"# Confirmed archive\r\n")
    values = {".DS_Store": b"\0Finder metadata\xff", "Thumbs.db": b"\0Thumbnail bytes\xfe",
              "common/mine/.tmp-pending.md": b"# Unconfirmed flow edit\r\n",
              "common/mine/unsaved.md~": b"# Editor backup\r\n", "common/mine/.unsaved.md.swp": b"\0Swap bytes\xff"}
    for name, raw in values.items():
        target = root / ".history" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    with file_lock(root / ".lock"):
        history = LibraryHistory(root)
        assert history.ensure() == {"status": "ready", "imported": 1}
        assert history.read("common/mine.md", alias) == b"# Confirmed archive\r\n"
        assert not (root / ".history").exists()
        history.git.run("gc", "--prune=now")
        reopened = LibraryHistory(root)
        reopened.ensure()
        assert saved_support(reopened) == values
        assert reopened.events([LEGACY_SUPPORT_REF]) == []
        for event in reopened.export_events():
            assert event["content"] not in values.values()
            assert not any(raw in values.values() for raw in event["changes"].values())


def test_legacy_support_verification_failure_preserves_working_files_and_retries(history, monkeypatch):
    target = history.root / ".history/.tmp-pending.md"
    target.parent.mkdir()
    target.write_bytes(b"# Pending bytes\r\n")
    original = history._blob
    with monkeypatch.context() as patch:
        def corrupt_verified_read(oid):
            raw = original(oid)
            return b"corrupt read" if raw == b"# Pending bytes\r\n" else raw
        patch.setattr(history, "_blob", corrupt_verified_read)
        with pytest.raises(HistoryError, match="support file verification failed"):
            history.import_legacy()
    assert target.read_bytes() == b"# Pending bytes\r\n"
    tip = history._rev(LEGACY_SUPPORT_REF)
    assert tip
    history.import_legacy()
    assert history._rev(LEGACY_SUPPORT_REF) == tip
    assert saved_support(history) == {".tmp-pending.md": b"# Pending bytes\r\n"}
    assert not target.exists()


def test_legacy_support_changed_before_removal_keeps_both_exact_rounds(history, monkeypatch):
    target = history.root / ".history/.tmp-pending.md"
    target.parent.mkdir()
    target.write_bytes(b"# Initial pending\n")
    original = history._store_legacy_support
    with monkeypatch.context() as patch:
        def edit_after_store(entries):
            original(entries)
            target.write_bytes(b"# Changed pending\r\n")
        patch.setattr(history, "_store_legacy_support", edit_after_store)
        with pytest.raises(HistoryError, match="changed before removal"):
            history.import_legacy()
    old = history._rev(LEGACY_SUPPORT_REF)
    assert saved_support(history) == {".tmp-pending.md": b"# Initial pending\n"}
    assert target.read_bytes() == b"# Changed pending\r\n"
    history.import_legacy()
    assert saved_support(history) == {".tmp-pending.md": b"# Changed pending\r\n"}
    assert history.git.text("rev-parse", LEGACY_SUPPORT_REF + "^") == old
    history.git.run("gc", "--prune=now")
    old_tree = history._tree(old)
    old_manifest = json.loads(history._blob(old_tree[LEGACY_SUPPORT_MANIFEST]))
    assert history._blob(old_tree[old_manifest["files"][0]["stored"]]) == b"# Initial pending\n"


@pytest.mark.parametrize("invalid", ["unknown", "checksum", pytest.param("link",
    marks=pytest.mark.skipif(os.name == "nt", reason="Windows symlinks require privileges"))])
def test_known_legacy_leftovers_do_not_hide_invalid_archive_entries(history, tmp_path, invalid):
    _, archive = legacy(history.root, "common/mine.md", b"# Archive\n")
    target = history.root / ".history/.tmp-pending.md"
    target.write_bytes(b"# Pending\n")
    if invalid == "unknown":
        (history.root / ".history/notes.txt").write_bytes(b"# Unrecognized\n")
    elif invalid == "checksum":
        archive.write_bytes(b"# Different\n")
    else:
        external = tmp_path / "external"
        external.write_bytes(b"# External\n")
        target.unlink()
        target.symlink_to(external)
    with pytest.raises(HistoryError):
        history.import_legacy()
    assert archive.exists()
    assert target.exists()
    assert history._rev(LEGACY_SUPPORT_REF) is None
    if invalid == "link":
        assert external.read_bytes() == b"# External\n"


@pytest.mark.parametrize("stale", [False, True])
def test_private_legacy_support_lock_recovery_preserves_active_writer(history, stale):
    target = history.root / ".history/.tmp-pending.md"
    target.parent.mkdir()
    target.write_bytes(b"# Pending\n")
    lock = history.root / ".git/refs/agents-core/legacy-support.lock"
    lock.write_bytes(b"crashed writer" if stale else b"active writer")
    if stale:
        old = time.time() - 60
        os.utime(lock, (old, old))
        history.ensure()
        assert not lock.exists()
        assert saved_support(history) == {".tmp-pending.md": b"# Pending\n"}
        assert not target.exists()
    else:
        with pytest.raises(HistoryError, match="flow_history"):
            history.ensure()
        assert lock.read_bytes() == b"active writer"
        assert target.read_bytes() == b"# Pending\n"
        assert history._rev(LEGACY_SUPPORT_REF) is None


def test_retained_ref_inventory_exceeds_windows_command_limit_without_breaking_reads(history, monkeypatch):
    path = "common/mine.md"
    history.apply({path: b"# Before\r\n"}, flow_path=path)
    version = history.apply({path: b"# Current\r\n"}, flow_path=path)
    expected = history.events()
    tip = history._rev(HISTORY_REF)
    refs = [RECEIVED_PREFIX + f"{number:040x}" for number in range(600)]
    assert sum(len(ref) + 1 for ref in refs) > 32767
    history.git.run("update-ref", "--stdin", input="".join(f"create {ref} {tip}\n" for ref in refs).encode())
    original = gitcmd.subprocess.run

    def windows_command_limit(arguments, **options):
        if len(subprocess.list2cmdline([os.fspath(arg) for arg in arguments])) > 32767:
            raise OSError("simulated Windows CreateProcess command-line limit")
        return original(arguments, **options)

    monkeypatch.setattr(gitcmd.subprocess, "run", windows_command_limit)
    reader = LibraryHistory(history.root)
    assert reader.events() == expected
    assert reader.read(path, version) == b"# Before\r\n"
    assert reader.events([HISTORY_REF, "^" + tip]) == []
    assert reader.events([]) == []
    assert len(reader.git.text("for-each-ref", "--format=%(refname)", RECEIVED_PREFIX).splitlines()) == 600



def expectations(changes, history):
    return {path: scope.blob_id(raw) if (raw := history._disk(path)) is not None else None for path in changes}


def test_replace_archives_exact_current_flows_and_exports_only_accepted_generation(history):
    replaced, deleted, kept, created = "common/replaced.md", "common/deleted.md", "common/kept.md", "common/created.md"
    history.apply({replaced: b"# Original\n"}, flow_path=replaced)
    old = history.apply({replaced: b"# Discarded current\r\n"}, flow_path=replaced)
    history.apply({deleted: b"# Deleted current\n"}, flow_path=deleted)
    history.apply({kept: b"# Retained exclusion\n"}, flow_path=kept)
    remote = source_event()
    remote_ref = portable(history, remote, "remote")
    unrelated = {**source_event(), "id": "git-" + "b" * 40}
    portable(history, unrelated, "unrelated")
    tip = history._rev(remote_ref)
    changes = {replaced: b"# Selected remote\n", deleted: None, created: b"# Created remote\n"}
    head = history.git.text("symbolic-ref", "HEAD")
    boundary = history.adopt(changes, expected_before=expectations(changes, history),
        accepted_tips={"remote": tip}, remote_head=tip, remote_identity="remote:main", decision_hash="confirmed")
    oid = boundary.removeprefix("git-")
    assert history.generation() == oid
    assert history.adoption()["remote_identity"] == "remote:main"
    assert history.adoption()["decision_hash"] == "confirmed"
    assert history.adoption()["archive_paths"] == [deleted, replaced]
    assert not history.adoption()["pending"]
    assert history.read(replaced, boundary) == b"# Discarded current\r\n"
    assert history.read(deleted, boundary) == b"# Deleted current\n"
    assert history.read(replaced, old) == b"# Original\n"
    assert history.history(deleted)[0]["deleted"]
    assert history.history(kept) == []
    assert history.history(created) == []
    assert (history.root / kept).read_bytes() == b"# Retained exclusion\n"
    assert {event["id"] for event in history.export_events()} == {remote["id"]}
    assert history.read(unrelated["path"], unrelated["id"]) == unrelated["previous"]
    assert history.accepted_refs() == [ACCEPTED_PREFIX + oid + "/tips/remote"]
    assert history._rev(ACCEPTED_PREFIX + oid + "/remote-head") == tip
    assert history.git.text("symbolic-ref", "HEAD") == head
    assert not (history.root / ".git/index").exists()
    post = history.apply({replaced: b"# Later edit\n"}, flow_path=replaced)
    assert {event["id"] for event in history.export_events()} == {remote["id"], post}
    assert history.read(replaced, post) == b"# Selected remote\n"


def test_zero_write_replace_still_creates_boundary_without_archiving_unchanged_flow(history):
    path = "common/mine.md"
    history.apply({path: b"# Unchanged\n"}, flow_path=path)
    before = history._rev(HISTORY_REF)
    boundary = history.adopt({path: b"# Unchanged\n"}, expected_before=expectations([path], history), accepted_tips={})
    assert history._rev(HISTORY_REF) != before
    assert history.generation() == boundary.removeprefix("git-")
    assert history.adoption()["changes"] == {}
    assert history.adoption()["archive_paths"] == []
    assert history.history(path) == []
    assert history.export_events() == []
    assert (history.root / path).read_bytes() == b"# Unchanged\n"


def test_replace_refuses_stale_expectation_and_merge_decision_before_content_changes(history):
    path = "common/mine.md"
    history.apply({path: b"# Current\n"}, flow_path=path)
    before = history._rev(HISTORY_REF)
    with pytest.raises(HistoryError, match="changed externally"):
        history.adopt({path: b"# Remote\n"}, expected_before={path: scope.blob_id(b"# Earlier\n")}, accepted_tips={})
    with pytest.raises(HistoryError, match="invalid adoption decision"):
        history.adopt({path: b"# Remote\n"}, expected_before=expectations([path], history), accepted_tips={}, decision="merge")
    assert history._rev(HISTORY_REF) == before
    assert history._rev(PENDING_REF) is None
    assert history.generation() is None
    assert (history.root / path).read_bytes() == b"# Current\n"


def test_interrupted_replace_retains_confirmed_manifest_and_recovers_after_gc(history, monkeypatch):
    path, overlay = "common/mine.md", "personas/common/mine.json"
    history.apply({path: b"# Original\n", overlay: b"old\n"}, flow_path=path)
    (history.root / path).write_bytes(b"# Manual current\r\n")
    event = source_event()
    ref = portable(history, event, "remote")
    tip, head = history._rev(ref), history._rev(HISTORY_REF)
    original = user_library.atomic_write
    with monkeypatch.context() as patch:
        def fail_overlay(target, raw):
            if Path(target) == history.root / overlay:
                raise OSError("interrupted Replace")
            original(target, raw)
        patch.setattr(user_library, "atomic_write", fail_overlay)
        with pytest.raises(OSError, match="interrupted Replace"):
            history.adopt({path: b"# Remote\n", overlay: b"new\n"},
                expected_before=expectations([path, overlay], history), accepted_tips={"remote": tip},
                remote_head=tip, remote_identity="remote:branch", decision_hash="exact-plan",
                retained_paths={"common/held.md": "unsafe_remote", "common/linked": "not_regular"})
    intent = history.adoption()
    assert intent["pending"]
    assert intent["base_head"] == head
    assert intent["changes"] == {path: b"# Remote\n", overlay: b"new\n"}
    assert intent["remote_head"] == tip and intent["decision_hash"] == "exact-plan"
    assert intent["retained_paths"] == {"common/held.md": "unsafe_remote", "common/linked": "not_regular"}
    assert history._rev(HISTORY_REF) == head
    assert history.generation() is None
    with pytest.raises(HistoryError, match="pending adoption"):
        history.abandon_pending()
    history.git.run("update-ref", "-d", ref)
    history.git.run("gc", "--prune=now")
    reopened = LibraryHistory(history.root)
    reopened.ensure()
    assert not reopened.adoption()["pending"]
    assert reopened.adoption()["retained_paths"] == intent["retained_paths"]
    assert reopened.generation() == intent["boundary"]
    assert reopened._rev(PENDING_REF) is None
    assert (history.root / overlay).read_bytes() == b"new\n"
    assert reopened.read(path, intent["id"]) == b"# Manual current\r\n"
    assert [reopened.read(path, entry["version"]) for entry in reopened.history(path)] == [
        b"# Manual current\r\n", b"# Original\n"]
    assert {item["id"] for item in reopened.export_events()} == {event["id"]}


def test_replace_refuses_external_edit_after_expectation_check_without_overwriting_it(history, monkeypatch):
    path = "common/mine.md"
    history.apply({path: b"# Current\n"}, flow_path=path)
    head = history._rev(HISTORY_REF)
    original = history._commit
    def edit_during_prepare(changes, metadata, **options):
        if metadata["action"] == "adopt":
            (history.root / path).write_bytes(b"# Concurrent manual edit\n")
        return original(changes, metadata, **options)
    monkeypatch.setattr(history, "_commit", edit_during_prepare)
    with pytest.raises(HistoryError, match="changed externally"):
        history.adopt({path: b"# Remote\n"}, expected_before=expectations([path], history), accepted_tips={})
    assert (history.root / path).read_bytes() == b"# Concurrent manual edit\n"
    assert history._rev(HISTORY_REF) == head
    assert history.generation() is None
    assert history.adoption()["pending"]
    assert history.adoption()["changes"] == {path: b"# Remote\n"}


def test_prepared_observed_commits_do_not_publish_before_adoption_intent_is_pinned(history, monkeypatch):
    path = "common/mine.md"
    history.apply({path: b"# Original\n"}, flow_path=path)
    (history.root / path).write_bytes(b"# Manual current\n")
    head = history._rev(HISTORY_REF)
    original = history.git.run
    def fail_pending(*arguments, **options):
        if arguments[:2] == ("update-ref", PENDING_REF):
            raise OSError("interrupted pin")
        return original(*arguments, **options)
    monkeypatch.setattr(history.git, "run", fail_pending)
    with pytest.raises(OSError, match="interrupted pin"):
        history.adopt({path: b"# Remote\n"}, expected_before=expectations([path], history), accepted_tips={})
    assert history._rev(HISTORY_REF) == head
    assert history._rev(PENDING_REF) is None
    assert history.generation() is None
    assert (history.root / path).read_bytes() == b"# Manual current\n"


def test_replace_refuses_conflicting_legacy_alias_without_changing_files_or_refs(history):
    path = "common/shared.md"
    raw = b"# Private version\n"
    alias = "20261001T000000000001Z-" + hashlib.sha256(raw).hexdigest()[:12]
    history.apply({path: b"# Current\n"}, flow_path=path)
    history.record(path, raw, version=alias)
    event = {**source_event(content=b"# Remote incompatible alias\n"), "id": alias, "version": alias,
             "archive": True, "previous": None}
    event["changes"] = {path: event["content"]}
    ref = portable(history, event, "collision")
    head = history._rev(HISTORY_REF)
    with pytest.raises(HistoryError, match="conflicting copies"):
        history.adopt({path: b"# Remote\n"}, expected_before=expectations([path], history),
                      accepted_tips={"collision": history._rev(ref)})
    assert history._rev(HISTORY_REF) == head
    assert history._rev(PENDING_REF) is None
    assert history.generation() is None
    assert (history.root / path).read_bytes() == b"# Current\n"
    private = next(item for item in history.events([HISTORY_REF]) if item["id"] == alias)
    selected = history.events([ref])[0]
    assert private["content"] == raw
    assert selected["content"] == b"# Remote incompatible alias\n"
    assert history.git.text("for-each-ref", "--format=%(refname)", ACCEPTED_PREFIX) == ""


def test_replace_accepts_compatible_filtered_copies_and_keeps_native_metadata_semantics(history):
    path = "common/shared.md"
    secret = b"ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n"
    history.apply({path: secret}, flow_path=path)
    version = history.apply({path: b"# Clean\n"}, flow_path=path)
    source = next(item for item in history.export_events() if item["id"] == version)
    restricted = portable(history, source, "restricted")
    allowed = portable(history, source, "allowed", allow=(hashlib.sha256(secret).hexdigest(),))
    meta = "repos/sample/.repo.json"
    original = b'{"origin":"github.com/owner/sample","path":"/local/clone"}\n'
    history.apply({meta: original}, action="baseline")
    baseline = history.export_events()[-1]
    metadata_ref = portable(history, baseline, "metadata", origins={"repos/sample": "github.com/owner/sample"})
    tips = {name: history._rev(ref) for name, ref in [("restricted", restricted), ("allowed", allowed),
                                                   ("metadata", metadata_ref)]}
    history.adopt({path: b"# Remote\n"}, expected_before=expectations([path], history), accepted_tips=tips)
    assert history.read(path, version) == secret
    exported = {item["id"]: item for item in history.export_events()}
    assert exported[version]["previous"] == secret
    assert json.loads(exported[baseline["id"]]["changes"][meta]) == {"origin": "github.com/owner/sample"}
    assert (history.root / meta).read_bytes() == original


def test_accepted_history_updates_survive_policy_rewrite_and_next_replace_stays_isolated(history):
    first = source_event()
    ref = portable(history, first, "stream")
    tip = history._rev(ref)
    first_boundary = history.adopt({}, expected_before={}, accepted_tips={"stream": tip})
    second = {**source_event(previous=b"# New\n", content=b"# Next\n"), "id": "git-" + "b" * 40}
    portable(history, second, "stream")
    next_tip = history._rev(ref)
    assert history.accept({"stream": next_tip}) == 1
    assert history.accept({"stream": next_tip}) == 0
    with pytest.raises(HistoryError, match="not the current incoming tip"):
        history.accept({"stream": tip})
    history.git.run("update-ref", "-d", ref)
    history.git.run("gc", "--prune=now")
    assert {item["id"] for item in history.export_events()} == {first["id"], second["id"]}
    assert history._rev(ACCEPTED_PREFIX + first_boundary.removeprefix("git-") + "/retained/" + tip) == tip
    third = {**source_event(), "id": "git-" + "c" * 40}
    third_ref = portable(history, third, "different-remote")
    history.adopt({}, expected_before={}, accepted_tips={"different-remote": history._rev(third_ref)})
    assert {item["id"] for item in history.export_events()} == {third["id"]}
    assert history.read(first["path"], first["id"]) == first["previous"]
    assert history.read(second["path"], second["id"]) == second["previous"]


def test_repeated_private_legacy_record_after_boundary_cannot_revive_discarded_export(history):
    path, raw = "common/mine.md", b"# Old private archive\n"
    alias = "20261001T000000000001Z-" + hashlib.sha256(raw).hexdigest()[:12]
    history.record(path, raw, version=alias)
    history.adopt({}, expected_before={}, accepted_tips={})
    head = history._rev(HISTORY_REF)
    assert history.record(path, raw, version=alias) == alias
    assert history._rev(HISTORY_REF) == head
    assert history.read(path, alias) == raw
    assert history.export_events() == []


def test_adoption_read_apis_do_not_create_a_missing_library(tmp_path):
    root = tmp_path / "not-created"
    history = LibraryHistory(root)
    assert history.adoption() is None
    assert history.generation() is None
    assert history.accepted_refs() == []
    assert not root.exists()


def test_replace_retained_paths_allow_unsafe_directories_without_following_them(history, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "private.md"
    external.write_bytes(b"# Outside\n")
    (history.root / "common").mkdir(exist_ok=True)
    (history.root / "common/linked").symlink_to(outside, target_is_directory=True)
    boundary = history.adopt({}, expected_before={}, accepted_tips={},
                             retained_paths={"common/linked": "not_regular"})
    assert history.adoption()["retained_paths"] == {"common/linked": "not_regular"}
    assert history.generation() == boundary.removeprefix("git-")
    assert (history.root / "common/linked").is_symlink()
    assert external.read_bytes() == b"# Outside\n"


@pytest.mark.parametrize("retained", [{"../outside": "size"}, {"common/held.md": "unknown"},
                                      {"common/held.md": []}, ["common/held.md"],
                                      {"common/mine.md": "unsafe_remote"}, {"common": "unreadable"}])
def test_replace_refuses_invalid_or_changed_retained_paths_before_intent(history, retained):
    path = "common/mine.md"
    history.apply({path: b"# Current\n"}, flow_path=path)
    head = history._rev(HISTORY_REF)
    with pytest.raises(HistoryError):
        history.adopt({path: b"# Remote\n"}, expected_before=expectations([path], history),
                      accepted_tips={}, retained_paths=retained)
    assert history._rev(HISTORY_REF) == head
    assert history._rev(PENDING_REF) is None
    assert (history.root / path).read_bytes() == b"# Current\n"


@pytest.mark.parametrize("kept_before", [b"# Kept\n", None])
def test_replace_guards_unchanged_or_absent_paths_after_intent_preparation(history, monkeypatch, kept_before):
    path, kept = "common/mine.md", "common/kept.md"
    history.apply({path: b"# Current\n", kept: kept_before}, flow_path=path)
    expected = expectations([path, kept], history)
    head, original = history._rev(HISTORY_REF), history._commit
    def edit_kept_during_prepare(changes, metadata, **options):
        commit = original(changes, metadata, **options)
        if metadata["action"] == "adopt":
            (history.root / kept).write_bytes(b"# Concurrent kept edit\n")
        return commit
    monkeypatch.setattr(history, "_commit", edit_kept_during_prepare)
    with pytest.raises(HistoryError, match="unchanged adoption target changed externally"):
        history.adopt({path: b"# Remote\n"}, expected_before=expected, accepted_tips={})
    assert history.adoption()["unchanged_before"] == {kept: expected[kept]}
    assert history.adoption()["pending"]
    assert history._rev(HISTORY_REF) == head
    assert history.generation() is None
    assert (history.root / path).read_bytes() == b"# Current\n"
    assert (history.root / kept).read_bytes() == b"# Concurrent kept edit\n"


def test_interrupted_replace_rechecks_unchanged_guards_before_any_recovery_write(history, monkeypatch):
    path, kept, overlay = "common/mine.md", "common/kept.md", "personas/common/mine.json"
    history.apply({path: b"# Current\n", kept: b"# Kept\n", overlay: b"old\n"}, flow_path=path)
    head = history._rev(HISTORY_REF)
    expected = expectations([path, kept, overlay], history)
    original = user_library.atomic_write
    with monkeypatch.context() as patch:
        def interrupt_overlay(target, raw):
            if Path(target) == history.root / overlay:
                raise OSError("interrupted guarded Replace")
            original(target, raw)
        patch.setattr(user_library, "atomic_write", interrupt_overlay)
        with pytest.raises(OSError, match="interrupted guarded Replace"):
            history.adopt({path: b"# Remote\n", overlay: b"new\n"}, expected_before=expected, accepted_tips={})
    intent = history.adoption()
    assert intent["unchanged_before"] == {kept: expected[kept]}
    assert (history.root / path).read_bytes() == b"# Remote\n"
    (history.root / kept).write_bytes(b"# Manual edit after interruption\n")
    history.git.run("gc", "--prune=now")
    reopened = LibraryHistory(history.root)
    with pytest.raises(HistoryError, match="unchanged adoption target changed externally"):
        reopened.ensure()
    assert reopened.adoption()["boundary"] == intent["boundary"]
    assert reopened.adoption()["pending"]
    assert reopened._rev(HISTORY_REF) == head
    assert reopened.generation() is None
    assert (history.root / overlay).read_bytes() == b"old\n"
    assert (history.root / path).read_bytes() == b"# Remote\n"
    assert (history.root / kept).read_bytes() == b"# Manual edit after interruption\n"


def test_replace_rechecks_unchanged_guards_after_writes_before_publishing_boundary(history, monkeypatch):
    path, kept = "common/mine.md", "common/kept.md"
    history.apply({path: b"# Current\n", kept: b"# Kept\n"}, flow_path=path)
    head = history._rev(HISTORY_REF)
    original = user_library.atomic_write
    with monkeypatch.context() as patch:
        def edit_kept_after_write(target, raw):
            original(target, raw)
            if Path(target) == history.root / path:
                (history.root / kept).write_bytes(b"# Concurrent kept edit\n")
        patch.setattr(user_library, "atomic_write", edit_kept_after_write)
        with pytest.raises(HistoryError, match="unchanged adoption target changed externally"):
            history.adopt({path: b"# Remote\n"}, expected_before=expectations([path, kept], history),
                          accepted_tips={})
    assert history.adoption()["pending"]
    assert history._rev(HISTORY_REF) == head
    assert history.generation() is None
    assert (history.root / path).read_bytes() == b"# Remote\n"
    assert (history.root / kept).read_bytes() == b"# Concurrent kept edit\n"
    (history.root / kept).write_bytes(b"# Kept\n")
    history.ensure()
    assert not history.adoption()["pending"]
    assert history.generation() == history.adoption()["boundary"]
    assert (history.root / kept).read_bytes() == b"# Kept\n"


def test_replace_unchanged_guards_cannot_include_retained_paths(history):
    path, kept = "common/mine.md", "common/kept.md"
    history.apply({path: b"# Current\n", kept: b"# Kept\n"}, flow_path=path)
    head = history._rev(HISTORY_REF)
    with pytest.raises(HistoryError, match="invalid unchanged expectations"):
        history.adopt({path: b"# Remote\n"}, expected_before=expectations([path, kept], history),
                      accepted_tips={}, retained_paths={kept: "unsafe_remote"})
    assert history._rev(HISTORY_REF) == head
    assert history._rev(PENDING_REF) is None
    assert (history.root / path).read_bytes() == b"# Current\n"
    assert (history.root / kept).read_bytes() == b"# Kept\n"


def test_replace_captures_exact_unchanged_disk_bytes_before_boundary_without_reviving_old_export(history):
    kept = "common/kept.md"
    private = b"# Discarded private predecessor\n"
    selected = b"# Same selected current bytes\n"
    history.apply({kept: private}, flow_path=kept)
    (history.root / kept).write_bytes(selected)
    boundary = history.adopt({}, expected_before=expectations([kept], history), accepted_tips={})
    assert history._blob(history._tree(boundary.removeprefix("git-"))[kept]) == selected
    assert [history.read(kept, item["version"]) for item in history.history(kept)] == [private]
    assert history.reconcile() == 0
    assert history.export_events() == []
    assert (history.root / kept).read_bytes() == selected
