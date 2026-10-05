"""User library sync (#165): two libraries as two machines, synced through a local bare repository."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import time

import pytest

from src import component_toggles, user_library
from src.file_lock import file_lock
from src.user_flows import FlowLibrary
from src.user_sync import engine as engine_module, gitcmd, keys, merge as merging, scope
from src.user_sync.__main__ import main as cli
from src.user_sync.engine import SyncError, Syncer

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def git_bytes(*args, cwd=None, input=None) -> bytes:
    """git for test fixtures, isolated from the developer's configuration and inherited GIT_* variables."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                       GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.com")
    return subprocess.run(["git", *args], cwd=cwd, env=environment, capture_output=True,
                          input=input, check=True).stdout


def plain_git(*args, cwd=None, input=None) -> str:
    return git_bytes(*args, cwd=cwd, input=input).decode().strip()


def remote_files(remote: Path, branch: str = "main") -> dict[str, bytes]:
    """The files of the remote branch's last commit; empty while the branch does not exist."""
    if subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "--verify", "--quiet", branch],
                      capture_output=True, env={"PATH": os.environ.get("PATH", "")}).returncode != 0:
        return {}
    listing = plain_git("--git-dir", str(remote), "ls-tree", "-r", "--name-only", branch)
    return {path: git_bytes("--git-dir", str(remote), "show", f"{branch}:{path}") for path in listing.splitlines()}


def remote_objects(remote: Path) -> bytes:
    """Every object the remote holds, reachable or not: what was pushed, in any commit."""
    return git_bytes("--git-dir", str(remote), "cat-file", "--batch-all-objects", "--batch")


@contextmanager
def library_env(root: Path):
    """``component_toggles`` finds the library through AGENTS_USER_FLOWS_DIR."""
    previous = os.environ.get("AGENTS_USER_FLOWS_DIR")
    os.environ["AGENTS_USER_FLOWS_DIR"] = str(root)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENTS_USER_FLOWS_DIR", None)
        else:
            os.environ["AGENTS_USER_FLOWS_DIR"] = previous


class Machine:
    def __init__(self, root: Path, name: str, remote: Path, **options):
        self.name, self.remote = name, remote
        self.lib, self.state = root / name / "library", root / name / "state"
        options.setdefault("visibility", lambda remote: "private")
        self.sync = Syncer(self.lib, self.state, allow_file_remote=True, **options)
        self.flows = FlowLibrary(user_dir=self.lib)

    def save(self, name: str, text: str) -> dict:
        current = self.revision(name)
        return self.flows.save(name, text, expected_revision=current)

    def revision(self, name: str) -> str | None:
        try:
            return self.flows.get(name)["flow"]["revision"]
        except Exception:
            return None

    def text(self, path: str) -> str:
        return (self.lib / path).read_text(encoding="utf-8")

    def connect(self, **setup) -> dict:
        self.sync.setup(remote=str(self.remote), name="Owner", email="owner@example.com",
                        label=f"machine-{self.name}", **setup)
        self.sync.check()
        preview = self.sync.preview()
        result = self.sync.start(preview["hash"])
        assert result["status"] in ("synced", "attention"), result
        return result

    def run(self, **options) -> dict:
        return self.sync.run(**options)

    def switch(self, kind: str, component: str, enabled: bool) -> None:
        with library_env(self.lib):
            component_toggles.set_enabled(kind, component, enabled)

    def disabled(self, kind: str) -> frozenset:
        with library_env(self.lib):
            return component_toggles.disabled(kind)


@pytest.fixture
def remote(tmp_path) -> Path:
    path = tmp_path / "remote.git"
    plain_git("init", "--bare", "--quiet", "--initial-branch=main", str(path))
    return path


@pytest.fixture
def machine(tmp_path, remote):
    def make(name: str, **options) -> Machine:
        return Machine(tmp_path, name, remote, **options)
    return make


@pytest.fixture
def pair(machine):
    """Two machines connected to one library; A uploaded first."""
    a, b = machine("a"), machine("b")
    a.save("user:shared", "# Shared\n\nfirst text\n")
    a.connect()
    b.connect()
    assert b.text("common/shared.md") == "# Shared\n\nfirst text\n"
    return a, b


def records(m: Machine) -> list[dict]:
    return m.sync.conflicts()


# --- joining and recovery (S1-S5) -----------------------------------------------------------


def test_first_machine_uploads_to_an_empty_remote(machine, remote):
    a = machine("a")
    a.save("user:hello", "# Hello\n")
    a.sync.setup(remote=str(remote), name="Owner", email="owner@example.com", label="mac-a")
    assert a.sync.status()["state"] == "waiting_for_access"
    assert a.sync.check()["remote_state"] == "empty"
    preview = a.sync.preview()
    assert preview["kind"] == "push"
    assert "common/hello.md" in [entry["path"] for entry in preview["upload"]]
    assert a.sync.run()["reason"] == "confirmation_needed"  # nothing leaves before the preview is confirmed
    assert remote_files(remote) == {}
    result = a.sync.start(preview["hash"])
    assert result["status"] == "synced" and result["pushed"]
    files = remote_files(remote)
    assert files["common/hello.md"] == b"# Hello\n"
    assert json.loads(files[".agents-library.json"])["format"] == user_library.FORMAT
    log = plain_git("--git-dir", str(remote), "log", "-1", "--format=%an <%ae>%n%B", "main")
    assert log.startswith("Owner <owner@example.com>")
    assert "sync(mac-a): " in log and "Agents-Sync-Machine: mac-a" in log
    assert a.sync.status()["state"] == "synced"


@pytest.mark.parametrize("branch", ["main", "master"])
def test_foreign_remote_is_refused(machine, remote, tmp_path, branch):
    clone = tmp_path / "foreign"
    plain_git("init", "--quiet", f"--initial-branch={branch}", str(clone))
    (clone / "README.md").write_text("not a library\n")
    plain_git("add", "README.md", cwd=clone)
    plain_git("commit", "--quiet", "-m", "readme", cwd=clone)
    plain_git("push", "--quiet", str(remote), f"{branch}:{branch}", cwd=clone)
    a = machine("a")
    a.save("user:hello", "# Hello\n")
    a.sync.setup(remote=str(remote), name="Owner", email="owner@example.com", label="mac-a")
    with pytest.raises(SyncError) as refused:
        a.sync.check()
    assert refused.value.reason == "unknown_remote"
    assert a.sync.status()["reason"] == "unknown_remote"


def test_empty_machine_clones_the_library(machine):
    a, b = machine("a"), machine("b")
    a.save("user:hello", "# Hello\n")
    a.connect()
    b.sync.setup(remote=str(a.remote), name="Owner", email="owner@example.com", label="b")
    preview = b.sync.preview()
    assert preview["conflicts"] == []
    assert "common/hello.md" in [entry["path"] for entry in preview["download"]]
    b.sync.start(preview["hash"])
    assert b.text("common/hello.md") == "# Hello\n"
    assert [flow["id"] for flow in b.flows.list("user")["flows"]] == ["user:hello"]
    assert b.sync.status()["state"] == "synced"


def test_join_with_own_flows_keeps_both_versions(machine):
    a, b = machine("a"), machine("b")
    a.save("user:same", "# Same\n")
    a.save("user:differs", "# Differs\n\nA\n")
    a.save("user:only-a", "# A\n")
    a.connect()
    b.save("user:same", "# Same\n")
    b.save("user:differs", "# Differs\n\nB\n")
    b.save("user:only-b", "# B\n")
    b.sync.setup(remote=str(a.remote), name="Owner", email="owner@example.com", label="b")
    assert b.sync.run()["reason"] == "confirmation_needed"
    preview = b.sync.preview()
    assert preview["kind"] == "join"
    assert [(c["path"], c["kind"]) for c in preview["conflicts"]] == [("common/differs.md", "both_changed")]
    b.sync.start(preview["hash"])
    assert b.text("common/differs.md") == "# Differs\n\nA\n"  # the version that reached the remote first
    [record] = records(b)
    assert record["kind"] == "both_changed" and record["kept"] == "remote" and record["flow"] == "user:differs"
    kept = b.flows.get("user:differs", version=Path(record["local_version"]).stem)
    assert kept["content"] == "# Differs\n\nB\n"  # a normal history version, listed by the editor
    a.run()
    for m in (a, b):
        assert {f["id"] for f in m.flows.list("user")["flows"]} == {
            "user:same", "user:differs", "user:only-a", "user:only-b"}
    assert [r["id"] for r in records(a)] == [record["id"]]


