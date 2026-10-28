"""Complete Git history transport, including privacy of superseded versions."""
import hashlib
import json

import pytest

from src.library_history import HISTORY_REF, HistoryError, LibraryHistory
from src.user_sync.gitcmd import Git, GitError
from src.user_sync import scope, versions
from src.user_sync.versions import INCOMING_REFS, PORTABLE_REFS
from tests.test_user_sync import (TOKEN, _repo_group, machine, pair, plain_git, remote, remote_files,
                                  remote_objects)  # noqa: F401


def test_offline_intermediate_versions_arrive_on_another_machine(machine):
    a, b = machine("a"), machine("b")
    for content in ("# A\n", "# B\n", "# C\n"):
        a.save("user:shared", content)
    a.connect()
    b.connect()
    assert b.archived_texts("common/shared.md") == {b"# A\n", b"# B\n"}
    assert b.text("common/shared.md") == "# C\n"
    for m in (a, b):
        assert not (m.lib / ".history").exists()
        plain_git("gc", "--prune=now", cwd=m.lib)
        assert {b"# A\n", b"# B\n"} <= m.archived_texts("common/shared.md")


def test_history_only_changes_sync_when_current_snapshot_is_identical(pair):
    a, b = pair
    current = a.text("common/shared.md")
    a.save("user:shared", "# Intermediate\n")
    a.save("user:shared", current)
    result = a.run()
    assert result["pushed"] and result["status"] == "synced"
    b.run()
    assert b.text("common/shared.md") == current
    assert b"# Intermediate\n" in b.archived_texts("common/shared.md")
    assert a.sync.preview()["history_versions"] == 0


@pytest.mark.parametrize("snapshot_changes", [True, False])
def test_failed_history_push_keeps_snapshot_activity_and_retries(pair, monkeypatch, snapshot_changes):
    a, b = pair
    before_success = a.sync.status()["last_success"]
    current = a.text("common/shared.md")
    a.save("user:shared", "# Intermediate before interrupted publication\n")
    if snapshot_changes:
        a.save("user:shared", "# Updated snapshot\n")
        _repo_group(a, "github.com-me-tool", "github.com/me/tool")
    else:
        a.save("user:shared", current)
    original_run = Git.run

    def reject_history(git, *args, **kwargs):
        if args and args[0] == "push" and any(PORTABLE_REFS in arg for arg in args):
            raise GitError("push", "network", "connection interrupted")
        return original_run(git, *args, **kwargs)

    monkeypatch.setattr(Git, "run", reject_history)
    result = a.run()
    assert result["status"] == "offline" and result["reason"] == "network"
    assert result["retry_at"] and a.sync.status()["last_success"] == before_success
    assert a.sync.preview()["history_versions"] > 0
    if snapshot_changes:
        assert result["pushed"] and result["history_pending"]
        assert "common/shared.md" in result["sent"]
        assert remote_files(a.remote)["common/shared.md"] == b"# Updated snapshot\n"
        status = a.sync.status()
        assert [group["group"] for group in status["announced_groups"]] == ["repos/github.com-me-tool"]
        assert status["activity"][-1]["new_groups"] == ["repos/github.com-me-tool"]
        assert "user:shared" in status["activity"][-1]["sent"]
        assert "files were sent" in result["message"]
        a.save("user:another", "# Another sent snapshot\n")
        again = a.run(force=True)
        assert again["status"] == "offline" and again["history_pending"]
        assert "user:another" in a.sync.status()["activity"][-1]["sent"]
        assert remote_files(a.remote)["common/another.md"] == b"# Another sent snapshot\n"
    else:
        assert not result["pushed"] and not result["sent"]
        assert remote_files(a.remote)["common/shared.md"] == current.encode()
    monkeypatch.undo()
    assert a.run(force=True)["status"] == "synced"
    b.run()
    assert b"# Intermediate before interrupted publication\n" in b.archived_texts("common/shared.md")
    assert a.sync.preview()["history_versions"] == 0


