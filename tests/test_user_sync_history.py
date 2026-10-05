"""Each repository's history merged across machines (#172).

Two libraries and clones of one project (same ``origin``) act as machines, synced through a local
bare repository by the real engine. Nothing here touches the real sync state, library or keychain:
every machine has its own temporary state directory and library.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import threading
import time
import types

import pytest

from src.file_lock import file_lock
from src.memory import history as journal
from src.memory.history import HistoryReader, HistoryStore, HistoryWriter
from src.user_flows import FlowLibrary
from src.user_sync import engine as engine_module, history as history_sync
from src.user_sync.__main__ import main as cli
from tests.test_user_sync import Machine, plain_git, remote, remote_files, remote_objects  # noqa: F401 (fixture)

ORIGIN = "git@github.com:me/project.git"
NORMALIZED = "github.com/me/project"
KEY = "github.com-me-project"
SEGMENTS = f"repos/{KEY}/history"


class Box:
    """One checkout of the project on a machine (a synced library and its private state)."""

    def __init__(self, root: Path, name: str, remote_path: Path, origin: str | None = ORIGIN,
                 machine: Machine | None = None, checkout: str = "project"):
        self.machine = machine or Machine(root, name, remote_path)
        self.label = f"machine-{self.machine.name}"
        self.repo = root / name / checkout
        self.repo.mkdir(parents=True)
        plain_git("init", "--quiet", str(self.repo))
        if origin:
            plain_git("-C", str(self.repo), "remote", "add", "origin", origin)
        self.history = self.repo / "history.md"
        self.writer = HistoryWriter(str(self.history), str(self.repo / "history"))

    def integration(self) -> history_sync.Integration:
        return history_sync.Integration(state_dir=self.machine.state, library=self.machine.lib)

    @contextmanager
    def active(self):
        """This machine's sync integration, as its MCP server installs it at startup."""
        previous = journal.set_sync(self.integration())
        try:
            yield
        finally:
            journal.set_sync(previous)

    def log(self, intent: str, outcome: str = "done", *, settle: bool = True, **options) -> dict:
        with self.active():
            result = self.writer.append_entry(intent, "Agent: software_engineer", outcome, **options)
        if settle:
            assert history_sync.drain(60)  # the worker has shared what it could
        return result

    def read(self, **options) -> list:
        with self.active():
            return HistoryReader(str(self.history)).read_recent(**options)

    def export(self, **options) -> dict:
        return history_sync.export(state_dir=self.machine.state, library=self.machine.lib, **options)

    def approve(self, **options) -> dict:
        preview = self.export(**options)
        assert preview["status"] == "confirmation_needed", preview
        done = self.export(confirm=preview["hash"], **options)
        assert done["status"] == "exported", done
        return preview

    def status(self) -> dict:
        return self.machine.sync.status()

    def segments(self) -> dict[str, str]:
        base = self.machine.lib / SEGMENTS
        if not base.exists():
            return {}
        return {path.relative_to(base).as_posix(): path.read_text(encoding="utf-8")
                for path in sorted(base.rglob("*.md"))}

    def exported(self) -> list[str]:
        """Intents in this machine's own segments, in file order."""
        return [entry.intent for name, text in self.segments().items() if name.startswith(self.label + "/")
                for entry, _ in journal.entry_blocks(text)]

    def sync(self) -> dict:
        result = self.machine.run()
        assert result["status"] in ("synced", "attention"), result
        return result


@pytest.fixture
def clock(monkeypatch):
    """Entry times the test chooses, one second apart unless moved."""
    class Clock:
        now = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

        def tick(self, **delta):
            self.now += timedelta(**(delta or {"seconds": 1}))

    clock = Clock()

    def now(tz=None):
        moment = clock.now
        clock.tick()
        return moment

    monkeypatch.setattr(journal, "_dt", types.SimpleNamespace(
        datetime=types.SimpleNamespace(now=now), timezone=timezone))
    return clock


@pytest.fixture(autouse=True)
def isolated_sync():
    """No installed integration, fresh caches, and the worker idle before and after each test."""
    previous = journal.set_sync(None)
    installed = history_sync._EXPORTER.installed
    history_sync._EXPORTER.installed = False
    for cache in (history_sync._WORKSPACES, history_sync._HEADERS, history_sync._KNOWN):
        cache.clear()
    yield
    assert history_sync.drain(60)
    journal.set_sync(previous)
    history_sync._EXPORTER.installed = installed