def test_start_refuses_a_changed_preview(machine, remote):
    a = machine("a")
    a.save("user:hello", "# Hello\n")
    a.sync.setup(remote=str(remote), name="Owner", email="owner@example.com", label="a")
    preview = a.sync.preview()
    a.save("user:later", "# Added after the preview\n")
    result = a.sync.start(preview["hash"])
    assert result["status"] == "attention" and result["reason"] == "confirmation_needed"
    assert remote_files(remote) == {}


def test_rewritten_remote_needs_confirmation(pair, tmp_path):
    a, b = pair
    clone = tmp_path / "rewrite"
    plain_git("init", "--quiet", "--initial-branch=main", str(clone))
    (clone / ".agents-library.json").write_text('{"format": 1}\n')
    (clone / "common").mkdir()
    (clone / "common" / "replacement.md").write_text("# Replacement\n")
    plain_git("add", ".", cwd=clone)
    plain_git("commit", "--quiet", "-m", "new history", cwd=clone)
    plain_git("push", "--quiet", "--force", str(a.remote), "main:main", cwd=clone)
    result = a.run()
    assert result["status"] == "attention" and result["reason"] == "confirmation_needed"
    preview = a.sync.preview()
    assert preview["kind"] == "join"
    assert a.run(confirm=preview["hash"])["status"] == "synced"
    files = remote_files(a.remote)
    assert "common/replacement.md" in files and "common/shared.md" in files


def test_format_newer_stops_sync(pair, tmp_path):
    a, b = pair
    clone = tmp_path / "newer"
    plain_git("clone", "--quiet", str(a.remote), str(clone))
    (clone / ".agents-library.json").write_text('{"format": 99}\n')
    plain_git("commit", "--quiet", "-am", "newer format", cwd=clone)
    plain_git("push", "--quiet", "origin", "main", cwd=clone)
    result = a.run()
    assert result["status"] == "attention" and result["reason"] == "format_newer"


# --- the conflict table -----------------------------------------------------------------------


def test_flow_changed_on_two_machines_keeps_the_remote_version(pair):
    a, b = pair
    a.save("user:shared", "# Shared\n\nA's edit\n")
    b.save("user:shared", "# Shared\n\nB's edit\n")
    a.run()
    result = b.run()
    assert result["status"] == "synced" and len(result["conflicts"]) == 1
    assert b.text("common/shared.md") == "# Shared\n\nA's edit\n"
    [record] = records(b)
    assert (record["kind"], record["kept"], record["machine"]) == ("both_changed", "remote", "machine-b")
    assert (b.lib / record["local_version"]).read_text() == "# Shared\n\nB's edit\n"
    a.run()
    assert a.text("common/shared.md") == "# Shared\n\nA's edit\n"
    assert (a.lib / record["local_version"]).read_text() == "# Shared\n\nB's edit\n"
    assert [r["id"] for r in records(a)] == [record["id"]]


def test_edit_beats_a_deletion_on_the_other_machine(pair):
    a, b = pair
    a.flows.delete("user:shared", expected_revision=a.revision("user:shared"))
    b.save("user:shared", "# Shared\n\nedited on B\n")
    a.run()
    b.run()
    assert b.text("common/shared.md") == "# Shared\n\nedited on B\n"
    [record] = records(b)
    assert (record["kind"], record["kept"], record["deleted_on"]) == ("deletion_undone", "local", "remote")
    a.run()
    assert a.text("common/shared.md") == "# Shared\n\nedited on B\n"


def test_a_deletion_is_undone_when_the_remote_edited_first(pair):
    a, b = pair
    b.save("user:shared", "# Shared\n\nedited on B\n")
    b.run()
    a.flows.delete("user:shared", expected_revision=a.revision("user:shared"))
    a.run()
    assert a.text("common/shared.md") == "# Shared\n\nedited on B\n"
    [record] = records(a)
    assert (record["kind"], record["kept"], record["deleted_on"]) == ("deletion_undone", "remote", "local")


def test_same_change_and_double_deletion_do_not_conflict(pair):
    a, b = pair
    a.save("user:twice", "# Twice\n")
    a.run()
    b.run()
    for m in (a, b):
        m.save("user:shared", "# Shared\n\nsame edit\n")
        m.flows.delete("user:twice", expected_revision=m.revision("user:twice"))
    a.run()
    b.run()
    a.run()
    assert records(a) == records(b) == []
    for m in (a, b):
        assert m.text("common/shared.md") == "# Shared\n\nsame edit\n"
        assert not (m.lib / "common/twice.md").exists()


def test_history_from_two_machines_merges_without_conflicts(pair):
    a, b = pair
    a.save("user:alpha", "# Alpha\n")
    b.save("user:beta", "# Beta\n")
    for round_ in range(2):
        a.save("user:alpha", f"# Alpha\n\nround {round_}\n")
        b.save("user:beta", f"# Beta\n\nround {round_}\n")
        a.run()
        b.run()
    a.run()
    assert records(a) == records(b) == []
    histories = [sorted(p.relative_to(m.lib).as_posix() for p in (m.lib / ".history").rglob("*.md"))
                 for m in (a, b)]
    assert histories[0] == histories[1] and len(histories[0]) == 4


def set_overlay(m: Machine, agent: str) -> None:
    """A persona overlay as ``FlowLibrary.set_persona`` writes it, without loading the components."""
    m.flows.user_dir.joinpath("personas", "common").mkdir(parents=True, exist_ok=True)
    user_library.atomic_write(m.lib / "personas" / "common" / "shared.json",
                              json.dumps({"persona": {"agent": agent}}, indent=2).encode() + b"\n")


def test_persona_overlay_conflict_keeps_the_local_json_in_the_record(pair):
    a, b = pair
    set_overlay(a, "software_engineer")
    set_overlay(b, "code_reviewer")
    a.run()
    b.run()
    overlay = json.loads(b.text("personas/common/shared.json"))
    assert overlay["persona"]["agent"] == "software_engineer"
    [record] = records(b)
    assert record["path"] == "personas/common/shared.json" and record["kept"] == "remote"
    assert json.loads(record["local_content"])["persona"]["agent"] == "code_reviewer"


def test_whole_flow_returns_when_its_persona_changed_while_deleted(pair):
    a, b = pair
    set_overlay(b, "code_reviewer")
    a.flows.delete("user:shared", expected_revision=a.revision("user:shared"))
    a.run()
    b.run()
    assert b.text("common/shared.md") == "# Shared\n\nfirst text\n"
    assert json.loads(b.text("personas/common/shared.json"))["persona"]["agent"] == "code_reviewer"
    [record] = records(b)
    assert record["kind"] == "deletion_undone"


@pytest.mark.parametrize("first, second, expected", [
    ("alpha", "alpha", {"alpha", "gamma"}),       # both switch the same ID off
    ("alpha", "beta", {"alpha", "beta", "gamma"}),  # different IDs
    ("+gamma", "beta", {"beta"}),                 # one turns a disabled ID back on
    ("+gamma", "+gamma", set()),                  # both turn the same ID back on
])
def test_component_switches_merge_without_conflicts(pair, first, second, expected):
    a, b = pair
    a.switch("rules", "gamma", False)
    a.run()
    b.run()
    for m, change in ((a, first), (b, second)):
        m.switch("rules", change.lstrip("+"), change.startswith("+"))
    a.run()
    b.run()
    a.run()
    assert a.disabled("rules") == b.disabled("rules") == frozenset(expected)
    assert records(a) == records(b) == []


# --- scope ------------------------------------------------------------------------------------


def _repo_group(m: Machine, key: str, origin: str | None, flow: str = "deploy", text: str | None = None) -> None:
    group = m.lib / "repos" / key
    group.mkdir(parents=True, exist_ok=True)
    (group / ".repo.json").write_text(json.dumps({"origin": origin}) + "\n")
    library = FlowLibrary(user_dir=m.lib, repo_key=key)
    try:
        revision = library.get(f"repo:{flow}")["flow"]["revision"]
    except Exception:
        revision = None
    library.save(f"repo:{flow}", text or f"# {flow} for {key}\n", expected_revision=revision)