def test_historical_secret_and_excluded_versions_never_reach_remote(machine):
    a, b = machine("a"), machine("b")
    a.save("user:shared", f"# Secret\n{TOKEN}\n")
    a.save("user:shared", "# Clean\n")
    a.save("user:private", "# Private historical content\n")
    a.save("user:private", "# Private current content\n")
    a.sync.change_scopes(exclude_files=["common/private.md"])
    a.sync.setup(remote=str(a.remote), name="Owner", email="owner@example.com", label="a")
    preview = a.sync.preview()
    assert any(item["pattern"] == "github_token" for item in preview["history_blocked"])
    assert a.sync.start(preview["hash"])["reason"] == "secret"
    b.connect()
    objects = remote_objects(a.remote)
    assert TOKEN.encode() not in objects
    assert b"Private historical content" not in objects
    assert b"Private current content" not in objects
    private = plain_git("rev-parse", HISTORY_REF, cwd=a.lib)
    assert private not in plain_git("--git-dir", str(a.remote), "rev-list", "--all").splitlines()
    assert plain_git("--git-dir", str(a.remote), "for-each-ref", "--format=%(refname)", PORTABLE_REFS)
    assert plain_git("for-each-ref", "--format=%(refname)", INCOMING_REFS, cwd=b.lib)


def test_history_group_exclusion_preserves_local_versions_without_export(machine):
    a, b = machine("a"), machine("b")
    a.save("user:shared", "# Private old version\n")
    a.save("user:shared", "# Public current version\n")
    a.sync.change_scopes(exclude=["history"])
    a.connect()
    b.connect()
    assert b"Private old version" not in remote_objects(a.remote)
    assert a.archived_texts("common/shared.md") == {b"# Private old version\n"}
    assert b.archived_texts("common/shared.md") == set()


def test_historical_edit_invalidates_first_upload_preview(machine):
    a = machine("a")
    a.save("user:shared", "# Current\n")
    a.sync.setup(remote=str(a.remote), name="Owner", email="owner@example.com", label="a")
    preview = a.sync.preview()
    a.save("user:shared", "# Extra version\n")
    a.save("user:shared", "# Current\n")
    assert a.sync.start(preview["hash"])["reason"] == "confirmation_needed"
    assert not plain_git("--git-dir", str(a.remote), "for-each-ref", "--format=%(refname)")


def test_legacy_remote_archive_migrates_to_git_on_both_machines(machine, tmp_path):
    a, b = machine("a"), machine("b")
    old = b"# Legacy version\n"
    version = "20261005T000000000000Z-" + hashlib.sha256(old).hexdigest()[:12]
    clone = tmp_path / "legacy"
    plain_git("init", "--quiet", "--initial-branch=main", str(clone))
    (clone / ".agents-library.json").write_text('{"format": 1}\n')
    (clone / "common").mkdir()
    (clone / "common/shared.md").write_text("# Current version\n")
    archive = clone / ".history/common/shared" / (version + ".md")
    archive.parent.mkdir(parents=True)
    archive.write_bytes(old)
    plain_git("add", ".", cwd=clone)
    plain_git("commit", "--quiet", "-m", "Legacy library", cwd=clone)
    plain_git("push", "--quiet", str(a.remote), "main:main", cwd=clone)
    a.connect()
    b.connect()
    for m in (a, b):
        assert m.version_text(version) == old.decode()
        assert not (m.lib / ".history").exists()
    assert json.loads(plain_git("--git-dir", str(a.remote), "show", "main:.agents-library.json"))["format"] == 2
    assert not any(name.startswith(".history/") for name in
                   plain_git("--git-dir", str(a.remote), "ls-tree", "-r", "--name-only", "main").splitlines())


def test_remote_move_preserves_received_intermediate_versions_locally(machine, tmp_path):
    a, b = machine("a"), machine("b")
    for content in ("# A\n", "# B\n", "# C\n"):
        a.save("user:shared", content)
    a.connect()
    b.connect()
    assert b.archived_texts("common/shared.md") == {b"# A\n", b"# B\n"}
    b.sync.disconnect()
    fresh = tmp_path / "fresh.git"
    plain_git("init", "--bare", "--quiet", "--initial-branch=main", str(fresh))
    b.sync.setup(remote=str(fresh), name="Owner", email="owner@example.com", label="b")
    plain_git("gc", "--prune=now", cwd=b.lib)
    assert {b"# A\n", b"# B\n"} <= b.archived_texts("common/shared.md")
    assert b.sync.start(b.sync.preview()["hash"])["status"] == "synced"


def test_legacy_repository_clone_path_is_scrubbed_from_every_published_version(machine):
    a = machine("a")
    group = a.lib / "repos/github.com-owner-project"
    group.mkdir(parents=True)
    private_path = "/private/user/company/never-publish-this-clone-path"
    (group / ".repo.json").write_text(json.dumps({"origin": "github.com/owner/project", "path": private_path}))
    (group / "deploy.md").write_text("# Deploy\n")
    a.connect()
    assert json.loads((group / ".repo.local.json").read_text())["path"] == private_path
    assert private_path.encode() not in remote_objects(a.remote)


