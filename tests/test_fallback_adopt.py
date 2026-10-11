"""Entries of the fallback journal move into a project only when the user says so (#292)."""
import json
from pathlib import Path
import sys

import pytest

from src.daemon.state import atomic_private, write_json
from src.memory import fallback
from src.memory.history import HistoryReader, HistoryWriter


def kept(root, intent, claimed, outcome="done"):
    writer = HistoryWriter(str(root / "history.md"), str(root / "history"))
    return writer.append_entry(intent, "Agent: software_engineer", outcome,
                               metadata={"unresolved": "workspace_required", "claimed_workspace": claimed})


def intents(path):
    return sorted(entry.intent for entry in HistoryReader(str(path / "history.md"), str(path / "history")).journal_entries())


def test_append_existing_keeps_id_time_and_metadata_and_adds_it_once(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    kept(source, "question", "/work/one")
    [entry] = HistoryReader(str(source / "history.md")).journal_entries()
    writer = HistoryWriter(str(target / "history.md"), str(target / "history"))

    assert writer.append_existing(entry)["status"] == "recorded"
    assert writer.append_existing(entry)["status"] == "duplicate"

    [copy] = HistoryReader(str(target / "history.md")).journal_entries()
    assert (copy.id, copy.timestamp, copy.intent, copy.metadata) == (entry.id, entry.timestamp, entry.intent, entry.metadata)


def test_adopt_moves_one_claimed_directory_and_leaves_the_rest(tmp_path, fallback_journal):
    project = tmp_path / "project"; project.mkdir()
    kept(fallback_journal, "first", "/work/one")
    kept(fallback_journal, "other", "/work/two")
    kept(fallback_journal, "second", "/work/one")

    preview = fallback.adopt(project, claimed="/work/one", dry_run=True)
    assert sorted(item["intent"] for item in preview["moved"]) == ["first", "second"]
    assert not (project / "history.md").exists()

    result = fallback.adopt(project, claimed="/work/one/")  # the claim matches as it was normalized
    assert sorted(item["intent"] for item in result["moved"]) == ["first", "second"]
    assert intents(project) == ["first", "second"]
    assert intents(fallback_journal) == ["other"]
    assert fallback.adopt(project, claimed="/work/one")["moved"] == []  # nothing left: idempotent


def test_adopt_by_ids_reports_what_it_did_not_find(tmp_path, fallback_journal):
    project = tmp_path / "project"; project.mkdir()
    entry_id = kept(fallback_journal, "chosen", "/work/one")["entry_id"]
    kept(fallback_journal, "not chosen", "/work/one")

    result = fallback.adopt(project, entry_ids=[entry_id, "0123456789ab"])

    assert [item["intent"] for item in result["moved"]] == ["chosen"] and result["missing"] == ["0123456789ab"]
    assert intents(project) == ["chosen"] and intents(fallback_journal) == ["not chosen"]


def test_an_entry_the_project_already_holds_still_leaves_the_journal(tmp_path, fallback_journal):
    project = tmp_path / "project"; project.mkdir()
    kept(fallback_journal, "question", "/work/one")
    [entry] = HistoryReader(str(fallback_journal / "history.md")).journal_entries()
    HistoryWriter(str(project / "history.md")).append_existing(entry)

    result = fallback.adopt(project, claimed="/work/one")

    assert result["already"][0]["id"] == entry.id and result["moved"] == []
    assert intents(project) == ["question"] and intents(fallback_journal) == []


def test_adopt_needs_a_selection(tmp_path, fallback_journal):
    with pytest.raises(ValueError):
        fallback.adopt(tmp_path, claimed=None)


def test_the_groups_list_each_claimed_directory(fallback_journal):
    kept(fallback_journal, "a", "/work/one")
    kept(fallback_journal, "b", "/work/one")
    kept(fallback_journal, "c", "/work/two")
    groups = {group["claimed"]: group["count"] for group in fallback.unresolved_groups(fallback_journal)}
    assert groups == {"/work/one": 2, "/work/two": 1}


@pytest.fixture
def service(tmp_path, monkeypatch):
    state = tmp_path / "service"; state.mkdir(mode=0o700)
    write_json(state / "service.json", {"installation": str(tmp_path / "install"), "port": 8765, "node": sys.executable})
    atomic_private(state / "token", "t" * 48)
    return state


def test_the_history_commands(service, tmp_path, capsys):
    from src.daemon.control import main
    journal = service / "unresolved"
    kept(journal, "question", "/work/one")
    project = tmp_path / "project"; project.mkdir()

    main(["--state", str(service), "history", "unresolved"])
    listed = json.loads(capsys.readouterr().out)
    assert [(group["claimed"], group["count"]) for group in listed["groups"]] == [("/work/one", 1)]

    main(["--state", str(service), "history", "adopt", "--to", str(project), "--claimed", "/work/one", "--dry-run"])
    assert [item["intent"] for item in json.loads(capsys.readouterr().out)["moved"]] == ["question"]
    registry = service / "workspaces.json"
    assert not (project / "history.md").exists()
    assert not registry.exists() or str(project.resolve()) not in json.loads(registry.read_text()).values()

    main(["--state", str(service), "history", "adopt", "--to", str(project), "--claimed", "/work/one"])
    assert [item["intent"] for item in json.loads(capsys.readouterr().out)["moved"]] == ["question"]
    assert intents(project) == ["question"] and intents(journal) == []
    assert str(project.resolve()) in json.loads((service / "workspaces.json").read_text()).values()

    with pytest.raises(SystemExit):
        main(["--state", str(service), "history", "adopt", "--to", str(project)])



def test_the_journal_is_no_project_to_move_into(tmp_path, fallback_journal):
    """Moving into the journal itself would delete the entries it holds."""
    kept(fallback_journal, "question", "/work/one")
    with pytest.raises(ValueError, match="no project"):
        fallback.adopt(fallback_journal, claimed="/work/one")
    assert intents(fallback_journal) == ["question"]


def test_a_claim_that_is_no_absolute_directory_is_an_error(tmp_path, fallback_journal):
    project = tmp_path / "project"; project.mkdir()
    with pytest.raises(ValueError, match="no absolute directory"):
        fallback.adopt(project, claimed="relative/dir")


def test_an_old_entry_moved_in_does_not_name_the_archive(tmp_path, fallback_journal, monkeypatch):
    """An archive of month M must hold nothing later than M, whatever order entries arrive in."""
    import datetime
    import types
    from src.memory import history
    project = tmp_path / "project"; project.mkdir()
    moments = iter([datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)] +
                   [datetime.datetime(2026, 10, day, tzinfo=datetime.timezone.utc) for day in range(1, 20)])
    monkeypatch.setattr(history, "_dt", types.SimpleNamespace(
        datetime=types.SimpleNamespace(now=lambda tz=None: next(moments)), timezone=datetime.timezone))
    kept(fallback_journal, "from march", "/work/one")
    writer = HistoryWriter(str(project / "history.md"), str(project / "history"), rotation_kb=1)
    for day in range(3):
        writer.append_entry(f"october {day} " + "x" * 400, "Agent: lawyer", "o" * 400)
    [old] = HistoryReader(str(fallback_journal / "history.md")).journal_entries()
    HistoryWriter(str(project / "history.md"), str(project / "history"), rotation_kb=1).append_existing(old)
    archives = sorted(path.name for path in (project / "history").glob("*.md"))
    assert archives and all(name.startswith("2026-10") for name in archives), archives


def test_the_command_line_tells_sync_what_moved(service, tmp_path, monkeypatch, capsys):
    """A move from the command line leaves what this machine shared, as one in a server does."""
    from src.daemon.control import main
    from src.user_sync import history as history_sync
    heard = []

    class Integration:
        def appended(self, path, entry_id=None):
            heard.append(("appended", Path(path).parent.name, entry_id))

        def deleted(self, path, entry_id, timestamp):
            heard.append(("deleted", Path(path).parent.name, entry_id))

        def machines(self, path):
            return None
    monkeypatch.setattr(history_sync, "Integration", Integration)
    entry_id = kept(service / "unresolved", "question", "/work/one")["entry_id"]
    project = tmp_path / "project"; project.mkdir()

    main(["--state", str(service), "history", "adopt", "--to", str(project), "--entry", entry_id])

    assert ("appended", "project", entry_id) in heard and ("deleted", "unresolved", entry_id) in heard