def test_machine_local_groups_never_sync(pair):
    a, b = pair
    _repo_group(a, "scratch-1a2b3c4d", None)  # its history stays local as well
    (a.lib / ".history" / "repos" / "scratch-1a2b3c4d" / "deploy").mkdir(parents=True)
    (a.lib / ".history/repos/scratch-1a2b3c4d/deploy/20261005T000000000000Z-0123456789ab.md").write_text("old\n")
    a.run()
    assert not [path for path in remote_files(a.remote) if "scratch-1a2b3c4d" in path]
    assert b"scratch-1a2b3c4d" not in remote_objects(a.remote) and b"old\n" not in remote_objects(a.remote)


def test_excluding_a_group_deletes_nothing_elsewhere(pair):
    a, b = pair
    _repo_group(a, "github.com-me-work", "github.com/me/work")
    a.run()
    b.run()
    assert b.text("repos/github.com-me-work/deploy.md").startswith("# deploy")
    assert a.sync.change_scopes(exclude=["repos/github.com-me-work"])["status"] == "saved"
    a.run()
    b.run()
    assert not [p for p in remote_files(a.remote) if p.startswith("repos/github.com-me-work/")]
    for m in (a, b):
        assert (m.lib / "repos/github.com-me-work/deploy.md").exists()
    preview = a.sync.change_scopes(include=["repos/github.com-me-work"])
    assert preview["status"] == "confirmation_needed"
    assert "repos/github.com-me-work/deploy.md" in preview["upload"]


def test_new_repository_group_is_announced(pair):
    a, b = pair
    _repo_group(a, "github.com-me-tool", "github.com/me/tool")
    a.run()
    announced = a.sync.status()["announced_groups"]
    assert [g["group"] for g in announced] == ["repos/github.com-me-tool"]
    assert announced[0]["origin"] == "github.com/me/tool"
    assert a.sync.status()["activity"][-1]["new_groups"] == ["repos/github.com-me-tool"]


def test_asking_before_uploading_a_new_repository(machine):
    a = machine("a")
    a.save("user:hello", "# Hello\n")
    a.connect(ask_new_repositories=True)
    _repo_group(a, "github.com-corp-secret", "github.com/corp/secret")
    result = a.run()
    assert result["reason"] == "new_repository"
    assert a.sync.status()["pending_groups"] == ["repos/github.com-corp-secret"]
    assert not [p for p in remote_files(a.remote) if "corp-secret" in p]
    asked = a.sync.change_scopes(approve=["repos/github.com-corp-secret"])
    a.sync.change_scopes(approve=["repos/github.com-corp-secret"], confirm=asked["hash"])
    assert a.run()["status"] == "synced"
    assert "repos/github.com-corp-secret/deploy.md" in remote_files(a.remote)


def test_secret_scanner_blocks_a_file_until_its_content_is_allowed(pair):
    a, b = pair
    a.save("user:creds", f"# Creds\n\ntoken: {TOKEN}\n")
    result = a.run()
    assert result["status"] == "attention" and result["reason"] == "secret"
    assert a.sync.status()["blocked"][0]["path"] == "common/creds.md"
    assert "common/creds.md" not in remote_files(a.remote) and TOKEN.encode() not in remote_objects(a.remote)
    asked = a.sync.change_scopes(allow_paths=["common/creds.md"])
    assert asked["status"] == "confirmation_needed"
    a.sync.change_scopes(allow_paths=["common/creds.md"], confirm=asked["hash"])
    assert a.run()["status"] == "synced"
    assert TOKEN.encode() in remote_files(a.remote)["common/creds.md"]


@pytest.mark.parametrize("verdict, confirmed, refused", [("public", True, True), ("unknown", False, True),
                                                         ("unknown", True, False)])
def test_remote_privacy_is_checked_before_the_first_upload(machine, remote, verdict, confirmed, refused):
    a = machine("a", visibility=lambda r: verdict)
    a.save("user:hello", "# Hello\n")
    a.sync.setup(remote=str(remote), name="Owner", email="owner@example.com", label="a",
                 confirm_private=confirmed)
    result = a.sync.start(a.sync.preview()["hash"])
    assert (result["reason"] == "public_repo") is refused
    assert bool(remote_files(remote)) is not refused


# --- robustness -------------------------------------------------------------------------------


def test_hostile_global_git_config_changes_nothing(machine, remote, tmp_path, monkeypatch):
    hooks = tmp_path / "hostile-hooks"
    hooks.mkdir()
    for name in ("pre-commit", "commit-msg", "post-commit", "pre-push", "reference-transaction"):
        (hooks / name).write_text("#!/bin/sh\nexit 1\n")
        (hooks / name).chmod(0o755)
    hostile = tmp_path / "hostile.gitconfig"
    hostile.write_text(f"[user]\n\tname = Work Account\n\temail = work@corp.example\n"
                       f"[commit]\n\tgpgSign = true\n[gpg]\n\tprogram = false\n"
                       f"[core]\n\thooksPath = {hooks.as_posix()}\n\tautocrlf = true\n"
                       f"[url \"https://evil.example/\"]\n\tinsteadOf = {remote.as_posix()}\n")
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    shutil.copy(hostile, home / ".gitconfig")
    shutil.copy(hostile, home / ".config" / "git" / "config")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.email")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "injected@corp.example")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere.git"))
    a = machine("a")
    crlf = b"# Windows text\r\n\r\nline\r\n"
    a.lib.mkdir(parents=True)
    a.flows.save("user:crlf", crlf.decode())
    a.connect()
    assert remote_files(remote)["common/crlf.md"] == crlf
    author = plain_git("--git-dir", str(remote), "log", "-1", "--format=%an <%ae>|%cn <%ce>", "main")
    assert author == "Owner <owner@example.com>|Owner <owner@example.com>"


def test_a_symlink_in_the_remote_is_not_created(pair, tmp_path):
    a, b = pair
    clone = tmp_path / "symlink"
    plain_git("clone", "--quiet", str(a.remote), str(clone))
    blob = plain_git("hash-object", "-w", "--stdin", cwd=clone, input=b"/etc/passwd")
    plain_git("update-index", "--add", "--cacheinfo", f"120000,{blob},common/link.md", cwd=clone)
    plain_git("commit", "--quiet", "-m", "symlink", cwd=clone)
    plain_git("push", "--quiet", "origin", "main", cwd=clone)
    assert b.run()["status"] == "synced"
    link = b.lib / "common" / "link.md"
    assert not link.is_symlink() and not link.exists()


def test_a_save_during_the_sync_waits_for_the_library_lock_and_is_kept(pair, monkeypatch):
    a, b = pair
    b.save("user:shared", "# Shared\n\nfrom B\n")
    b.run()
    started, finished = threading.Event(), threading.Event()
    real_apply = a.sync._apply

    def apply_with_a_concurrent_save(*args, **kwargs):
        def writer():
            started.set()
            a.save("user:concurrent", "# Saved while syncing\n")
            finished.set()
        threading.Thread(target=writer).start()
        started.wait(5)
        time.sleep(0.2)
        assert not finished.is_set()  # the writer waits for the library lock
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(a.sync, "_apply", apply_with_a_concurrent_save)
    a.run()
    assert finished.wait(5)
    assert a.text("common/shared.md") == "# Shared\n\nfrom B\n"
    assert a.text("common/concurrent.md") == "# Saved while syncing\n"
    monkeypatch.undo()
    a.run()
    assert "common/concurrent.md" in remote_files(a.remote)


def test_an_edit_that_bypasses_the_lock_is_kept_and_reconciled(pair, monkeypatch):
    a, b = pair
    b.save("user:shared", "# Shared\n\nfrom B\n")
    b.run()
    real_apply = a.sync._apply

    def apply_after_a_manual_edit(*args, **kwargs):
        (a.lib / "common" / "shared.md").write_text("# Shared\n\nmanual edit on A\n")
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(a.sync, "_apply", apply_after_a_manual_edit)
    a.run()
    assert a.text("common/shared.md") == "# Shared\n\nmanual edit on A\n"  # never overwritten
    monkeypatch.undo()
    a.run()
    assert a.text("common/shared.md") == "# Shared\n\nfrom B\n"
    [record] = records(a)
    assert (a.lib / record["local_version"]).read_text() == "# Shared\n\nmanual edit on A\n"