@pytest.fixture
def two(tmp_path, remote):
    a, b = Box(tmp_path, "a", remote), Box(tmp_path, "b", remote)
    a.machine.save("user:shared", "# Shared\n")
    a.machine.connect()
    b.machine.connect()
    return a, b


@pytest.fixture
def shared(two, clock):
    """Two machines that both approved sharing the project's history."""
    a, b = two
    a.log("approval on a")
    b.log("approval on b")
    a.approve()
    b.approve()
    return a, b


def run_cli(box: Box, *arguments) -> int:
    return cli(["--state", str(box.machine.state), "--library", str(box.machine.lib), *arguments])


# --- the acceptance: two machines, one repository ----------------------------------------------


def test_two_machines_see_each_others_entries_once_and_in_time_order(shared, clock):
    a, b = shared
    first = a.log("first on a")
    b.log("first on b")
    a.log("second on a")
    # B also saves a repository flow: its .repo.json and the one the export wrote are the same bytes.
    FlowLibrary(user_dir=b.machine.lib, repo_root=b.repo).save("repo:deploy", "# Deploy\n")
    for box in (a, b, a, b):
        box.sync()

    for box, own in ((a, "a"), (b, "b")):
        entries = [e for e in box.read(limit=10) if not e.intent.startswith("approval")]
        assert [e.intent for e in entries] == ["second on a", "first on b", "first on a"]
        assert len({e.id for e in box.read(limit=10)}) == 5
        assert [e.machine for e in entries] == [None if own == "a" else "machine-a",
                                                None if own == "b" else "machine-b",
                                                None if own == "a" else "machine-a"]
    assert [e for e in b.read(limit=10) if e.intent == "first on a"][0].id == first["entry_id"]
    assert a.machine.sync.conflicts() == [] and b.machine.sync.conflicts() == []
    files = remote_files(a.machine.remote)
    assert f"{SEGMENTS}/machine-a/2026-10.md" in files and f"{SEGMENTS}/machine-b/2026-10.md" in files
    assert json.loads(files[f"repos/{KEY}/.repo.json"]) == {"origin": NORMALIZED}
    # history.md stays this checkout's journal: nothing from the other machine is written into it.
    assert "first on b" not in a.history.read_text(encoding="utf-8")


def test_an_entry_on_both_machines_appears_once_and_the_local_copy_wins(shared, clock):
    a, b = shared
    a.log("same question", "same answer")
    clock.tick(minutes=5)
    b.log("same question", "same answer")
    for box in (a, b, a):
        box.sync()
    on_a = [e for e in a.read() if e.intent == "same question"]
    on_b = [e for e in b.read() if e.intent == "same question"]
    assert len(on_a) == len(on_b) == 1
    assert on_a[0].machine is None and on_b[0].machine is None
    assert on_a[0].timestamp < on_b[0].timestamp  # each machine shows its own copy


def test_the_machine_filter(shared, clock):
    a, b = shared
    a.log("mine")
    b.log("theirs")
    b.sync()
    a.sync()
    assert [e.intent for e in a.read(machine="local")] == ["mine", "approval on a"]
    assert [e.intent for e in a.read(machine="machine-a")] == ["mine", "approval on a"]
    assert [e.intent for e in a.read(machine="machine-b")] == ["theirs", "approval on b"]
    assert a.read(machine="machine-c") == []
    assert [e.intent for e in a.read(machine="machine-b", since="2026-10-05T09:00:04")] == ["theirs"]


def test_reading_without_sync_is_unchanged(tmp_path, remote, clock):
    box = Box(tmp_path, "solo", remote)
    writer = HistoryWriter(str(box.history), str(box.repo / "history"), rotation_kb=1)
    for number in range(4):
        writer.append_entry(f"entry {number} " + "x" * 600, "a", "o" * 600)
    assert list((box.repo / "history").glob("*.md"))  # some entries were rotated into an archive
    plain = HistoryReader(str(box.history)).read_recent(limit=50)
    with box.active():  # installed, but sync is not set up on this machine
        assert HistoryReader(str(box.history)).read_recent(limit=50) == plain
    assert len(plain) < 4 and all(e.machine is None for e in plain)
    assert HistoryReader(str(box.history)).read_recent(machine="local") == plain
    assert HistoryReader(str(box.history)).read_recent(machine="machine-b") == []


# --- approval ------------------------------------------------------------------------------------