def test_matching_legacy_aliases_for_distinct_flows_both_transfer(machine):
    a, b = machine("a"), machine("b")
    raw = b"# Identical earlier version\n"
    version = "20261005T000000000000Z-" + hashlib.sha256(raw).hexdigest()[:12]
    for flow in ("first", "second"):
        a.save("user:" + flow, "# Current\n")
        LibraryHistory(a.lib).record(f"common/{flow}.md", raw, version=version)
    a.connect()
    b.connect()
    for flow in ("first", "second"):
        assert b.version_text(version, path=f"common/{flow}.md") == raw.decode()


def test_remote_history_prune_keeps_received_versions_after_gc(machine):
    a, b = machine("a"), machine("b")
    for content in ("# A\n", "# B\n", "# C\n"):
        a.save("user:shared", content)
    a.connect()
    b.connect()
    refs = plain_git("--git-dir", str(a.remote), "for-each-ref", "--format=%(refname)", PORTABLE_REFS)
    for ref in refs.splitlines():
        plain_git("--git-dir", str(a.remote), "update-ref", "-d", ref)
    assert b.run()["status"] == "synced"
    plain_git("gc", "--prune=now", cwd=b.lib)
    assert {b"# A\n", b"# B\n"} <= b.archived_texts("common/shared.md")


def test_legacy_archive_file_exclusion_survives_migration_and_neighbouring_archives(machine):
    a, b = machine("a"), machine("b")
    a.save("user:shared", "# Current\n")
    private = b"# Excluded legacy version\n"
    public = b"# Allowed legacy version\n"
    private_id = "20261005T000000000000Z-" + hashlib.sha256(private).hexdigest()[:12]
    public_id = "20261006T000000000000Z-" + hashlib.sha256(public).hexdigest()[:12]
    alias = f".history/common/shared/{private_id}.md"
    archive = a.lib / alias
    archive.parent.mkdir(parents=True)
    archive.write_bytes(private)
    (archive.parent / (public_id + ".md")).write_bytes(public)
    a.sync.change_scopes(exclude_files=[alias])
    a.connect()
    b.connect()
    assert private not in remote_objects(a.remote)
    assert public in remote_objects(a.remote)
    assert a.version_text(private_id) == private.decode()
    assert b.version_text(public_id) == public.decode()
    assert not (a.lib / ".history").exists()
    proposal = a.sync.change_scopes(include_files=[alias])
    assert proposal["status"] == "confirmation_needed" and proposal["history_versions"] == 1


def test_sync_reads_and_scans_each_history_version_once(pair, monkeypatch):
    """#535: status, preview and cycles filter the whole history, but scan only versions no
    earlier pass scanned; the working tree's own files are scanned on every pass."""
    a, b = pair
    versions.clear_cache()
    texts = [f"# Version {number} (#535)\n".encode() for number in range(4)]
    for text in texts[:3]:
        a.save("user:shared", text.decode())
    assert a.run()["status"] == "synced"
    scanned, history_scanned = [], []
    scan, verdict = scope.scan, versions._verdict
    monkeypatch.setattr(scope, "scan", lambda data, path=None: scanned.append(data) or scan(data, path=path))
    monkeypatch.setattr(versions, "_verdict", lambda data, path: history_scanned.append(data) or verdict(data, path))
    assert a.sync.status()["state"] == "synced"
    assert a.sync.preview()["history_versions"] == 0
    assert a.run(force=True)["status"] == "synced"
    assert not set(texts[:2]) & set(scanned)  # earlier versions: no longer in the working tree
    assert not set(texts) & set(history_scanned)
    a.save("user:shared", texts[3].decode())
    scanned.clear()
    assert a.run()["status"] == "synced"
    assert a.sync.status()["state"] == "synced"
    assert not set(texts[:3]) & set(scanned)
    assert history_scanned.count(texts[3]) == 1  # by the first pass that met the new version
    assert b.run()["status"] == "synced"
    assert set(texts[:3]) <= b.archived_texts("common/shared.md")


def test_a_history_version_that_cannot_be_read_stops_the_cycle(pair, monkeypatch):
    """#535: versions are read only when a pass needs them; a failed read still ends the cycle as
    history_unavailable before anything is pushed, and the next cycle reads them again."""
    a, _ = pair
    a.save("user:shared", "# Unreadable for a while (#535)\n")
    versions.clear_cache()

    def unreadable(backend, oids):
        raise HistoryError("flow_history: a history object has invalid data")

    monkeypatch.setattr(LibraryHistory, "read_blobs", unreadable)
    assert a.run()["reason"] == "history_unavailable"
    monkeypatch.undo()
    assert a.run()["status"] == "synced"
