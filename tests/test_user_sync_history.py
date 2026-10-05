"""Each repository's history merged across machines (#172).

Two libraries and two clones of one project (same ``origin``) act as two machines, synced through a
local bare repository by the real engine.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import threading
import types

import pytest

from src.file_lock import file_lock
from src.memory import history as journal
from src.memory.history import HistoryReader, HistoryStore, HistoryWriter
from src.user_flows import FlowLibrary
from src.user_sync import history as history_sync
from src.user_sync.__main__ import main as cli
from tests.test_user_sync import Machine, plain_git, remote, remote_files, remote_objects  # noqa: F401 (fixture)

ORIGIN = "git@github.com:me/project.git"
KEY = "github.com-me-project"
SEGMENTS = f"repos/{KEY}/history"


class Box:
    """One machine: its synced library and its clone of the project."""

    def __init__(self, root: Path, name: str, remote_path: Path, origin: str | None = ORIGIN):
        self.machine = Machine(root, name, remote_path)
        self.label = f"machine-{name}"
        self.repo = root / name / "project"
        self.repo.mkdir(parents=True)
        plain_git("init", "--quiet", str(self.repo))
        if origin:
            plain_git("-C", str(self.repo), "remote", "add", "origin", origin)
        self.history = self.repo / "history.md"
        self.writer = HistoryWriter(str(self.history), str(self.repo / "history"))

    @contextmanager
    def active(self):
        """This machine's sync integration, as its MCP server installs it at startup."""
        previous = journal.set_sync(history_sync.Integration(state_dir=self.machine.state,
                                                             library=self.machine.lib))
        try:
            yield
        finally:
            journal.set_sync(previous)

    def log(self, intent: str, outcome: str = "done", **options) -> dict:
        with self.active():
            return self.writer.append_entry(intent, "Agent: software_engineer", outcome, **options)

    def read(self, **options) -> list:
        with self.active():
            return HistoryReader(str(self.history)).read_recent(**options)

    def segments(self) -> dict[str, str]:
        base = self.machine.lib / SEGMENTS
        if not base.exists():
            return {}
        return {path.relative_to(base).as_posix(): path.read_text(encoding="utf-8")
                for path in sorted(base.rglob("*.md"))}

    def exported(self) -> str:
        return "".join(text for name, text in self.segments().items() if name.startswith(self.label + "/"))

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
def no_integration():
    """Every test starts and ends without an installed sync integration."""
    previous = journal.set_sync(None)
    yield
    journal.set_sync(previous)


@pytest.fixture
def two(tmp_path, remote):
    a, b = Box(tmp_path, "a", remote), Box(tmp_path, "b", remote)
    a.machine.save("user:shared", "# Shared\n")
    a.machine.connect()
    b.machine.connect()
    return a, b


def run_cli(box: Box, *arguments) -> int:
    return cli(["--state", str(box.machine.state), "--library", str(box.machine.lib), *arguments, "--json"])


# --- the acceptance: two machines, one repository ----------------------------------------------


def test_two_machines_see_each_others_entries_once_and_in_time_order(two, clock):
    a, b = two
    first = a.log("first on a")
    b.log("first on b")
    a.log("second on a")
    # B also saves a repository flow: its .repo.json and the one the export wrote are the same bytes.
    FlowLibrary(user_dir=b.machine.lib, repo_root=b.repo).save("repo:deploy", "# Deploy\n")
    for box in (a, b, a, b):
        box.sync()

    for box, own in ((a, "a"), (b, "b")):
        entries = box.read(limit=10)
        assert [e.intent for e in entries] == ["second on a", "first on b", "first on a"]
        assert len({e.id for e in entries}) == 3
        assert [e.machine for e in entries] == [None if own == "a" else "machine-a",
                                                None if own == "b" else "machine-b",
                                                None if own == "a" else "machine-a"]
    # The content hash is the same on every machine.
    assert b.read(limit=10)[-1].id == first["entry_id"]
    assert a.machine.sync.conflicts() == [] and b.machine.sync.conflicts() == []
    files = remote_files(a.machine.remote)
    assert f"{SEGMENTS}/machine-a/2026-10.md" in files and f"{SEGMENTS}/machine-b/2026-10.md" in files
    assert json.loads(files[f"repos/{KEY}/.repo.json"]) == {"origin": "github.com/me/project"}
    # history.md stays this machine's journal: nothing from the other machine is written into it.
    assert "first on b" not in a.history.read_text(encoding="utf-8")


