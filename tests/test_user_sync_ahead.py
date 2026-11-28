"""Commits made in the library outside sync (#754), and every other relation of the two histories.

Someone may run ``git commit`` in the library. The local branch is then ahead of the remote head
the last cycle integrated. Each relation of the local branch to the remote branch is pinned here,
on two libraries and a local bare repository as in ``test_user_sync``: equal, behind, ahead,
diverged, unrelated and a rewritten remote. No relation loses a local commit.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from src.user_sync import wizard
from src.user_sync.engine import Syncer
from src.user_sync.scope import MAX_FILE_BYTES
from tests.test_user_sync import (TOKEN, Machine, git_bytes, machine, pair, plain_git, records,  # noqa: F401 (fixtures)
                                  remote, remote_files, remote_objects)

LOCAL_COMMITS = "refs/agents-core/local-commits/"
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


def remote_contains(m: Machine, commit: str) -> bool:
    """The remote branch's history holds ``commit``."""
    return remote_has(m, commit) and answers(m, "merge-base", "--is-ancestor", commit, "main")


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


# --- ahead: commits made outside sync on top of the remote head ------------------------------------


def test_commits_made_outside_sync_go_up_with_their_history(pair):
    a, b = settled(pair)
    first = by_hand(a, "hooks by hand", {"common/shared.md": EDITED, "common/hooks/guard.sh": "#!/bin/sh\nexit 0\n"},
                    executable=["common/hooks/guard.sh"])
    second = by_hand(a, "notes by hand", {"common/notes.md": "# Notes\n"})
    preview = a.sync.preview()
    assert preview["kind"] == "push"  # not a join: nothing reverts and nothing conflicts
    assert preview["conflicts"] == [] and preview["download"] == [] and preview["delete_local"] == []
    assert {item["path"] for item in preview["upload"]} == {"common/shared.md", "common/hooks/guard.sh",
                                                           "common/notes.md"}
    assert preview["local_commits"] == {"count": 2, "pushed": True, "unsynced": []}
    assert ("Commits made in this library outside sync: 2, uploaded with their history."
            in wizard.describe_preview(preview))
    result = a.run()
    assert result["status"] == "synced" and result["pushed"], result
    assert result["conflicts"] == [] and records(a) == []
    assert a.text("common/shared.md") == EDITED  # never back to the remote's version
    assert remote_branch(a) == second and remote_contains(a, first)  # the local head itself went up
    mode = plain_git("--git-dir", str(a.remote), "ls-tree", "main", "common/hooks/guard.sh").split()[0]
    assert mode == "100755" and kept(a) == {}
    assert b.run()["status"] == "synced"
    assert b.text("common/shared.md") == EDITED and b.text("common/notes.md") == "# Notes\n"
    assert b.text("common/hooks/guard.sh") == "#!/bin/sh\nexit 0\n"
    again = a.run()
    assert again["status"] == "synced" and not again["pushed"] and branch(a) == remote_branch(a)


def test_commits_that_change_nothing_go_up_too_so_the_branch_cannot_diverge_later(pair):
    a, b = settled(pair)
    by_hand(a, "an edit by hand", {"common/shared.md": EDITED})
    local = by_hand(a, "and its revert", {"common/shared.md": "# Shared\n\nfirst text\n"})
    result = a.run()
    assert result["status"] == "synced" and result["sent"] == [] and remote_branch(a) == local
    b.save("user:from-b", "# B\n")
    assert b.run()["pushed"]
    assert a.run()["status"] == "synced" and a.text("common/from-b.md") == "# B\n"  # behind, not diverged


def test_working_tree_changes_go_up_in_a_commit_on_top_of_them(pair):
    a, b = pair
    a.save("user:keep", "# Keep\n")
    a, b = settled((a, b))
    local = by_hand(a, "remove keep by hand", {"common/keep.md": None})
    (a.lib / "common" / "later.md").write_text("# Later\n")  # not committed
    result = a.run()
    assert result["status"] == "synced" and sorted(result["sent"]) == ["common/keep.md", "common/later.md"]
    files = remote_files(a.remote)
    assert "common/keep.md" not in files and files["common/later.md"] == b"# Later\n"
    assert plain_git("--git-dir", str(a.remote), "rev-parse", "main^") == local
    assert "Agents-Sync-Machine: machine-a" in plain_git("--git-dir", str(a.remote), "log", "-1", "--format=%B", "main")
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
    assert "common/flow-3.md" not in remote_files(a.remote) and remote_contains(a, local)