def test_nothing_is_shared_before_approval_and_status_counts_the_waiting_entries(two, clock, capsys):
    a, b = two
    for number in range(3):
        a.log(f"waiting {number}")
    a.sync()
    assert a.segments() == {}
    assert b"waiting 0" not in remote_objects(a.machine.remote)
    status = a.status()
    assert status["history_waiting"] == [{"key": KEY, "origin": NORMALIZED, "entries": 3}]
    assert status["state"] == "synced" and status["history_repositories"] == []

    assert run_cli(a, "history", "export") == 0
    shown = capsys.readouterr().out
    assert "history: confirmation_needed" in shown and f"{NORMALIZED} ({KEY}): 3 entries" in shown
    assert "2026-10-05 09:00:01Z  waiting 0" in shown and "total: 3 entries in 1 repository" in shown
    assert run_cli(a, "history", "export", "--json") == 0
    preview = json.loads(capsys.readouterr().out)
    listed = preview["repositories"][0]["entries"]
    assert [entry["intent"] for entry in listed] == ["waiting 0", "waiting 1", "waiting 2"]
    assert all(len(entry["id"]) == 12 and entry["timestamp"].startswith("2026-10-05") for entry in listed)

    assert run_cli(a, "history", "export", "--confirm", preview["hash"], "--json") == 0
    assert json.loads(capsys.readouterr().out)["approved"] == [KEY]
    assert a.exported() == ["waiting 0", "waiting 1", "waiting 2"]
    status = a.status()
    assert status["history_waiting"] == [] and status["history_repositories"] == [KEY]
    a.log("follows by itself")
    assert a.exported()[-1] == "follows by itself"

    assert run_cli(a, "history", "revoke", KEY, "--json") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "revoked"
    a.log("after revoking")
    assert "after revoking" not in a.exported() and "follows by itself" in a.exported()
    assert a.status()["history_waiting"] == [{"key": KEY, "origin": NORMALIZED, "entries": 1}]
    # Another machine's approval never shares this machine's entries.
    b.log("on b")
    b.approve()
    a.log("still not shared")
    assert "still not shared" not in a.exported()


def test_the_preview_and_the_approval_can_be_limited_to_one_repository(tmp_path, two, clock, capsys):
    a, _ = two
    other = Box(tmp_path, "a-other", a.machine.remote, origin="git@github.com:me/other.git", machine=a.machine)
    a.log("in the project")
    other.log("in the other repository")
    assert run_cli(a, "history", "export", "--json") == 0
    assert [repo["key"] for repo in json.loads(capsys.readouterr().out)["repositories"]] == \
        ["github.com-me-other", KEY]
    assert run_cli(a, "history", "export", "--repo", KEY, "--json") == 0
    preview = json.loads(capsys.readouterr().out)
    assert [repo["key"] for repo in preview["repositories"]] == [KEY]
    assert run_cli(a, "history", "export", "--repo", KEY, "--confirm", preview["hash"], "--json") == 0
    assert json.loads(capsys.readouterr().out)["approved"] == [KEY]
    status = a.status()
    assert status["history_repositories"] == [KEY]
    assert status["history_waiting"] == [{"key": "github.com-me-other", "origin": "github.com/me/other",
                                          "entries": 1}]
    assert a.exported() == ["in the project"]
    assert not (a.machine.lib / "repos" / "github.com-me-other").exists()


def test_a_preview_stays_valid_for_entries_written_after_it(two, clock):
    a, _ = two
    a.log("previewed")
    preview = a.export()
    a.log("written after the preview")  # an agent session logs its turn between preview and confirm
    done = a.export(confirm=preview["hash"])
    assert done["status"] == "exported"
    assert a.exported() == ["previewed", "written after the preview"]


def test_an_older_entry_that_was_not_previewed_changes_the_preview(tmp_path, two, clock):
    a, _ = two
    clock.now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    a.log("previewed")
    preview = a.export()
    other = Box(tmp_path, "a2", a.machine.remote, machine=a.machine)
    clock.now = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    other.writer.append_entry("older, in another checkout", "a", "o")
    changed = a.export(confirm=preview["hash"], paths=[str(other.repo)])
    assert changed["status"] == "confirmation_needed" and changed["message"].startswith("the preview changed")
    assert a.segments() == {}
    a.approve()
    assert sorted(a.exported()) == ["older, in another checkout", "previewed"]


# --- when entries are shared ------------------------------------------------------------------


def test_a_server_start_catches_up_entries_written_without_sync(shared, clock):
    a, _ = shared
    a.writer.append_entry("written by a process without sync", "a", "o")  # no integration installed
    assert "written by a process without sync" not in a.exported()
    history_sync.install(state_dir=a.machine.state, library=a.machine.lib)  # as a server starting
    assert history_sync.drain(60)
    assert a.exported()[-1] == "written by a process without sync"


