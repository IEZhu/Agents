"""read_history keeps its listings under Claude Code's 50,000-character limit, pages through
what a listing left out, and returns one entry whole by its id (#212)."""
from __future__ import annotations

import json
import os

import pytest

from src.engine import config as engine_config
from src.memory.history import HistoryEntry, HistoryReader, HistoryStore, HistoryWriter, MachineHistory, set_sync
from src.memory.history_results import LIST_ITEMS, RESULT_BUDGET, TEXTS, listing, preview, to_json
from src.result_size import INLINE_LIMIT, shown_size

# Each character class Claude Code's second JSON escape treats differently: Cyrillic, quotes,
# backslashes and plain ASCII.
SAMPLE = 'Ответ "с кавычками" и C:\\путь\\к\\файлу; plain words follow. '


def long_text(index: int, size: int) -> str:
    return (f"{index:03d} " + SAMPLE * (size // len(SAMPLE) + 1))[:size]


@pytest.fixture
def client_root(tmp_path, monkeypatch):
    root = tmp_path / "client_repo"
    root.mkdir()
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(root))
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)  # over HTTP, memory needs a registered workspace
    engine_config._reset_client_repo_root_cache()
    yield root.resolve()
    engine_config._reset_client_repo_root_cache()


def write_entries(root, count: int, intent: int = 1_200, outcome: int = 4_000) -> dict:
    """``count`` entries with long texts; returns (intent, outcome) per entry id."""
    writer = HistoryWriter(str(root / "history.md"), str(root / "history"), rotation_kb=100_000)
    texts = {}
    for index in range(count):
        texts_of = long_text(index, intent), long_text(index, outcome)
        texts[writer.append_entry(texts_of[0], f"Agent: software_engineer {index}", texts_of[1])["entry_id"]] = texts_of
    return texts


@pytest.fixture
def long_history(client_root):
    """Forty entries whose texts alone pass the limit twice over."""
    texts = write_entries(client_root, 40)
    assert sum(len(intent) + len(outcome) for intent, outcome in texts.values()) > 2 * INLINE_LIMIT
    return texts


@pytest.mark.asyncio
async def test_a_default_listing_of_a_long_history_stays_inline(long_history):
    import src.server as server

    text = await server.read_history()
    assert shown_size(text) < INLINE_LIMIT and len(text) < INLINE_LIMIT
    result = json.loads(text)
    assert result["mode"] == "recency" and result["total"] == 20 and "omitted" not in result
    assert "entry_id" in result["instruction"]
    for entry in result["entries"]:
        intent, outcome = long_history[entry["id"]]
        assert entry["truncated"] == {"outcome": len(outcome), "intent": len(intent)}
        assert entry["outcome"].endswith("…") and outcome.startswith(entry["outcome"][:-1])
        assert len(entry["outcome"]) <= TEXTS["outcome"] and len(entry["intent"]) <= TEXTS["intent"]


@pytest.mark.asyncio
async def test_claude_code_counts_the_structured_result_this_listing_was_sized_for(long_history):
    import src.server as server

    # Claude Code shows JSON.stringify(structuredContent) and compares its length with the limit.
    content, structured = await server.mcp.call_tool("read_history", {})
    assert json.loads(content[0].text)["total"] == 20
    assert structured == {"result": content[0].text}
    shown = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
    assert len(shown.encode("utf-16-le")) // 2 == shown_size(content[0].text) <= RESULT_BUDGET


@pytest.mark.asyncio
async def test_an_entry_is_read_whole_by_its_id(long_history):
    import src.server as server

    listed = json.loads(await server.read_history(limit=3))["entries"][1]
    whole = json.loads(await server.read_history(entry_id=listed["id"].upper(), query="ignored", limit=1))
    assert whole["mode"] == "entry" and whole["total"] == 1
    entry = whole["entries"][0]
    assert (entry["intent"], entry["outcome"]) == long_history[listed["id"]]
    assert "truncated" not in entry and "instruction" not in whole

    missing = json.loads(await server.read_history(entry_id="0123456789ab"))
    assert (missing["mode"], missing["total"], missing["entries"]) == ("entry", 0, [])
    malformed = json.loads(await server.read_history(entry_id="2026-10-06"))
    assert malformed["status"] == "error" and "12 hexadecimal" in malformed["error"]


