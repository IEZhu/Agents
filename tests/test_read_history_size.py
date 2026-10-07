"""read_history keeps its listings under Claude Code's 50,000-character limit, and one entry
stays readable whole by its id (#212)."""
from __future__ import annotations

import json

import pytest

from src.engine import config as engine_config
from src.memory.history import HistoryReader, HistoryStore, HistoryWriter
from src.memory.history_results import (
    BRIEF_CHARS,
    INLINE_LIMIT,
    PREVIEW_CHARS,
    RECENCY_TEXTS,
    RESULT_BUDGET,
    listing,
    shown_size,
)

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
    engine_config._reset_client_repo_root_cache()
    yield root.resolve()
    engine_config._reset_client_repo_root_cache()


@pytest.fixture
def long_history(client_root):
    """Forty entries whose texts alone pass the limit twice over: (intent, outcome) per entry id."""
    writer = HistoryWriter(str(client_root / "history.md"), str(client_root / "history"))
    texts = {}
    for index in range(40):
        intent, outcome = long_text(index, 1_200), long_text(index, 4_000)
        entry_id = writer.append_entry(intent, f"Agent: software_engineer {index}", outcome)["entry_id"]
        texts[entry_id] = (intent, outcome)
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
        assert len(entry["outcome"]) <= PREVIEW_CHARS + 1 and len(entry["intent"]) <= BRIEF_CHARS + 1


@pytest.mark.asyncio
async def test_claude_code_counts_the_structured_result_this_listing_was_sized_for(long_history):
    import src.server as server

    # Claude Code shows JSON.stringify(structuredContent) and compares its length with the limit.
    content, structured = await server.mcp.call_tool("read_history", {})
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


@pytest.mark.asyncio
async def test_a_short_history_is_listed_as_before(client_root):
    import src.server as server

    writer = HistoryWriter(str(client_root / "history.md"), str(client_root / "history"))
    writer.append_entry("short intent", "short action", "short outcome", tags=["t"])
    result = json.loads(await server.read_history())
    assert set(result) == {"mode", "total", "entries", "workspace", "pid"}
    [entry] = result["entries"]
    assert entry == HistoryReader(str(client_root / "history.md")).read_all()[0].to_dict()


@pytest.mark.asyncio
async def test_a_semantic_listing_previews_its_documents(client_root, monkeypatch):
    import src.server as server

    async def ready(name):
        return None

    def search(self, query, limit=5, machine=None, **kwargs):
        return [{"id": f"{index:012x}", "distance": 0.1 * index, "document": long_text(index, 8_000),
                 "timestamp": "2026-10-07T10:00:00+00:00", "intent": long_text(index, 900),
                 "tags": ["#t"], "machine": None} for index in range(limit)]

    monkeypatch.setattr(server, "_readiness_problem", ready)
    monkeypatch.setattr(HistoryStore, "search", search)
    text = await server.read_history(query="anything", limit=10)
    assert shown_size(text) <= RESULT_BUDGET
    result = json.loads(text)
    assert result["mode"] == "semantic" and result["total"] == 10
    for index, entry in enumerate(result["entries"]):
        assert entry["truncated"] == {"document": 8_000, "intent": 900}
        assert entry["distance"] == pytest.approx(0.1 * index) and entry["tags"] == ["#t"]


def entries(count: int, outcome: int = 3_000):
    return [{"id": f"{index:012x}", "timestamp": f"2026-10-07T{index // 60 % 24:02d}:{index % 60:02d}:00",
             "intent": long_text(index, 500), "action": long_text(index, 400), "outcome": long_text(index, outcome),
             "files": ["src/a.py"], "tags": ["#t"], "metadata": None, "machine": None} for index in range(count)]


def test_previews_shrink_before_any_entry_is_left_out():
    result = listing("recency", entries(60), RECENCY_TEXTS, {"pid": 1})
    shown = json.loads(result.text)
    assert shown_size(result.text) <= RESULT_BUDGET
    assert (result.total, result.omitted) == (60, 0) and "omitted" not in shown
    assert max(len(entry["outcome"]) for entry in shown["entries"]) < PREVIEW_CHARS


def test_the_last_entries_are_left_out_when_the_shortest_previews_do_not_fit():
    many = entries(500)
    result = listing("recency", many, RECENCY_TEXTS, {"pid": 1})
    shown = json.loads(result.text)
    assert shown_size(result.text) <= RESULT_BUDGET
    assert 0 < result.total < 500 and result.total + result.omitted == 500
    assert shown["omitted"] == result.omitted and f"{result.omitted} more entries" in shown["instruction"]
    assert [entry["id"] for entry in shown["entries"]] == [entry["id"] for entry in many[:result.total]]