def test_catch_up_after_pause_and_resume(shared, clock):
    a, _ = shared
    a.machine.sync.pause()
    a.log("while paused")
    assert "while paused" not in a.exported()
    a.machine.sync.resume()  # this process runs no server: the catch-up runs right away
    assert a.exported()[-1] == "while paused"


def test_catch_up_after_a_failed_export(shared, clock, monkeypatch):
    a, _ = shared
    append = history_sync._append

    def broken(*args, **kwargs):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(history_sync, "_append", broken)
    assert a.log("first")["status"] == "recorded"
    assert a.log("second")["status"] == "recorded"
    error = a.status()["history_error"]
    assert error["reason"] == "write_failed" and error["key"] == KEY and "denied" in error["message"]
    monkeypatch.setattr(history_sync, "_append", append)
    a.log("third")
    assert a.exported()[-3:] == ["first", "second", "third"]
    assert a.status()["history_error"] is None


def test_a_failure_between_parts_then_a_rerun_leaves_no_duplicates(shared, clock, monkeypatch):
    a, _ = shared
    monkeypatch.setattr(history_sync, "PART_BYTES", 900)
    append = history_sync._append
    calls = []

    def fail_on_a_new_part(path, data, *, header, label):
        calls.append(header is not None)
        if header is not None and len(calls) > 1:
            raise OSError(28, "no space")
        return append(path, data, header=header, label=label)

    a.machine.sync.pause()
    for number in range(4):
        a.log(f"entry {number}", "z" * 250)
    monkeypatch.setattr(history_sync, "_append", fail_on_a_new_part)
    a.machine.sync.resume()
    assert a.status()["history_error"]["reason"] == "write_failed"
    monkeypatch.setattr(history_sync, "_append", append)
    history_sync.request_catch_up(a.machine.state, a.machine.lib)
    exported = a.exported()
    assert sorted(exported) == sorted(set(exported))
    assert set(exported) >= {f"entry {number}" for number in range(4)}
    assert len(a.segments()) >= 2 and a.status()["history_error"] is None


def test_a_repository_without_origin_keeps_its_history_local(tmp_path, remote, clock):
    a = Box(tmp_path, "a", remote, origin=None)
    a.machine.save("user:shared", "# Shared\n")
    a.machine.connect()
    assert a.log("local only")["status"] == "recorded"
    assert a.export()["status"] == "up_to_date"
    assert not (a.machine.lib / "repos").exists()
    a.sync()
    assert b"local only" not in remote_objects(a.machine.remote)
    assert [e.machine for e in a.read()] == [None]


def test_a_subfolder_workspace_keeps_its_history_local(shared, clock):
    a, _ = shared
    folder = a.repo / "service"
    folder.mkdir()
    inner = HistoryWriter(str(folder / "history.md"), str(folder / "history"))
    with a.active():
        inner.append_entry("in a subfolder", "a", "o")
    assert history_sync.drain(60)
    assert "in a subfolder" not in a.exported()
    assert str(folder.resolve()) not in history_sync._roots(history_sync._machine(a.machine.state, a.machine.lib))
    with a.active():
        assert [e.intent for e in HistoryReader(str(folder / "history.md")).read_recent()] == ["in a subfolder"]


@pytest.mark.parametrize("group", [f"repos/{KEY}", "history"])
def test_excluded_groups_are_not_exported(shared, clock, group):
    a, b = shared
    a.log("shared before the exclusion")
    a.sync()
    b.sync()
    assert a.machine.sync.change_scopes(exclude=[group])["status"] == "saved"
    a.log("private after the exclusion")
    a.sync()
    b.sync()
    assert "private after the exclusion" not in a.exported()
    assert b"private after the exclusion" not in remote_objects(a.machine.remote)
    assert "private after the exclusion" not in [e.intent for e in b.read()]


def test_the_receiving_machine_merges_nothing_it_excludes(shared, clock):
    a, b = shared
    a.log("from a")
    a.sync()
    b.sync()
    assert "from a" in [e.intent for e in b.read()]
    assert b.machine.sync.change_scopes(exclude=[f"repos/{KEY}"])["status"] == "saved"
    assert [e.machine for e in b.read()] == [None]  # A's segment is still on disk, but excluded here
    b.sync()
    a.sync()
    a.log("after b excluded it")
    a.sync()
    b.sync()
    assert "after b excluded it" not in a.exported()  # the exclusions are shared
    assert not (b.machine.lib / SEGMENTS / "machine-a" / "2026-10.md").read_text().count("after b excluded")


