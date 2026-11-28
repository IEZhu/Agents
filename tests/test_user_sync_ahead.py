"""Commits made in the library outside sync (#754), and every other relation of the two histories.

Someone may run ``git commit`` (or merge, rebase, amend) in the library. Such commits never leave
the machine: a cycle takes their files as edits of the working tree since a commit the remote held,
uploads what the scanner and the scope allow in a commit of its own, and keeps the commits under
``refs/agents-core/local-commits``. Each relation of the local branch to the remote branch is
pinned here on two libraries and a local bare repository, as in ``test_user_sync``: equal, behind,
ahead, rebased or merged onto the remote, amended, diverged, unrelated, a rewritten remote and a
remote recreated empty. No relation loses a local commit, and none sends what may not leave.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from src.user_sync import wizard
from src.user_sync.scope import MAX_FILE_BYTES
from tests.test_user_sync import (TOKEN, Machine, git_bytes, machine, pair, plain_git, records,  # noqa: F401 (fixtures)
                                  remote, remote_files, remote_objects)

LOCAL_COMMITS = "refs/agents-core/local-commits/"
FIRST = "# Shared\n\nfirst text\n"
EDITED = "# Shared\n\nedited by hand\n"
GROUP = "repos/github.com-corp-work"


def by_hand(m: Machine, message: str, files: dict[str, str | bytes | None], *, executable=()) -> str:
    """What a session does in the library: change files, then ``git add`` and ``git commit``."""
    for path, content in files.items():
        target = m.lib / path
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode() if isinstance(content, str) else content)
    plain_git("add", "-A", "--", *files, cwd=m.lib)
    for path in executable:
        plain_git("update-index", "--chmod=+x", "--", path, cwd=m.lib)
    plain_git("commit", "--quiet", "--no-verify", "-m", message, cwd=m.lib)
    return branch(m)


def index_entry(m: Machine, mode: str, path: bytes, data: bytes, message: str) -> tuple[str, str]:
    """Commit an entry no working tree can hold as it is (a link, a name that is not UTF-8)."""
    blob = plain_git("hash-object", "-w", "--stdin", cwd=m.lib, input=data)
    git_bytes("update-index", "-z", "--index-info", cwd=m.lib,
              input=f"{mode} {blob}\t".encode() + path + b"\0")
    plain_git("commit", "--quiet", "--no-verify", "-m", message, cwd=m.lib)
    return branch(m), blob


def branch(m: Machine) -> str:
    return plain_git("rev-parse", "refs/heads/main", cwd=m.lib)


def remote_branch(m: Machine) -> str:
    return plain_git("--git-dir", str(m.remote), "rev-parse", "main")


def answers(m: Machine, *args: str) -> bool:
    """Whether git succeeds on the remote, isolated as ``git_bytes`` isolates fixtures."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "--git-dir", str(m.remote), *args], env=environment,
                          capture_output=True).returncode == 0


def remote_has(m: Machine, oid: str) -> bool:
    return answers(m, "cat-file", "-e", oid)


def remote_parent(m: Machine) -> str:
    return plain_git("--git-dir", str(m.remote), "rev-parse", "main^")


def kept(m: Machine) -> dict[str, str]:
    """The refs that keep commits made outside sync on this machine."""
    listing = plain_git("for-each-ref", "--format=%(refname) %(objectname)", LOCAL_COMMITS, cwd=m.lib)
    return dict(line.split(" ", 1) for line in listing.splitlines())


def settled(pair) -> tuple[Machine, Machine]:
    """Both machines on the same remote head, which each integrated last."""
    a, b = pair
    for m in (a, b, a):
        assert m.run()["status"] == "synced"
    assert branch(a) == branch(b) == remote_branch(a)
    return a, b


def recreate_empty(remote_path) -> None:
    """The owner deletes the repository and creates it again at the same address."""
    shutil.rmtree(remote_path)
    plain_git("init", "--bare", "--quiet", "--initial-branch=main", str(remote_path))


# --- ahead: commits made outside sync on top of the remote head ------------------------------------