def test_an_entry_on_both_machines_appears_once_and_the_local_copy_wins(two, clock):
    a, b = two
    a.log("same question", "same answer")
    clock.tick(minutes=5)
    b.log("same question", "same answer")
    for box in (a, b, a):
        box.sync()
    on_a, on_b = a.read(), b.read()
    assert len(on_a) == len(on_b) == 1
    assert on_a[0].machine is None and on_b[0].machine is None
    assert on_a[0].timestamp < on_b[0].timestamp  # each machine shows its own copy


def test_the_machine_filter(two, clock):
    a, b = two
    a.log("mine")
    b.log("theirs")
    b.sync()
    a.sync()
    assert [e.intent for e in a.read(machine="local")] == ["mine"]
    assert [e.intent for e in a.read(machine="machine-a")] == ["mine"]  # this machine's own label
    assert [e.intent for e in a.read(machine="machine-b")] == ["theirs"]
    assert a.read(machine="machine-c") == []
    assert [e.intent for e in a.read(machine="machine-b", since="2026-10-05T09:00:01")] == ["theirs"]


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


# --- when entries are shared ------------------------------------------------------------------


def test_export_needs_a_started_and_unpaused_sync(tmp_path, remote, clock):
    a = Box(tmp_path, "a", remote)
    a.log("before setup")
    a.machine.sync.setup(remote=str(remote), name="Owner", email="owner@example.com", label=a.label)
    a.log("before start")
    assert a.segments() == {}
    a.machine.connect()
    a.log("after start")
    a.machine.sync.pause()
    a.log("while paused")
    a.machine.sync.resume()
    a.log("after resume")
    assert [entry.intent for entry, _ in journal.entry_blocks(a.exported())] == ["after start", "after resume"]
    # history.md has every entry.
    assert len(HistoryReader(str(a.history)).read_all()) == 5


def test_a_repository_without_origin_keeps_its_history_local(tmp_path, remote, clock):
    a = Box(tmp_path, "a", remote, origin=None)
    a.machine.save("user:shared", "# Shared\n")
    a.machine.connect()
    assert a.log("local only")["status"] == "recorded"
    assert not (a.machine.lib / "repos").exists()
    a.sync()
    assert b"local only" not in remote_objects(a.machine.remote)
    assert [e.machine for e in a.read()] == [None]


@pytest.mark.parametrize("group", [f"repos/{KEY}", "history"])
def test_excluded_groups_are_not_exported(two, clock, group):
    a, b = two
    a.log("shared before the exclusion")
    a.sync()
    b.sync()
    assert a.machine.sync.change_scopes(exclude=[group])["status"] == "saved"
    a.log("private after the exclusion")
    a.sync()
    b.sync()
    assert "private after the exclusion" not in a.exported()
    assert b"private after the exclusion" not in remote_objects(a.machine.remote)
    assert [e.intent for e in b.read()] == ["shared before the exclusion"]


def test_an_entry_with_credentials_stays_on_this_machine(two, clock):
    a, b = two
    a.log("connect to the database", "used password=hunter2 for it")
    a.log("an ordinary entry")
    result = a.sync()
    assert result["reason"] != "secret"  # the segment itself stays clean and keeps syncing
    assert "hunter2" in a.history.read_text(encoding="utf-8")
    assert b"hunter2" not in remote_objects(a.machine.remote)
    b.sync()
    assert [e.intent for e in b.read()] == ["an ordinary entry"]


# --- segments ---------------------------------------------------------------------------------


def _block(number: int, size: int, month: str = "2026-10") -> str:
    return HistoryWriter._render_entry(f"{number:012x}", f"{month}-05T09:00:{number:02d}+00:00",
                                       f"entry {number}", "a", "y" * size, None, None, None)