def test_a_single_excluded_part_keeps_its_month_local_until_included(shared, clock):
    a, _ = shared
    part = f"{SEGMENTS}/machine-a/2026-10.md"
    assert a.machine.sync.change_scopes(exclude_files=[part])["status"] == "saved"
    a.log("held in an excluded part")
    assert "held in an excluded part" not in a.exported()
    asked = a.machine.sync.change_scopes(include_files=[part])
    a.machine.sync.change_scopes(include_files=[part], confirm=asked["hash"])
    a.log("next")
    assert a.exported()[-2:] == ["held in an excluded part", "next"]


def test_an_entry_with_credentials_stays_on_this_machine(shared, clock):
    a, b = shared
    a.log("connect to the database", "used password=hunter2 for it")
    a.log("an ordinary entry")
    result = a.sync()
    assert result["reason"] != "secret"  # the segment itself stays clean and keeps syncing
    assert "hunter2" in a.history.read_text(encoding="utf-8")
    assert b"hunter2" not in remote_objects(a.machine.remote)
    b.sync()
    assert "an ordinary entry" in [e.intent for e in b.read()]
    assert "connect to the database" not in [e.intent for e in b.read()]


# --- segments ---------------------------------------------------------------------------------


def test_a_month_continues_in_a_new_part_before_4_mib(two, clock):
    a, _ = two
    assert history_sync.PART_BYTES == 4 * 1024 * 1024
    for number in range(5):  # about 1 MiB each: three fit in a part, two continue in the next
        a.log(f"entry {number}", "y" * (1024 * 1024))
    a.approve()
    parts = a.segments()
    assert list(parts) == ["machine-a/2026-10-2.md", "machine-a/2026-10.md"]
    for text in parts.values():
        assert len(text.encode("utf-8")) <= history_sync.PART_BYTES
    assert [len(journal.entry_blocks(parts[name])) for name in ("machine-a/2026-10.md", "machine-a/2026-10-2.md")] == [3, 2]


def test_parts_and_months_sync_and_merge_without_conflicts(shared, clock, monkeypatch):
    a, b = shared
    monkeypatch.setattr(history_sync, "PART_BYTES", 1200)
    for number in range(4):
        a.log(f"october {number}", "z" * 300)
    clock.now = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
    a.log("november")
    b.log("on b in november")
    for box in (a, b, a, b):
        box.sync()
    names = sorted(a.segments())
    assert "machine-a/2026-10-2.md" in names and "machine-a/2026-11.md" in names and "machine-b/2026-11.md" in names
    assert len(b.read(limit=50)) == 8
    assert a.machine.sync.conflicts() == [] and b.machine.sync.conflicts() == []


def test_appends_extend_the_part_in_place(shared, clock):
    a, _ = shared
    part = a.machine.lib / SEGMENTS / "machine-a" / "2026-10.md"
    before, data = part.stat(), part.read_bytes()
    a.log("appended")
    after = part.stat()
    assert after.st_ino == before.st_ino and after.st_size > before.st_size  # not rewritten
    assert part.read_bytes().startswith(data)


def test_a_block_cut_short_by_a_crash_is_written_again_whole(shared, clock):
    a, _ = shared
    a.log("complete")
    part = a.machine.lib / SEGMENTS / "machine-a" / "2026-10.md"
    with open(part, "ab") as stream:
        stream.write(b"\n## 2026-10-05T09:59:59+00:00 | 0123456789ab\n**Intent:** cut sh")
    a.log("after the crash")
    assert a.exported()[-2:] == ["complete", "after the crash"]
    assert "cut sh" not in part.read_text(encoding="utf-8")


def test_the_segment_block_keeps_the_hash_and_the_parser_reads_the_machine(shared, clock):
    a, _ = shared
    a.log("with every field", "ok", files=["src/a.py", "src/b.py"], tags=["feature"], metadata={"k": 1})
    local = [e for e in HistoryReader(str(a.history)).read_all() if e.intent == "with every field"][0]
    segment = a.segments()["machine-a/2026-10.md"]
    assert "**Machine:** machine-a" in segment
    parsed = [e for e in HistoryReader._parse(segment) if e.intent == "with every field"][0]
    assert parsed.machine == "machine-a"
    assert {**parsed.to_dict(), "machine": None} == local.to_dict()
    assert parsed.id == HistoryWriter._compute_entry_hash("with every field", "Agent: software_engineer", "ok")


