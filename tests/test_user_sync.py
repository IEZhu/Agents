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


def plain_git(*args, cwd=None, input=None) -> str:
    """git for test fixtures, isolated from the developer's configuration."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                       GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.com")
    result = subprocess.run(["git", *args], cwd=cwd, env=environment, capture_output=True,
                            input=input, check=True)
    return result.stdout.decode().strip()


def remote_files(remote: Path, branch: str = "main") -> dict[str, bytes]:
    try:
        listing = plain_git("--git-dir", str(remote), "ls-tree", "-r", "--name-only", branch)
    except subprocess.CalledProcessError:
        return {}
    return {path: subprocess.run(["git", "--git-dir", str(remote), "show", f"{branch}:{path}"],
                                 capture_output=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull}).stdout
            for path in listing.splitlines()}


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


def _repo_group(m: Machine, key: str, origin: str | None, flow: str = "deploy") -> None:
    group = m.lib / "repos" / key
    group.mkdir(parents=True, exist_ok=True)
    (group / ".repo.json").write_text(json.dumps({"origin": origin}) + "\n")
    library = FlowLibrary(user_dir=m.lib, repo_key=key)
    library.save(f"repo:{flow}", f"# {flow} for {key}\n")


def test_machine_local_groups_never_sync(pair):
    a, b = pair
    _repo_group(a, "scratch-1a2b3c4d", None)  # its history stays local as well
    (a.lib / ".history" / "repos" / "scratch-1a2b3c4d" / "deploy").mkdir(parents=True)
    (a.lib / ".history/repos/scratch-1a2b3c4d/deploy/20261005T000000000000Z-0123456789ab.md").write_text("old\n")
    a.run()
    assert not [path for path in remote_files(a.remote) if "scratch-1a2b3c4d" in path]


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
    assert "common/creds.md" not in remote_files(a.remote)
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
    b.run()
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
