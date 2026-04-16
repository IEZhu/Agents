"""Tests for the history.md writer/reader and the lazy semantic store."""

from __future__ import annotations

import datetime as _dt
import json
import os
import time
from pathlib import Path

import pytest

from src.memory.history import (
    HistoryEntry,
    HistoryReader,
    HistoryStore,
    HistoryWriter,
)


@pytest.fixture
def history_path(tmp_path):
    return str(tmp_path / "history.md")


@pytest.fixture
def writer(tmp_path, history_path):
    return HistoryWriter(
        history_path=history_path,
        archive_dir=str(tmp_path / "history"),
        rotation_kb=512,
    )


# --- Writer ------------------------------------------------------------------

class TestAppend:
    def test_creates_file_with_header(self, writer, history_path):
        result = writer.append_entry("intent A", "did A", "ok A")
        assert result["status"] == "recorded"
        assert os.path.exists(history_path)
        with open(history_path) as f:
            content = f.read()
        # YAML frontmatter
        assert content.startswith("---\n")
        assert "format_version:" in content
        # Record
        assert "intent A" in content
        assert "## " in content

    def test_validation_rejects_empty_fields(self, writer):
        result = writer.append_entry("", "x", "y")
        assert result["status"] == "error"

    def test_returns_entry_id(self, writer):
        r1 = writer.append_entry("intent", "action", "outcome")
        r2 = writer.append_entry("intent", "action", "outcome2")
        assert r1["entry_id"] != r2["entry_id"]
        assert len(r1["entry_id"]) == 12

    def test_entry_id_is_deterministic(self, writer):
        r1 = writer.append_entry("intent X", "act X", "out X")
        # Re-instantiate writer to bypass dedup tail-scan
        os.unlink(writer.history_path)
        r2 = writer.append_entry("intent X", "act X", "out X")
        assert r1["entry_id"] == r2["entry_id"]


class TestDedup:
    def test_duplicate_short_circuits(self, writer):
        r1 = writer.append_entry("same", "same", "same")
        r2 = writer.append_entry("same", "same", "same")
        assert r1["status"] == "recorded"
        assert r2["status"] == "duplicate"
        assert r2["entry_id"] == r1["entry_id"]

    def test_outside_dedup_window_rerecords(self, tmp_path, history_path):
        # Tighten window to 1 so the second identical entry slips in once we
        # have appended a different entry between them.
        w = HistoryWriter(history_path=history_path, dedup_tail=1)
        w.append_entry("a", "a", "a")
        w.append_entry("b", "b", "b")
        # 'a' is now outside the 1-entry tail window, so it can be re-recorded
        r = w.append_entry("a", "a", "a")
        assert r["status"] == "recorded"


class TestFormatRoundtrip:
    def test_files_tags_metadata_roundtrip(self, writer):
        writer.append_entry(
            intent="add HistoryStore",
            action="implemented HistoryStore class",
            outcome="tests pass",
            files=["src/memory/history.py", "tests/test_history.py"],
            tags=["feature", "#memory"],
            metadata={"phase": 3, "loc": 250},
        )
        reader = HistoryReader(writer.history_path)
        entries = reader.read_recent(limit=10)
        assert len(entries) == 1
        e = entries[0]
        assert e.intent == "add HistoryStore"
        assert e.files == ["src/memory/history.py", "tests/test_history.py"]
        # Both tag forms ("feature" and "#memory") are stored as #-prefixed
        assert any("memory" in t for t in e.tags)
        assert e.metadata == {"phase": 3, "loc": 250}


class TestRecency:
    def test_recent_order_newest_first(self, writer):
        writer.append_entry("first", "a", "b")
        time.sleep(1.05)  # ISO timestamp resolution is seconds
        writer.append_entry("second", "a", "b")
        time.sleep(1.05)
        writer.append_entry("third", "a", "b")
        reader = HistoryReader(writer.history_path)
        entries = reader.read_recent(limit=10)
        assert [e.intent for e in entries] == ["third", "second", "first"]

    def test_limit_caps_results(self, writer):
        for i in range(5):
            writer.append_entry(f"intent {i}", "a", "b")
            time.sleep(0.01)
        reader = HistoryReader(writer.history_path)
        assert len(reader.read_recent(limit=3)) == 3

    def test_since_filter(self, writer):
        writer.append_entry("old", "a", "b")
        time.sleep(1.05)
        cutoff = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        time.sleep(1.05)
        writer.append_entry("new", "a", "b")
        reader = HistoryReader(writer.history_path)
        entries = reader.read_recent(limit=10, since=cutoff)
        assert [e.intent for e in entries] == ["new"]