def test_own_entries_of_another_checkout_are_merged(tmp_path, shared, clock):
    a, _ = shared
    clone = Box(tmp_path, "a-clone", a.machine.remote, machine=a.machine)
    clone.log("in the clone")
    a.log("in the first checkout")
    entries = a.read()
    assert [(e.intent, e.machine) for e in entries][:2] == [("in the first checkout", None),
                                                           ("in the clone", "machine-a")]
    assert "in the clone" not in [e.intent for e in a.read(machine="local")]
    assert "in the clone" in [e.intent for e in a.read(machine="machine-a")]
    assert len({e.id for e in entries}) == len(entries)


def test_parts_of_another_origin_are_skipped_and_reported(shared, clock):
    a, _ = shared
    stranger = a.machine.lib / SEGMENTS / "machine-z" / "2026-10.md"
    stranger.parent.mkdir(parents=True)
    stranger.write_text("---\nrepo: github.com/other/project\nmachine: machine-z\nmonth: 2026-10\n"
                        "format_version: 1\n---\n\n## 2026-10-05T09:30:00+00:00 | aaaaaaaaaaaa\n"
                        "**Intent:** not this repository\n**Action:** a\n**Outcome:** o\n"
                        "**Machine:** machine-z\n\n", encoding="utf-8")
    a.log("ours")
    assert "not this repository" not in [e.intent for e in a.read()]
    assert a.status()["history_other_origin"] == [{"key": KEY, "origin": NORMALIZED,
                                                   "found": ["github.com/other/project"]}]
    (a.machine.lib / "repos" / KEY / ".repo.json").write_text(json.dumps({"origin": "github.com/other/x"}))
    a.log("not exported while the group names another origin")
    assert "not exported while the group names another origin" not in a.exported()
    assert [e.machine for e in a.read()] == [None] * len(a.read())
    assert a.status()["history_other_origin"][0]["found"] == ["github.com/other/x"]
    a.export()  # nothing to approve: the key is approved
    history_sync.revoke(KEY, state_dir=a.machine.state, library=a.machine.lib)
    a.log("waiting again")
    preview = a.export()
    assert preview["status"] == "up_to_date" and preview["repositories"] == []
    assert {"key": KEY, "origin": NORMALIZED, "reason": "other_origin", "found": "github.com/other/x"} \
        in preview["notes"]


def test_a_library_that_is_gone_is_never_recreated(shared, clock, tmp_path):
    a, _ = shared
    a.log("x", settle=False)
    assert history_sync.drain(60)
    gone = tmp_path / "moved-away"
    result = history_sync.reconcile(a.repo, state_dir=a.machine.state, library=gone, full=True)
    assert result["status"] == "failed" and result["reason"] == "no_library"
    assert not gone.exists()


# --- reads and locks ------------------------------------------------------------------------------


def test_recency_reads_stop_once_the_limit_is_filled(two, clock, monkeypatch):
    a, b = two
    for month in range(1, 7):
        clock.now = datetime(2026, month, 10, 12, 0, tzinfo=timezone.utc)
        b.log(f"b in month {month}")
    clock.now = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    b.log("approval on b")
    a.log("approval on a")
    b.approve()
    a.approve()
    b.sync()
    a.sync()
    parsed = []
    real = journal.parsed_file

    def recording(path):
        parsed.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(journal, "parsed_file", recording)
    journal._PARSED = journal._ParsedFiles()
    newest = a.read(limit=1, machine="machine-b")
    assert [e.intent for e in newest] == ["approval on b"]
    assert "2026-01.md" not in parsed and "2026-02.md" not in parsed
    everything = a.read(limit=50, machine="machine-b")
    assert len(everything) == 7 and "2026-01.md" in parsed
    opened = []
    real_open = open

    def recording_open(file, *args, **kwargs):
        opened.append(Path(file).name)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(journal, "open", recording_open, raising=False)
    assert a.read(limit=50, machine="machine-b") == everything
    assert not [name for name in opened if name.startswith("2026-0")]  # unchanged parts come from the cache


def test_readers_wait_for_a_writer_or_rotation_in_progress(shared, clock):
    """Readers take the history lock shared: they never see a rotation half way (#169 lock, on Windows too)."""
    a, _ = shared
    entered, release = threading.Event(), threading.Event()

    def rotating():  # holds the history lock exclusively, as an append or a rotation does
        with file_lock(journal._sidecar(str(a.history))):
            entered.set()
            release.wait(10)

    holder = threading.Thread(target=rotating)
    holder.start()
    entered.wait(5)
    results = []
    reader = threading.Thread(target=lambda: results.append(a.read()))
    reader.start()
    reader.join(0.3)
    try:
        assert reader.is_alive() and results == []
    finally:
        release.set()
        holder.join(5)
        reader.join(5)
    assert [e.intent for e in results[0]][-1] == "approval on a"


