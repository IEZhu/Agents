"""Resolution of ``get_client_repo_root()`` — env → walk-up → start dir, or a call's workspace.

Issue #36: per-repo memory requires the client repo root to be resolved
dynamically so one global install can serve many client repos.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, ListRootsResult, Root

from src.daemon import workspaces
from src.daemon.workspaces import ClientContext, WorkspaceError, client_context, resolve_client_context
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

    def test_env_override_tilde_is_refused(self, monkeypatch):
        # `~` expands to the home directory, which would collect every project.
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", "~")
        with pytest.raises(engine_config.ClientRootError, match="home directory") as info:
            engine_config.get_client_repo_root()
        assert info.value.code == "workspace_unsafe"

    def test_env_override_expands_tilde_below_home(self, tmp_path, monkeypatch):
        project = tmp_path / "home" / "project"
        project.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", "~/project")
        assert engine_config.get_client_repo_root_info() == (str(project.resolve()), "env")

    def test_env_override_into_system_dir_is_refused(self, fake_windows, monkeypatch):
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(fake_windows / "System32"))
        with pytest.raises(engine_config.ClientRootError, match="Windows directory") as info:
            engine_config.get_client_repo_root()
        assert info.value.code == "workspace_unsafe"

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

    def test_markerless_cwd_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        # No ancestor of the start directory may carry a marker.
        monkeypatch.setattr(engine_config, "_CLIENT_ROOT_MARKERS", ("no-such-marker",))
        isolated = tmp_path / "no_markers_here"
        isolated.mkdir()
        monkeypatch.chdir(isolated)
        with pytest.raises(engine_config.ClientRootError, match="no .git or CLAUDE.md") as info:
            engine_config.get_client_repo_root()
        assert info.value.code == "workspace_required"

    def test_markerless_claude_project_dir_is_accepted(self, tmp_path, monkeypatch):
        # The client named this directory as the project.
        monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
        monkeypatch.setattr(engine_config, "_CLIENT_ROOT_MARKERS", ("no-such-marker",))
        project = tmp_path / "plain"
        project.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
        assert engine_config.get_client_repo_root_info() == (str(project.resolve()), "CLAUDE_PROJECT_DIR")


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
        with pytest.raises(WorkspaceError, match="^workspace_unsafe: refusing"):
            context.require_root()


class TestMoreUnsafeRoots:
    """Program directories, `C:\\Users` and the home directory hold no project."""

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.setattr(engine_config, "_CLIENT_ROOT_MARKERS", ("no-such-marker",))

    @pytest.mark.parametrize("variable", ["ProgramFiles", "ProgramFiles(x86)", "ProgramData"])
    def test_windows_program_directories(self, tmp_path, monkeypatch, variable):
        base = tmp_path / "prog"
        (base / "tool").mkdir(parents=True)
        monkeypatch.setattr(engine_config, "_is_windows", lambda: True)
        monkeypatch.setattr(engine_config, "_windows_directory", lambda: None)
        monkeypatch.setenv(variable, str(base))
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(base / "tool"))
        with pytest.raises(engine_config.ClientRootError, match=variable.replace("(", "\\(").replace(")", "\\)")):
            engine_config.get_client_repo_root()

    def test_windows_users_directory_is_exact(self, tmp_path, monkeypatch):
        users = tmp_path / "Users"
        (users / "me" / "project").mkdir(parents=True)
        monkeypatch.setattr(engine_config, "_is_windows", lambda: True)
        monkeypatch.setattr(engine_config, "_windows_directory", lambda: None)
        monkeypatch.setenv("SystemDrive", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(users))
        with pytest.raises(engine_config.ClientRootError, match="users directory"):
            engine_config.get_client_repo_root()
        engine_config._reset_client_repo_root_cache()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(users / "me" / "project"))
        assert engine_config.get_client_repo_root() == str((users / "me" / "project").resolve())

    def test_home_directory_is_refused_but_projects_below_are_not(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / "project").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(home))
        with pytest.raises(engine_config.ClientRootError, match="home directory"):
            engine_config.get_client_repo_root()
        engine_config._reset_client_repo_root_cache()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(home / "project"))
        assert engine_config.get_client_repo_root() == str((home / "project").resolve())

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX system directories")
    @pytest.mark.parametrize("path", ["/usr", "/var", "/home", "/etc", "/etc/ssh", "/usr/bin", "/usr/share/doc", "/bin"])
    def test_posix_system_directories(self, monkeypatch, path):
        if not os.path.isdir(path):
            pytest.skip(f"{path} does not exist")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", path)
        with pytest.raises(engine_config.ClientRootError, match="system directory"):
            engine_config.get_client_repo_root()

    @pytest.mark.parametrize("path", ["/usr/local", "/private/tmp", "/private/var/folders", "/tmp"])
    def test_posix_allowed_directories(self, path):
        if sys.platform == "win32":
            pytest.skip("POSIX paths")
        root = engine_config.Path(os.path.realpath(path))
        assert engine_config._unsafe_client_root_reason(root) is None

    def test_pytest_tmp_path_is_allowed(self, tmp_path):
        assert engine_config._unsafe_client_root_reason(tmp_path.resolve()) is None


def _project(path):
    """A directory carrying the project marker (the patched one under fake_windows)."""
    path.mkdir(parents=True)
    (path / engine_config._CLIENT_ROOT_MARKERS[0]).write_text("")
    return path


def _worktree(main, name):
    """A git worktree of the checkout at *main*, laid out as `git worktree add` does."""
    gitdir = main / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    tree = main / ".claude" / "worktrees" / name
    tree.mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {gitdir.as_posix()}\n")
    return tree


class FakeSession:
    """The roots side of an MCP ServerSession."""

    def __init__(self, uris=(), *, declared=True, error=None, hang=False, client="local-agent-mode-Agents-Core"):
        self.uris, self.declared, self.error, self.hang = list(uris), declared, error, hang
        self.client_params = SimpleNamespace(clientInfo=SimpleNamespace(name=client))
        self.asked = 0

    def check_client_capability(self, capability):
        assert capability.roots is not None
        return self.declared

    async def list_roots(self):
        self.asked += 1
        if self.hang:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        return ListRootsResult(roots=[Root(uri=uri) for uri in self.uris])


@pytest.fixture
def desktop(fake_windows, monkeypatch):
    """Started like the Claude desktop app's stdio servers: in System32, without CLAUDE_PROJECT_DIR."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
    monkeypatch.chdir(fake_windows / "System32")
    return fake_windows


