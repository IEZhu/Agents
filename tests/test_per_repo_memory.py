"""Issue #36: per-repo memory isolation end-to-end.

Verifies that a per-session `AGENTS_CLIENT_REPO_ROOT` override routes
`HistoryWriter`, `HistoryStore`, and `RepoDescriber` into the client repo
instead of the Agents install directory.
"""

from __future__ import annotations

import os

import pytest

from src.engine import config as engine_config
from src.memory import config as memory_config
from src.memory.describer import RepoDescriber
from src.memory.history import HistoryStore, HistoryWriter


@pytest.fixture
def client_root(tmp_path, monkeypatch):
    """Point the resolver at a fresh tmp client repo for this test.

    Yields the *resolved* (realpath'd) Path — the production resolver
    always returns ``os.path.realpath(...)``, so comparing against a
    raw ``tmp_path`` would spuriously fail on macOS where ``/var`` is a
    symlink to ``/private/var``.
    """
    root = tmp_path / "client_repo"
    root.mkdir()
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(root))
    engine_config._reset_client_repo_root_cache()
    # resolve() collapses symlinked ancestors (macOS /var -> /private/var)
    # so tests compare apples to apples.
    yield root.resolve()
    engine_config._reset_client_repo_root_cache()


class TestHistoryWriterDefault:
    def test_writes_into_client_root(self, client_root):
        writer = HistoryWriter()
        assert writer.history_path == str(client_root / "history.md")
        result = writer.append_entry("intent", "action", "outcome")
        assert result["status"] == "recorded"
        assert (client_root / "history.md").exists()

    def test_does_not_touch_install_root(self, client_root):
        writer = HistoryWriter()
        writer.append_entry("intent", "action", "outcome")
        # The writer must resolve outside the install root. commonpath gives
        # us a portable (Windows-safe) containment check — if the history
        # path were inside INSTALL_ROOT, their commonpath would equal it.
        history_path = os.path.realpath(writer.history_path)
        install_root = os.path.realpath(engine_config.INSTALL_ROOT)
        assert os.path.commonpath([history_path, install_root]) != install_root


class TestHistoryStoreDefault:
    def test_data_dir_is_client_scoped(self, client_root):
        store = HistoryStore()
        assert store.data_dir == str(client_root / "data" / "memory")
        assert store.history_path == str(client_root / "history.md")


class TestDescriberDefault:
    def test_repo_path_defaults_to_client_root(self, client_root):
        d = RepoDescriber()
        assert d.repo_path == str(client_root)
        assert d.claude_md_path == str(client_root / "CLAUDE.md")
        assert d.hash_file == str(client_root / "data" / "memory" / ".describe_hash")

    def test_explicit_repo_path_wins_over_client_root(self, client_root, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        d = RepoDescriber(repo_path=str(other))
        assert d.repo_path == str(other)
        assert d.claude_md_path == str(other / "CLAUDE.md")


class TestMemoryConfigLazyAttrs:
    def test_history_file_resolves_against_client_root(self, client_root):
        assert memory_config.HISTORY_FILE == str(client_root / "history.md")

    def test_memory_data_dir_resolves_against_client_root(self, client_root):
        assert memory_config.MEMORY_DATA_DIR == str(client_root / "data" / "memory")

    def test_describe_hash_file_resolves_against_client_root(self, client_root):
        assert memory_config.DESCRIBE_HASH_FILE == str(
            client_root / "data" / "memory" / ".describe_hash"
        )


class TestInstallDataStaysShared:
    """Skills/implants/router stores index install-shipped content and must
    stay rooted at the install dir regardless of the client override."""

    def test_skills_store_uses_install_data_dir(self, client_root):
        from src.engine.config import INSTALL_DATA_DIR

        # Matches the binding in src/engine/skills.py / router.py / implants.py.
        assert INSTALL_DATA_DIR.endswith(os.sep + "data")
        assert not INSTALL_DATA_DIR.startswith(str(client_root))


class TestHistoryFailureReporting:
    """An unwritable history.md is reported, not hidden in a traceback."""

    @pytest.mark.asyncio
    async def test_unwritable_history_warns_once_and_is_reported(self, client_root, monkeypatch, caplog):
        import json

        import src.server as server

        monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
        server._history_errors.clear()
        server._history_warned.clear()
        real_open = open

        def refuse(path, *args, **kwargs):
            if os.path.basename(str(path)) == "history.md":
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("src.memory.history.open", refuse, raising=False)
        with caplog.at_level("WARNING", logger=server.logger.name):
            first = json.loads(await server.log_interaction("software_engineer", "q1", "r1"))
            assert server.drain_pending_logs(5)
            second = json.loads(await server.log_interaction("software_engineer", "q2", "r2"))
            assert server.drain_pending_logs(5)
        assert first["workspace"] == {"root": str(client_root), "source": "env"}
        assert first["pid"] == os.getpid()
        assert "history_last_error" not in first
        assert second["history_last_error"]["code"] == "history_unwritable"
        assert second["history_last_error"]["errno"] == 13
        assert second["history_last_error"]["path"] == str(client_root / "history.md")
        warnings = [r for r in caplog.records if "code=history_unwritable" in r.getMessage()]
        assert len(warnings) == 1 and warnings[0].exc_info is None
        assert not [r for r in caplog.records if r.levelname == "ERROR"]
        read = json.loads(await server.read_history())
        assert read["history_last_error"]["errno"] == 13
        assert read["workspace"]["root"] == str(client_root)

        monkeypatch.undo()
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(client_root))
        await server.log_interaction("software_engineer", "q3", "r3")
        assert server.drain_pending_logs(5)
        assert "history_last_error" not in json.loads(await server.read_history())


@pytest.mark.asyncio
async def test_semantic_read_readiness_error_still_reports_workspace(client_root, monkeypatch):
    import json

    import src.server as server

    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)

    async def not_ready(name):
        return "warming_up"

    monkeypatch.setattr(server, "_readiness_problem", not_ready)
    result = json.loads(await server.read_history(query="anything"))
    assert result["status"] == "warming_up"
    assert result["workspace"]["root"] == str(client_root)
    assert result["pid"] == os.getpid()