def test_an_append_never_waits_for_a_held_library_lock(shared, clock):
    a, _ = shared
    held, release = threading.Event(), threading.Event()

    def sync_run():  # holds the library lock, as a sync cycle does for its local steps
        with file_lock(a.machine.lib / ".lock"):
            held.set()
            release.wait(30)

    holder = threading.Thread(target=sync_run)
    holder.start()
    held.wait(5)
    try:
        started = time.monotonic()
        assert a.log("while the library is locked", settle=False)["status"] == "recorded"
        assert time.monotonic() - started < 2
        assert "while the library is locked" in a.history.read_text(encoding="utf-8")
        assert not history_sync.drain(0.5)  # the worker retries the lock in the background
    finally:
        release.set()
        holder.join(5)
    assert history_sync.drain(60)
    assert a.exported()[-1] == "while the library is locked"


def test_a_lock_held_past_the_wait_is_left_to_the_next_run(shared, clock, monkeypatch):
    a, _ = shared
    monkeypatch.setattr(history_sync, "LOCK_WAIT_SECONDS", 0.3)
    with file_lock(a.machine.lib / ".lock"):  # a sync run that does not end soon
        a.log("while the library stays locked")
        error = a.status()["history_error"]
        assert error["reason"] == "library_busy" and error["key"] == KEY
        assert "while the library stays locked" not in a.exported()
    a.log("after the lock was released")
    assert a.exported()[-2:] == ["while the library stays locked", "after the lock was released"]
    assert a.status()["history_error"] is None


def test_appends_of_one_checkout_coalesce_in_the_worker(shared, clock, monkeypatch):
    a, _ = shared
    runs = []
    real = history_sync.reconcile
    monkeypatch.setattr(history_sync, "reconcile", lambda root, **options: runs.append(root) or real(root, **options))
    held, release = threading.Event(), threading.Event()

    def sync_run():
        with file_lock(a.machine.lib / ".lock"):
            held.set()
            release.wait(30)

    holder = threading.Thread(target=sync_run)
    holder.start()
    held.wait(5)
    try:
        for number in range(5):
            a.log(f"turn {number}", settle=False)
    finally:
        release.set()
        holder.join(5)
    assert history_sync.drain(60)
    assert len(runs) <= 2  # the first run, and one for everything marked while it waited
    assert a.exported()[-5:] == [f"turn {number}" for number in range(5)]


def _numpy_available() -> bool:
    try:
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _numpy_available(), reason="numpy unavailable in this env")
@pytest.mark.parametrize("merged", [False, True])
def test_a_read_and_an_append_during_an_index_rebuild_do_not_wait(shared, clock, tmp_path, monkeypatch, merged):
    import src.engine.embedder as embedder
    from tests.test_history import FakeEmbedder

    monkeypatch.setattr(embedder, "_model_fingerprint", None)
    a, _ = shared
    a.log("alpha")
    fake = FakeEmbedder(["alpha", "beta"])
    embedding, release = threading.Event(), threading.Event()

    def slow(texts):
        embedding.set()
        release.wait(10)
        return fake.embed_texts(texts)

    def search():
        previous = journal.set_sync(a.integration() if merged else None)
        try:
            HistoryStore(str(a.history), str(tmp_path / "index")).search(
                "alpha", embed_query=fake.embed_query, embed_texts=slow)
        finally:
            journal.set_sync(previous)

    searcher = threading.Thread(target=search)
    searcher.start()
    try:
        assert embedding.wait(10)
        started = time.monotonic()
        assert HistoryReader(str(a.history)).read_recent()[0].intent == "alpha"
        assert a.writer.append_entry("beta", "a", "o")["status"] == "recorded"
        assert time.monotonic() - started < 2
    finally:
        release.set()
        searcher.join(10)