class TestWorkspaceArgument:
    """A tool call names its workspace, which must lie inside the client's MCP roots.

    Regression: the Claude desktop app starts one stdio server for all its Code
    sessions, in C:\\Windows\\System32 without CLAUDE_PROJECT_DIR, and answers
    roots/list with the folders of every open session, so neither the process
    nor its roots tell which project a call belongs to.
    """

    def test_workspace_inside_a_root_resolves_to_its_project(self, desktop, tmp_path):
        project = _project(tmp_path / "project")
        (project / "pkg").mkdir()
        roots = [(tmp_path / "other").as_uri(), project.as_uri()]
        assert engine_config.client_root_from_workspace(str(project / "pkg"), roots) == str(project.resolve())

    def test_unmarked_workspace_is_used_as_named(self, desktop, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert engine_config.client_root_from_workspace(str(plain), [tmp_path.as_uri()]) == str(plain.resolve())

    def test_workspace_outside_the_roots_is_refused(self, desktop, tmp_path):
        inside, outside = _project(tmp_path / "inside"), _project(tmp_path / "outside")
        with pytest.raises(engine_config.ClientRootError, match="outside the client's MCP roots") as info:
            engine_config.client_root_from_workspace(str(outside), [inside.as_uri()])
        assert info.value.code == "workspace_invalid"

    @pytest.mark.parametrize("name", ["relative", "missing"])
    def test_relative_or_missing_workspace_is_refused(self, desktop, tmp_path, name):
        workspace = "relative/dir" if name == "relative" else str(tmp_path / "missing")
        with pytest.raises(engine_config.ClientRootError) as info:
            engine_config.client_root_from_workspace(workspace, [tmp_path.as_uri()])
        assert info.value.code == "workspace_invalid"

    def test_unsafe_workspace_is_refused_even_inside_a_root(self, desktop, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        with pytest.raises(engine_config.ClientRootError, match="home directory") as info:
            engine_config.client_root_from_workspace(str(home), [tmp_path.as_uri()])
        assert info.value.code == "workspace_unsafe"

    def test_worktree_resolves_to_its_main_checkout(self, tmp_path):
        # Claude Code gives a worktree session the main checkout as CLAUDE_PROJECT_DIR.
        main = tmp_path / "repo"
        (main / ".git").mkdir(parents=True)
        tree = _worktree(main, "feature")
        assert engine_config.client_root_from_workspace(str(tree), [tree.as_uri()]) == str(main.resolve())

    def test_submodule_stays_itself(self, tmp_path):
        # A submodule's .git file points to modules/<name>, which has no commondir.
        modules = tmp_path / "super" / ".git" / "modules" / "lib"
        modules.mkdir(parents=True)
        lib = tmp_path / "super" / "lib"
        lib.mkdir()
        (lib / ".git").write_text(f"gitdir: {modules.as_posix()}\n")
        assert engine_config.client_root_from_workspace(str(lib), [lib.as_uri()]) == str(lib.resolve())

    def test_session_directory_resolves_to_its_project(self, desktop, tmp_path):
        # The shared service's bridge reports the directory its client started it in (#253).
        project = _project(tmp_path / "project")
        (project / "pkg").mkdir()
        assert engine_config.client_root_from_directory(str(project / "pkg")) == str(project.resolve())

    @pytest.mark.parametrize("directory,code", [("relative", "workspace_invalid"), (None, "workspace_unsafe")])
    def test_session_directory_refuses_relative_and_unsafe(self, desktop, directory, code):
        with pytest.raises(engine_config.ClientRootError) as info:
            engine_config.client_root_from_directory(directory or str(desktop / "System32"))
        assert info.value.code == code

    def test_uri_percent_escapes_are_decoded(self, tmp_path):
        folder = tmp_path / "Доработки 1С"
        folder.mkdir()
        assert engine_config._root_uri_path(folder.as_uri()) == folder

    @pytest.mark.parametrize("uri", ["https://example.com/repo", "untitled:Untitled-1"])
    def test_other_schemes_are_no_local_path(self, uri):
        assert engine_config._root_uri_path(uri) is None

    def test_remote_host_is_no_local_path_on_posix(self, monkeypatch):
        monkeypatch.setattr(engine_config, "_is_windows", lambda: False)
        assert engine_config._root_uri_path("file://server/share/repo") is None

    @pytest.mark.skipif(sys.platform != "win32", reason="UNC paths exist on Windows")
    def test_remote_host_is_a_unc_path_on_windows(self):
        assert engine_config._root_uri_path("file://server/share/repo") == Path(r"\\server\share\repo")


class TestResolveClientContext:
    """Each call resolves its own workspace; nothing is pinned for the process."""

    @pytest.mark.asyncio
    async def test_each_call_gets_its_own_project(self, desktop, tmp_path):
        first, second = _project(tmp_path / "first"), _project(tmp_path / "second")
        session = FakeSession([first.as_uri(), second.as_uri()])
        for project in (first, second, first):
            context = await resolve_client_context(SimpleNamespace(session=session), str(project))
            assert (context.require_root(), context.source) == (project.resolve(), "workspace")
        assert session.asked == 3
        # The process-wide root is untouched: the stand-in System32 is still refused.
        with pytest.raises(engine_config.ClientRootError):
            engine_config.get_client_repo_root()

    @pytest.mark.asyncio
    async def test_without_workspace_the_error_says_to_pass_one(self, desktop, tmp_path):
        session = FakeSession([_project(tmp_path / "project").as_uri()])
        context = await resolve_client_context(SimpleNamespace(session=session))
        with pytest.raises(WorkspaceError, match="^workspace_unsafe: refusing .* pass workspace"):
            context.require_root()
        assert session.asked == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ctx", [None, SimpleNamespace(), SimpleNamespace(session=FakeSession(declared=False))])
    async def test_a_client_without_roots_keeps_the_process_root(self, desktop, tmp_path, ctx):
        context = await resolve_client_context(ctx, str(_project(tmp_path / "project")))
        with pytest.raises(WorkspaceError, match="^workspace_unsafe: refusing") as info:
            context.require_root()
        assert "declares no MCP roots" in str(info.value)

    @pytest.mark.asyncio
    async def test_a_client_without_roots_still_uses_a_usable_cwd(self, tmp_path, monkeypatch):
        for name in ("AGENTS_CLIENT_REPO_ROOT", "CLAUDE_PROJECT_DIR", "AGENTS_TRANSPORT"):
            monkeypatch.delenv(name, raising=False)
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        ctx = SimpleNamespace(session=FakeSession(declared=False))
        context = await resolve_client_context(ctx, str(tmp_path / "anything"))
        assert (context.source, context.require_root()) == ("cwd", tmp_path.resolve())

    @pytest.mark.asyncio
    async def test_the_override_stays_authoritative(self, desktop, tmp_path, monkeypatch):
        override = _project(tmp_path / "override")
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(override))
        session = FakeSession([tmp_path.as_uri()])
        context = await resolve_client_context(SimpleNamespace(session=session), str(_project(tmp_path / "project")))
        assert (context.source, context.require_root(), session.asked) == ("env", override.resolve(), 0)

    @pytest.mark.asyncio
    async def test_a_refused_override_gets_no_workspace_hint(self, desktop, monkeypatch):
        # The override decides alone, so asking for workspace would only cost a retry.
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(desktop / "System32"))
        context = await resolve_client_context(SimpleNamespace(session=FakeSession()))
        assert context.error.startswith("workspace_unsafe: refusing")
        assert "AGENTS_CLIENT_REPO_ROOT" in context.error and "pass workspace" not in context.error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure,reported", [
        ({"hang": True}, "TimeoutError()"),
        ({"error": McpError(ErrorData(code=-32601, message="Method not found"))}, "McpError('Method not found')"),
    ])
    async def test_a_failed_roots_request_refuses_the_call(self, desktop, tmp_path, monkeypatch, failure, reported):
        monkeypatch.setattr(workspaces, "MCP_ROOTS_TIMEOUT_SECONDS", 0.01)
        context = await resolve_client_context(SimpleNamespace(session=FakeSession(**failure)), str(tmp_path))
        assert context.error.startswith("workspace_invalid: could not read the client's MCP roots")
        assert context.error.endswith(reported)

    @pytest.mark.asyncio
    async def test_http_ignores_the_argument(self, tmp_path):
        identity = ClientContext("r", "http", "w", tmp_path)
        session = FakeSession([tmp_path.as_uri()])
        request = SimpleNamespace(state=SimpleNamespace(client_context=identity))
        ctx = SimpleNamespace(session=session, request_context=SimpleNamespace(request=request))
        assert await resolve_client_context(ctx, str(tmp_path)) is identity
        assert session.asked == 0
        assert workspaces.workspace_inputs(ctx) is None

    def test_workspace_inputs_describe_the_process(self, desktop):
        inputs = workspaces.workspace_inputs(SimpleNamespace(session=FakeSession()))
        assert inputs == {"cwd": os.getcwd(), "claude_project_dir": False, "agents_client_repo_root": False,
                          "client": "local-agent-mode-Agents-Core", "roots": True}


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


def test_darwin_case_variants_are_unsafe(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_config.sys, "platform", "darwin")
    monkeypatch.setattr(engine_config, "_real", lambda path: engine_config.Path(path) if path else None)
    monkeypatch.setattr(engine_config, "_home_directory", lambda: engine_config.Path("/Users/Alex"))
    assert "system directory" in engine_config._unsafe_client_root_reason(engine_config.Path("/users"))
    assert "system directory" in engine_config._unsafe_client_root_reason(engine_config.Path("/system/library"))
    assert "home directory" in engine_config._unsafe_client_root_reason(engine_config.Path("/users/alex"))
    assert engine_config._unsafe_client_root_reason(engine_config.Path("/Users/Alex/project")) is None