class TestRotation:
    def test_triggers_when_threshold_exceeded(self, tmp_path, history_path):
        archive_dir = str(tmp_path / "history")
        # 1 KB threshold so the test stays fast
        w = HistoryWriter(
            history_path=history_path,
            archive_dir=archive_dir,
            rotation_kb=1,
        )
        # Each entry adds ~150 bytes; ~10 entries should trip the 1 KB threshold.
        rotated_to = None
        for i in range(15):
            r = w.append_entry(f"intent {i}", "action " * 10, "outcome " * 10)
            if "rotated_to" in r:
                rotated_to = r["rotated_to"]
                break
        assert rotated_to is not None
        assert os.path.exists(rotated_to)
        # Fresh history.md exists and contains a pointer to the archive
        assert os.path.exists(history_path)
        with open(history_path) as f:
            content = f.read()
        assert "archived to" in content
        # Archive contains the prior content
        with open(rotated_to) as f:
            archived = f.read()
        assert "intent 0" in archived

    def test_rotation_filename_uses_yyyy_mm(self, tmp_path, history_path):
        w = HistoryWriter(
            history_path=history_path,
            archive_dir=str(tmp_path / "history"),
            rotation_kb=1,
        )
        for i in range(20):
            r = w.append_entry(f"i{i}", "a" * 200, "o" * 200)
            if "rotated_to" in r:
                fname = os.path.basename(r["rotated_to"])
                # YYYY-MM.md
                assert len(fname) == 10
                assert fname[4] == "-"
                assert fname.endswith(".md")
                break
        else:
            pytest.fail("rotation never triggered")


# --- Lazy semantic recall ----------------------------------------------------

class FakeEmbedder:
    """Maps each known phrase to a unit vector pointing along a unique axis.

    Provides deterministic, numpy-free-during-collection embedding for tests.
    Only imports numpy when actually called.
    """

    def __init__(self, vocabulary: list[str], dim: int = 8):
        import numpy as np
        self.np = np
        unique_vocabulary = list(dict.fromkeys(vocabulary))
        # Ensure dimension is large enough to give each phrase its own axis.
        self.dim = max(dim, len(unique_vocabulary))
        # Deterministic, collision-free axis assignment by enumeration.
        self._axis = {
            phrase: axis for axis, phrase in enumerate(unique_vocabulary)
        }

    def _vec(self, text: str):
        v = self.np.zeros(self.dim, dtype=self.np.float32)
        # Anchor on whichever vocabulary phrase appears in the text; otherwise
        # use the text hash directly.
        for phrase, axis in self._axis.items():
            if phrase.lower() in text.lower():
                v[axis] = 1.0
                return v
        v[hash(text) % self.dim] = 1.0
        return v

    def embed_texts(self, texts):
        return self.np.array([self._vec(t) for t in texts])

    def embed_query(self, text):
        return self._vec(text)


def _numpy_available() -> bool:
    try:
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


# Skip semantic tests when numpy can't load (NixOS-without-nix-ld scenario);
# the writer/reader tests above stay valid.
@pytest.mark.skipif(not _numpy_available(), reason="numpy unavailable in this env")
class TestSemanticStore:
    def test_index_lazy_until_search(self, tmp_path, writer):
        writer.append_entry("first", "a", "b")
        writer.append_entry("second", "x", "y")
        store_dir = str(tmp_path / "memory_data")
        # No file on disk yet
        assert not os.path.exists(os.path.join(store_dir, "history_store.npz"))
        # Construct without searching — still nothing on disk
        store = HistoryStore(history_path=writer.history_path, data_dir=store_dir)
        assert not os.path.exists(os.path.join(store_dir, "history_store.npz"))

    def test_search_returns_relevant(self, tmp_path, writer):
        writer.append_entry(
            intent="add semantic recall",
            action="wired NumpyVectorStore",
            outcome="search returns relevant entries",
        )
        time.sleep(0.05)
        writer.append_entry(
            intent="bake bread",
            action="kneaded the dough",
            outcome="loaf was tasty",
        )
        store_dir = str(tmp_path / "memory_data")
        store = HistoryStore(history_path=writer.history_path, data_dir=store_dir)

        embedder = FakeEmbedder(["semantic recall", "bake bread"])
        results = store.search(
            "semantic recall",
            limit=2,
            embed_query=embedder.embed_query,
            embed_texts=embedder.embed_texts,
        )
        assert len(results) >= 1
        # Top hit must be the semantic-recall entry
        assert "semantic recall" in results[0]["intent"].lower()

    def test_index_rebuilds_on_file_change(self, tmp_path, writer):
        writer.append_entry("seed", "a", "b")
        store_dir = str(tmp_path / "memory_data")
        store = HistoryStore(history_path=writer.history_path, data_dir=store_dir)
        embedder = FakeEmbedder(["seed", "fresh"])
        store.search(
            "seed",
            limit=1,
            embed_query=embedder.embed_query,
            embed_texts=embedder.embed_texts,
        )
        npz_path = os.path.join(store_dir, "history_store.npz")
        assert os.path.exists(npz_path)
        first_mtime = os.path.getmtime(npz_path)

        # Append new entry; ensure history.md mtime is strictly newer
        time.sleep(1.05)
        writer.append_entry("fresh", "x", "y")
        os.utime(writer.history_path, None)

        results = store.search(
            "fresh",
            limit=1,
            embed_query=embedder.embed_query,
            embed_texts=embedder.embed_texts,
        )
        assert results
        assert "fresh" in results[0]["intent"].lower()
        # Store file was rewritten
        assert os.path.getmtime(npz_path) >= first_mtime