def test_a_month_continues_in_a_new_part_before_4_mib(two):
    a, _ = two
    assert history_sync.PART_BYTES == 4 * 1024 * 1024
    for number in range(5):  # about 1 MiB each: three fit in a part, two continue in the next
        assert history_sync.export_entry(a.repo, _block(number, 1024 * 1024), state_dir=a.machine.state,
                                         library=a.machine.lib)["status"] == "exported"
    parts = a.segments()
    assert list(parts) == ["machine-a/2026-10-2.md", "machine-a/2026-10.md"]
    for text in parts.values():
        assert len(text.encode("utf-8")) <= history_sync.PART_BYTES
    assert [len(journal.entry_blocks(parts[name])) for name in ("machine-a/2026-10.md", "machine-a/2026-10-2.md")] == [3, 2]


def test_parts_and_months_sync_and_merge_without_conflicts(two, clock, monkeypatch):
    a, b = two
    monkeypatch.setattr(history_sync, "PART_BYTES", 1200)
    for number in range(4):
        a.log(f"october {number}", "z" * 300)
    clock.now = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
    a.log("november")
    b.log("on b in november")
    for box in (a, b, a, b):
        box.sync()
    assert sorted(a.segments()) == ["machine-a/2026-10-2.md", "machine-a/2026-10.md", "machine-a/2026-11.md",
                                    "machine-b/2026-11.md"]
    assert len(b.read(limit=50)) == 6
    assert a.machine.sync.conflicts() == [] and b.machine.sync.conflicts() == []
    # A repeated export of an entry already in its month's parts adds nothing.
    block = journal.entry_blocks(a.history.read_text(encoding="utf-8"))[0][1]
    assert history_sync.export_entry(a.repo, block, state_dir=a.machine.state,
                                     library=a.machine.lib)["status"] == "unchanged"


def test_the_segment_block_keeps_the_hash_and_the_parser_reads_the_machine(two, clock):
    a, _ = two
    a.log("with every field", "ok", files=["src/a.py", "src/b.py"], tags=["feature"], metadata={"k": 1})
    local = HistoryReader(str(a.history)).read_all()[0]
    segment = a.exported()
    assert "**Machine:** machine-a" in segment
    parsed = HistoryReader._parse(segment)[0]
    assert parsed.machine == "machine-a"
    assert {**parsed.to_dict(), "machine": None} == local.to_dict()
    assert parsed.id == HistoryWriter._compute_entry_hash("with every field", "Agent: software_engineer", "ok")


# --- failures and locks -------------------------------------------------------------------------


def test_export_failures_never_fail_the_writer(two, clock, monkeypatch, caplog):
    a, _ = two
    write = history_sync._write

    def broken(repo, items):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(history_sync, "_write", broken)
    with caplog.at_level(logging.WARNING):
        assert a.log("first")["status"] == "recorded"
        assert a.log("second")["status"] == "recorded"
    assert len([r for r in caplog.records if "Could not share a history entry" in r.getMessage()]) == 1
    monkeypatch.setattr(history_sync, "_write", write)

    class Raising:
        def exported(self, history_path, block):
            raise RuntimeError("boom")

        def machines(self, history_path):
            raise RuntimeError("boom")

    previous = journal.set_sync(Raising())
    try:
        assert a.writer.append_entry("third", "a", "o")["status"] == "recorded"
        assert [e.intent for e in HistoryReader(str(a.history)).read_recent()][0] == "third"
    finally:
        journal.set_sync(previous)
    (a.machine.lib / ".agents-sync").mkdir(exist_ok=True)
    (a.machine.lib / ".agents-sync" / "scopes.json").write_text("{broken", encoding="utf-8")
    assert a.log("fourth")["status"] == "recorded"
    assert "fourth" not in a.exported()
    assert len(HistoryReader(str(a.history)).read_all()) == 4