@pytest.mark.skipif(not _numpy_available(), reason="numpy unavailable in this env")
def test_the_index_covers_every_machine_and_embeds_only_new_entries(shared, clock, tmp_path, monkeypatch):
    import src.engine.embedder as embedder
    from tests.test_history import FakeEmbedder

    monkeypatch.setattr(embedder, "_model_fingerprint", None)  # as a process that has not loaded a model
    a, b = shared
    fake = FakeEmbedder(["alpha", "beta", "gamma", "delta", "approval"])
    embedded = []

    def embed_texts(texts):
        embedded.append(list(texts))
        return fake.embed_texts(texts)

    def search(text, **options):
        with a.active():
            store = HistoryStore(str(a.history), str(tmp_path / "index"))
            return store.search(text, limit=5, embed_query=fake.embed_query, embed_texts=embed_texts, **options)

    a.log("alpha")
    b.log("beta")
    b.log("gamma")
    b.sync()
    a.sync()
    assert search("beta")[0]["intent"] == "beta" and search("beta")[0]["machine"] == "machine-b"
    assert [len(batch) for batch in embedded] == [5]
    embedded.clear()
    assert search("alpha") and embedded == []  # nothing changed: no embedding at all

    b.log("delta")
    b.sync()
    a.sync()
    assert search("delta")[0]["intent"] == "delta"
    assert embedded == [["Intent: delta\nAction: Agent: software_engineer\nOutcome: done"]]
    assert {r["intent"] for r in search("beta", machine="machine-b")} == {"approval on b", "beta", "gamma", "delta"}
    assert {r["intent"] for r in search("alpha", machine="local")} == {"alpha", "approval on a"}


# --- the MCP server ---------------------------------------------------------------------------


def test_log_interaction_never_waits_for_the_library_lock(tmp_path, shared, clock, monkeypatch):
    pytest.importorskip("mcp")
    import asyncio

    import src.server as server
    from src.engine import config as engine_config

    a, _ = shared
    plain = Box(tmp_path, "plain", a.machine.remote, origin=None, machine=a.machine)
    monkeypatch.setattr(server, "is_langfuse_configured", lambda: False)
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
    held, release = threading.Event(), threading.Event()

    def sync_run():
        with file_lock(a.machine.lib / ".lock"):
            held.set()
            release.wait(30)

    holder = threading.Thread(target=sync_run)
    holder.start()
    held.wait(5)
    previous = journal.set_sync(a.integration())
    try:
        for workspace, query in ((a.repo, "shared turn"), (plain.repo, "unrelated turn")):
            monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(workspace))
            engine_config._reset_client_repo_root_cache()
            asyncio.run(server.log_interaction("software_engineer", query, "answer"))
        assert server._history_worker.drain(time.monotonic() + 5)  # both journal writes, lock still held
        assert "unrelated turn" in plain.history.read_text(encoding="utf-8")
        assert "shared turn" in a.history.read_text(encoding="utf-8")
        started = time.monotonic()
        server.drain_pending_logs(10)
        assert time.monotonic() - started < server.HISTORY_SYNC_DRAIN_SECONDS + 1  # briefly
    finally:
        engine_config._reset_client_repo_root_cache()
        release.set()
        holder.join(5)
        assert history_sync.drain(60)
        journal.set_sync(previous)
    assert a.exported()[-1] == "shared turn"


def test_read_history_merges_and_filters_through_the_server(shared, clock, monkeypatch):
    pytest.importorskip("mcp")
    import asyncio

    import src.server as server
    from src.engine import config as engine_config

    a, b = shared
    a.log("mine")
    b.log("theirs")
    b.sync()
    a.sync()
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(a.repo))
    engine_config._reset_client_repo_root_cache()
    try:
        with a.active():
            merged = json.loads(asyncio.run(server.read_history()))
            theirs = json.loads(asyncio.run(server.read_history(machine="machine-b")))
        without = json.loads(asyncio.run(server.read_history()))
    finally:
        engine_config._reset_client_repo_root_cache()
    assert [(e["intent"], e["machine"]) for e in merged["entries"]][:2] == [("theirs", "machine-b"), ("mine", None)]
    assert [e["intent"] for e in theirs["entries"]] == ["theirs", "approval on b"]
    assert [(e["intent"], e["machine"]) for e in without["entries"]] == [("mine", None), ("approval on a", None)]


def test_servers_install_the_integration_and_catch_up_at_startup(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    import src.server as server

    monkeypatch.setattr(engine_module, "default_state_dir", lambda: tmp_path / "state")  # never the real one
    calls = []
    real = history_sync.reconcile_all
    monkeypatch.setattr(history_sync, "reconcile_all", lambda **options: calls.append(options) or real(**options))
    server.install_history_sync()
    try:
        assert isinstance(journal._sync, history_sync.Integration)
        assert history_sync.drain(10) and calls  # the startup catch-up ran in the worker
    finally:
        journal.set_sync(None)