def test_commits_made_outside_sync_go_up_as_a_sync_commit_and_stay_here(pair):
    a, b = settled(pair)
    start = remote_branch(a)
    first = by_hand(a, f"rotate token {TOKEN}", {"common/shared.md": EDITED,
                                                 "common/hooks/guard.sh": "#!/bin/sh\nexit 0\n"},
                    executable=["common/hooks/guard.sh"])
    second = by_hand(a, "notes by hand", {"common/notes.md": "# Notes\n"})
    preview = a.sync.preview()
    assert preview["kind"] == "push"  # not a join: nothing reverts and nothing conflicts
    assert preview["conflicts"] == [] and preview["download"] == [] and preview["delete_local"] == []
    assert {item["path"] for item in preview["upload"]} == {"common/shared.md", "common/hooks/guard.sh",
                                                           "common/notes.md"}
    assert preview["local_commits"] == {"count": 2}
    assert any(line.startswith("Commits made in this library outside sync: 2. They stay on this machine")
               for line in wizard.describe_preview(preview))
    result = a.run()
    assert result["status"] == "synced" and result["pushed"], result
    assert result["conflicts"] == [] and records(a) == []
    assert a.text("common/shared.md") == EDITED  # never back to the remote's version
    assert remote_parent(a) == start and not remote_has(a, first) and not remote_has(a, second)
    assert TOKEN.encode() not in remote_objects(a.remote)  # the commit message stays here too
    log = plain_git("--git-dir", str(a.remote), "log", "-1", "--format=%an <%ae>%n%B", "main")
    assert log.startswith("Owner <owner@example.com>") and "Agents-Sync-Machine: machine-a" in log
    assert kept(a) == {LOCAL_COMMITS + second: second} and branch(a) == remote_branch(a)
    assert b.run()["status"] == "synced"
    assert b.text("common/shared.md") == EDITED and b.text("common/notes.md") == "# Notes\n"
    assert b.text("common/hooks/guard.sh") == "#!/bin/sh\nexit 0\n"
    again = a.run()
    assert again["status"] == "synced" and not again["pushed"] and branch(a) == remote_branch(a)


def test_commits_that_change_nothing_leave_the_remote_alone_and_stay_here(pair):
    a, b = settled(pair)
    start = remote_branch(a)
    by_hand(a, "an edit by hand", {"common/shared.md": EDITED})
    local = by_hand(a, "and its revert", {"common/shared.md": FIRST})
    result = a.run()
    assert result["status"] == "synced" and result["sent"] == []
    assert remote_branch(a) == start == branch(a) and kept(a) == {LOCAL_COMMITS + local: local}
    b.save("user:from-b", "# B\n")
    assert b.run()["pushed"]
    assert a.run()["status"] == "synced" and a.text("common/from-b.md") == "# B\n"  # behind, not diverged


def test_working_tree_changes_and_a_deletion_by_hand_go_up_together(pair):
    a, b = pair
    a.save("user:keep", "# Keep\n")
    a, b = settled((a, b))
    start = remote_branch(a)
    local = by_hand(a, "remove keep by hand", {"common/keep.md": None})
    (a.lib / "common" / "later.md").write_text("# Later\n")  # not committed
    result = a.run()
    assert result["status"] == "synced" and sorted(result["sent"]) == ["common/keep.md", "common/later.md"]
    files = remote_files(a.remote)
    assert "common/keep.md" not in files and files["common/later.md"] == b"# Later\n"
    assert remote_parent(a) == start and kept(a) == {LOCAL_COMMITS + local: local}
    assert b.run()["status"] == "synced"
    assert not (b.lib / "common" / "keep.md").exists() and b.text("common/later.md") == "# Later\n"