def _excluded(m):
    m.sync.change_scopes(exclude=[GROUP])
    return {f"{GROUP}/.repo.json": json.dumps({"origin": "github.com/corp/work"}) + "\n",
            f"{GROUP}/deploy.md": "# deploy\n\nMARKER TEXT\n"}, {f"{GROUP}/.repo.json": "excluded",
                                                                 f"{GROUP}/deploy.md": "excluded"}


def _pending(m):
    m.sync.configure(ask_new_repositories=True)
    group = "repos/github.com-corp-new"
    return {f"{group}/.repo.json": json.dumps({"origin": "github.com/corp/new"}) + "\n",
            f"{group}/deploy.md": "# deploy\n\nMARKER TEXT\n"}, {f"{group}/.repo.json": "pending",
                                                                 f"{group}/deploy.md": "pending"}


CASES = {  # written to the working tree and committed
    "excluded": _excluded,
    "pending": _pending,
    "local": lambda m: ({"repos/scratch-1a2b3c4d/deploy.md": "MARKER TEXT\n"},
                        {"repos/scratch-1a2b3c4d/deploy.md": "local"}),
    "never": lambda m: ({"notes.md": "MARKER TEXT\n"}, {"notes.md": "never"}),
    "size": lambda m: ({"common/big.bin": b"MARKER TEXT\n" + b"x" * MAX_FILE_BYTES}, {"common/big.bin": "size"}),
    "invalid mcp record": lambda m: ({"mcp/servers/demo.json": '{"MARKER TEXT": true}\n'},
                                    {"mcp/servers/demo.json": "invalid_mcp_record"}),
}
INDEX_CASES = {  # committed from the index only: no working tree holds them as they are
    "not_regular": ("120000", b"common/link.md", "not_regular"),
    "legacy history": ("100644", b".history/common/old/notes.md", "never"),
    "not utf-8": ("100644", b"common/caf\xe9.md", "never"),
}
# What the files that stay here report once the files that sync went up: a group waiting for
# approval, the saved version of a file over the size limit, which publication holds too, and a
# record the scanner holds in the working tree as well.
AFTERWARDS = {"pending": "new_repository", "size": "secret", "invalid mcp record": "secret"}


@pytest.mark.parametrize("case", [*CASES, "secret", *INDEX_CASES])
def test_a_commit_holding_a_file_sync_does_not_upload_is_never_pushed_without_asking(pair, case):
    if case == "not utf-8" and sys.platform == "win32":
        pytest.skip("a name that is not UTF-8 needs a POSIX file system for git's index")
    a, b = settled(pair)
    marker = None
    if case in CASES:
        files, expected = CASES[case](a)
        a, b = settled((a, b))
        local = by_hand(a, f"{case} by hand", {"common/shared.md": EDITED, **files})
        marker = plain_git("hash-object", "--stdin", cwd=a.lib, input=next(
            (v if isinstance(v, bytes) else v.encode()) for k, v in files.items() if not k.endswith(".repo.json")))
    elif case == "secret":  # only an intermediate commit holds it
        by_hand(a, "credentials by hand", {"common/shared.md": EDITED, "common/creds.md": f"token: {TOKEN}\n"})
        local = by_hand(a, "credentials removed by hand", {"common/creds.md": None})
        expected = {"common/creds.md": "secret"}
    else:
        mode, raw, reason = INDEX_CASES[case]
        by_hand(a, "an edit by hand", {"common/shared.md": EDITED})
        local, marker = index_entry(a, mode, raw, b"MARKER TEXT\n", f"{case} by hand")
        expected = {raw.decode("utf-8", "replace"): reason}
    before = remote_branch(a)
    result = a.run()
    assert result["status"] == "attention" and result["reason"] == "confirmation_needed", result
    assert "made outside sync" in result["message"] and "rewritten" not in result["message"]
    for path, reason in expected.items():
        assert f"{path} ({reason.replace('_', ' ')})" in result["message"]
    assert remote_branch(a) == before and branch(a) == local  # kept for the next push, nothing sent
    assert b"MARKER TEXT" not in remote_objects(a.remote) and TOKEN.encode() not in remote_objects(a.remote)
    assert a.text("common/shared.md") == EDITED
    preview = a.sync.preview()
    assert preview["kind"] == "push" and preview["conflicts"] == [] and preview["download"] == []
    assert preview["delete_local"] == [] and preview["local_commits"]["pushed"] is False
    assert {item["path"]: item["reason"] for item in preview["local_commits"]["unsynced"]} == expected
    described = wizard.describe_preview(preview)
    assert any(line.startswith("Files in them that sync does not upload") for line in described)
    confirmed = a.run(confirm=preview["hash"])
    assert confirmed["pushed"] and confirmed.get("reason") == AFTERWARDS.get(case), confirmed
    assert b"MARKER TEXT" not in remote_objects(a.remote) and TOKEN.encode() not in remote_objects(a.remote)
    if marker:
        assert not remote_has(a, marker)
    assert not remote_contains(a, local) and plain_git("--git-dir", str(a.remote), "rev-parse", "main^") == before
    assert remote_files(a.remote)["common/shared.md"] == EDITED.encode()  # the files that sync went up
    assert kept(a) == {LOCAL_COMMITS + local: local} and branch(a) == remote_branch(a)
    assert a.text("common/shared.md") == EDITED
    assert b.run()["status"] == "synced" and b.text("common/shared.md") == EDITED


