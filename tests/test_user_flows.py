"""Personal and per-repository flows stored as Markdown in flows/.user, plus the editor API."""
import asyncio
import hashlib
import json
import subprocess

import httpx
import pytest
import pytest_asyncio

import src.server as server
from src.daemon.app import create_app
from src.daemon.workspaces import WorkspaceRegistry
from src.engine import config
from src.flows import FlowCatalog, FlowError
from src.user_flows import FlowLibrary, normalize_origin, parse_reference, repo_key


TOKEN = "u" * 48
BUILTIN = "# Review\n\nBuilt-in steps.\n"


def revision(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def install(tmp_path):
    flows = tmp_path / "install" / "flows"
    flows.mkdir(parents=True)
    (flows / "README.md").write_text("# Catalog\n", encoding="utf-8")
    (flows / "review.md").write_text(BUILTIN, encoding="utf-8")
    return flows


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin",
                    "https://someone:secret@github.com/Owner/Project.git"], check=True)
    return root


@pytest.fixture
def library(install, repo):
    return FlowLibrary(FlowCatalog(install), repo_root=repo)


def test_library_lives_in_git_ignored_flows_user(library, install):
    assert library.user_dir == install / ".user"
    result = library.save("mine", "# Mine\n\nStep.\n")
    assert result["status"] == "created"
    assert (install / ".user" / "common" / "mine.md").read_text() == "# Mine\n\nStep.\n"
    # The built-in catalog does not pick up personal files.
    assert FlowCatalog(install).ids() == ["review"]


def test_repository_ignores_every_dot_directory():
    ignored = subprocess.run(["git", "check-ignore", "-q", "flows/.user/common/example.md"],
                             cwd=config.INSTALL_ROOT)
    assert ignored.returncode == 0


@pytest.mark.parametrize("url, expected", [
    ("git@github.com:Owner/Repo.git", "github.com/owner/repo"),
    ("https://user:token@github.com/Owner/Repo.git", "github.com/owner/repo"),
    ("ssh://git@gitlab.example:2222/group/sub/repo.git", "gitlab.example/group/sub/repo"),
    ("/local/path", None),
])
def test_origin_normalization_drops_credentials(url, expected):
    assert normalize_origin(url) == expected


def test_repo_key_from_origin_and_fallback(repo, tmp_path):
    assert repo_key(repo) == ("github.com-owner-project", "github.com/owner/project")
    plain = tmp_path / "Plain Dir"
    plain.mkdir()
    key, origin = repo_key(plain)
    assert origin is None and key.startswith("plain-dir-")


@pytest.mark.parametrize("name, expected", [
    ("review", (None, "review")), ("user:mine", ("user", "mine")),
    ("repo:flows/x.md", ("repo", "x")), ("builtin:review.md", ("builtin", "review")),
])
def test_references(name, expected):
    assert parse_reference(name) == expected


@pytest.mark.parametrize("name", ["", "../x", "user:../x", "other:x", "README", "x y", "Mine"])
def test_invalid_references(name):
    with pytest.raises(FlowError, match="flow_invalid"):
        parse_reference(name)


def test_scopes_and_bare_name_precedence(library):
    library.save("mine", "# Mine for all\n")
    assert library.resolve("mine").id == "user:mine"
    library.save("mine", "# Mine here\n", scope="repo")
    assert library.resolve("mine").id == "repo:mine"
    assert library.resolve("user:mine").title == "Mine for all"
    listing = library.list()
    assert [f["id"] for f in listing["flows"]] == ["review", "user:mine", "repo:mine"]
    assert listing["repo"]["key"] == "github.com-owner-project"
    assert "secret" not in json.dumps(listing)
    assert [f["id"] for f in library.list("user")["flows"]] == ["user:mine"]


def test_builtin_is_read_only_and_never_silently_shadowed(library, install):
    with pytest.raises(FlowError, match="flow_read_only"):
        library.save("builtin:review", "# Changed\n")
    with pytest.raises(FlowError, match="flow_shadows_builtin"):
        library.save("review", "# Local\n")
    with pytest.raises(FlowError, match="override=true needs"):
        library.save("absent", "# X\n", override=True)
    saved = library.save("review", "# Review for me\n", override=True)
    assert saved["flow"]["overrides"] == "builtin:review"
    assert saved["flow"]["upstream_changed"] is False
    assert library.resolve("review").id == "user:review"
    entries = {f["id"]: f for f in library.list()["flows"]}
    assert entries["review"]["overridden_by"] == "user:review"
    assert (install / "review.md").read_text() == BUILTIN


