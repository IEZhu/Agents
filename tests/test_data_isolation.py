"""Guard for tests/conftest.py: the suite must never write the live data/ (issue #68)."""
import os

import pytest
from dotenv import dotenv_values

from src import self_update
from src.engine import config
from src.engine.enrichment import implant_retriever, skill_retriever
from tests.conftest import TEST_DATA_DIR

LIVE_DATA_DIR = os.path.join(config.INSTALL_ROOT, "data")


def _outside_live(path: str) -> bool:
    live = os.path.realpath(LIVE_DATA_DIR) + os.sep
    return not (os.path.realpath(path) + os.sep).startswith(live)


def test_install_data_dir_points_at_the_test_copy():
    assert config.INSTALL_DATA_DIR == config.DATA_DIR == TEST_DATA_DIR
    assert _outside_live(TEST_DATA_DIR)


def test_import_time_retrievers_use_the_test_copy():
    for retriever in (skill_retriever, implant_retriever):
        assert retriever.store._data_dir == TEST_DATA_DIR
        assert _outside_live(retriever.HASH_FILE)


def test_self_update_bookkeeping_uses_the_test_copy():
    for path in (self_update.STATE_FILE, self_update.CHECK_STAMP, self_update.LOCK_FILE,
                 self_update.PREPARED_MARKER, self_update.STAGING_ROOT):
        assert _outside_live(path), path


def test_embedding_model_follows_the_install_env():
    expected = dotenv_values(os.path.join(config.INSTALL_ROOT, ".env")).get("EMBEDDING_MODEL")
    if not expected:
        pytest.skip(".env does not pin EMBEDDING_MODEL")
    # An explicit shell value wins; otherwise conftest pins the .env value.
    assert config.EMBEDDING_MODEL == os.environ.get("EMBEDDING_MODEL", expected)
