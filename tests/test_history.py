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

    Provides deterministic embedding for tests. Imports numpy in __init__
    so the ``skipif`` guard can exclude the entire test class when numpy
    is unavailable.
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
    except Exception:
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


@pytest.mark.skipif(not _numpy_available(), reason="numpy unavailable in this env")
class TestIncrementalIndex:
    """After the first build, only new or edited entries are embedded (#157)."""

    @pytest.fixture(autouse=True)
    def unloaded_model(self, monkeypatch):
        # Earlier tests may load the real model; these tests label the index
        # with the fingerprint they set, as a process that has not loaded one.
        import src.engine.embedder as embedder

        monkeypatch.setattr(embedder, "_model_fingerprint", None)

    @pytest.fixture
    def embedded(self):
        return []

    @pytest.fixture
    def search(self, tmp_path, writer, embedded):
        embedder = FakeEmbedder(["alpha", "beta", "gamma", "delta"])

        def embed_texts(texts):
            embedded.append(list(texts))
            return embedder.embed_texts(texts)

        def run(query="alpha"):
            store = HistoryStore(history_path=writer.history_path, data_dir=str(tmp_path / "memory_data"))
            return store.search(query, limit=5, embed_query=embedder.embed_query, embed_texts=embed_texts)

        return run

    @pytest.fixture
    def seeded(self, writer, search, embedded):
        for intent in ("alpha", "beta", "gamma"):
            writer.append_entry(intent, "act", "out")
        search()
        assert [len(batch) for batch in embedded] == [3]
        embedded.clear()

    def test_append_embeds_only_the_new_entry(self, writer, search, embedded, seeded):
        writer.append_entry("delta", "act", "out")

        results = search("delta")

        assert len(embedded) == 1 and len(embedded[0]) == 1
        assert embedded[0][0].startswith("Intent: delta")
        assert results[0]["intent"] == "delta"
        # Reused vectors stay attached to their own entries.
        for intent in ("alpha", "beta", "gamma"):
            assert search(intent)[0]["intent"] == intent
        assert len(search("alpha")) == 4
        assert len(embedded) == 1

    def test_unchanged_history_makes_no_embedding_call(self, search, embedded, seeded):
        search()
        assert embedded == []

    def test_changed_fingerprint_reembeds_everything(self, writer, search, embedded, seeded, monkeypatch):
        import src.engine.fingerprint as fingerprint_module

        monkeypatch.setattr(fingerprint_module, "fingerprint", lambda model=None: "other-model")
        writer.append_entry("delta", "act", "out")

        search()

        assert [len(batch) for batch in embedded] == [4]

    def test_marker_names_the_model_that_embedded(self, tmp_path, writer, search, embedded, seeded, monkeypatch):
        import src.engine.embedder as embedder
        import src.engine.fingerprint as fingerprint_module

        marker = tmp_path / "memory_data" / ".history_fingerprint"
        loaded = marker.read_text().partition(":")[2]
        # This process loaded its model earlier; another process has since
        # downloaded a newer snapshot into the shared model cache.
        monkeypatch.setattr(embedder, "_model_fingerprint", loaded)
        monkeypatch.setattr(fingerprint_module, "fingerprint", lambda model=None: "newer-snapshot")
        writer.append_entry("delta", "act", "out")

        search()

        assert [len(batch) for batch in embedded] == [1]
        assert marker.read_text().endswith(":" + loaded)

        # After a restart the newer snapshot is loaded: every entry is re-embedded.
        monkeypatch.setattr(embedder, "_model_fingerprint", "newer-snapshot")
        embedded.clear()

        search()

        assert [len(batch) for batch in embedded] == [4]
        assert marker.read_text().endswith(":newer-snapshot")

    def test_search_loads_the_model_before_checking_the_index(self, tmp_path, writer, embedded, seeded, monkeypatch):
        import src.engine.embedder as embedder

        marker = tmp_path / "memory_data" / ".history_fingerprint"
        fake = FakeEmbedder(["alpha", "beta", "gamma", "delta"])

        def embed_query(text):
            # The first query loads a snapshot newer than the cached fingerprint.
            monkeypatch.setattr(embedder, "_model_fingerprint", "newer-snapshot")
            return fake.embed_query(text)

        def embed_texts(texts):
            embedded.append(list(texts))
            return fake.embed_texts(texts)

        store = HistoryStore(history_path=writer.history_path, data_dir=str(tmp_path / "memory_data"))
        results = store.search("alpha", limit=5, embed_query=embed_query, embed_texts=embed_texts)

        assert [len(batch) for batch in embedded] == [3]
        assert marker.read_text().endswith(":newer-snapshot")
        assert results[0]["intent"] == "alpha"

    def test_reuse_reads_the_vectors_the_marker_describes(self, tmp_path, writer, embedded, seeded):
        import numpy as np

        fake = FakeEmbedder(["alpha", "beta", "gamma", "delta"])

        def embed_texts(texts):
            embedded.append(list(texts))
            return fake.embed_texts(texts)

        long_lived = HistoryStore(history_path=writer.history_path, data_dir=str(tmp_path / "memory_data"))
        long_lived.ensure_index(embed_texts=embed_texts)
        # Its copy no longer matches the store file the marker describes, as
        # after another process rewrote that file.
        memory = long_lived._store
        ids, documents, metadatas = list(memory._ids), list(memory._documents), list(memory._metadatas)
        vectors = memory.get_embeddings(ids)
        memory.replace(ids=ids, embeddings=np.stack([vectors[i] for i in reversed(ids)]),
                       documents=documents, metadatas=metadatas)
        writer.append_entry("delta", "act", "out")

        results = long_lived.search("alpha", limit=5, embed_query=fake.embed_query, embed_texts=embed_texts)

        assert [len(batch) for batch in embedded] == [1]
        assert results[0]["intent"] == "alpha"

    def test_missing_history_returns_nothing_without_the_model(self, tmp_path, writer, embedded, seeded):
        os.remove(writer.history_path)

        def embed_query(text):
            raise AssertionError("a missing history must not load the model")

        store = HistoryStore(history_path=writer.history_path, data_dir=str(tmp_path / "memory_data"))

        assert store.search("alpha", embed_query=embed_query, embed_texts=embed_query) == []
        assert store._store.count() == 0

    def test_interrupted_rebuild_does_not_vouch_for_saved_vectors(
        self, tmp_path, writer, search, embedded, seeded, monkeypatch
    ):
        import src.daemon.state as state_module
        import src.engine.embedder as embedder
        import src.engine.fingerprint as fingerprint_module

        current = fingerprint_module.fingerprint()
        with monkeypatch.context() as crash:
            crash.setattr(fingerprint_module, "fingerprint", lambda model=None: "other-model")
            crash.setattr(state_module, "atomic_private", lambda *args: (_ for _ in ()).throw(OSError("crash")))
            writer.append_entry("delta", "act", "out")
            with pytest.raises(OSError):
                search()
        assert not (tmp_path / "memory_data" / ".history_fingerprint").exists()
        embedded.clear()

        assert fingerprint_module.fingerprint() == current
        assert embedder._model_fingerprint is None  # class isolation still applies
        search()

        assert [len(batch) for batch in embedded] == [4]

    def test_tag_edit_reembeds_only_that_entry(self, writer, search, embedded, seeded):
        text = Path(writer.history_path).read_text(encoding="utf-8")
        text = text.replace("**Intent:** beta\n**Action:** act\n**Outcome:** out\n",
                            "**Intent:** beta\n**Action:** act\n**Outcome:** out\n**Tags:** #edited\n")
        Path(writer.history_path).write_text(text, encoding="utf-8")

        search()

        assert len(embedded) == 1 and len(embedded[0]) == 1
        assert embedded[0][0].startswith("Intent: beta") and "Tags: #edited" in embedded[0][0]

    def test_removed_entry_leaves_the_index(self, writer, search, embedded, seeded):
        text = Path(writer.history_path).read_text(encoding="utf-8")
        head, _, rest = text.partition("**Intent:** beta")
        heading_start = head.rstrip("\n").rfind("## ")
        _, _, tail = rest.partition("**Outcome:** out\n")
        Path(writer.history_path).write_text(head[:heading_start] + tail.lstrip("\n"), encoding="utf-8")

        results = search()

        assert embedded == []
        assert sorted(r["intent"] for r in results) == ["alpha", "gamma"]


