"""An existing remote requires a reviewed connection decision; discarded history stays private."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
import re
import shutil

import pytest

from src import user_library
from src.library_history import HISTORY_REF, LibraryHistory
from src.user_sync.engine import SyncError
from src.user_sync.versions import PORTABLE_REFS
from tests.test_user_sync import (TOKEN, machine, plain_git, remote, remote_files, remote_objects)  # noqa: F401


def setup(m):
    m.sync.setup(remote=str(m.remote), name="Owner", email="owner@example.com", label="machine-" + m.name)
    m.sync.check()


def existing(machine):
    a, b = machine("a"), machine("b")
    a.save("user:shared", "# Remote old\n")
    a.save("user:shared", "# Remote current\n")
    a.save("user:remote-only", "# Remote only\n")
    a.connect()
    b.save("user:shared", "# Discarded old\n")
    b.save("user:shared", "# Discarded current\n")
    b.save("user:local-only", "# Discarded local only\n")
    setup(b)
    return a, b


def refs(m):
    return plain_git("--git-dir", str(m.remote), "show-ref")


def test_populated_connection_requires_choice_even_when_conflict_free(machine):
    a, b = machine("a"), machine("b")
    a.save("user:remote", "# Remote\n")
    a.connect()
    b.save("user:mine", "# Mine\n")
    setup(b)
    before = refs(b)
    neutral = b.sync.preview()
    assert neutral["connection_choice_required"] and neutral["connection_mode"] is None
    assert neutral["allowed_modes"] == ["merge", "replace"] and not neutral["conflicts"]
    assert b.sync.start(neutral["hash"])["reason"] == "connection_choice_required"
    assert b.sync.run(confirm=neutral["hash"])["reason"] == "connection_choice_required"
    assert b.sync.settings().started is None and refs(b) == before
    chosen = b.sync.preview(connection_mode="merge")
    assert chosen["hash"] != neutral["hash"]
    assert b.sync.start(neutral["hash"], connection_mode="merge")["reason"] == "confirmation_needed"
    assert b.sync.start(chosen["hash"], connection_mode="merge")["status"] == "synced"
    assert "common/mine.md" in remote_files(b.remote)


def test_full_comparison_and_exact_text_bind_selected_mode(machine):
    a, b = existing(machine)
    preview = b.sync.preview(connection_mode="replace")
    rows = {r["path"]: r for r in preview["comparison"]["files"]}
    assert rows["common/shared.md"]["state"] == "different"
    assert rows["common/shared.md"]["merge"]["action"] == "conflict"
    assert rows["common/shared.md"]["replace"]["action"] == "replace"
    assert rows["common/local-only.md"]["replace"]["action"] == "delete"
    assert rows["common/remote-only.md"]["state"] == "remote_only"
    detail = b.sync.comparison("common/shared.md", confirm=preview["hash"], connection_mode="replace")
    assert detail["local"]["text"] == "# Discarded current\n"
    assert detail["remote"]["text"] == detail["result"]["text"] == "# Remote current\n"
    with pytest.raises(SyncError, match="preview changed"):
        b.sync.comparison("common/shared.md", confirm=preview["hash"], connection_mode="merge")
    b.save("user:shared", "# New edit invalidates comparison\n")
    with pytest.raises(SyncError, match="preview changed"):
        b.sync.comparison("common/shared.md", confirm=preview["hash"], connection_mode="replace")


def test_remote_change_invalidates_replacement_before_any_local_discard(machine):
    a, b = existing(machine)
    preview = b.sync.preview(connection_mode="replace")
    a.save("user:shared", "# Remote changed since preview\n")
    a.run()
    result = b.sync.start(preview["hash"], connection_mode="replace")
    assert result["reason"] == "confirmation_needed" and b.sync.settings().started is None
    assert b.text("common/shared.md") == "# Discarded current\n"
    assert (b.lib / "common/local-only.md").exists()
    assert LibraryHistory(b.lib).generation() is None


def test_replace_keeps_private_recovery_but_never_publishes_discarded_versions(machine, tmp_path):
    a, b = existing(machine)
    before = refs(b)
    preview = b.sync.preview(connection_mode="replace")
    assert preview["upload"] == [] and preview["history_versions"] == 0
    assert "common/local-only.md" in preview["delete_local"]
    result = b.sync.start(preview["hash"], connection_mode="replace")
    assert result["status"] == "synced" and not result["pushed"]
    assert refs(b) == before
    assert b.sync.preview()["history_versions"] == 0
    assert not b.run()["pushed"] and refs(b) == before
    assert b.text("common/shared.md") == "# Remote current\n"
    assert not (b.lib / "common/local-only.md").exists()
    assert {b"# Discarded old\n", b"# Discarded current\n", b"# Remote old\n"} <= b.archived_texts("common/shared.md")
    assert b"# Discarded local only\n" in b.archived_texts("common/local-only.md")
    plain_git("gc", "--prune=now", cwd=b.lib)
    assert b"# Discarded current\n" in b.archived_texts("common/shared.md")
    b.save("user:shared", "# Post adoption edit\n")
    assert b.run()["status"] == "synced"
    assert a.run()["status"] == "synced"
    assert a.text("common/shared.md") == "# Post adoption edit\n"
    assert b"# Discarded old\n" not in a.archived_texts("common/shared.md")
    assert b"# Discarded current\n" not in a.archived_texts("common/shared.md")
    assert b"# Discarded local only\n" not in a.archived_texts("common/local-only.md")
    assert b"Discarded" not in remote_objects(b.remote)
    assert b"Remote old" in remote_objects(b.remote)
    private = plain_git("rev-parse", HISTORY_REF, cwd=b.lib)
    assert private not in plain_git("--git-dir", str(b.remote), "rev-list", "--all").splitlines()
    # A new repository receives the active generation, never retired private ancestry.
    b.sync.disconnect()
    fresh = tmp_path / "fresh.git"
    plain_git("init", "--bare", "--quiet", "--initial-branch=main", str(fresh))
    b.remote = fresh
    setup(b)
    assert b.sync.start(b.sync.preview()["hash"])["status"] == "synced"
    assert b"Discarded" not in remote_objects(fresh)
    assert b"Remote old" in remote_objects(fresh)


def test_excluded_and_machine_metadata_are_listed_and_retained_until_explicit_include(machine):
    a, b = existing(machine)
    b.save("user:private", "# Retained excluded content\n")
    b.sync.change_scopes(exclude_files=["common/private.md"])
    group = b.lib / "repos/github.com-owner-project"
    group.mkdir(parents=True)
    (group / ".repo.local.json").write_text('{"path":"/private/local/clone"}\n')
    (group / ".repo.json").write_text('{"origin":null}\n')
    (group / "deploy.md").write_text("# Machine local\n")
    preview = b.sync.preview(connection_mode="replace")
    rows = {r["path"]: r for r in preview["comparison"]["files"]}
    assert rows["common/private.md"]["replace"]["action"] == "retain"
    assert rows["repos/github.com-owner-project/.repo.local.json"]["retained_reason"] == "machine_local"
    assert rows["repos/github.com-owner-project/deploy.md"]["replace"]["action"] == "retain"
    detail = b.sync.comparison("repos/github.com-owner-project/.repo.local.json", confirm=preview["hash"],
                               connection_mode="replace")
    assert detail["local"]["text"] is None
    assert b.sync.start(preview["hash"], connection_mode="replace")["status"] == "synced"
    assert b.text("common/private.md") == "# Retained excluded content\n"
    b.run()
    assert b"Retained excluded content" not in remote_objects(b.remote)
    approval = b.sync.change_scopes(include_files=["common/private.md"])
    assert approval["status"] == "confirmation_needed"
    b.sync.change_scopes(include_files=["common/private.md"], confirm=approval["hash"])
    assert b.run()["status"] == "synced"
    assert b"Retained excluded content" in remote_objects(b.remote)
    assert b"Discarded" not in remote_objects(b.remote)


def test_replace_can_discard_scanner_blocked_local_bytes_privately(machine):
    a, b = existing(machine)
    b.save("user:shared", "# Private credential\n" + TOKEN + "\n")
    preview = b.sync.preview(connection_mode="replace")
    row = next(r for r in preview["comparison"]["files"] if r["path"] == "common/shared.md")
    assert row["replace"]["action"] == "replace"
    assert b.sync.start(preview["hash"], connection_mode="replace")["status"] == "synced"
    b.save("user:shared", "# New safe content\n")
    b.run()
    assert TOKEN.encode() not in remote_objects(b.remote)
    assert any(TOKEN.encode() in raw for raw in b.archived_texts("common/shared.md"))


def test_incoming_thin_copy_cannot_hide_newly_approved_predecessor(machine):
    a, b = existing(machine)
    preview = b.sync.preview(connection_mode="replace")
    b.sync.start(preview["hash"], connection_mode="replace")
    secret = "# Post adoption credential\n" + TOKEN + "\n"
    b.save("user:shared", secret)
    b.save("user:shared", "# Clean after credential\n")
    b.run()
    assert TOKEN.encode() not in remote_objects(b.remote)
    history = LibraryHistory(b.lib)
    source = next(e["id"] for e in history.export_events() if e["path"] == "common/shared.md"
                  and e["previous"] == secret.encode())
    b.save("user:shared", secret)
    approval = b.sync.change_scopes(allow_paths=["common/shared.md"])
    b.sync.change_scopes(allow_paths=["common/shared.md"], confirm=approval["hash"])
    b.save("user:shared", "# Clean after credential\n")
    assert b.sync.preview()["history_versions"] > 0
    b.run()
    copies = history.events([ref for ref in plain_git("for-each-ref", "--format=%(refname)",
                                                    "refs/agents-core/incoming/", cwd=b.lib).splitlines()])
    assert any(e["id"] == source and e["previous"] == secret.encode() for e in copies)
    assert TOKEN.encode() in remote_objects(b.remote)
    assert b"Discarded" not in remote_objects(b.remote)


def test_saved_versions_alone_require_choice_and_empty_remote_cannot_replace(machine):
    a, b = machine("a"), machine("b")
    a.save("user:remote", "# Remote\n")
    a.connect()
    b.save("user:deleted", "# Kept old version\n")
    b.flows.delete("user:deleted", expected_revision=b.revision("user:deleted"))
    setup(b)
    assert b.sync.preview()["connection_choice_required"]
    c = machine("c")
    empty = c.remote.with_name("empty.git")
    plain_git("init", "--bare", "--quiet", "--initial-branch=main", str(empty))
    c.remote = empty
    setup(c)
    assert c.sync.preview()["allowed_modes"] == ["merge"]
    with pytest.raises(SyncError, match="empty remote"):
        c.sync.preview(connection_mode="replace")


@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_received_only_history_requires_choice_on_same_remote_reconnect(machine, mode):
    a, b = machine("a"), machine("b")
    a.save("user:deleted", "# Received deleted version\n")
    a.flows.delete("user:deleted", expected_revision=a.revision("user:deleted"))
    a.connect()
    setup(b)
    # Fetches for access checks and repeated previews are comparison data, not
    # an existing local library on this first connection.
    for _ in range(2):
        preview = b.sync.preview()
        assert not preview["connection_choice_required"]
        assert preview["comparison"]["history"]["local_versions"] == 0
        b.sync.check()
    assert b.sync.start(preview["hash"])["status"] == "synced"
    assert b"# Received deleted version\n" in b.archived_texts("common/deleted.md")
    assert not any(e["path"] == "common/deleted.md" for e in LibraryHistory(b.lib).events([HISTORY_REF]))
    b.sync.disconnect()
    a.save("user:new", "# New remote flow\n")
    a.run()
    setup(b)
    before = refs(b)
    neutral = b.sync.preview()
    assert neutral["connection_choice_required"] and neutral["connection_mode"] is None
    assert neutral["comparison"]["history"]["local_versions"] > 0
    assert b.sync.start(neutral["hash"])["reason"] == "connection_choice_required"
    assert b.sync.settings().started is None and refs(b) == before
    chosen = b.sync.preview(connection_mode=mode)
    assert b.sync.start(chosen["hash"], connection_mode=mode)["status"] == "synced"
    assert b.text("common/new.md") == "# New remote flow\n"
    assert b"# Received deleted version\n" in b.archived_texts("common/deleted.md")


def test_cancelled_setup_fetches_remain_comparison_data_on_reconnect(machine):
    a, b = machine("a"), machine("b")
    a.save("user:shared", "# Earlier remote version\n")
    a.save("user:shared", "# Current remote version\n")
    a.connect()
    setup(b)
    assert b.sync.preview()["comparison"]["history"]["remote_versions"] > 0
    b.sync.disconnect()
    setup(b)
    preview = b.sync.preview()
    assert not preview["connection_choice_required"]
    assert preview["comparison"]["history"]["local_versions"] == 0
    assert b.sync.start(preview["hash"])["status"] == "synced"


def test_disconnect_preserves_history_when_initial_sync_finishes_while_waiting_for_lock(machine, monkeypatch):
    a, b = machine("a"), machine("b")
    a.save("user:deleted", "# Received deleted version\n")
    a.flows.delete("user:deleted", expected_revision=a.revision("user:deleted"))
    a.connect()
    setup(b)
    preview = b.sync.preview()
    original_lock = b.sync._sync_lock_for
    completed = False

    @contextmanager
    def start_before_lock(settings, *, create=True):
        nonlocal completed
        if not completed:
            completed = True
            assert b.sync.start(preview["hash"])["status"] == "synced"
        with original_lock(settings, create=create):
            yield

    with monkeypatch.context() as patch:
        patch.setattr(b.sync, "_sync_lock_for", start_before_lock)
        b.sync.disconnect()
    a.save("user:new", "# New remote flow\n")
    a.run()
    setup(b)
    reconnect = b.sync.preview()
    assert reconnect["connection_choice_required"]
    assert reconnect["comparison"]["history"]["local_versions"] > 0


def test_replace_accepts_remote_legacy_aliases_without_republishing_local_legacy_versions(machine, tmp_path):
    a, b = machine("a"), machine("b")
    clone = tmp_path / "legacy"
    plain_git("init", "--quiet", "--initial-branch=main", str(clone))
    (clone / user_library.MARKER).write_text('{"format":1}\n')
    (clone / "common").mkdir()
    (clone / "common/shared.md").write_text("# Remote current\n")
    raw = b"# Accepted remote legacy\n"
    alias = "20261005T000000000000Z-" + hashlib.sha256(raw).hexdigest()[:12]
    target = clone / ".history/common/shared" / (alias + ".md")
    target.parent.mkdir(parents=True)
    target.write_bytes(raw)
    plain_git("add", ".", cwd=clone)
    plain_git("commit", "--quiet", "-m", "Legacy library", cwd=clone)
    plain_git("push", "--quiet", str(a.remote), "main:main", cwd=clone)
    b.save("user:shared", "# Discarded local legacy current\n")
    localraw = b"# Discarded local legacy\n"
    localalias = "20261006T000000000000Z-" + hashlib.sha256(localraw).hexdigest()[:12]
    LibraryHistory(b.lib).record("common/shared.md", localraw, version=localalias)
    setup(b)
    preview = b.sync.preview(connection_mode="replace")
    assert b.sync.start(preview["hash"], connection_mode="replace")["status"] == "synced"
    assert b.version_text(alias) == raw.decode()
    assert b.version_text(localalias) == localraw.decode()
    b.save("user:shared", "# Safe later edit\n")
    b.run()
    assert raw in remote_objects(b.remote)
    assert b"Discarded" not in remote_objects(b.remote)
    plain_git("gc", "--prune=now", cwd=b.lib)
    assert b.version_text(alias) == raw.decode()


def test_accepted_remote_history_extends_and_survives_prune_without_old_local_revival(machine):
    a, b = existing(machine)
    preview = b.sync.preview(connection_mode="replace")
    b.sync.start(preview["hash"], connection_mode="replace")
    a.save("user:shared", "# Later remote\n")
    a.run()
    b.run()
    assert {b"# Remote old\n", b"# Remote current\n"} <= b.archived_texts("common/shared.md")
    for ref in plain_git("--git-dir", str(b.remote), "for-each-ref", "--format=%(refname)", PORTABLE_REFS).splitlines():
        plain_git("--git-dir", str(b.remote), "update-ref", "-d", ref)
    b.run()
    plain_git("gc", "--prune=now", cwd=b.lib)
    b.save("user:shared", "# Edit after upstream pruning\n")
    b.run()
    assert b"Discarded" not in remote_objects(b.remote)
    assert b"Remote old" in remote_objects(b.remote)


def test_interrupted_replace_resumes_exact_accepted_manifest_without_publication(machine, monkeypatch):
    a, b = existing(machine)
    preview = b.sync.preview(connection_mode="replace")
    before = refs(b)
    original = user_library.atomic_write
    written = []

    def interrupted(path, raw):
        if path.name.endswith(".md"):
            written.append(path)
            if len(written) == 2:
                raise OSError("simulated interrupted adoption")
        return original(path, raw)

    monkeypatch.setattr(user_library, "atomic_write", interrupted)
    result = b.sync.start(preview["hash"], connection_mode="replace")
    assert result["reason"] == "library_unreadable" and b.sync.settings().started is None
    assert LibraryHistory(b.lib).adoption()["pending"] and refs(b) == before
    for read in (b.sync.preview, lambda: b.sync.comparison("common/shared.md", confirm=preview["hash"])):
        with pytest.raises(SyncError, match=re.escape("python -m src.user_sync run")) as failure:
            read()
        assert failure.value.reason == "adoption_pending"
    with pytest.raises(SyncError, match="accepted"):
        b.sync.preview(connection_mode="merge")
    with pytest.raises(SyncError, match="accepted"):
        b.sync.disconnect()
    monkeypatch.setattr(user_library, "atomic_write", original)
    a.save("user:shared", "# New remote after accepted manifest\n")
    a.run()
    current_remote = refs(b)
    result = b.sync.run(force=True)
    assert result["status"] == "synced" and not result["pushed"]
    assert b.text("common/shared.md") == "# Remote current\n"
    assert refs(b) == current_remote and b"Discarded" not in remote_objects(b.remote)
    assert not LibraryHistory(b.lib).adoption()["pending"]
    b.run()
    assert b.text("common/shared.md") == "# New remote after accepted manifest\n"


def test_comparison_binary_size_limits_and_traversal_are_safe(machine):
    a, b = existing(machine)
    (b.lib / "common/binary.dat").write_bytes(b"\0binary\xff")
    (b.lib / "common/large.txt").write_bytes(b"x" * 1024)
    preview = b.sync.preview(connection_mode="merge")
    detail = b.sync.comparison("common/binary.dat", confirm=preview["hash"], connection_mode="merge")
    assert detail["local"]["binary"] and detail["local"]["text"] is None
    detail = b.sync.comparison("common/large.txt", confirm=preview["hash"], connection_mode="merge", text_limit=20)
    assert detail["local"]["too_large"] and detail["local"]["text"] is None
    for path in ("../secret", ".git/config", "/etc/passwd"):
        with pytest.raises(SyncError, match="comparison"):
            b.sync.comparison(path, confirm=preview["hash"], connection_mode="merge")


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires privileges")
def test_linked_directory_is_unknown_and_never_discarded_or_read(machine, tmp_path):
    a, b = existing(machine)
    external = tmp_path / "outside"
    external.mkdir()
    (external / "secret.md").write_text("# Never read this linked text\n")
    (b.lib / "common/linked").symlink_to(external, target_is_directory=True)
    preview = b.sync.preview(connection_mode="replace")
    row = next(r for r in preview["comparison"]["files"] if r["path"] == "common/linked")
    assert row["state"] == "unknown" and row["replace"]["action"] == "retain"
    detail = b.sync.comparison("common/linked", confirm=preview["hash"], connection_mode="replace")
    assert detail["local"]["text"] is None
    b.sync.start(preview["hash"], connection_mode="replace")
    assert (external / "secret.md").read_text() == "# Never read this linked text\n"
    assert b"Never read this linked text" not in remote_objects(b.remote)
    (b.lib / "common/linked").unlink()
    (b.lib / "common/linked").mkdir()
    (b.lib / "common/linked/retained.md").write_text("# Retained content now readable\n")
    b.run()
    assert b"Retained content now readable" not in remote_objects(b.remote)
    assert "common/linked" in b.sync.status()["pending_files"]
    b.save("user:linked-sibling", "# Prefix sibling can publish\n")
    b.run()
    assert b"Prefix sibling can publish" in remote_objects(b.remote)
    approval = b.sync.change_scopes(approve_files=["common/linked"])
    assert approval["status"] == "confirmation_needed"
    b.sync.change_scopes(approve_files=["common/linked"], confirm=approval["hash"])
    b.run()
    assert b"Retained content now readable" in remote_objects(b.remote)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires privileges")
def test_unsafe_remote_entry_retains_local_file_until_new_scope_approval(machine, tmp_path):
    a, b = existing(machine)
    clone = tmp_path / "remote-link"
    plain_git("clone", "--quiet", str(a.remote), str(clone))
    (clone / "common/local-only.md").symlink_to("shared.md")
    plain_git("add", ".", cwd=clone)
    plain_git("commit", "--quiet", "-m", "Remote link", cwd=clone)
    plain_git("push", "--quiet", "origin", "main", cwd=clone)
    preview = b.sync.preview(connection_mode="replace")
    row = next(r for r in preview["comparison"]["files"] if r["path"] == "common/local-only.md")
    assert row["replace"]["action"] == "retain" and row["retained_reason"] == "unsafe_remote"
    result = b.sync.start(preview["hash"], connection_mode="replace")
    assert result["reason"] == "new_repository"
    assert b.text("common/local-only.md") == "# Discarded local only\n"
    b.run()
    assert b"Discarded local only" not in remote_objects(b.remote)
    approval = b.sync.change_scopes(approve_files=["common/local-only.md"])
    b.sync.change_scopes(approve_files=["common/local-only.md"], confirm=approval["hash"])
    b.run()
    assert b"Discarded local only" in remote_objects(b.remote)
    assert b"Discarded old" not in remote_objects(b.remote)


def test_started_connections_reject_new_initial_mode(machine):
    a = machine("a")
    a.save("user:shared", "# Shared\n")
    a.connect()
    with pytest.raises(SyncError, match="before sync starts"):
        a.sync.preview(connection_mode="replace")
    with pytest.raises(SyncError, match="before sync starts"):
        a.sync.start("anything", connection_mode="merge")


def test_replace_checks_unchanged_eligible_paths_before_boundary(machine, monkeypatch):
    a, b = existing(machine)
    b.save("user:equal", "# Same kept content\n")
    a.save("user:equal", "# Same kept content\n")
    a.run()
    preview = b.sync.preview(connection_mode="replace")
    original = LibraryHistory.adopt

    def externally_edited(backend, changes, **arguments):
        (backend.root / "common/equal.md").write_bytes(b"# Unconfirmed manual edit\n")
        assert "common/equal.md" in arguments["expected_before"]
        return original(backend, changes, **arguments)

    monkeypatch.setattr(LibraryHistory, "adopt", externally_edited)
    result = b.sync.start(preview["hash"], connection_mode="replace")
    assert result["reason"] == "history_unavailable" and b.sync.settings().started is None
    assert LibraryHistory(b.lib).generation() is None
    assert b.text("common/shared.md") == "# Discarded current\n"
    assert b.text("common/equal.md") == "# Unconfirmed manual edit\n"
    assert b"Unconfirmed manual edit" not in remote_objects(b.remote)


@pytest.mark.parametrize("shape", [pytest.param("remote_link", marks=pytest.mark.skipif(
    os.name == "nt", reason="symlink creation requires privileges")), "remote_file", "local_file"])
@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_connection_retains_file_directory_collisions_and_minimal_parent_hold(machine, tmp_path, shape, mode):
    a, b = existing(machine)
    if shape == "local_file":
        (b.lib / "common/directory").write_text("# Private parent file\n")
    else:
        (b.lib / "common/directory").mkdir()
        (b.lib / "common/directory/child.md").write_text("# Private child file\n")
    clone = tmp_path / "remote-shape"
    plain_git("clone", "--quiet", str(a.remote), str(clone))
    if shape == "remote_link":
        (clone / "common/directory").symlink_to("shared.md")
    elif shape == "remote_file":
        (clone / "common/directory").write_text("# Remote parent file\n")
    else:
        (clone / "common/directory").mkdir()
        (clone / "common/directory/child.md").write_text("# Remote child file\n")
    plain_git("add", ".", cwd=clone)
    plain_git("commit", "--quiet", "-m", "Path shape", cwd=clone)
    plain_git("push", "--quiet", "origin", "main", cwd=clone)
    before_remote = remote_files(b.remote)
    preview = b.sync.preview(connection_mode=mode)
    rows = [r for r in preview["comparison"]["files"] if r["path"].startswith("common/directory")]
    assert rows and all(r[mode]["action"] == "retain" for r in rows)
    assert all(r["retained_reason"] == ("unsafe_remote" if shape == "remote_link" else "not_regular") for r in rows)
    assert not any(p["path"].startswith("common/directory") for p in preview["upload"] + preview["download"])
    assert not any(p.startswith("common/directory") for p in preview["delete_local"])
    assert "common/directory" in preview["pending_files"]
    assert b.sync._state().get("pending_included_files", []) == []
    result = b.sync.start(preview["hash"], connection_mode=mode)
    assert result["reason"] == "new_repository" and b.sync.settings().started
    if mode == "replace":
        assert LibraryHistory(b.lib).adoption()["retained_paths"] == {
            "common/directory": "unsafe_remote" if shape == "remote_link" else "not_regular"}
    else:
        assert LibraryHistory(b.lib).adoption() is None
    assert b.sync._state()["pending_included_files"] == ["common/directory"]
    b.run()
    assert {p: v for p, v in remote_files(b.remote).items() if p.startswith("common/directory")} == {
        p: v for p, v in before_remote.items() if p.startswith("common/directory")}
    assert b"Private parent file" not in remote_objects(b.remote)
    assert b"Private child file" not in remote_objects(b.remote)
    if shape == "local_file":
        assert b.text("common/directory") == "# Private parent file\n"
    else:
        assert b.text("common/directory/child.md") == "# Private child file\n"


def test_non_object_components_require_choice_and_replace_can_recover(machine):
    a, b = machine("a"), machine("b")
    a.save("user:remote", "# Remote\n")
    a.connect()
    user_library.ensure_root_files(b.lib)
    (b.lib / "components.json").write_text('["corrupt"]\n')
    setup(b)
    assert b.sync.preview()["connection_choice_required"]
    preview = b.sync.preview(connection_mode="replace")
    result = b.sync.start(preview["hash"], connection_mode="replace")
    assert result["status"] == "synced"
    expected = remote_files(b.remote).get("components.json")
    assert (b.lib / "components.json").exists() is (expected is not None)
    if expected is not None:
        assert (b.lib / "components.json").read_bytes() == expected


@pytest.mark.parametrize("container,shape", [
    ("common", "file"),
    pytest.param("common", "link", marks=pytest.mark.skipif(os.name == "nt",
                                                           reason="symlink creation requires privileges")),
    ("repos", "file"),
    pytest.param("repos/github.com-owner-project", "link", marks=pytest.mark.skipif(
        os.name == "nt", reason="symlink creation requires privileges"))])
@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_connection_retains_structural_container_ancestors(machine, tmp_path, container, shape, mode):
    a, b = existing(machine)
    if container.startswith("repos"):
        group = b.lib / "repos/github.com-owner-project"
        group.mkdir(parents=True)
        (group / ".repo.json").write_text('{"origin":"https://github.com/owner/project.git"}\n')
        local_path = "repos/github.com-owner-project/deploy.md"
        (b.lib / local_path).write_text("# Private retained deployment\n")
    else:
        local_path = "common/local-only.md"
    before_local = (b.lib / local_path).read_bytes()
    clone = tmp_path / "remote-container"
    plain_git("clone", "--quiet", str(a.remote), str(clone))
    target = clone / container
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if shape == "link":
        target.symlink_to("unavailable-container")
    else:
        target.write_text("# Remote container file\n")
    plain_git("add", "--all", cwd=clone)
    plain_git("commit", "--quiet", "-m", "Container shape", cwd=clone)
    plain_git("push", "--quiet", "origin", "main", cwd=clone)
    preview = b.sync.preview(connection_mode=mode)
    rows = {r["path"]: r for r in preview["comparison"]["files"]}
    assert container in rows and rows[container][mode]["action"] == "retain"
    assert rows[local_path][mode]["action"] == "retain"
    assert not any(p == local_path for p in preview["delete_local"])
    result = b.sync.start(preview["hash"], connection_mode=mode)
    assert result["reason"] == "new_repository" and b.sync.settings().started
    assert b.sync._state()["pending_included_files"] == [container]
    assert (b.lib / local_path).read_bytes() == before_local
    b.run()
    assert (b.lib / local_path).read_bytes() == before_local
    assert b"Private retained deployment" not in remote_objects(b.remote)
    if container == "common" or mode == "replace":
        assert b"Discarded local only" not in remote_objects(b.remote)
