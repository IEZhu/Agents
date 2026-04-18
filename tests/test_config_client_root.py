"""Resolution of ``get_client_repo_root()`` — env → walk-up → cwd.

Issue #36: per-repo memory requires the client repo root to be resolved
dynamically so one global install can serve many client repos.
"""

from __future__ import annotations

import os

import pytest

from src.engine import config as engine_config


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear the memoized client root between tests."""
    engine_config._reset_client_repo_root_cache()
    yield
    engine_config._reset_client_repo_root_cache()


class TestResolutionOrder:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path))
        # Move cwd somewhere unrelated to prove env takes priority.
        monkeypatch.chdir("/")
        assert engine_config.get_client_repo_root() == os.path.realpath(str(tmp_path))

    def test_env_override_expands_tilde(self, monkeypatch):
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", "~")
        resolved = engine_config.get_client_repo_root()
        assert resolved == os.path.realpath(os.path.expanduser("~"))

    def test_env_override_realpaths_symlink(self, tmp_path, monkeypatch):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(link))
        assert engine_config.get_client_repo_root() == str(real.resolve())

    def test_walk_up_finds_git_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
        (tmp_path / ".git").mkdir()
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert engine_config.get_client_repo_root() == str(tmp_path.resolve())

    def test_walk_up_accepts_git_file_worktree(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
        # Git worktrees use a .git *file*, not a directory.
        (tmp_path / ".git").write_text("gitdir: /elsewhere")
        nested = tmp_path / "x" / "y"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert engine_config.get_client_repo_root() == str(tmp_path.resolve())

    def test_walk_up_accepts_claude_md_marker(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
        (tmp_path / "CLAUDE.md").write_text("# managed section")
        nested = tmp_path / "sub"
        nested.mkdir()
        monkeypatch.chdir(nested)
        assert engine_config.get_client_repo_root() == str(tmp_path.resolve())

    def test_cwd_unavailable_falls_back_to_install_root(self, monkeypatch, caplog):
        """os.getcwd() raises FileNotFoundError when the cwd was deleted.

        Long-running daemons started from ephemeral dirs hit this; without
        the guard the first memory-tool call would crash the session.
        """
        monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)

        def _raise_cwd():
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(engine_config.os, "getcwd", _raise_cwd)
        with caplog.at_level("WARNING", logger=engine_config.__name__):
            resolved = engine_config.get_client_repo_root()
        assert resolved == engine_config.INSTALL_ROOT
        assert any("cwd unavailable" in rec.message for rec in caplog.records)

    def test_cwd_fallback_when_no_marker(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
        isolated = tmp_path / "no_markers_here"
        isolated.mkdir()
        monkeypatch.chdir(isolated)
        # Walk-up will still find `/` likely lacking markers — but tmp_path
        # itself is under / so it may hit a distant marker. Only assert we
        # returned *some* absolute path, not that it equals cwd — real
        # filesystems often have .git at / or elsewhere in the ancestry.
        resolved = engine_config.get_client_repo_root()
        assert os.path.isabs(resolved)


class TestInstallRootUnchanged:
    """Install-scoped constants must not drift with the client root."""

    def test_install_paths_are_stable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path))
        # Client-scoped values shift...
        engine_config._reset_client_repo_root_cache()
        assert engine_config.get_client_repo_root() == os.path.realpath(str(tmp_path))
        # ...but install-scoped values do not. Assert structural relationships
        # (direct children of INSTALL_ROOT) rather than the repo folder name,
        # which can differ across CI checkouts.
        install_root = os.path.realpath(engine_config.INSTALL_ROOT)
        assert os.path.realpath(engine_config.AGENTS_DIR) == os.path.join(install_root, "agents")
        assert os.path.realpath(engine_config.SKILLS_DIR) == os.path.join(install_root, "skills")
        assert os.path.realpath(engine_config.IMPLANTS_DIR) == os.path.join(install_root, "implants")
        assert os.path.realpath(engine_config.INSTALL_DATA_DIR) == os.path.join(install_root, "data")


class TestDeprecatedAliases:
    """PEP 562 aliases preserve `from src.engine.config import REPO_ROOT` callsites."""

    def test_repo_root_alias_maps_to_install_root(self):
        assert engine_config.REPO_ROOT == engine_config.INSTALL_ROOT

    def test_data_dir_alias_maps_to_install_data_dir(self):
        assert engine_config.DATA_DIR == engine_config.INSTALL_DATA_DIR

    def test_debug_log_dir_resolves_client_scoped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path))
        engine_config._reset_client_repo_root_cache()
        assert engine_config.DEBUG_LOG_DIR == os.path.join(
            os.path.realpath(str(tmp_path)), "logs"
        )


class TestCacheReset:
    def test_reset_lets_tests_swap_roots(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path / "a"))
        (tmp_path / "a").mkdir()
        engine_config._reset_client_repo_root_cache()
        first = engine_config.get_client_repo_root()

        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path / "b"))
        (tmp_path / "b").mkdir()
        # Without reset, the lru_cache returns the old value.
        assert engine_config.get_client_repo_root() == first
        engine_config._reset_client_repo_root_cache()
        assert engine_config.get_client_repo_root() == os.path.realpath(
            str(tmp_path / "b")
        )