@pytest.mark.asyncio
async def test_offset_reads_the_entries_a_listing_left_out(client_root):
    import src.server as server

    write_entries(client_root, 150, intent=400, outcome=900)
    order = [e.id for e in HistoryReader(str(client_root / "history.md")).read_recent(limit=150)]
    first = json.loads(await server.read_history(limit=150))
    shown = first["total"]
    assert first["omitted"] == 150 - shown > 0
    assert f"offset={shown}" in first["instruction"]
    assert [entry["id"] for entry in first["entries"]] == order[:shown]
    rest = json.loads(await server.read_history(limit=150 - shown, offset=shown))
    assert [entry["id"] for entry in rest["entries"]] == order[shown:shown + rest["total"]]
    page = json.loads(await server.read_history(limit=5, offset=5))
    assert [entry["id"] for entry in page["entries"]] == order[5:10]


def test_an_entry_archived_by_rotation_is_still_found(tmp_path):
    history = tmp_path / "history.md"
    writer = HistoryWriter(str(history), str(tmp_path / "history"), rotation_kb=1)
    recorded = writer.append_entry("the first intent", "action", "o" * 2_000)  # past 1 KB: rotated at once
    first = recorded["entry_id"]
    assert recorded.get("rotated_to")
    writer.append_entry("the second intent", "action", "outcome")
    reader = HistoryReader(str(history), str(tmp_path / "history"))
    assert first not in {entry.id for entry in reader.read_all()}
    assert reader.find(first).outcome == "o" * 2_000
    assert reader.find("0123456789ab") is None


def test_an_unreadable_archive_does_not_fail_a_lookup(tmp_path, caplog):
    history = tmp_path / "history.md"
    writer = HistoryWriter(str(history), str(tmp_path / "history"), rotation_kb=1)
    archived = writer.append_entry("archived", "action", "o" * 2_000)
    current = writer.append_entry("current", "action", "outcome")["entry_id"]
    os.chmod(archived["rotated_to"], 0)
    try:
        reader = HistoryReader(str(history), str(tmp_path / "history"))
        assert reader.find(current).intent == "current"
        assert reader.find(archived["entry_id"]) is None
    finally:
        os.chmod(archived["rotated_to"], 0o644)
    assert "could not read history archive" in caplog.text


def block(entry_id: str, timestamp: str, machine: str | None = None) -> str:
    lines = [f"## {timestamp} | {entry_id}", "**Intent:** same intent", "**Action:** same action",
             "**Outcome:** same outcome"]
    if machine:
        lines.append(f"**Machine:** {machine}")
    return "\n".join(lines) + "\n\n"


def test_with_sync_a_lookup_shows_the_copy_a_merged_read_shows(tmp_path):
    entry_id = "abcdef012345"
    (tmp_path / "history").mkdir()
    (tmp_path / "history.md").write_text("# History\n\n", encoding="utf-8")
    (tmp_path / "history" / "2026-08.md").write_text(block(entry_id, "2026-08-10T10:00:00+00:00"), encoding="utf-8")
    segment = tmp_path / "2026-10.md"
    segment.write_text(block(entry_id, "2026-10-05T10:00:00+00:00", "machine-b"), encoding="utf-8")

    class Sync:
        def machines(self, history_path):
            return MachineHistory("this-mac", (("machine-b", str(segment)),))

        def appended(self, history_path, entry_id):
            pass

    previous = set_sync(Sync())
    try:
        reader = HistoryReader(str(tmp_path / "history.md"), str(tmp_path / "history"))
        merged = {entry.id: entry for entry in reader.read_merged(Sync().machines(None))}
        found = reader.find(entry_id)
        assert (found.timestamp, found.machine) == (merged[entry_id].timestamp, merged[entry_id].machine)
        assert (found.timestamp, found.machine) == ("2026-08-10T10:00:00+00:00", None)  # this checkout's copy
        (tmp_path / "history" / "2026-08.md").unlink()
        assert reader.find(entry_id).machine == "machine-b"
    finally:
        set_sync(previous)


@pytest.mark.asyncio
async def test_a_short_history_is_listed_as_before(client_root):
    import src.server as server

    writer = HistoryWriter(str(client_root / "history.md"), str(client_root / "history"))
    writer.append_entry("short intent", "short action", "short outcome", tags=["t"])
    result = json.loads(await server.read_history())
    assert set(result) == {"mode", "total", "entries", "workspace", "pid"}
    [entry] = result["entries"]
    assert entry == HistoryReader(str(client_root / "history.md")).read_all()[0].to_dict()