def test_the_history_lock_is_free_while_the_export_waits_for_the_library(two, clock):
    a, _ = two
    held, release = threading.Event(), threading.Event()

    def sync_run():  # holds the library lock, as a sync cycle does for its local steps
        with file_lock(a.machine.lib / ".lock"):
            held.set()
            release.wait(10)

    holder = threading.Thread(target=sync_run)
    holder.start()
    held.wait(5)
    results = []
    writer = threading.Thread(target=lambda: results.append(a.log("while syncing")))
    writer.start()
    try:
        for _ in range(500):  # the entry reaches history.md before the export waits
            if a.history.exists() and "while syncing" in a.history.read_text(encoding="utf-8"):
                break
            release.wait(0.01)
        writer.join(0.2)
        assert writer.is_alive()  # the export waits for the library lock ...
        with file_lock(journal._sidecar(str(a.history)), blocking=False):
            pass  # ... without holding the history lock
        assert journal._WRITE_LOCK.acquire(blocking=False)
        journal._WRITE_LOCK.release()
        assert a.read()[0].intent == "while syncing"
    finally:
        release.set()
        holder.join(5)
        writer.join(5)
    assert results[0]["status"] == "recorded"
    assert "while syncing" in a.exported()


@pytest.mark.parametrize("synced", [False, True])
def test_readers_wait_for_a_writer_or_rotation_in_progress(two, clock, synced):
    """Readers take the history lock shared: they never see a rotation half way (#169 lock, on Windows too)."""
    a, _ = two
    a.writer.append_entry("written before", "a", "o")
    entered, release = threading.Event(), threading.Event()

    def rotating():  # holds the history lock exclusively, as an append or a rotation does
        with file_lock(journal._sidecar(str(a.history))):
            entered.set()
            release.wait(10)

    holder = threading.Thread(target=rotating)
    holder.start()
    entered.wait(5)
    results = []
    reader = threading.Thread(target=lambda: results.append(a.read() if synced else
                                                            HistoryReader(str(a.history)).read_recent()))
    reader.start()
    reader.join(0.3)
    try:
        assert reader.is_alive() and results == []
    finally:
        release.set()
        holder.join(5)
        reader.join(5)
    assert [e.intent for e in results[0]] == ["written before"]


def test_a_library_that_is_gone_is_never_recreated(two, clock, tmp_path):
    a, _ = two
    gone = tmp_path / "moved-away"
    assert history_sync.export_entry(a.repo, _block(1, 10), state_dir=a.machine.state,
                                     library=gone)["status"] == "skipped"
    assert not gone.exists()


# --- earlier entries --------------------------------------------------------------------------


def test_backfill_previews_then_adds_the_earlier_entries(tmp_path, remote, clock, capsys):
    a, b = Box(tmp_path, "a", remote), Box(tmp_path, "b", remote)
    clock.now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    early = HistoryWriter(str(a.history), str(a.repo / "history"), rotation_kb=1)
    for number in range(3):
        early.append_entry(f"september {number} " + "x" * 400, "a", "o" * 400)
    clock.now = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    early.append_entry("october early", "a", "o")
    early.append_entry("with a secret", "a", "export PGPASSWORD=s3cr3t")
    assert list((a.repo / "history").glob("2026-*.md"))  # September was rotated into an archive

    assert run_cli(a, "history", "export", "--repo", str(a.repo)) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "not_set_up"
    a.machine.save("user:shared", "# Shared\n")
    a.machine.connect()
    b.machine.connect()
    a.log("live after start")

    assert cli(["--state", str(a.machine.state), "--library", str(a.machine.lib),
                "history", "export", "--repo", str(a.repo)]) == 0
    shown = capsys.readouterr().out
    assert "history: confirmation_needed" in shown and "2026-09: 3 entries" in shown
    assert "1 entries stay on this machine (secret)" in shown and "confirm with: --confirm " in shown
    assert run_cli(a, "history", "export", "--repo", str(a.repo)) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "confirmation_needed"
    assert preview["entries"] == 4 and preview["present"] == 1
    assert preview["months"] == [{"month": "2026-09", "entries": 3}, {"month": "2026-10", "entries": 1}]
    assert preview["skipped"] == {"secret": 1}
    assert "october early" not in a.exported()

    assert run_cli(a, "history", "export", "--repo", str(a.repo), "--confirm", "0" * 64) == 0
    assert json.loads(capsys.readouterr().out)["message"].startswith("the preview changed")
    assert "october early" not in a.exported()

    assert run_cli(a, "history", "export", "--repo", str(a.repo), "--confirm", preview["hash"]) == 0
    done = json.loads(capsys.readouterr().out)
    assert done["status"] == "exported" and done["entries"] == 4
    assert sorted(a.segments()) == ["machine-a/2026-09.md", "machine-a/2026-10.md"]
    assert run_cli(a, "history", "export", "--repo", str(a.repo)) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "up_to_date"

    a.sync()
    b.sync()
    seen = [e.intent.split(" x")[0] for e in b.read(limit=50)]
    assert seen == ["live after start", "october early", "september 2", "september 1", "september 0"]
    assert b"s3cr3t" not in remote_objects(remote)

    assert cli(["--state", str(a.machine.state), "--library", str(a.machine.lib),
                "history", "export", "--repo", str(a.repo)]) == 0
    assert "up_to_date" in capsys.readouterr().out


