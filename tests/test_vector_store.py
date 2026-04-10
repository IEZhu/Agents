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
    store.replace(
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
            s.replace(
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

    def test_query_2d_embedding_squeezed(self, populated_store):
        """Query with (1, D) embedding should be squeezed to 1-D and work."""
        result = populated_store.query(np.array([[1.0, 0.0, 0.0]]), n_results=1)
        assert result.ids == ["a"]

    def test_query_invalid_shape_raises(self, populated_store):
        """Query with (2, D) embedding cannot be squeezed to 1-D."""
        with pytest.raises(ValueError, match="must be 1-D"):
            populated_store.query(np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]), n_results=1)


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


class TestUpsert:
    def test_upsert_drops_stale_entries(self, populated_store):
        """Reindex with fewer IDs must remove entries not in new set."""
        # Store has ["a", "b", "c"]; reindex with only ["a", "b"]
        embs = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        populated_store.replace(
            ids=["a", "b"],
            embeddings=embs,
            documents=["doc_a_v2", "doc_b_v2"],
            metadatas=[{"k": "a2"}, {"k": "b2"}],
        )
        assert populated_store.count() == 2
        # "c" must be gone
        result = populated_store.get(ids=["c"])
        assert result.ids == []
        # "a" and "b" must have updated documents
        result = populated_store.get(ids=["a"])
        assert result.documents == ["doc_a_v2"]


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


class TestCorruptionRecovery:
    def test_load_mismatched_lengths_resets_and_removes_files(self, tmp_path):
        """Store with mismatched embeddings vs metadata should reset and delete files."""
        npz_path = tmp_path / "corrupt.npz"
        meta_path = tmp_path / "corrupt.json"
        # 2 embeddings but 3 ids
        np.savez(npz_path, embeddings=np.array([[1, 0], [0, 1]], dtype=np.float32))
        import json
        with open(meta_path, "w") as f:
            json.dump({"ids": ["a", "b", "c"], "documents": ["d1", "d2", "d3"],
                        "metadatas": [{}, {}, {}]}, f)

        store = NumpyVectorStore(name="corrupt", data_dir=str(tmp_path))
        assert store.count() == 0
        assert not npz_path.exists()
        assert not meta_path.exists()

    def test_load_1d_embeddings_resets_and_removes_files(self, tmp_path):
        """Store with 1-D embeddings (invalid ndim) should reset and delete files."""
        npz_path = tmp_path / "bad_ndim.npz"
        meta_path = tmp_path / "bad_ndim.json"
        np.savez(npz_path, embeddings=np.array([1, 0, 0], dtype=np.float32))
        import json
        with open(meta_path, "w") as f:
            json.dump({"ids": ["a"], "documents": ["d1"], "metadatas": [{}]}, f)

        store = NumpyVectorStore(name="bad_ndim", data_dir=str(tmp_path))
        assert store.count() == 0
        assert not npz_path.exists()
        assert not meta_path.exists()


class TestDimensionMismatch:
    def test_query_dimension_mismatch(self, populated_store):
        """Querying with wrong dimension should raise ValueError."""
        with pytest.raises(ValueError, match="Dimension mismatch"):
            populated_store.query(np.array([1.0, 0.0]), n_results=1)  # 2-D vs 3-D store

    def test_add_dimension_mismatch(self, populated_store):
        """Adding embeddings with wrong dimension should raise ValueError."""
        with pytest.raises(ValueError, match="Dimension mismatch"):
            populated_store.add(
                ids=["x"],
                embeddings=np.array([[1.0, 0.0]], dtype=np.float32),  # 2-D vs 3-D store
                documents=["doc_x"],
                metadatas=[{"k": "x"}],
            )