def test_override_reports_upstream_change_and_deleting_it_restores_builtin(library, install):
    library.save("review", "# Mine\n", scope="repo", override=True)
    (install / "review.md").write_text("# Review v2\n", encoding="utf-8")
    flow = library.get("review")
    assert flow["flow"]["id"] == "repo:review" and flow["flow"]["upstream_changed"] is True
    assert flow["upstream"]["content"] == "# Review v2\n"
    # Saving again acknowledges the current built-in text.
    again = library.save("repo:review", "# Mine 2\n", override=True,
                         expected_revision=flow["flow"]["revision"])
    assert again["flow"]["upstream_changed"] is False
    library.delete("repo:review", expected_revision=again["flow"]["revision"])
    assert library.resolve("review").id == "builtin:review"


def test_revisions_prevent_lost_updates(library):
    created = library.save("mine", "# One\n")
    first = created["flow"]["revision"]
    library.save("mine", "# Two\n", expected_revision=first)
    with pytest.raises(FlowError, match="flow_conflict: current revision is " + revision("# Two\n")):
        library.save("mine", "# Three\n", expected_revision=first)
    with pytest.raises(FlowError, match="flow_conflict"):
        library.save("mine", "# Recreate\n")  # Creating needs the flow to be absent.
    assert library.save("mine", "# Two\n", expected_revision=revision("# Two\n"))["status"] == "unchanged"


def test_history_and_restore(library):
    library.save("mine", "# One\n")
    library.save("mine", "# Two\n", expected_revision=revision("# One\n"))
    history = library.get("user:mine")["history"]
    assert len(history) == 1 and not history[0]["deleted"]
    old = library.get("user:mine", version=history[0]["version"])
    assert old["content"] == "# One\n"
    restored = library.save("mine", old["content"], expected_revision=revision("# Two\n"))
    assert library.resolve("mine").content == "# One\n"
    library.delete("mine", expected_revision=restored["flow"]["revision"])
    history = library.history("user", "mine")
    assert history[0]["deleted"] and len(history) == 3
    with pytest.raises(FlowError, match="flow_not_found"):
        library.resolve("mine")


def test_plain_save_clears_a_stale_override_marker(library, install):
    saved = library.save("review", "# Mine\n", override=True)
    (install / "review.md").unlink()  # The built-in is removed upstream.
    assert library.resolve("review").metadata()["upstream_changed"] is True
    plain = library.save("user:review", "# Mine 2\n", expected_revision=saved["flow"]["revision"])
    assert "overrides" not in plain["flow"] and "upstream_changed" not in plain["flow"]
    assert not (library.user_dir / "common" / "review.meta.json").exists()


def test_unchanged_saves_still_update_the_override_marker(library, install):
    saved = library.save("review", "# Mine\n", override=True)
    revision_ = saved["flow"]["revision"]
    (install / "review.md").write_text("# Review v2\n", encoding="utf-8")
    assert library.resolve("review").metadata()["upstream_changed"] is True
    again = library.save("user:review", "# Mine\n", override=True, expected_revision=revision_)
    assert again["status"] == "unchanged" and again["flow"]["upstream_changed"] is False
    (install / "review.md").unlink()
    plain = library.save("user:review", "# Mine\n", expected_revision=revision_)
    assert plain["status"] == "unchanged" and "overrides" not in plain["flow"]


def test_failed_write_leaves_no_temporary_file(library, monkeypatch):
    import src.user_flows as user_flows
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(user_flows.os, "replace", fail)
    with pytest.raises(OSError):
        library.save("mine", "# Mine\n")
    assert not list((library.user_dir / "common").glob(".tmp-*"))


@pytest.mark.parametrize("content, error", [("", "empty"), ("   \n", "empty"),
                                            ("x" * (256 * 1024 + 1), "256 KiB")])
def test_content_validation(library, content, error):
    with pytest.raises(FlowError, match=error):
        library.save("mine", content)