def test_a_push_race_between_two_machines_retries(pair, monkeypatch):
    a, b = pair
    a.save("user:from-a", "# A\n")
    b.save("user:from-b", "# B\n")
    real_push = a.sync._push
    calls = []

    def push_after_b(*args, **kwargs):
        if not calls:
            assert b.run()["pushed"]
        calls.append(1)
        return real_push(*args, **kwargs)

    monkeypatch.setattr(a.sync, "_push", push_after_b)
    result = a.run()
    assert result["status"] == "synced" and result["pushed"] and len(calls) == 2
    assert {"common/from-a.md", "common/from-b.md"} <= set(remote_files(a.remote))


def test_a_second_runner_reports_lock_held(pair):
    a, _ = pair
    with file_lock(a.lib / ".git" / "agents-sync.lock", blocking=False):
        assert a.run()["status"] == "lock_held"
        assert a.sync.status()["state"] == "syncing"


def test_a_stale_index_lock_is_removed(pair):
    a, _ = pair
    lock = a.lib / ".git" / "index.lock"
    lock.write_text("")
    old = time.time() - 60
    os.utime(lock, (old, old))
    a.save("user:after-crash", "# After a crash\n")
    assert a.run()["status"] == "synced"
    assert not lock.exists()
    assert "common/after-crash.md" in remote_files(a.remote)


def _fake_ssh(tmp_path: Path, message: str) -> tuple[str, Path]:
    calls = tmp_path / f"ssh-calls-{abs(hash(message))}"
    script = tmp_path / f"fake-ssh-{abs(hash(message))}.py"
    script.write_text("import sys\n"
                      f"open({str(calls)!r}, 'a').write('x')\n"
                      f"sys.stderr.write({message!r} + '\\n')\n"
                      "sys.exit(255)\n")
    return f"{shlex.quote(Path(sys.executable).as_posix())} {shlex.quote(script.as_posix())}", calls


def _ssh_machine(tmp_path, message: str, clock=None) -> tuple[Syncer, Path]:
    command, calls = _fake_ssh(tmp_path, message)
    offered = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBmFjZWJvb2tob3N0a2V5Zm9ydGVzdHNvbmx5MDA"
    syncer = Syncer(tmp_path / "library", tmp_path / "state", ssh_command=command,
                    scan_host_keys=lambda host, port: [offered], visibility=lambda r: "private",
                    clock=clock)
    syncer.setup(remote="git@git.example.invalid:me/library.git", name="Owner", email="owner@example.com",
                 label="ssh-test", trust_host_key=keys.fingerprint(offered))
    return syncer, calls


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_an_unreachable_remote_goes_offline_and_waits_before_retrying(tmp_path):
    now = [datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)]
    syncer, calls = _ssh_machine(tmp_path, "ssh: connect to host git.example.invalid port 22: "
                                           "Network is unreachable", clock=lambda: now[0])
    settings = syncer.settings()
    settings.started = "2026-10-05T11:00:00+00:00"
    settings.save(syncer.settings_path)
    result = syncer.run()
    assert result["status"] == "offline" and result["retry_at"] == "2026-10-05T12:01:00+00:00"
    attempts = calls.read_text()
    assert syncer.run()["status"] == "offline" and calls.read_text() == attempts  # waits for retry_at
    now[0] += timedelta(minutes=1, seconds=1)
    assert syncer.run()["retry_at"] == "2026-10-05T12:03:01+00:00"  # then 2 minutes
    assert syncer.status()["state"] == "offline"


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_a_refused_key_waits_for_access(tmp_path):
    syncer, _ = _ssh_machine(tmp_path, "git@git.example.invalid: Permission denied (publickey).")
    with pytest.raises(SyncError) as refused:
        syncer.check()
    assert refused.value.reason == "auth"
    status = syncer.status()
    assert status["state"] == "waiting_for_access" and status["public_key"].startswith("ssh-ed25519 ")
    assert "agents-core-sync:ssh-test" in status["public_key"]


def test_status_reports_off_paused_pending_and_stale(machine, remote):
    now = [datetime.now(timezone.utc)]
    a = machine("a", clock=lambda: now[0])
    assert a.sync.status()["state"] == "off"
    a.save("user:hello", "# Hello\n")
    a.connect()
    assert a.sync.status()["state"] == "synced"
    a.save("user:hello", "# Hello\n\nchanged\n")
    status = a.sync.status()
    assert status["state"] == "pending" and status["pending"] == 1
    a.sync.pause()
    assert a.sync.status()["state"] == "paused" and a.run()["status"] == "paused"
    a.sync.resume()
    now[0] += timedelta(hours=25)
    assert a.sync.status()["stale"] is True
    now[0] += timedelta(hours=48)
    status = a.sync.status()
    assert status["state"] == "attention" and status["reason"] == "stale"


def test_resolving_with_mine_restores_the_local_version(pair):
    a, b = pair
    a.save("user:shared", "# Shared\n\nA\n")
    b.save("user:shared", "# Shared\n\nB\n")
    a.run()
    b.run()
    [record] = records(b)
    assert b.sync.resolve(record["id"], "mine")["status"] == "resolved"
    assert b.text("common/shared.md") == "# Shared\n\nB\n"
    assert records(b) == []
    b.run()
    a.run()
    assert a.text("common/shared.md") == "# Shared\n\nB\n" and records(a) == []


def test_a_library_git_directory_made_elsewhere_is_never_used(machine, remote):
    a = machine("a")
    a.lib.mkdir(parents=True)
    plain_git("init", "--quiet", str(a.lib))
    with pytest.raises(SyncError) as refused:
        a.sync.setup(remote=str(remote), name="Owner", email="owner@example.com", label="a")
    assert refused.value.reason == "foreign_git"


def test_disconnect_keeps_the_files_and_git_directory(pair):
    a, _ = pair
    assert a.sync.disconnect()["status"] == "off"
    assert a.sync.status()["state"] == "off"
    assert (a.lib / ".git").is_dir() and (a.lib / "common/shared.md").exists()