def test_a_mass_deletion_made_by_hand_still_waits_for_confirmation(pair):
    a, b = pair
    for index in range(12):
        a.save(f"user:flow-{index}", f"# Flow {index}\n")
    a, b = settled((a, b))
    local = by_hand(a, "remove the flows by hand", {f"common/flow-{index}.md": None for index in range(12)})
    result = a.run()
    assert result["reason"] == "confirmation_needed" and "would be deleted from the remote" in result["message"]
    assert "common/flow-3.md" in remote_files(a.remote) and branch(a) == local
    preview = a.sync.preview()
    assert preview["kind"] == "push" and "common/flow-3.md" in preview["remove_from_remote"]
    assert a.run(confirm=preview["hash"])["status"] == "synced"
    assert "common/flow-3.md" not in remote_files(a.remote) and kept(a) == {LOCAL_COMMITS + local: local}


def _excluded(m):
    m.sync.change_scopes(exclude=[GROUP])
    return {f"{GROUP}/.repo.json": json.dumps({"origin": "github.com/corp/work"}) + "\n",
            f"{GROUP}/deploy.md": "# deploy\n\nMARKER TEXT\n"}


def _pending(m):
    m.sync.configure(ask_new_repositories=True)
    group = "repos/github.com-corp-new"
    return {f"{group}/.repo.json": json.dumps({"origin": "github.com/corp/new"}) + "\n",
            f"{group}/deploy.md": "# deploy\n\nMARKER TEXT\n"}


CASES = {  # written to the working tree and committed by hand
    "excluded": _excluded,
    "pending": _pending,
    "local": lambda m: {"repos/scratch-1a2b3c4d/deploy.md": "MARKER TEXT\n"},
    "never": lambda m: {"notes.md": "MARKER TEXT\n"},
    "size": lambda m: {"common/big.bin": b"MARKER TEXT\n" + b"x" * MAX_FILE_BYTES},
    "invalid mcp record": lambda m: {"mcp/servers/demo.json": '{"MARKER TEXT": true}\n'},
}
INDEX_CASES = {  # committed from the index only: no working tree holds them as they are
    "not_regular": ("120000", b"common/link.md"),
    "legacy history": ("100644", b".history/common/old/notes.md"),
    "not utf-8": ("100644", b"common/caf\xe9.md"),
}
# What the files that stay here report once the files that sync went up: a group waiting for
# approval, the saved version of a file over the size limit, which publication holds back, and a
# record the scanner holds in the working tree.
AFTERWARDS = {"pending": "new_repository", "size": "secret", "invalid mcp record": "secret"}


@pytest.mark.parametrize("case", [*CASES, "secret", *INDEX_CASES])
def test_a_commit_holding_a_file_sync_does_not_upload_never_sends_it(pair, case):
    if case == "not utf-8" and sys.platform == "win32":
        pytest.skip("a name that is not UTF-8 needs a POSIX file system for git's index")
    a, b = settled(pair)
    if case in CASES:
        files = CASES[case](a)
        a, b = settled((a, b))
        local = by_hand(a, f"{case} by hand", {"common/shared.md": EDITED, **files})
    elif case == "secret":  # only an intermediate commit holds it
        by_hand(a, "credentials by hand", {"common/shared.md": EDITED, "common/creds.md": f"token: {TOKEN}\n"})
        local = by_hand(a, "credentials removed by hand", {"common/creds.md": None})
    else:
        mode, raw = INDEX_CASES[case]
        by_hand(a, "an edit by hand", {"common/shared.md": EDITED})
        local, _ = index_entry(a, mode, raw, b"MARKER TEXT\n", f"{case} by hand")
    start = remote_branch(a)
    result = a.run()
    assert result["pushed"] and result.get("reason") == AFTERWARDS.get(case), result
    assert b"MARKER TEXT" not in remote_objects(a.remote) and TOKEN.encode() not in remote_objects(a.remote)
    assert not remote_has(a, local) and remote_parent(a) == start
    assert remote_files(a.remote)["common/shared.md"] == EDITED.encode()  # the files that sync went up
    assert kept(a) == {LOCAL_COMMITS + local: local} and branch(a) == remote_branch(a)
    assert a.text("common/shared.md") == EDITED
    assert b.run()["status"] == "synced" and b.text("common/shared.md") == EDITED


