"""Tests for NumpyVectorStore: correctness, persistence, edge cases."""

import tempfile

import numpy as np
import pytest

from src.engine.vector_store import NumpyVectorStore


@pytest.fixture
def store(tmp_path):
    """Fresh store in a temp directory."""
    return NumpyVectorStore(name="test", data_dir=str(tmp_path))


@pytest.fixture
def populated_store(store):
    """Store with 3 entries."""
    embs = np.array([
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ], dtype=np.float32)
    store.upsert(
        ids=["a", "b", "c"],
        embeddings=embs,
        documents=["doc_a", "doc_b", "doc_c"],
        metadatas=[{"k": "a"}, {"k": "b"}, {"k": "c"}],
    )
    return store


class TestQuery:
    def test_nearest_neighbor_exact_match(self, populated_store):
        """Query vector [1,0,0] should return 'a' as nearest."""
        result = populated_store.query(np.array([1.0, 0.0, 0.0]), n_results=1)
        assert result.ids == ["a"]
        assert result.distances[0] == pytest.approx(0.0, abs=1e-6)

    def test_nearest_neighbor_top2(self, populated_store):
        """Query [0.7, 0.7, 0] should return 'a' and 'b' as top-2."""
        result = populated_store.query(np.array([0.7, 0.7, 0.0]), n_results=2)
        assert len(result.ids) == 2
        assert set(result.ids) == {"a", "b"}
        # Both should have same distance (symmetric)
        assert result.distances[0] == pytest.approx(result.distances[1], abs=1e-6)

    def test_n_results_equals_store_size(self, populated_store):
        """n_results == count() should not crash (argpartition edge case)."""
        result = populated_store.query(np.array([1.0, 0.0, 0.0]), n_results=3)
        assert len(result.ids) == 3

    def test_n_results_exceeds_store_size(self, populated_store):
        """n_results > count() should return all entries without crash."""
        result = populated_store.query(np.array([1.0, 0.0, 0.0]), n_results=100)
        assert len(result.ids) == 3

    def test_query_empty_store(self, store):
        result = store.query(np.array([1.0, 0.0, 0.0]), n_results=1)
        assert result.ids == []
        assert result.distances == []

    def test_n_results_one_entry(self):
        """Store with exactly 1 entry, n_results=1 — must not crash."""
        with tempfile.TemporaryDirectory() as tmp:
            s = NumpyVectorStore(name="single", data_dir=tmp)
            s.upsert(
                ids=["only"],
                embeddings=np.array([[1.0, 0.0]]),
                documents=["doc"],
                metadatas=[{"k": "v"}],
            )
            result = s.query(np.array([1.0, 0.0]), n_results=1)
            assert result.ids == ["only"]

    def test_results_sorted_by_distance(self, populated_store):
        """Results should be sorted ascending by distance."""
        result = populated_store.query(np.array([1.0, 0.1, 0.0]), n_results=3)
        assert result.distances == sorted(result.distances)


class TestGetById:
    def test_get_existing(self, populated_store):
        result = populated_store.get(ids=["b"])
        assert result.ids == ["b"]
        assert result.documents == ["doc_b"]

    def test_get_missing(self, populated_store):
        result = populated_store.get(ids=["nonexistent"])
        assert result.ids == []

    def test_get_mixed(self, populated_store):
        result = populated_store.get(ids=["a", "nonexistent", "c"])
        assert result.ids == ["a", "c"]


class TestPersistence:
    def test_save_and_load_roundtrip(self, populated_store, tmp_path):
        populated_store.save()

        loaded = NumpyVectorStore(name="test", data_dir=str(tmp_path))
        assert loaded.count() == 3
        result = loaded.query(np.array([1.0, 0.0, 0.0]), n_results=1)
        assert result.ids == ["a"]

    def test_load_empty_dir(self, tmp_path):
        """Loading from empty dir should create empty store."""
        store = NumpyVectorStore(name="empty", data_dir=str(tmp_path))
        assert store.count() == 0


class TestAdd:
    def test_add_new_entries(self, populated_store):
        new_emb = np.array([[0.5, 0.5, 0.0]], dtype=np.float32)
        populated_store.add(
            ids=["d"],
            embeddings=new_emb,
            documents=["doc_d"],
            metadatas=[{"k": "d"}],
        )
        assert populated_store.count() == 4

    def test_add_duplicate_skipped(self, populated_store):
        new_emb = np.array([[0.5, 0.5, 0.0]], dtype=np.float32)
        populated_store.add(
            ids=["a"],  # already exists
            embeddings=new_emb,
            documents=["dup"],
            metadatas=[{"k": "dup"}],
        )
        assert populated_store.count() == 3


class TestTrim:
    def test_trim_keeps_most_recent(self, populated_store):
        populated_store.trim(2)
        assert populated_store.count() == 2
        # 'a' was first, should be evicted
        result = populated_store.get(ids=["a"])
        assert result.ids == []
        # 'b' and 'c' should remain
        result = populated_store.get(ids=["b", "c"])
        assert len(result.ids) == 2

    def test_trim_noop_under_limit(self, populated_store):
        populated_store.trim(100)
        assert populated_store.count() == 3