def test_the_confirmation_names_the_first_files_that_keep_the_commits_here():
    plan = SimpleNamespace(outside=["c1"], unsynced={f"notes-{number}.md": "never" for number in range(7)})
    message = Syncer._unsynced_message(plan)
    assert message.startswith("the library's branch has 1 commit made outside sync that the remote does not have")
    assert "notes-4.md (never) and 2 more;" in message and "notes-5.md" not in message


def test_a_commit_lifting_an_exclusion_by_hand_waits_for_approval(pair):
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
    assert result["reason"] == "confirmation_needed" and f"{GROUP}/deploy.md (pending)" in result["message"]
    assert b"MARKER TEXT" not in remote_objects(a.remote)


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


def test_diverged_histories_still_need_confirmation_and_keep_the_local_commits(pair):
    a, b = settled(pair)
    local = by_hand(a, "an edit by hand", {"common/shared.md": EDITED})
    b.save("user:from-b", "# B\n")
    assert b.run()["pushed"]
    pushed = remote_branch(a)
    result = a.run()
    assert result["status"] == "attention" and result["reason"] == "confirmation_needed"
    assert "1 commit made outside sync" in result["message"] and "the remote has changed since" in result["message"]
    assert "rewritten" not in result["message"]
    assert branch(a) == local and remote_branch(a) == pushed and a.text("common/shared.md") == EDITED
    preview = a.sync.preview()
    assert preview["kind"] == "join"  # as before: no common base
    assert preview["local_commits"] == {"count": 1, "pushed": False, "unsynced": []}
    assert [(c["path"], c["reason"]) for c in preview["conflicts"]] == [("common/shared.md", "no_common_base")]
    assert a.run(confirm=preview["hash"])["status"] == "synced"
    assert kept(a) == {LOCAL_COMMITS + local: local}  # the commit stays here
    [record] = records(a)
    assert a.version_text(record["local_version"]) == EDITED  # and so does its text
    assert a.text("common/from-b.md") == "# B\n"


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
    assert kept(a) == {LOCAL_COMMITS + local: local}


def test_a_remote_reset_below_a_local_commit_is_a_rewritten_remote(pair):
    """The remote head is an ancestor of the local head here too, but not the one integrated last."""
    a, _ = settled(pair)
    earlier = remote_branch(a)
    a.save("user:leaked", "# Leaked\n")
    assert a.run()["pushed"]
    local = by_hand(a, "an edit by hand", {"common/shared.md": EDITED})
    plain_git("--git-dir", str(a.remote), "update-ref", "refs/heads/main", earlier)
    result = a.run()
    assert result["reason"] == "confirmation_needed" and "rewritten or is unrelated" in result["message"]
    assert remote_branch(a) == earlier and branch(a) == local
    preview = a.sync.preview()
    assert preview["kind"] == "join" and "common/leaked.md" in [entry["path"] for entry in preview["upload"]]
    assert a.run(confirm=preview["hash"])["status"] == "synced"
    assert kept(a) == {LOCAL_COMMITS + local: local}


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