def test_a_commit_lifting_an_exclusion_by_hand_still_waits_for_approval(pair):
    a, b = pair
    a.sync.change_scopes(exclude=[GROUP])
    a, b = settled((a, b))
    scopes = json.loads(a.text(".agents-sync/scopes.json"))
    scopes["exclude"] = []
    by_hand(a, "lift the exclusion by hand", {
        ".agents-sync/scopes.json": json.dumps(scopes) + "\n",
        f"{GROUP}/.repo.json": json.dumps({"origin": "github.com/corp/work"}) + "\n",
        f"{GROUP}/deploy.md": "# deploy\n\nMARKER TEXT\n"})
    result = a.run()
    assert result["reason"] == "new_repository" and GROUP in a.sync.status()["pending_groups"]
    assert b"MARKER TEXT" not in remote_objects(a.remote)


# --- merged, rebased or amended by hand ---------------------------------------------------------------


@pytest.mark.parametrize("how", ["merge", "rebase"])
def test_a_branch_merged_or_rebased_onto_the_remote_by_hand_is_ahead(pair, how):
    a, b = settled(pair)
    by_hand(a, "an edit by hand", {"common/shared.md": EDITED})
    b.save("user:from-b", "# B\n")
    assert b.run()["pushed"]
    pushed = remote_branch(a)
    plain_git("fetch", "--quiet", "origin", cwd=a.lib)
    if how == "merge":
        plain_git("merge", "--quiet", "--no-edit", "origin/main", cwd=a.lib)
    else:
        plain_git("rebase", "--quiet", "origin/main", cwd=a.lib)
    local = branch(a)
    preview = a.sync.preview()
    assert preview["kind"] == "push" and preview["conflicts"] == []
    assert preview["local_commits"] == {"count": 2 if how == "merge" else 1}  # the remote's commit is not one
    result = a.run()
    assert result["status"] == "synced" and result["conflicts"] == [] and records(a) == []
    assert remote_parent(a) == pushed and remote_files(a.remote)["common/shared.md"] == EDITED.encode()
    assert a.text("common/shared.md") == EDITED and a.text("common/from-b.md") == "# B\n"
    assert kept(a) == {LOCAL_COMMITS + local: local}
    assert b.run()["status"] == "synced" and b.text("common/shared.md") == EDITED


def test_an_amend_of_the_last_sync_commit_is_ahead(pair):
    a, b = settled(pair)
    a.save("user:note", "# Note\n")
    assert a.run()["pushed"]
    pushed = remote_branch(a)
    (a.lib / "common" / "extra.md").write_text("# Extra\n")
    plain_git("add", "--", "common/extra.md", cwd=a.lib)
    plain_git("commit", "--quiet", "--no-verify", "--amend", "--no-edit", cwd=a.lib)
    local = branch(a)
    result = a.run()
    assert result["status"] == "synced" and result["sent"] == ["common/extra.md"], result
    assert remote_parent(a) == pushed and kept(a) == {LOCAL_COMMITS + local: local}
    assert b.run()["status"] == "synced" and b.text("common/extra.md") == "# Extra\n"


# --- diverged, rewritten, recreated: what may not leave never does -----------------------------------------


def test_diverged_histories_still_ask_and_merge_on_the_head_integrated_last(pair):
    a, b = settled(pair)
    local = by_hand(a, "an edit by hand", {"common/shared.md": EDITED})
    b.save("user:from-b", "# B\n")
    assert b.run()["pushed"]
    pushed = remote_branch(a)
    result = a.run()
    assert result["status"] == "attention" and result["reason"] == "confirmation_needed"
    assert "1 commit made outside sync" in result["message"] and "the remote has changed since" in result["message"]
    assert branch(a) == local and remote_branch(a) == pushed and a.text("common/shared.md") == EDITED
    preview = a.sync.preview()
    assert preview["kind"] == "merge" and preview["conflicts"] == []  # the base is what both started from
    assert preview["local_commits"] == {"count": 1}
    assert a.run(confirm=preview["hash"])["status"] == "synced"
    assert a.text("common/shared.md") == EDITED and a.text("common/from-b.md") == "# B\n" and records(a) == []
    assert remote_parent(a) == pushed and kept(a) == {LOCAL_COMMITS + local: local}
    assert b.run()["status"] == "synced" and b.text("common/shared.md") == EDITED