def test_cli_reports_status_and_refuses_a_bad_remote(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(engine_module, "default_state_dir", lambda: tmp_path / "state")
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "library"))
    assert cli(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "off"
    assert cli(["setup", "--remote=-oProxyCommand=evil", "--name", "Owner", "--email", "o@example.com"]) == 1
    assert "unknown_remote" in capsys.readouterr().out


# --- pure rules -------------------------------------------------------------------------------


@pytest.mark.parametrize("url, kind", [
    ("git@github.com:me/agents-library.git", "ssh"), ("ssh://git@host.example:2222/me/lib.git", "ssh"),
    ("https://gitlab.example/me/lib.git", "https"),
])
def test_remote_urls_that_sync_accepts(url, kind):
    assert gitcmd.parse_remote(url).kind == kind


@pytest.mark.parametrize("url", [
    "-oProxyCommand=evil", "git@github.com:-oProxyCommand=evil", "git@github.com:me/lib .git",
    "https://user:token@github.com/me/lib.git", "https://github.com/me/../lib.git", "ext::sh -c evil",
    "file:///tmp/lib.git", "/tmp/lib.git", "git@github.com:me/lib.git\n", "",
])
def test_remote_urls_that_sync_refuses(url):
    with pytest.raises(gitcmd.RemoteError):
        gitcmd.parse_remote(url)


def test_ssh_command_quotes_paths_with_spaces(tmp_path):
    command = gitcmd.ssh_command(tmp_path / "Application Support" / "id_ed25519",
                                 tmp_path / "Application Support" / "known_hosts",
                                 tmp_path / "Application Support" / "ssh_config")
    words = shlex.split(command)
    assert words[words.index("-i") + 1].endswith("Application Support/id_ed25519")
    assert any(word.startswith('UserKnownHostsFile="') and "Application Support" in word for word in words)
    assert "IdentitiesOnly=yes" in words and "StrictHostKeyChecking=yes" in words and "BatchMode=yes" in words


@pytest.mark.parametrize("path, expected", [
    ("common/a.md", ("common",)), ("personas/builtin/a.json", ("personas",)),
    ("personas/repos/k/a.json", ("personas", "repos/k")), (".history/repos/k/a/v.md", ("history", "repos/k")),
    ("repos/k/a.md", ("repos/k",)), ("components.json", ("components",)), (".agents-library.json", ()),
    ("repos/k/history/mac-a/2026-10.md", ("history", "repos/k")), ("repos/k/history.md", ("repos/k",)),
    (".agents-sync/scopes.json", ()), (".lock", None), ("common/.tmp-x", None), ("notes.txt", None),
    ("common/__pycache__/x.pyc", None), ("repos/k/.repo.local.json", None), ("common/con.md", None),
    ("common/a:b.md", None), ("common/.DS_Store", None),
])
def test_scope_groups(path, expected):
    assert scope.groups(path) == expected


@pytest.mark.parametrize("text, hit", [
    (f"token {TOKEN}", "github_token"), ("-----BEGIN OPENSSH PRIVATE KEY-----", "private_key"),
    ("AKIAABCDEFGHIJKLMNOP", "aws_access_key"), ("password=hunter2", "password"),
    ("glpat-abcdefghijklmnopqrst", "gitlab_token"), ("xoxb-1234567890-abc", "slack_token"),
    ("sk-ant-api03-abcdefghijklmnopqrstuv", "api_key"), ("github_pat_" + "a" * 30, "github_pat"),
    ("tokens start with ghp_ and passwords are secret", None), ("password = <your password>", None),
    ('password="hunter2"', "password"), ("password = 'hunter2'", "password"),
    ('DB_PASSWORD="s3cr3t"', "password"), ("export PGPASSWORD=s3cr3t", "password"),
    ('{"password": "x1"}', "password"), ("password=${PASS}", None), ("password: required", None),
])
def test_secret_patterns(text, hit):
    assert scope.scan(text.encode()) == hit


def test_merge_of_component_switches_and_scopes_is_a_set_merge():
    blobs = {}

    def blob(data: bytes) -> str:
        key = scope.blob_id(data)
        blobs[key] = data
        return key

    def components(*rules):
        return blob(json.dumps({"disabled": {"rules": sorted(rules), "skills": [], "implants": []}}).encode())

    base = {"components.json": components("a", "b")}
    local = {"components.json": components("b", "c")}       # a on, c off
    remote = {"components.json": components("a", "b", "d")}  # d off
    result = merging.merge(base, local, remote, blobs.__getitem__, label="m",
                           now=datetime(2026, 10, 5, tzinfo=timezone.utc))
    merged = json.loads(result.tree["components.json"])["disabled"]["rules"]
    assert merged == ["b", "c", "d"] and result.conflicts == []
    scopes = scope.merge_scopes(scope.Scopes(frozenset({"history"})), scope.Scopes(frozenset()),
                                scope.Scopes(frozenset({"history", "repos/x"})))
    assert scopes.exclude == frozenset({"repos/x"})


def test_commit_messages_name_the_changed_flows():
    old = {"common/a.md": "1", "personas/builtin/pr-review.json": "1"}
    new = {"common/a.md": "2", "common/b.md": "1", "common/b.meta.json": "1",
           "personas/builtin/pr-review.json": "2", "components.json": "1",
           ".history/common/a/x.md": "1"}
    assert engine_module.describe(old, new) == "update user:a, add user:b, components, persona builtin:pr-review"


def test_a_mass_deletion_waits_for_confirmation(pair):
    a, b = pair
    for index in range(12):
        a.save(f"user:flow-{index}", f"# Flow {index}\n")
    a.run()
    b.run()
    for path in (a.lib / "common").glob("*.md"):
        path.unlink()
    result = a.run()
    assert result["status"] == "attention" and result["reason"] == "confirmation_needed"
    assert "common/flow-3.md" in remote_files(a.remote)
    preview = a.sync.preview()
    assert "common/flow-3.md" in preview["remove_from_remote"]
    assert a.run(confirm=preview["hash"])["status"] == "synced"
    result = b.run()  # the other machine asks too before it deletes most of its library
    assert result["reason"] == "confirmation_needed" and (b.lib / "common" / "flow-3.md").exists()
    preview = b.sync.preview()
    assert "common/flow-3.md" in preview["delete_local"]
    assert b.run(confirm=preview["hash"])["status"] == "synced"
    assert not (b.lib / "common" / "flow-3.md").exists()


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_an_unreadable_directory_is_not_a_deletion(pair):
    a, b = pair
    a.save("user:second", "# Second\n")
    a.run()
    b.run()
    common = b.lib / "common"
    common.chmod(0)
    try:
        result = b.run()  # common/ cannot be listed now
    finally:
        common.chmod(0o700)
    assert result["status"] in ("synced", "attention")
    assert {"common/shared.md", "common/second.md"} <= set(remote_files(a.remote))


def test_undoing_a_flow_deletion_keeps_changes_from_both_sides():
    """Remote deleted the flow but changed its overlay; local edited the text: nothing is lost."""
    blobs = {}

    def blob(data: bytes) -> str:
        key = scope.blob_id(data)
        blobs[key] = data
        return key

    base = {"common/x.md": blob(b"# X\n"), "personas/common/x.json": blob(b'{"persona": null}\n')}
    local = {"common/x.md": blob(b"# X\n\nedited\n"), "personas/common/x.json": base["personas/common/x.json"]}
    remote = {"personas/common/x.json": blob(b'{"persona": {"agent": "a"}}\n')}
    result = merging.merge(base, local, remote, blobs.__getitem__, label="m",
                           now=datetime(2026, 10, 5, tzinfo=timezone.utc))
    assert blobs[result.tree["common/x.md"]] == b"# X\n\nedited\n"
    assert blobs[result.tree["personas/common/x.json"]] == b'{"persona": {"agent": "a"}}\n'
    assert [(c["kind"], c["deleted_on"]) for c in result.conflicts] == [("deletion_undone", "remote")]


def _library_files(m: Machine) -> dict[str, bytes]:
    return {p.relative_to(m.lib).as_posix(): p.read_bytes() for p in sorted(m.lib.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(m.lib).parts[:1] and p.name != ".lock"}


@pytest.mark.parametrize("seed", range(6))
def test_random_edits_on_two_machines_converge_without_losing_text(machine, seed):
    """Random saves, deletions, overlays and switches on two machines, synced in random order."""
    import random
    rnd = random.Random(seed)
    a, b = machine("a"), machine("b")
    a.save("user:f1", "# f1\n")
    a.connect()
    b.connect()
    written = set()
    for step in range(8):
        m, flow = rnd.choice((a, b)), rnd.choice(("f1", "f2", "f3"))
        operation = rnd.choice(("save", "save", "delete", "overlay", "switch", "sync", "sync"))
        if operation == "save":
            text = f"# {flow}\n\n{m.name} {seed} {step}\n"
            m.save(f"user:{flow}", text)
            written.add(text.encode())
        elif operation == "delete" and m.revision(f"user:{flow}"):
            m.flows.delete(f"user:{flow}", expected_revision=m.revision(f"user:{flow}"))
        elif operation == "overlay" and (m.lib / f"common/{flow}.md").exists():
            (m.lib / "personas" / "common").mkdir(parents=True, exist_ok=True)
            user_library.atomic_write(m.lib / f"personas/common/{flow}.json",
                                      json.dumps({"persona": {"agent": f"{m.name}{step}"}}).encode())
        elif operation == "switch":
            m.switch("rules", rnd.choice(("r1", "r2")), rnd.random() < 0.5)
        elif operation == "sync":
            assert m.run()["status"] in ("synced", "attention")
    for m in (a, b, a, b):
        assert m.run()["status"] == "synced"
    assert _library_files(a) == _library_files(b)
    kept = set(_library_files(a).values())
    assert {text for text in written if text not in kept} == set()  # every text: current, in .history or a record



# --- review findings ------------------------------------------------------------------------


def test_content_excluded_elsewhere_never_reaches_the_remote_when_joining(machine):
    a, b = machine("a"), machine("b")
    a.save("user:hello", "# Hello\n")
    a.connect()
    a.sync.change_scopes(exclude=["repos/github.com-corp-work"])
    a.run()
    _repo_group(b, "github.com-corp-work", "github.com/corp/work", text="# deploy\n\nCORP SECRET TEXT\n")
    b.save("user:mine", "# Mine\n")
    b.sync.setup(remote=str(b.remote), name="Owner", email="owner@example.com", label="b")
    preview = b.sync.preview()
    assert not [e for e in preview["upload"] if "corp-work" in e["path"]]
    assert b.sync.start(preview["hash"])["status"] == "synced"
    assert b"CORP SECRET TEXT" not in remote_objects(b.remote)
    assert "CORP SECRET TEXT" in b.text("repos/github.com-corp-work/deploy.md")
    assert "common/mine.md" in remote_files(b.remote)


def test_an_edit_in_a_group_excluded_meanwhile_stays_on_this_machine(pair):
    a, b = pair
    _repo_group(a, "github.com-corp-work", "github.com/corp/work")
    a.run()
    b.run()
    a.sync.change_scopes(exclude=["repos/github.com-corp-work"])
    a.run()
    _repo_group(b, "github.com-corp-work", "github.com/corp/work", text="# deploy\n\nB SECRET EDIT\n")
    assert b.run()["status"] == "synced"
    assert b"B SECRET EDIT" not in remote_objects(b.remote)
    assert records(b) == []
    assert "B SECRET EDIT" in b.text("repos/github.com-corp-work/deploy.md")


def test_a_conflict_keeps_the_local_text_when_history_is_excluded(pair):
    a, b = pair
    a.sync.change_scopes(exclude=["history"])
    a.run()
    b.run()
    a.save("user:shared", "# Shared\n\nA\n")
    b.save("user:shared", "# Shared\n\nB\n")
    a.run()
    b.run()
    assert b.text("common/shared.md") == "# Shared\n\nA\n"
    [record] = records(b)
    assert (b.lib / record["local_version"]).read_text() == "# Shared\n\nB\n"  # kept on this machine only
    b.sync.resolve(record["id"], "mine")
    assert b.text("common/shared.md") == "# Shared\n\nB\n"


def test_losing_the_scopes_file_keeps_every_exclusion(pair):
    a, b = pair
    a.sync.change_scopes(exclude=["repos/github.com-corp-work"])
    a.run()
    b.run()
    shutil.rmtree(b.lib / ".agents-sync")
    _repo_group(b, "github.com-corp-work", "github.com/corp/work", text="# deploy\n\nB CORP COPY\n")
    b.run()
    a.run()
    assert "repos/github.com-corp-work" in json.loads(b.text(".agents-sync/scopes.json"))["exclude"]
    assert "repos/github.com-corp-work" in json.loads(a.text(".agents-sync/scopes.json"))["exclude"]
    assert b"B CORP COPY" not in remote_objects(b.remote)


def test_including_on_one_machine_waits_for_approval_on_the_other(pair):
    a, b = pair
    a.sync.change_scopes(exclude=["repos/github.com-corp-work"])
    a.run()
    b.run()
    _repo_group(a, "github.com-corp-work", "github.com/corp/work", text="# deploy\n\nA CORP TEXT\n")
    a.run()
    asked = b.sync.change_scopes(include=["repos/github.com-corp-work"])
    assert asked["status"] == "confirmation_needed" and asked["upload"] == []  # B has no copy of the group
    b.sync.change_scopes(include=["repos/github.com-corp-work"], confirm=asked["hash"])
    b.run()
    result = a.run()
    assert result["reason"] == "new_repository"
    assert a.sync.status()["pending_groups"] == ["repos/github.com-corp-work"]
    assert b"A CORP TEXT" not in remote_objects(a.remote)
    asked = a.sync.change_scopes(approve=["repos/github.com-corp-work"])
    assert "repos/github.com-corp-work/deploy.md" in asked["upload"]
    a.sync.change_scopes(approve=["repos/github.com-corp-work"], confirm=asked["hash"])
    assert a.run()["status"] == "synced"
    assert b"A CORP TEXT" in remote_files(a.remote)["repos/github.com-corp-work/deploy.md"]


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_reconciling_never_overwrites_before_the_local_text_is_kept(pair, monkeypatch):
    a, b = pair
    b.save("user:shared", "# Shared\n\nfrom B\n")
    b.run()
    real_apply = a.sync._apply

    def apply_after_a_manual_edit(*args, **kwargs):
        (a.lib / "common" / "shared.md").write_text("# Shared\n\nmanual edit on A\n")
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(a.sync, "_apply", apply_after_a_manual_edit)
    a.run()
    monkeypatch.undo()
    history = a.lib / ".history" / "common" / "shared"
    history.mkdir(parents=True, exist_ok=True)
    history.chmod(0o500)
    try:
        result = a.run()
        assert a.text("common/shared.md") == "# Shared\n\nmanual edit on A\n"
        assert result["reason"] == "library_unreadable" and "common/shared.md" in result["message"]
    finally:
        history.chmod(0o700)
    a.run()
    [record] = records(a)
    assert (a.lib / record["local_version"]).read_text() == "# Shared\n\nmanual edit on A\n"
    assert a.text("common/shared.md") == "# Shared\n\nfrom B\n"


def test_resolving_a_deletion_with_mine_deletes_through_the_flow_library(pair):
    a, b = pair
    b.save("user:shared", "# Shared\n\nedited on B\n")
    set_overlay(b, "code_reviewer")
    b.run()
    a.flows.delete("user:shared", expected_revision=a.revision("user:shared"))
    a.run()
    [record] = records(a)
    assert (record["kind"], record["deleted_on"]) == ("deletion_undone", "local")
    a.sync.resolve(record["id"], "mine")
    assert not (a.lib / "common/shared.md").exists()
    assert not (a.lib / "personas/common/shared.json").exists()
    a.run()
    b.run()
    assert not (b.lib / "common/shared.md").exists() and not (b.lib / "personas/common/shared.json").exists()
    kept = [p.read_text() for p in (b.lib / ".history/common/shared").glob("*-deleted.md")]
    assert "# Shared\n\nedited on B\n" in kept


def test_the_mass_deletion_guard_ignores_history(pair):
    a, b = pair
    for index in range(12):
        for round_ in range(3):
            a.save(f"user:flow-{index}", f"# Flow {index}\n\nround {round_}\n")
    a.run()
    b.run()
    for path in (a.lib / "common").glob("*.md"):
        path.unlink()
    assert a.run()["reason"] == "confirmation_needed"
    assert "common/flow-3.md" in remote_files(a.remote)


def test_moving_to_another_remote_starts_a_new_history(pair, tmp_path):
    a, _ = pair
    _repo_group(a, "github.com-corp-work", "github.com/corp/work", text="# deploy\n\nOLD CORP TEXT\n")
    a.run()
    a.sync.change_scopes(exclude=["repos/github.com-corp-work"])
    a.run()
    a.sync.disconnect()
    fresh = tmp_path / "fresh.git"
    plain_git("init", "--bare", "--quiet", "--initial-branch=main", str(fresh))
    a.sync.setup(remote=str(fresh), name="Owner", email="owner@example.com", label="a")
    a.sync.check()
    assert a.sync.start(a.sync.preview()["hash"])["status"] == "synced"
    assert b"OLD CORP TEXT" not in remote_objects(fresh)
    assert plain_git("--git-dir", str(fresh), "rev-list", "--count", "main") == "1"
    assert "common/shared.md" in remote_files(fresh)


def test_a_remote_reset_to_an_earlier_commit_needs_confirmation(pair):
    a, b = pair
    earlier = plain_git("--git-dir", str(a.remote), "rev-parse", "main")
    a.save("user:leaked", "# Leaked\n")
    a.run()
    plain_git("--git-dir", str(a.remote), "update-ref", "refs/heads/main", earlier)
    result = a.run()
    assert result["status"] == "attention" and result["reason"] == "confirmation_needed"
    assert "common/leaked.md" not in remote_files(a.remote)
    preview = a.sync.preview()
    assert "common/leaked.md" in [entry["path"] for entry in preview["upload"]]


def test_a_rewritten_remote_never_deletes_local_files(pair, tmp_path):
    a, b = pair
    a.save("user:one", "# One\n")
    a.run()
    clone = tmp_path / "rewrite"
    plain_git("init", "--quiet", "--initial-branch=main", str(clone))
    (clone / ".agents-library.json").write_text('{"format": 1}\n')
    (clone / "common").mkdir()
    (clone / "common" / "shared.md").write_text("# Shared\n\nfirst text\n")
    plain_git("add", ".", cwd=clone)
    plain_git("commit", "--quiet", "-m", "without one.md", cwd=clone)
    plain_git("push", "--quiet", "--force", str(a.remote), "main:main", cwd=clone)
    preview = a.sync.preview()
    assert preview["delete_local"] == [] and "common/one.md" in [e["path"] for e in preview["upload"]]
    assert a.run(confirm=preview["hash"])["status"] == "synced"
    assert a.text("common/one.md") == "# One\n"


def test_files_sync_does_not_handle_stay_on_the_remote(pair, tmp_path):
    a, b = pair
    clone = tmp_path / "web-ui"
    plain_git("clone", "--quiet", str(a.remote), str(clone))
    (clone / "README.md").write_text("# My library\n")
    (clone / "skills").mkdir()
    (clone / "skills" / "x.md").write_text("x\n")
    plain_git("add", ".", cwd=clone)
    plain_git("commit", "--quiet", "-m", "web ui", cwd=clone)
    plain_git("push", "--quiet", "origin", "main", cwd=clone)
    b.run()
    assert not (b.lib / "README.md").exists() and not (b.lib / "skills").exists()
    b.save("user:after", "# After\n")
    b.run()
    a.run()
    files = remote_files(a.remote)
    assert {"README.md", "skills/x.md", "common/after.md"} <= set(files)


def test_resolve_mine_refuses_a_record_that_points_elsewhere(pair):
    a, _ = pair
    conflicts = a.lib / ".agents-sync" / "conflicts"
    conflicts.mkdir(parents=True, exist_ok=True)
    cases = [{"path": "common/shared.md", "kind": "both_changed", "local_version": ".agents-sync/scopes.json"},
             {"path": ".agents-sync/scopes.json", "kind": "both_changed", "local_content": "{}"},
             {"path": "common/shared.md", "kind": "both_changed",
              "local_version": ".history/common/other/20261005T000000000000Z-0123456789ab.md"}]
    for index, record in enumerate(cases):
        record_id = f"20261005T00000000000{index}Z-0123456789"
        (conflicts / f"{record_id}.json").write_text(json.dumps({"id": record_id, **record}))
        with pytest.raises(SyncError):
            a.sync.resolve(record_id, "mine")
    assert a.text("common/shared.md") == "# Shared\n\nfirst text\n"


def test_a_repo_file_from_before_the_split_loses_its_path_before_upload(pair):
    a, _ = pair
    group = a.lib / "repos" / "github.com-me-old"
    group.mkdir(parents=True)
    (group / ".repo.json").write_text(json.dumps({"origin": "github.com/me/old", "path": "/Users/me/private/old"}))
    (group / "x.md").write_text("# x\n")
    a.run()
    assert b"/Users/me/private/old" not in remote_objects(a.remote)
    assert json.loads((group / ".repo.local.json").read_text())["path"] == "/Users/me/private/old"
    assert json.loads((group / ".repo.json").read_text()) == {"origin": "github.com/me/old"}


def test_a_run_on_another_library_is_refused(pair, tmp_path):
    a, _ = pair
    other = Syncer(tmp_path / "elsewhere", a.state, allow_file_remote=True, visibility=lambda r: "private")
    assert other.run()["reason"] == "library_mismatch"


def test_cli_reports_errors_without_tracebacks_and_takes_explicit_directories(pair, tmp_path, capsys):
    a, _ = pair
    assert cli(["--state", str(a.state), "--library", str(a.lib), "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "synced"
    (a.lib / ".agents-sync").mkdir(exist_ok=True)
    (a.lib / ".agents-sync" / "scopes.json").write_text("{broken")
    assert cli(["--state", str(a.state), "--library", str(a.lib), "scope", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "scopes_invalid"


def test_the_macos_state_directory_follows_an_installed_daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_module.sys, "platform", "darwin")
    monkeypatch.delenv("AGENTS_SERVICE_DIR", raising=False)
    monkeypatch.setattr(engine_module, "installation_root", lambda: tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / ".shared-service.json").write_text(json.dumps({"directory": str(tmp_path / "service")}))
    assert engine_module.default_state_dir() == tmp_path / "service" / "user-sync"
    monkeypatch.setenv("AGENTS_SERVICE_DIR", str(tmp_path / "env-service"))
    assert engine_module.default_state_dir() == (tmp_path / "env-service").resolve() / "user-sync"


def test_configure_validates_the_fetch_interval(pair):
    a, _ = pair
    assert a.sync.configure(fetch_minutes=15)["fetch_minutes"] == 15
    for wrong in (0, 61, True, "5"):
        with pytest.raises(SyncError):
            a.sync.configure(fetch_minutes=wrong)
    assert a.sync.configure(ask_new_repositories=True)["ask_new_repositories"] is True



def _include_elsewhere(owner: Machine, group: str) -> None:
    asked = owner.sync.change_scopes(include=[group])
    owner.sync.change_scopes(include=[group], confirm=asked["hash"])
    owner.run()


def test_a_group_included_again_elsewhere_is_never_deleted_from_the_remote(pair):
    a, b = pair
    group = "repos/github.com-me-tool"
    _repo_group(a, "github.com-me-tool", "github.com/me/tool", text="# deploy\n\nshared v1\n")
    a.run()
    b.run()
    a.sync.change_scopes(exclude=[group])
    a.run()
    b.run()
    _repo_group(b, "github.com-me-tool", "github.com/me/tool", text="# deploy\n\nB edit while excluded\n")
    _include_elsewhere(b, group)
    assert a.run()["reason"] == "new_repository"  # A holds its own copy for approval
    remote_text = remote_files(a.remote)["repos/github.com-me-tool/deploy.md"]
    assert b"B edit while excluded" in remote_text  # and never deletes B's upload
    b.run()
    assert "B edit while excluded" in b.text("repos/github.com-me-tool/deploy.md")
    assert "shared v1" in a.text("repos/github.com-me-tool/deploy.md")
    asked = a.sync.change_scopes(approve=[group])
    a.sync.change_scopes(approve=[group], confirm=asked["hash"])
    a.run()
    b.run()
    assert "B edit while excluded" in a.text("repos/github.com-me-tool/deploy.md")
    [record] = records(a)
    assert "shared v1" in (a.lib / record["local_version"]).read_text()


def test_an_exclusion_withdraws_an_earlier_approval(pair):
    a, b = pair
    group = "repos/github.com-me-notes"
    a.sync.change_scopes(exclude=[group])
    a.run()
    b.run()
    _repo_group(a, "github.com-me-notes", "github.com/me/notes", text="# deploy\n\nv1\n")
    _include_elsewhere(b, group)
    a.run()
    asked = a.sync.change_scopes(approve=[group])
    a.sync.change_scopes(approve=[group], confirm=asked["hash"])
    assert a.run()["status"] == "synced"
    b.sync.change_scopes(exclude=[group])
    b.run()
    a.run()
    assert group not in a.sync.settings().approved_groups
    _repo_group(a, "github.com-me-notes", "github.com/me/notes", text="# deploy\n\nA PRIVATE NOTES\n")
    _include_elsewhere(b, group)
    assert a.run()["reason"] == "new_repository"
    assert b"A PRIVATE NOTES" not in remote_objects(a.remote)


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX symlinks")
def test_a_linked_directory_is_not_a_deletion(pair, tmp_path):
    a, b = pair
    moved = tmp_path / "moved-common"
    shutil.move(str(a.lib / "common"), moved)
    (a.lib / "common").symlink_to(moved, target_is_directory=True)
    a.run()
    b.run()
    assert "common/shared.md" in remote_files(a.remote)
    assert b.text("common/shared.md") == "# Shared\n\nfirst text\n"


def test_a_cycle_that_stops_half_way_does_not_duplicate_its_conflict(pair, monkeypatch):
    a, b = pair
    a.save("user:shared", "# Shared\n\nA\n")
    b.save("user:shared", "# Shared\n\nB\n")
    a.run()
    real_write = user_library.atomic_write

    def killed_before_the_flow(path, data):
        if Path(path).name == "shared.md":
            raise RuntimeError("killed")
        return real_write(path, data)

    monkeypatch.setattr(user_library, "atomic_write", killed_before_the_flow)
    with pytest.raises(RuntimeError):
        b.run()
    assert len(records(b)) == 1  # the record and the kept version were written first
    monkeypatch.undo()
    assert b.run()["status"] == "synced"
    a.run()
    assert len(records(b)) == len(records(a)) == 1
    assert b.text("common/shared.md") == "# Shared\n\nA\n"


def sharing_violation(path) -> PermissionError:
    """What Windows raises when a file another process holds open is replaced or deleted."""
    error = PermissionError(13, "The process cannot access the file", str(path))
    error.winerror = 32
    return error


def held_open(monkeypatch, paths, times=None):
    """Writes and deletions of ``paths`` fail as on Windows, ``times`` times or until undone."""
    real_write, real_unlink, failures = user_library.atomic_write, Path.unlink, []

    def fail(path) -> bool:
        if Path(path) in paths and (times is None or len(failures) < times):
            failures.append(path)
            return True
        return False

    def write(path, data):
        if fail(path):
            raise sharing_violation(path)
        return real_write(path, data)

    def unlink(path, *args, **kwargs):
        if fail(path):
            raise sharing_violation(path)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(engine_module, "_RETRY_DELAY", 0)
    monkeypatch.setattr(user_library, "atomic_write", write)
    monkeypatch.setattr(Path, "unlink", unlink)
    return failures


def test_a_file_a_reader_holds_open_for_a_moment_is_written_on_a_retry(pair, monkeypatch):
    a, b = pair
    b.save("user:shared", "# Shared\n\nfrom B\n")
    b.run()
    failures = held_open(monkeypatch, {a.lib / "common" / "shared.md"}, times=2)
    assert a.run()["status"] == "synced"
    assert len(failures) == 2 and a.text("common/shared.md") == "# Shared\n\nfrom B\n"
    assert records(a) == [] and a.sync._state()["held_remote"] == []


def test_files_a_reader_keeps_open_are_written_next_cycle_without_conflicts(pair, monkeypatch):
    a, b = pair
    b.save("user:shared", "# Shared\n\nfrom B\n")
    b.save("user:fresh", "# Fresh\n")
    b.run()
    held_open(monkeypatch, {a.lib / "common" / name for name in ("shared.md", "fresh.md")})
    assert a.run()["status"] == "synced"
    assert a.text("common/shared.md") == "# Shared\n\nfirst text\n"
    assert not (a.lib / "common" / "fresh.md").exists()
    monkeypatch.undo()
    a.run()
    assert a.text("common/shared.md") == "# Shared\n\nfrom B\n" and a.text("common/fresh.md") == "# Fresh\n"
    assert records(a) == [] and a.sync._state()["failed_writes"] == {}
    b.run()
    assert b.text("common/shared.md") == "# Shared\n\nfrom B\n" and records(b) == []


def test_a_deletion_a_reader_keeps_open_is_finished_next_cycle(pair, monkeypatch):
    a, b = pair
    a.save("user:gone", "# Gone\n")
    a.run()
    b.run()
    b.flows.delete("user:gone", expected_revision=b.revision("user:gone"))
    b.run()
    held_open(monkeypatch, {a.lib / "common" / "gone.md"})
    assert a.run()["status"] == "synced"
    assert a.text("common/gone.md") == "# Gone\n"
    monkeypatch.undo()
    a.run()
    assert not (a.lib / "common" / "gone.md").exists() and records(a) == []
    assert "common/gone.md" not in remote_files(a.remote)


def test_a_lost_version_whose_replacement_failed_is_kept_once(pair, monkeypatch):
    a, b = pair
    b.save("user:shared", "# Shared\n\nB\n")
    b.run()
    a.save("user:shared", "# Shared\n\nA\n")
    held_open(monkeypatch, {a.lib / "common" / "shared.md"})
    a.run()
    assert a.text("common/shared.md") == "# Shared\n\nA\n"
    monkeypatch.undo()
    a.run()
    assert a.text("common/shared.md") == "# Shared\n\nB\n"
    [record] = records(a)
    assert (a.lib / record["local_version"]).read_text() == "# Shared\n\nA\n"


def test_text_changed_between_reading_and_merging_is_never_uploaded(pair, monkeypatch):
    a, b = pair
    a.save("user:shared", "# Shared\n\nA edit\n")
    a.run()
    b.save("user:shared", "# Shared\n\nB edit\n")
    real_merge = merging.merge

    def merge_after_an_unlocked_edit(*args, **kwargs):
        (b.lib / "common" / "shared.md").write_text(f"# Shared\n\n{TOKEN}\n")
        return real_merge(*args, **kwargs)

    monkeypatch.setattr(engine_module.merging, "merge", merge_after_an_unlocked_edit)
    result = b.run()
    assert result["status"] == "pending"
    assert TOKEN.encode() not in remote_objects(b.remote)


def test_windows_links_are_name_surrogates_and_placeholders_are_files():
    from types import SimpleNamespace
    reparse = 0x400

    def info(tag):
        return SimpleNamespace(st_mode=0o100644, st_file_attributes=reparse, st_reparse_tag=tag)

    assert scope.is_link(info(0xA000000C)) and scope.is_link(info(0xA0000003))  # symlink, junction
    assert not scope.is_link(info(0x9000001A))  # a OneDrive placeholder
    assert scope.is_link(info(0))  # unknown: never followed, never taken for deleted
    assert not scope.is_link(SimpleNamespace(st_mode=0o100644, st_file_attributes=0x20))


def test_an_executable_file_keeps_its_mode_in_the_remote_while_unchanged(pair, tmp_path):
    a, b = pair
    clone = tmp_path / "tool"
    plain_git("clone", "--quiet", str(a.remote), str(clone))
    (clone / "common" / "helper.sh").write_text("#!/bin/sh\necho hi\n")
    plain_git("add", "common/helper.sh", cwd=clone)
    plain_git("update-index", "--chmod=+x", "common/helper.sh", cwd=clone)
    plain_git("commit", "--quiet", "-m", "an executable helper", cwd=clone)
    plain_git("push", "--quiet", "origin", "main", cwd=clone)
    b.run()
    b.save("user:other", "# Other\n")
    b.run()
    mode = plain_git("--git-dir", str(a.remote), "ls-tree", "main", "common/helper.sh").split()[0]
    assert mode == "100755"


@pytest.mark.parametrize("path", [".agents-sync/scopes.json", ".agents-library.json", ".agents-sync/conflicts/x.json"])
def test_the_library_control_files_cannot_be_excluded(pair, path):
    a, _ = pair
    with pytest.raises(SyncError):
        a.sync.change_scopes(exclude_files=[path])
    with pytest.raises(scope.ScopeError):
        scope.parse_scopes(json.dumps({"exclude_files": [path]}).encode())


def test_the_privacy_probe_keeps_an_https_port_and_runs_outside_any_repository(tmp_path, monkeypatch):
    assert gitcmd.parse_remote("https://git.example.com:8443/me/lib.git").https_url == \
        "https://git.example.com:8443/me/lib.git"
    assert gitcmd.parse_remote("ssh://git@git.example.com:2222/me/lib.git").https_url == \
        "https://git.example.com/me/lib.git"
    seen = {}

    def fake_run(command, **options):
        seen.update(options)
        return subprocess.CompletedProcess(command, 128, b"", b"fatal: could not read Username")

    monkeypatch.setattr(engine_module.subprocess, "run", fake_run)
    syncer = Syncer(tmp_path / "library", tmp_path / "state")
    verdict = syncer.anonymous_visibility(gitcmd.parse_remote("https://git.example.com/me/lib.git"))
    assert verdict == "private"
    assert seen["cwd"] == syncer.state_dir and seen["env"]["GIT_CEILING_DIRECTORIES"] == str(syncer.state_dir.parent)


def test_scope_changes_and_resolutions_refuse_another_library(pair, tmp_path):
    a, _ = pair
    other = Syncer(tmp_path / "elsewhere", a.state, allow_file_remote=True, visibility=lambda r: "private")
    with pytest.raises(SyncError) as refused:
        other.change_scopes(exclude=["history"])
    assert refused.value.reason == "library_mismatch"
    with pytest.raises(SyncError):
        other.resolve("20261005T000000000000Z-0123456789", "keep")