def test_symlinked_flow_is_rejected(library, tmp_path):
    library.save("mine", "# Mine\n")
    path = library.user_dir / "common" / "mine.md"
    path.unlink()
    (tmp_path / "elsewhere.md").write_text("# Elsewhere\n")
    path.symlink_to(tmp_path / "elsewhere.md")
    with pytest.raises(FlowError, match="symlink"):
        library.save("mine", "# New\n", expected_revision=revision("# Elsewhere\n"))
    assert [f["id"] for f in library.list("user")["flows"]] == []


def test_repo_scope_needs_a_workspace(install):
    library = FlowLibrary(FlowCatalog(install), repo_error="workspace_required")
    with pytest.raises(FlowError, match="repo_scope_unavailable: workspace_required"):
        library.save("mine", "# Mine\n", scope="repo")
    listing = library.list()
    assert listing["repo"] == {"status": "unavailable",
                               "error": "repo_scope_unavailable: workspace_required"}
    assert library.save("mine", "# Mine\n")["status"] == "created"


def test_environment_override_for_library_location(install, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "elsewhere"))
    library = FlowLibrary(FlowCatalog(install))
    library.save("mine", "# Mine\n")
    assert (tmp_path / "elsewhere" / "common" / "mine.md").is_file()


# --- MCP tools ----------------------------------------------------------------------------

@pytest.fixture
def stdio(install, repo, monkeypatch):
    monkeypatch.setattr(server, "FlowCatalog", lambda: FlowCatalog(install))
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(repo))
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
    monkeypatch.delenv("AGENTS_USER_FLOWS_DIR", raising=False)
    config._reset_client_repo_root_cache()
    yield
    config._reset_client_repo_root_cache()


@pytest.mark.asyncio
async def test_chat_tools_manage_and_run_personal_flows(stdio, repo, install):
    tools = {tool.name for tool in await server.mcp.list_tools()}
    assert {"list_flows", "get_flow", "save_flow", "delete_flow", "run_flow"} <= tools
    created = json.loads(await server.save_flow("release", "# Release\n\nShip it.\n", scope="repo"))
    assert created["status"] == "created" and created["flow"]["id"] == "repo:release"
    listing = json.loads(await server.list_flows())
    assert [f["id"] for f in listing["flows"]] == ["review", "repo:release"]
    bundle = json.loads(await server.run_flow("release", request="no-merge"))
    assert bundle["status"] == "needs_execution"
    assert bundle["flow"]["id"] == "repo:release" and bundle["repo_path"] == str(repo)
    assert bundle["content"] == "# Release\n\nShip it.\n"
    current = json.loads(await server.get_flow("release"))
    conflict = json.loads(await server.save_flow("repo:release", "# X\n", expected_revision="0" * 64))
    assert conflict["status"] == "error" and "flow_conflict" in conflict["error"]
    deleted = json.loads(await server.delete_flow("repo:release", current["flow"]["revision"]))
    assert deleted["status"] == "deleted"
    # Nothing was written into the caller's repository.
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                          capture_output=True, text=True).stdout == ""


@pytest.mark.asyncio
async def test_list_flows_without_workspace_still_lists_builtin_and_personal(stdio, monkeypatch):
    await server.save_flow("mine", "# Mine\n")
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    listing = json.loads(await server.list_flows())
    assert [f["id"] for f in listing["flows"]] == ["review", "user:mine"]
    assert listing["repo"]["status"] == "unavailable"
    failed = json.loads(await server.save_flow("x", "# X\n", scope="repo"))
    assert "repo_scope_unavailable" in failed["error"]


# --- Browser editor -------------------------------------------------------------------------

@pytest_asyncio.fixture
async def editor(install, repo, tmp_path, monkeypatch):
    monkeypatch.setattr("src.daemon.flows_ui.FlowCatalog", lambda: FlowCatalog(install))
    registry = WorkspaceRegistry(tmp_path / "service")
    workspace = registry.register(repo)

    def runtime(port):
        from mcp.server.fastmcp import FastMCP
        return FastMCP("test", stateless_http=True, json_response=True), None

    app = create_app(registry.directory, TOKEN, runtime_loader=runtime)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if app.state.service.state != "starting":
                break
            await asyncio.sleep(.01)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url="http://127.0.0.1:8765") as http:
            yield http, workspace


UI = {"X-Agents-UI": "1", "Origin": "http://127.0.0.1:8765"}


async def login(http):
    issued = await http.post("/admin/ui/code", headers={"Authorization": "Bearer " + TOKEN})
    code = issued.json()["code"]
    assert issued.json()["url"].endswith("/ui#code=" + code)
    response = await http.post("/ui/api/session", json={"code": code}, headers=UI)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/ui" in cookie
    return code


