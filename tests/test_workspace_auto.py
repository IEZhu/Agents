"""Automatic session identity without creating repository marker files."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import ListRootsResult, Root

from src.daemon.workspaces import resolve_client_context
from src.engine import config


@pytest.fixture(autouse=True)
def isolated_session(monkeypatch):
    for name in ("AGENTS_CLIENT_REPO_ROOT", "CLAUDE_PROJECT_DIR", "AGENTS_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    config._reset_client_repo_root_cache()
    yield
    config._reset_client_repo_root_cache()


def test_empty_session_directory_resolves_without_creating_markers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "_CLIENT_ROOT_MARKERS", ("absent-marker",))
    assert config.get_client_repo_root_info() == (str(tmp_path.resolve()), "cwd")
    assert config.client_root_from_directory(str(tmp_path)) == str(tmp_path.resolve())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_unverified_tool_argument_cannot_redirect_empty_session(tmp_path, monkeypatch):
    current, other = tmp_path / "current", tmp_path / "other"
    current.mkdir(); other.mkdir()
    monkeypatch.chdir(current)
    ctx = SimpleNamespace(session=SimpleNamespace(check_client_capability=lambda _: False))
    result = await resolve_client_context(ctx, str(other))
    assert result.require_root() == current.resolve()


@pytest.mark.parametrize("marked", [False, True])
def test_installation_cwd_never_becomes_automatic_project(tmp_path, monkeypatch, marked):
    install = tmp_path / "installation"
    child = install / "runtime"
    child.mkdir(parents=True)
    if marked:
        (install / "CLAUDE.md").write_text("installation instructions")
    monkeypatch.setattr(config, "INSTALL_ROOT", str(install))
    monkeypatch.chdir(child)
    for resolve in (config.get_client_repo_root, lambda: config.client_root_from_directory(str(child))):
        with pytest.raises(config.ClientRootError, match="installation|runtime") as error:
            resolve()
        assert error.value.code == "workspace_unsafe"


def test_extension_runtime_cwd_is_not_a_project(tmp_path, monkeypatch):
    extension = tmp_path / ".vscode" / "extensions" / "openai.codex" / "bin"
    extension.mkdir(parents=True)
    monkeypatch.chdir(extension)
    with pytest.raises(config.ClientRootError, match="runtime"):
        config.get_client_repo_root()


def test_native_agents_marker_groups_project_subdirectories(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("project instructions")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    assert config.get_client_repo_root() == str(tmp_path.resolve())


def test_global_instructions_do_not_merge_empty_session_folders(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "AGENTS.md").write_text("global instructions")
    child = profile / "empty-session"
    child.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(profile))
    monkeypatch.chdir(child)
    assert config.get_client_repo_root() == str(child.resolve())


def test_removed_cwd_does_not_fall_back_to_installation(monkeypatch):
    def unavailable():
        raise FileNotFoundError("removed")
    monkeypatch.setattr(os, "getcwd", unavailable)
    with pytest.raises(config.ClientRootError) as error:
        config.get_client_repo_root()
    assert error.value.code == "workspace_required"


class RootsSession:
    def __init__(self, paths):
        self.paths = paths
        self.asked = 0

    def check_client_capability(self, _):
        return True

    async def list_roots(self):
        self.asked += 1
        return ListRootsResult(roots=[Root(uri=path.as_uri()) for path in self.paths])


@pytest.mark.asyncio
async def test_shared_process_uses_current_unique_root_without_pinning(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir(); second.mkdir()
    monkeypatch.chdir(tmp_path.anchor)
    session = RootsSession([first])
    ctx = SimpleNamespace(session=session)
    assert (await resolve_client_context(ctx)).require_root() == first.resolve()
    session.paths = [first, second]
    assert (await resolve_client_context(ctx)).root is None
    assert (await resolve_client_context(ctx, str(second))).require_root() == second.resolve()
    session.paths = [second]
    assert (await resolve_client_context(ctx)).require_root() == second.resolve()
    assert session.asked == 4


@pytest.mark.asyncio
async def test_override_beats_unique_root(tmp_path, monkeypatch):
    own, other = tmp_path / "own", tmp_path / "other"
    own.mkdir(); other.mkdir()
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(own))
    session = RootsSession([other])
    result = await resolve_client_context(SimpleNamespace(session=session))
    assert result.require_root() == own.resolve()
    assert session.asked == 0