HELD_BACK = {  # a file the scanner or the size limit keeps out, committed by hand and still here
    "credential": ("common/creds.md", f"token: {TOKEN}\n".encode(), TOKEN.encode()),
    "big file": ("common/big.bin", b"MARKER TEXT\n" + b"x" * MAX_FILE_BYTES, b"MARKER TEXT"),
}


@pytest.mark.parametrize("held", HELD_BACK)
@pytest.mark.parametrize("relation", ["diverged", "remote reset below"])
def test_a_held_back_file_committed_by_hand_never_reaches_a_confirmed_merge(pair, held, relation):
    a, b = settled(pair)
    path, data, marker = HELD_BACK[held]
    if relation == "remote reset below":
        earlier = remote_branch(a)
        a.save("user:later", "# Later\n")
        assert a.run()["pushed"]
    by_hand(a, f"{held} by hand", {"common/shared.md": EDITED, path: data})
    if relation == "diverged":
        b.save("user:from-b", "# B\n")
        assert b.run()["pushed"]
    else:
        plain_git("--git-dir", str(a.remote), "update-ref", "refs/heads/main", earlier)
    assert a.run()["reason"] == "confirmation_needed"
    preview = a.sync.preview()
    assert path not in [entry["path"] for entry in preview["upload"]]
    confirmed = a.run(confirm=preview["hash"])
    assert confirmed["pushed"], confirmed
    assert marker not in remote_objects(a.remote) and path not in remote_files(a.remote)
    assert (a.lib / path).read_bytes() == data  # it stays here
    b.run()
    assert not (b.lib / path).exists()


def test_a_recreated_empty_remote_takes_only_what_syncs_and_no_hand_commit(pair):
    a, b = pair
    a.sync.change_scopes(exclude=[GROUP])
    a, b = settled((a, b))
    integrated = remote_branch(a)
    local = by_hand(a, f"hand commit {TOKEN}", {
        "common/shared.md": EDITED,
        f"{GROUP}/.repo.json": json.dumps({"origin": "github.com/corp/work"}) + "\n",
        f"{GROUP}/deploy.md": "MARKER-EXCL\n", "repos/scratch-1a2b3c4d/deploy.md": "MARKER-LOCAL\n",
        "common/creds.md": f"token: {TOKEN}\n"})
    recreate_empty(a.remote)
    assert "the remote branch is gone" in a.run()["message"]
    preview = a.sync.preview()
    assert preview["kind"] == "push" and preview["local_commits"] == {"count": 1}
    uploaded = {entry["path"] for entry in preview["upload"]}  # everything that syncs, to an empty remote
    assert "common/shared.md" in uploaded and not {"common/creds.md", f"{GROUP}/deploy.md",
                                                   "repos/scratch-1a2b3c4d/deploy.md"} & uploaded
    confirmed = a.run(confirm=preview["hash"])
    assert confirmed["pushed"] and confirmed["reason"] == "secret", confirmed  # creds.md is held back here
    objects = remote_objects(a.remote)
    assert b"MARKER-EXCL" not in objects and b"MARKER-LOCAL" not in objects and TOKEN.encode() not in objects
    assert remote_parent(a) == integrated and not remote_has(a, local)
    assert remote_files(a.remote)["common/shared.md"] == EDITED.encode()
    assert kept(a) == {LOCAL_COMMITS + local: local}