@pytest.mark.asyncio
async def test_editor_access_control(editor):
    http, _ = editor
    page = await http.get("/ui")
    assert page.status_code == 200 and "nonce-" in page.headers["content-security-policy"]
    assert "{{NONCE}}" not in page.text
    assert "{{VERSION}}" not in page.text
    from src.version import agents_core_version
    assert f"Agents-Core {agents_core_version()}" in page.text
    assert page.text.count(f"Agents-Core {agents_core_version()}") == 1
    assert "python -m src.daemon flows-ui" in page.text  # sign-in page names the command
    assert "one sign-in per browser" in page.text.lower()
    assert (await http.get("/ui/api/flows")).status_code == 401
    assert (await http.post("/admin/ui/code")).status_code == 401
    # The bearer token does not open the editor API, and the page is loopback-only.
    bearer = await http.get("/ui/api/flows", headers={"Authorization": "Bearer " + TOKEN})
    assert bearer.status_code == 401
    assert (await http.get("/ui", headers={"Host": "attacker.example"})).status_code == 403
    code = await login(http)
    replay = await http.post("/ui/api/session", json={"code": code}, headers=UI)
    assert replay.status_code == 401
    assert (await http.get("/ui/api/flows")).status_code == 200
    body = {"id": "mine", "content": "# Mine\n"}
    for headers in ({"X-Agents-UI": "1", "Origin": "https://attacker.example"},
                    {"Origin": "http://127.0.0.1:8765"}):
        assert (await http.put("/ui/api/flow", json=body, headers=headers)).status_code == 403
    # The editor cannot reach MCP or administration with its session.
    assert (await http.post("/admin/drain", headers=UI)).status_code == 401


@pytest.mark.asyncio
async def test_editor_edits_flows_with_conflicts_and_repository_scope(editor, install, repo):
    http, workspace = editor
    await login(http)
    listing = (await http.get("/ui/api/flows", params={"workspace": workspace})).json()
    assert listing["repo"]["status"] == "available"
    workspaces = (await http.get("/ui/api/workspaces")).json()["workspaces"]
    assert workspaces == [{"id": workspace, "path": str(repo), "name": "project"}]
    copy = {"id": "repo:review", "content": "# Review here\n", "scope": "repo",
            "override": True, "workspace": workspace}
    created = await http.put("/ui/api/flow", json=copy, headers=UI)
    assert created.status_code == 200 and created.json()["flow"]["overrides"] == "builtin:review"
    stale = {**copy, "content": "# Other\n", "expected_revision": "0" * 64}
    conflict = await http.put("/ui/api/flow", json=stale, headers=UI)
    assert conflict.status_code == 409
    readonly = await http.put("/ui/api/flow", json={"id": "builtin:review", "content": "# X\n"}, headers=UI)
    assert readonly.status_code == 403
    flow = (await http.get("/ui/api/flow", params={"id": "review", "workspace": workspace})).json()
    assert flow["flow"]["id"] == "repo:review" and flow["upstream"]["content"] == BUILTIN
    removed = await http.request("DELETE", "/ui/api/flow", headers=UI, json={
        "id": "repo:review", "expected_revision": flow["flow"]["revision"], "workspace": workspace})
    assert removed.status_code == 200
    assert (install / "review.md").read_text() == BUILTIN
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                          capture_output=True, text=True).stdout == ""


def cookie_of(response):
    return response.headers["set-cookie"].split(";")[0].split("=", 1)[1]