class TestRotationWithOpenHandle:
    """Windows cannot move an open file: rotation must run after the handle closed."""

    def test_rotation_happens_with_append_handle_closed(self, tmp_path, history_path, monkeypatch):
        import builtins

        writer = HistoryWriter(history_path, str(tmp_path / "history"), rotation_kb=1)
        opened = []
        real_open = builtins.open

        def tracking_open(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            if os.path.abspath(str(path)) == os.path.abspath(history_path) and "a" in (args[0] if args else kwargs.get("mode", "r")):
                opened.append(handle)
            return handle

        real_rotate = writer._maybe_rotate_locked

        def rotate_checking_handle():
            assert opened and all(handle.closed for handle in opened)
            return real_rotate()

        monkeypatch.setattr(builtins, "open", tracking_open)
        monkeypatch.setattr(writer, "_maybe_rotate_locked", rotate_checking_handle)
        statuses = [
            writer.append_entry(f"q{i} " + "x" * 600, "a", "o" * 600)["status"]
            for i in range(4)
        ]
        monkeypatch.undo()
        assert statuses == ["recorded"] * 4
        archives = list((tmp_path / "history").glob("*"))
        assert len(archives) >= 1

    def test_rotation_failure_still_reports_recorded(self, tmp_path, history_path, monkeypatch, caplog):
        writer = HistoryWriter(history_path, str(tmp_path / "history"), rotation_kb=1)

        def boom():
            raise PermissionError(32, "in use")

        monkeypatch.setattr(writer, "_maybe_rotate_locked", boom)
        with caplog.at_level("WARNING"):
            result = writer.append_entry("q " + "x" * 2000, "a", "o")
        assert result["status"] == "recorded"
        assert "history rotation failed" in caplog.text


def test_failed_merge_rotation_does_not_duplicate_archive(tmp_path, history_path, monkeypatch):
    archive_dir = tmp_path / "history"
    writer = HistoryWriter(history_path, str(archive_dir), rotation_kb=1)
    for i in range(3):
        writer.append_entry(f"q{i} " + "x" * 600, "a", "o" * 600)
    archives = list(archive_dir.glob("*.md"))
    assert archives
    before = archives[0].read_text(encoding="utf-8")

    def locked(src, dst):
        raise PermissionError(32, "in use")

    monkeypatch.setattr("src.memory.history.os.replace", locked)
    for i in range(3, 6):
        assert writer.append_entry(f"q{i} " + "y" * 600, "a", "o" * 600)["status"] == "recorded"
    assert archives[0].read_text(encoding="utf-8") == before


def test_failed_archive_append_restores_entries(tmp_path, history_path, monkeypatch):
    archive_dir = tmp_path / "history"
    writer = HistoryWriter(history_path, str(archive_dir), rotation_kb=1)
    for i in range(3):
        writer.append_entry(f"q{i} " + "x" * 600, "a", "o" * 600)
    assert list(archive_dir.glob("*.md"))
    real_open = open

    def refuse_archive(path, mode="r", *args, **kwargs):
        if str(path).startswith(str(archive_dir) + os.sep) and "w" in mode:
            raise OSError(28, "no space")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("src.memory.history.open", refuse_archive, raising=False)
    result = writer.append_entry("last " + "z" * 2000, "a", "o")
    monkeypatch.undo()
    assert result["status"] == "recorded"
    assert not os.path.exists(history_path + ".rotating")
    assert "last " in Path(history_path).read_text(encoding="utf-8")


def test_interrupted_rotation_is_recovered_not_overwritten(tmp_path, history_path):
    archive_dir = tmp_path / "history"
    writer = HistoryWriter(history_path, str(archive_dir), rotation_kb=1)
    for i in range(3):
        writer.append_entry(f"q{i} " + "x" * 600, "a", "o" * 600)
    Path(history_path + ".rotating").write_text("PENDING-ENTRIES", encoding="utf-8")
    writer.append_entry("more " + "y" * 2000, "a", "o")
    archived = "".join(p.read_text(encoding="utf-8") for p in archive_dir.glob("*.md"))
    assert "PENDING-ENTRIES" in archived


def test_partial_archive_write_never_reaches_the_archive(tmp_path, history_path, monkeypatch):
    archive_dir = tmp_path / "history"
    writer = HistoryWriter(history_path, str(archive_dir), rotation_kb=1)
    for i in range(3):
        writer.append_entry(f"q{i} " + "x" * 600, "a", "o" * 600)
    archive = next(archive_dir.glob("*.md"))
    before = archive.read_bytes()
    real_open = open

    def partial(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if str(path) == str(archive) + ".tmp" and "w" in mode:
            class Boom:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    handle.close()

                def write(self, data):
                    handle.write(data[:5])
                    handle.flush()
                    raise OSError(28, "no space")
            return Boom()
        return handle

    monkeypatch.setattr("src.memory.history.open", partial, raising=False)
    assert writer.append_entry("last " + "z" * 2000, "a", "o")["status"] == "recorded"
    monkeypatch.undo()
    assert archive.read_bytes() == before
    assert not list(archive_dir.glob("*.tmp"))
    assert "last " in Path(history_path).read_text(encoding="utf-8")


def test_pending_file_is_recovered_before_size_check(tmp_path, history_path):
    archive_dir = tmp_path / "history"
    writer = HistoryWriter(history_path, str(archive_dir), rotation_kb=512)
    Path(history_path + ".rotating").write_text(
        "## 2026-01-02T03:04:05+00:00 | abcdef123456\nPENDING-ENTRIES\n", encoding="utf-8")
    writer.append_entry("small", "a", "o")
    archived = "".join(p.read_text(encoding="utf-8") for p in archive_dir.glob("*.md"))
    assert "PENDING-ENTRIES" in archived
    assert (archive_dir / "2026-01.md").exists()
    assert not os.path.exists(history_path + ".rotating") or Path(history_path + ".rotating").stat().st_size == 0


def test_empty_pending_file_never_touches_the_archive(tmp_path, history_path):
    archive_dir = tmp_path / "history"
    writer = HistoryWriter(history_path, str(archive_dir), rotation_kb=512)
    archive_dir.mkdir()
    month = _dt.datetime.now(_dt.timezone.utc).isoformat()[:7]
    archive = archive_dir / f"{month}.md"
    archive.write_text("ARCHIVE", encoding="utf-8")
    Path(history_path + ".rotating").write_text("", encoding="utf-8")
    writer.append_entry("small", "a", "o")
    assert archive.read_text(encoding="utf-8") == "ARCHIVE"
    assert not os.path.exists(history_path + ".rotating")


def test_committed_pending_payload_is_not_archived_twice(tmp_path, history_path):
    archive_dir = tmp_path / "history"
    writer = HistoryWriter(history_path, str(archive_dir), rotation_kb=512)
    archive_dir.mkdir()
    payload = "## 2026-01-02T03:04:05+00:00 | abcdef123456\nENTRY\n"
    archive = archive_dir / "2026-01.md"
    archive.write_text("OLD\n\n<!-- merged on rotation -->\n\n" + payload, encoding="utf-8")
    Path(history_path + ".rotating").write_text(payload, encoding="utf-8")
    writer.append_entry("small", "a", "o")
    assert archive.read_text(encoding="utf-8").count("ENTRY") == 1
    assert not os.path.exists(history_path + ".rotating") or Path(history_path + ".rotating").stat().st_size == 0


def test_cleanup_failure_after_archive_commit_does_not_restore_pending(tmp_path, history_path, monkeypatch, caplog):
    archive_dir = tmp_path / "history"
    writer = HistoryWriter(history_path, str(archive_dir), rotation_kb=1)
    for i in range(3):
        writer.append_entry(f"q{i} " + "x" * 600, "a", "o" * 600)
    real_unlink = os.unlink
    real_open = open

    def locked_unlink(path, *args, **kwargs):
        if str(path).endswith(".rotating"):
            raise PermissionError(32, "in use")
        return real_unlink(path, *args, **kwargs)

    def locked_open(path, mode="r", *args, **kwargs):
        if str(path).endswith(".rotating") and "w" in mode:
            raise PermissionError(32, "in use")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("src.memory.history.os.unlink", locked_unlink)
    monkeypatch.setattr("src.memory.history.open", locked_open, raising=False)
    with caplog.at_level("WARNING"):
        writer.append_entry("tail " + "y" * 2000, "a", "o" * 600)
        writer.append_entry("again", "a", "o")
        writer.append_entry("and again", "a", "o")
    monkeypatch.undo()
    assert len([r for r in caplog.records if "could not clear archived pending" in r.getMessage()]) <= 1
    live = Path(history_path).read_text(encoding="utf-8")
    archived = "".join(p.read_text(encoding="utf-8") for p in archive_dir.glob("*.md"))
    assert "tail " in archived
    assert "tail " not in live
    # The leftover payload is recognized as archived on the next call.
    writer.append_entry("next", "a", "o")
    archived_after = "".join(p.read_text(encoding="utf-8") for p in archive_dir.glob("*.md"))
    assert archived_after.count("tail ") == archived.count("tail ")