def test_a_remote_without_the_scopes_file_lifts_no_exclusion(pair):
    a, b = pair
    a.sync.change_scopes(exclude=[GROUP])
    (a.lib / GROUP).mkdir(parents=True)
    (a.lib / GROUP / ".repo.json").write_text(json.dumps({"origin": "github.com/corp/work"}) + "\n")
    (a.lib / GROUP / "deploy.md").write_text("MARKER-EXCL\n")
    a, b = settled((a, b))
    recreate_empty(a.remote)
    a.save("user:after", "# After\n")  # something to upload: an unchanged library uploads nothing (a known gap)
    confirmed = a.run(confirm=a.sync.preview()["hash"])
    assert confirmed["status"] == "synced" and confirmed["reason"] is None, confirmed  # nothing waits for approval
    assert b"MARKER-EXCL" not in remote_objects(a.remote)
    assert GROUP in json.loads(remote_files(a.remote)[".agents-sync/scopes.json"])["exclude"]


def test_a_push_rejected_while_ahead_keeps_the_commits_and_asks(pair, monkeypatch):
    a, b = settled(pair)
    local = by_hand(a, "an edit by hand", {"common/shared.md": EDITED})
    b.save("user:from-b", "# B\n")
    real_push, calls = a.sync._push, []

    def push_after_b(*args, **kwargs):
        if not calls:
            assert b.run()["pushed"]
        calls.append(1)
        return real_push(*args, **kwargs)

    monkeypatch.setattr(a.sync, "_push", push_after_b)
    result = a.run()
    assert result["reason"] == "confirmation_needed" and "made outside sync" in result["message"]
    assert len(calls) == 1 and branch(a) == local and a.text("common/shared.md") == EDITED


# --- every other relation behaves as before ----------------------------------------------------------


def test_equal_histories_push_only_what_changed(pair):
    a, _ = settled(pair)
    preview = a.sync.preview()
    assert preview["kind"] == "none" and preview["local_commits"] is None
    a.save("user:new", "# New\n")
    result = a.run()
    assert result["status"] == "synced" and result["sent"] == ["common/new.md"]
    assert branch(a) == remote_branch(a) and kept(a) == {}


def test_a_branch_behind_the_remote_takes_its_changes(pair):
    a, b = settled(pair)
    b.save("user:from-b", "# B\n")
    assert b.run()["pushed"]
    result = a.run()
    assert result["status"] == "synced" and "common/from-b.md" in result["received"]
    assert a.text("common/from-b.md") == "# B\n" and branch(a) == remote_branch(a) and kept(a) == {}


def test_an_unrelated_remote_still_needs_confirmation_and_keeps_the_local_commits(pair, tmp_path):
    a, _ = settled(pair)
    local = by_hand(a, "an edit by hand", {"common/mine.md": "# Mine\n"})
    clone = tmp_path / "unrelated"
    plain_git("init", "--quiet", "--initial-branch=main", str(clone))
    (clone / ".agents-library.json").write_text('{"format": 1}\n')
    (clone / "common").mkdir()
    (clone / "common" / "replacement.md").write_text("# Replacement\n")
    plain_git("add", ".", cwd=clone)
    plain_git("commit", "--quiet", "-m", "new history", cwd=clone)
    plain_git("push", "--quiet", "--force", str(a.remote), "main:main", cwd=clone)
    result = a.run()
    assert result["reason"] == "confirmation_needed" and "rewritten or is unrelated" in result["message"]
    assert branch(a) == local
    preview = a.sync.preview()
    assert preview["kind"] == "join"
    assert a.run(confirm=preview["hash"])["status"] == "synced"
    assert {"common/replacement.md", "common/mine.md", "common/shared.md"} <= set(remote_files(a.remote))
    assert kept(a) == {LOCAL_COMMITS + local: local} and not remote_has(a, local)


def test_a_rewritten_remote_without_local_commits_keeps_no_ref(pair):
    """Commits the remote held once are not kept: a rewrite may have meant to drop them."""
    a, _ = settled(pair)
    earlier = remote_branch(a)
    a.save("user:leaked", "# Leaked\n")
    assert a.run()["pushed"]
    plain_git("--git-dir", str(a.remote), "update-ref", "refs/heads/main", earlier)
    assert a.run()["reason"] == "confirmation_needed"
    assert a.run(confirm=a.sync.preview()["hash"])["status"] == "synced"
    assert kept(a) == {}