@pytest.mark.asyncio
async def test_session_is_persistent_sliding_and_revocable(editor, tmp_path):
    import stat
    from src.daemon import flows_ui as module
    http, _ = editor
    service = http._transport.app.state.service
    now = [1_800_000_000.0]
    service.flows_ui.clock = lambda: now[0]
    assert (await http.get("/ui/api/flows")).status_code == 401
    code = await login(http)
    key_path = service.directory / module.KEY_FILE
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    key = key_path.read_bytes()
    first = (await http.get("/ui/api/flows")).headers["set-cookie"]
    assert code not in first and key.hex() not in first

    # One hour and more than eight hours idle: still signed in; each visit renews.
    for step in (3600, 9 * 3600):
        now[0] += step
        assert (await http.get("/ui/api/flows")).status_code == 200
    # A daemon restart or update builds a new instance over the same state directory.
    restarted = module.FlowsUI(service, clock=lambda: now[0])
    service.flows_ui = restarted
    assert (await http.get("/ui/api/flows")).status_code == 200
    # Day 20 renews the window, so day 40 is still inside it.
    for step in (20 * 86400, 20 * 86400):
        now[0] += step
        assert (await http.get("/ui/api/flows")).status_code == 200
    # Unused for more than 30 days: back to the sign-in page.
    stale = http.cookies.get(module.COOKIE, path="/ui")
    now[0] += module.SESSION_TTL + 1
    assert (await http.get("/ui/api/flows")).status_code == 401
    assert stale

    # Forged, malformed and future-dated cookies are refused.
    now[0] += 0
    await login(http)
    good = http.cookies.get(module.COOKIE, path="/ui")
    issued, nonce, signature = good.split(".")
    bad_values = [good[:-1] + ("0" if good[-1] != "0" else "1"), f"{issued}.{nonce}", "garbage", "",
                  f"{int(issued) + 10_000}.{nonce}.{service.flows_ui._sign(key, f'{int(issued) + 10_000}.{nonce}')}",
                  f"{issued}.{nonce}.{service.flows_ui._sign(b'x' * 32, f'{issued}.{nonce}')}"]
    for value in bad_values:
        http.cookies.clear()
        http.cookies.set(module.COOKIE, value, domain="127.0.0.1", path="/ui")
        assert (await http.get("/ui/api/flows")).status_code == 401, value

    # Non-ASCII bytes in a cookie are a 401, not a server error.
    http.cookies.clear()
    for raw in (f"{issued}.{nonce}.".encode() + "é".encode("latin-1"),
                "²".encode("latin-1") + f".{nonce}.{signature}".encode()):
        response = await http.get("/ui/api/flows", headers={"cookie": b"agents_flows_ui=" + raw})
        assert response.status_code == 401

    # Revocation: a new key invalidates the old cookie; a fresh sign-in works.
    http.cookies.clear()
    http.cookies.set(module.COOKIE, good, domain="127.0.0.1", path="/ui")
    assert (await http.get("/ui/api/flows")).status_code == 200
    module.replace_session_key(service.directory)
    assert key_path.read_bytes() != key
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert (await http.get("/ui/api/flows")).status_code == 401
    await login(http)
    assert (await http.get("/ui/api/flows")).status_code == 200


@pytest.mark.asyncio
async def test_session_key_stays_out_of_the_page_and_logs(editor, caplog):
    from src.daemon import flows_ui as module
    http, _ = editor
    service = http._transport.app.state.service
    await login(http)
    key = (service.directory / module.KEY_FILE).read_bytes()
    page = await http.get("/ui")
    assert key.hex() not in page.text and key.hex() not in caplog.text
    assert key.hex() not in (await http.get("/ui/api/flows")).text


def test_flows_ui_revoke_replaces_the_key(tmp_path, monkeypatch, capsys):
    from src.daemon import control, flows_ui as module
    monkeypatch.setenv("AGENTS_SERVICE_DIR", str(tmp_path / "state"))
    control.main(["flows-ui", "--revoke"])
    first = (tmp_path / "state" / module.KEY_FILE).read_bytes()
    control.main(["flows-ui", "--revoke"])
    second = (tmp_path / "state" / module.KEY_FILE).read_bytes()
    assert len(first) == len(second) == module.KEY_BYTES and first != second
    assert '"revoked"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_renewal_does_not_cross_a_revocation(editor, monkeypatch):
    from src.daemon import flows_ui as module
    http, _ = editor
    service = http._transport.app.state.service
    await login(http)
    original = service.flows_ui._api

    async def revoke_midway(request, path):
        response = await original(request, path)
        module.replace_session_key(service.directory)  # revoked while the request runs
        return response

    monkeypatch.setattr(service.flows_ui, "_api", revoke_midway)
    response = await http.get("/ui/api/flows")
    assert response.status_code == 200
    stale = http.cookies.get(module.COOKIE, path="/ui")
    monkeypatch.undo()
    http.cookies.clear()
    http.cookies.set(module.COOKIE, stale, domain="127.0.0.1", path="/ui")
    assert (await http.get("/ui/api/flows")).status_code == 401