def test_backfill_explains_why_it_cannot_run(tmp_path, remote, capsys):
    a = Box(tmp_path, "a", remote, origin=None)
    a.writer.append_entry("x", "a", "o")
    a.machine.save("user:shared", "# Shared\n")
    a.machine.connect()
    assert run_cli(a, "history", "export", "--repo", str(a.repo)) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "no_origin"
    plain_git("-C", str(a.repo), "remote", "add", "origin", ORIGIN)
    a.machine.sync.pause()
    assert run_cli(a, "history", "export", "--repo", str(a.repo)) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "paused"
    a.machine.sync.resume()
    a.machine.sync.change_scopes(exclude=["history"])
    assert run_cli(a, "history", "export", "--repo", str(a.repo)) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "excluded"
    assert run_cli(a, "history", "export", "--repo", str(tmp_path)) == 1
    assert json.loads(capsys.readouterr().out)["reason"] in ("no_origin", "invalid")


# --- the index --------------------------------------------------------------------------------


def _numpy_available() -> bool:
    try:
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _numpy_available(), reason="numpy unavailable in this env")
def test_the_index_covers_every_machine_and_embeds_only_new_entries(two, clock, tmp_path, monkeypatch):
    import src.engine.embedder as embedder
    from tests.test_history import FakeEmbedder

    monkeypatch.setattr(embedder, "_model_fingerprint", None)  # as a process that has not loaded a model
    a, b = two
    fake = FakeEmbedder(["alpha", "beta", "gamma", "delta"])
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
    assert [len(batch) for batch in embedded] == [3]
    embedded.clear()
    assert search("alpha") and embedded == []  # nothing changed: no embedding at all

    b.log("delta")
    b.sync()
    a.sync()
    assert search("delta")[0]["intent"] == "delta"
    assert embedded == [["Intent: delta\nAction: Agent: software_engineer\nOutcome: done"]]
    assert {r["intent"] for r in search("alpha", machine="machine-b")} == {"beta", "gamma", "delta"}
    assert [r["intent"] for r in search("alpha", machine="local")] == ["alpha"]


# --- the MCP server ---------------------------------------------------------------------------


def test_read_history_merges_and_filters_through_the_server(two, clock, monkeypatch):
    pytest.importorskip("mcp")
    import asyncio

    import src.server as server
    from src.engine import config as engine_config

    assert journal._sync is None  # importing the server installs nothing
    a, b = two
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
    assert [(e["intent"], e["machine"]) for e in merged["entries"]] == [("theirs", "machine-b"), ("mine", None)]
    assert [e["intent"] for e in theirs["entries"]] == ["theirs"]
    assert [(e["intent"], e["machine"]) for e in without["entries"]] == [("mine", None)]


def test_servers_install_the_integration_at_startup(monkeypatch):
    pytest.importorskip("mcp")
    import src.server as server

    server.install_history_sync()
    try:
        assert isinstance(journal._sync, history_sync.Integration)
    finally:
        journal.set_sync(None)