def test_semantic_results_carry_the_action_and_outcome_of_their_document():
    entry = HistoryEntry("abcdef012345", "2026-10-07T10:00:00+00:00", "an intent", "Agent: a: b", "the outcome: yes",
                         tags=["#t"])
    document = HistoryStore._format_for_embedding(entry)
    assert HistoryStore._document_fields(document) == {"action": "Agent: a: b", "outcome": "the outcome: yes"}


@pytest.mark.asyncio
async def test_a_semantic_listing_previews_its_outcomes(client_root, monkeypatch):
    import src.server as server

    async def ready(name):
        return None

    def search(self, query, limit=5, machine=None, **kwargs):
        return [{"id": f"{index:012x}", "distance": 0.1 * index, "timestamp": "2026-10-07T10:00:00+00:00",
                 "intent": long_text(index, 900), "action": "Agent: software_engineer",
                 "outcome": long_text(index, 8_000), "tags": ["#t"], "machine": None} for index in range(limit)]

    monkeypatch.setattr(server, "_readiness_problem", ready)
    monkeypatch.setattr(HistoryStore, "search", search)
    text = await server.read_history(query="anything", limit=10, offset=2)
    assert shown_size(text) <= RESULT_BUDGET
    result = json.loads(text)
    assert result["mode"] == "semantic" and result["total"] == 10
    for index, entry in enumerate(result["entries"], start=2):
        assert entry["id"] == f"{index:012x}"
        assert entry["truncated"] == {"outcome": 8_000, "intent": 900}
        assert entry["distance"] == pytest.approx(0.1 * index) and entry["tags"] == ["#t"]


@pytest.mark.asyncio
async def test_an_entry_with_a_lone_surrogate_still_comes_back(client_root):
    import src.server as server

    # A hand-edited Meta line can decode to a lone surrogate, which no transport can encode.
    (client_root / "history.md").write_text(
        '## 2026-10-07T10:00:00+00:00 | abcdef012345\n**Intent:** i\n**Action:** a\n**Outcome:** o\n'
        '**Meta:** {"note": "\\ud83d"}\n', encoding="utf-8")
    for text in (await server.read_history(entry_id="abcdef012345"), await server.read_history()):
        text.encode("utf-8")
        assert json.loads(text)["entries"][0]["metadata"] == {"note": "\ud83d"}


def entries(count: int, outcome: int = 3_000, files: int = 1):
    return [{"id": f"{index:012x}", "timestamp": f"2026-10-07T{index // 60 % 24:02d}:{index % 60:02d}:00",
             "intent": long_text(index, 500), "action": long_text(index, 400), "outcome": long_text(index, outcome),
             "files": [f"src/file_{n}.py" for n in range(files)], "tags": ["#t"], "metadata": None,
             "machine": None} for index in range(count)]


def test_previews_shrink_before_any_entry_is_left_out():
    payload = listing("recency", entries(60), {"pid": 1})
    assert shown_size(to_json(payload)) <= RESULT_BUDGET
    assert payload["total"] == 60 and "omitted" not in payload
    assert max(len(entry["outcome"]) for entry in payload["entries"]) < TEXTS["outcome"]


def test_the_last_entries_are_left_out_when_the_shortest_previews_do_not_fit():
    many = entries(500)
    payload = listing("recency", many, {"pid": 1}, offset=40)
    assert shown_size(to_json(payload)) <= RESULT_BUDGET
    shown = payload["total"]
    assert 0 < shown < 500 and payload["omitted"] == 500 - shown
    assert f"{500 - shown} more entries" in payload["instruction"]
    assert f"offset={40 + shown}" in payload["instruction"]
    assert [entry["id"] for entry in payload["entries"]] == [entry["id"] for entry in many[:shown]]


def test_a_long_list_is_cut_and_does_not_hide_the_entries_after_it():
    rows = entries(20)
    rows[0]["files"] = [f"src/a/very/long/path/to/module_{n:04d}.py" for n in range(920)]
    payload = listing("recency", rows, {"pid": 1})
    assert payload["total"] == 20 and "omitted" not in payload
    first = payload["entries"][0]
    assert len(first["files"]) == LIST_ITEMS and first["truncated"]["files"] == 920


def test_a_preview_with_its_ellipsis_stays_within_its_limit():
    assert len(preview("x" * 1_000, 600)) == 600 and preview("x" * 1_000, 600).endswith("…")
    worded = preview("word " * 200, 300)
    assert len(worded) <= 300 and worded.endswith("word…")
