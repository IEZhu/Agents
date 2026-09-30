"""Resolution of ``get_client_repo_root()`` — env → walk-up → start dir.

Issue #36: per-repo memory requires the client repo root to be resolved
dynamically so one global install can serve many client repos.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from src.daemon.workspaces import WorkspaceError, client_context
from src.engine import config as engine_config


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear the memoized client root between tests."""
    engine_config._reset_client_repo_root_cache()
    yield
    engine_config._reset_client_repo_root_cache()


@pytest.fixture
def fake_windows(tmp_path, monkeypatch):
    """A stand-in for ``%SystemRoot%`` under tmp_path, on any platform.

    The marker is renamed to something that exists nowhere, so the walk-up
    cannot escape tmp_path into a marked ancestor: a root falls back to its
    start directory, as C:\\Windows\\System32 did.
    """
    windows = tmp_path / "Windows"
    (windows / "System32").mkdir(parents=True)
    monkeypatch.setattr(engine_config, "_windows_directory", lambda: windows.resolve())
    monkeypatch.setattr(engine_config, "_CLIENT_ROOT_MARKERS", ("no-such-marker",))
    monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
    return windows


class TestResolutionOrder:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path))
        # Move cwd somewhere unrelated to prove env takes priority. Use a
        # tmp subdir rather than "/" so the test is portable to Windows.
        unrelated = tmp_path / "unrelated_cwd"
        unrelated.mkdir()
        monkeypatch.chdir(unrelated)
        assert engine_config.get_client_repo_root() == os.path.realpath(str(tmp_path))

    def test_env_override_expands_tilde(self, monkeypatch):
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", "~")
        resolved = engine_config.get_client_repo_root()
        assert resolved == os.path.realpath(os.path.expanduser("~"))

    def test_env_override_realpaths_symlink(self, tmp_path, monkeypatch):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(real)
        except OSError as exc:
            # Windows and some restricted CI sandboxes deny symlink creation.
            pytest.skip(f"symlink creation not supported here: {exc}")
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

    def test_claude_project_dir_beats_cwd(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
        project = tmp_path / "project"
        (project / ".git").mkdir(parents=True)
        (project / "pkg").mkdir()
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / ".git").mkdir(parents=True)
        monkeypatch.chdir(elsewhere)
        # Claude Code exports its project directory to the servers it spawns;
        # the walk-up starts there, not in the server's cwd.
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project / "pkg"))
        assert engine_config.get_client_repo_root() == str(project.resolve())

    def test_env_override_beats_claude_project_dir(self, tmp_path, monkeypatch):
        override = tmp_path / "override"
        override.mkdir()
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(override))
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        assert engine_config.get_client_repo_root() == os.path.realpath(str(override))

    def test_missing_claude_project_dir_falls_back_to_cwd(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "deleted"))
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


class TestUnsafeRootsRefused:
    """An inferred root outside any project fails loudly instead of receiving memory.

    Regression: the Claude desktop app starts stdio servers in
    C:\\Windows\\System32 without a project hint, and log_interaction appended
    to C:\\Windows\\System32\\history.md.
    """

    @pytest.mark.skipif(sys.platform != "win32", reason="needs the real Windows directory")
    def test_windows_system32_cwd_is_refused(self, monkeypatch):
        monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
        monkeypatch.chdir(os.path.join(os.environ["SystemRoot"], "System32"))
        with pytest.raises(engine_config.ClientRootError, match="refusing"):
            engine_config.get_client_repo_root()

    def test_filesystem_root_cwd_is_refused(self, tmp_path, monkeypatch):
        # launchd and systemd start services in "/"; a marker there changes nothing.
        monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
        monkeypatch.chdir(tmp_path.anchor)
        with pytest.raises(engine_config.ClientRootError, match="filesystem root"):
            engine_config.get_client_repo_root()

    def test_cwd_inside_windows_directory_is_refused(self, fake_windows, monkeypatch):
        monkeypatch.chdir(fake_windows / "System32")
        with pytest.raises(engine_config.ClientRootError, match="inside the Windows directory"):
            engine_config.get_client_repo_root()

    def test_marker_inside_windows_directory_is_refused(self, fake_windows, monkeypatch):
        # A marker there, say a CLAUDE.md written by describe_repo, is no project.
        (fake_windows / engine_config._CLIENT_ROOT_MARKERS[0]).write_text("")
        monkeypatch.chdir(fake_windows / "System32")
        with pytest.raises(engine_config.ClientRootError, match="inside the Windows directory"):
            engine_config.get_client_repo_root()

    def test_claude_project_dir_inside_windows_directory_is_refused(self, fake_windows, monkeypatch):
        # e.g. `claude` started from an elevated prompt, which opens in System32
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(fake_windows / "System32"))
        with pytest.raises(engine_config.ClientRootError, match="from CLAUDE_PROJECT_DIR"):
            engine_config.get_client_repo_root()

    def test_stdio_memory_tools_get_workspace_required(self, fake_windows, monkeypatch):
        monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
        monkeypatch.chdir(fake_windows / "System32")
        context = client_context()
        with pytest.raises(WorkspaceError, match="^workspace_required: refusing"):
            context.require_root()


class TestInstallRootUnchanged:
    """Install-scoped constants must not drift with the client root."""

    def test_install_paths_are_stable(self, tmp_path, monkeypatch):
        install_data_dir = engine_config.INSTALL_DATA_DIR
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
        # tests/conftest.py points INSTALL_DATA_DIR at a temporary copy (issue
        # #68); it must still not follow the client root.
        assert engine_config.INSTALL_DATA_DIR == install_data_dir

    def test_install_data_dir_defaults_under_install_root(self):
        # The suite's config has INSTALL_DATA_DIR redirected, so check the
        # default on a separate, unpatched load of the module.
        spec = importlib.util.spec_from_file_location("_unpatched_config", engine_config.__file__)
        unpatched = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(unpatched)
        install_root = os.path.realpath(unpatched.INSTALL_ROOT)
        assert os.path.realpath(unpatched.INSTALL_DATA_DIR) == os.path.join(install_root, "data")


class TestFastembedCacheDir:
    """FASTEMBED_CACHE_DIR is read at import time, so check fresh module loads."""

    @staticmethod
    def _load_config():
        spec = importlib.util.spec_from_file_location("_fresh_config", engine_config.__file__)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_value_falls_back_to_default(self, monkeypatch, blank):
        # The uncommented `FASTEMBED_CACHE_DIR=` line from env.example.
        monkeypatch.setenv("FASTEMBED_CACHE_DIR", blank)
        assert self._load_config().FASTEMBED_CACHE_DIR == os.path.expanduser("~/.cache/fastembed")

    def test_explicit_value_is_expanded(self, monkeypatch):
        monkeypatch.setenv("FASTEMBED_CACHE_DIR", "~/models")
        assert self._load_config().FASTEMBED_CACHE_DIR == os.path.expanduser("~/models")


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
    @pytest.mark.parametrize("first_allows_fallback", [True, False])
    def test_memory_and_flows_share_one_pinned_root(self, tmp_path, monkeypatch, first_allows_fallback):
        first, second = tmp_path / "first", tmp_path / "second"
        first.mkdir()
        second.mkdir()
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(first))
        assert engine_config.get_client_repo_root(
            allow_install_fallback=first_allows_fallback) == str(first.resolve())
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(second))
        assert engine_config.get_client_repo_root(
            allow_install_fallback=not first_allows_fallback) == str(first.resolve())
        assert engine_config.get_client_repo_root() == str(first.resolve())

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
