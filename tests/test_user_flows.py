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


def test_listing_includes_content_only_on_request(library):
    library.save("mine", "# Mine\n\nfindable phrase\n")
    library.save("here", "# Here\n\nrepo phrase\n", scope="repo")
    assert all("content" not in f for f in library.list()["flows"])
    listing = library.list(with_content=True)
    assert {f["id"]: f["content"] for f in listing["flows"]}["user:mine"] == "# Mine\n\nfindable phrase\n"
    assert all(isinstance(f["content"], str) for f in listing["flows"])
    group = library.repositories(with_content=True)[0]
    assert group["flows"][0]["content"] == "# Here\n\nrepo phrase\n"
    assert all("content" not in f for g in library.repositories() for f in g["flows"])


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
    assert ".venv/bin/python -m src.daemon flows-ui" in page.text  # sign-in page names the command
    assert "automatic sign-in works only for a browser of the os user" in page.text.lower()
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
    assert all("content" not in f for f in listing["flows"])
    with_text = (await http.get("/ui/api/flows", params={"workspace": workspace, "with_content": "1"})).json()
    assert all("content" in f for f in with_text["flows"])
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

    huge = "9" * 5000
    http.cookies.clear()
    http.cookies.set(module.COOKIE, f"{huge}.{nonce}.{signature}", domain="127.0.0.1", path="/ui")
    assert (await http.get("/ui/api/flows")).status_code == 401
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


# --- Automatic sign-in for the daemon's OS user ------------------------------------------

def peer_stub(service, result):
    calls = []

    def check(client, server):
        calls.append((client, server))
        return result

    service.flows_ui.peer_check = check
    return calls


@pytest.mark.asyncio
async def test_automatic_sign_in_for_the_daemon_user(editor):
    from src.daemon import flows_ui as module
    http, _ = editor
    service = http._transport.app.state.service
    calls = peer_stub(service, True)
    assert (await http.get("/ui/api/flows")).status_code == 401  # a GET never signs in
    assert calls == []
    response = await http.post("/ui/api/session", json={}, headers=UI)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/ui" in cookie
    assert [(client[0], server) for client, server in calls] == [("127.0.0.1", ("127.0.0.1", 8765))]
    assert (await http.get("/ui/api/flows")).status_code == 200
    # Revocation still ends the session; the same user simply signs in again.
    module.replace_session_key(service.directory)
    assert (await http.get("/ui/api/flows")).status_code == 401
    assert (await http.post("/ui/api/session", json={}, headers=UI)).status_code == 200
    assert (await http.get("/ui/api/flows")).status_code == 200


@pytest.mark.asyncio
async def test_automatic_sign_in_refusals(editor):
    http, _ = editor
    service = http._transport.app.state.service
    calls = peer_stub(service, False)
    response = await http.post("/ui/api/session", json={}, headers=UI)
    assert response.status_code == 401 and response.json() == {"error": "sign_in_required"}
    assert "set-cookie" not in response.headers and len(calls) == 1
    assert (await http.get("/ui/api/flows")).status_code == 401

    calls = peer_stub(service, True)
    # A cross-site page cannot reach the check: Origin and the custom header come first.
    for headers in ({"X-Agents-UI": "1"}, {"Origin": "http://127.0.0.1:8765"},
                    {"X-Agents-UI": "1", "Origin": "http://attacker.example"}):
        assert (await http.post("/ui/api/session", json={}, headers=headers)).status_code == 403
    assert (await http.post("/ui/api/session", json={}, headers={**UI, "Host": "attacker.example"})).status_code == 403
    # A wrong code or a malformed body never falls through to the automatic path.
    for body in ({"code": "bogus"}, {"code": None}, {"code": 5}):
        response = await http.post("/ui/api/session", json=body, headers=UI)
        assert response.status_code == 401 and response.json() == {"error": "code_invalid"}
    for raw in (b"not json", b"[]"):
        response = await http.post("/ui/api/session", content=raw, headers=UI)
        assert response.status_code == 401 and response.json() == {"error": "code_invalid"}
    assert calls == []
    assert (await http.get("/ui/api/flows")).status_code == 401



@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Forwarded-Host", "X-Real-IP"])
async def test_forwarded_requests_never_sign_in_automatically(editor, header):
    http, _ = editor
    service = http._transport.app.state.service
    calls = peer_stub(service, True)
    response = await http.post("/ui/api/session", json={}, headers={**UI, header: "127.0.0.1:54321"})
    assert response.status_code == 401 and response.json() == {"error": "sign_in_required"}
    assert calls == [] and "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_automatic_sign_in_can_be_turned_off(editor):
    from src.daemon import flows_ui as module
    http, _ = editor
    service = http._transport.app.state.service
    calls = peer_stub(service, True)
    module.set_auto_sign_in(service.directory, False)
    assert (await http.post("/ui/api/session", json={}, headers=UI)).status_code == 401
    assert calls == []
    await login(http)  # the one-use code still works
    assert (await http.get("/ui/api/flows")).status_code == 200
    http.cookies.clear()
    module.set_auto_sign_in(service.directory, True)
    assert (await http.post("/ui/api/session", json={}, headers=UI)).status_code == 200


def test_flows_ui_auto_switch_and_revoke_report_the_state(tmp_path, monkeypatch, capsys):
    from src.daemon import control, flows_ui as module
    state = tmp_path / "state"
    monkeypatch.setenv("AGENTS_SERVICE_DIR", str(state))
    control.main(["flows-ui", "--auto", "on"])
    assert json.loads(capsys.readouterr().out) == {"auto_sign_in": "on"}
    assert not (state / module.KEY_FILE).exists()  # switching on revokes nothing
    control.main(["flows-ui", "--auto", "off"])
    off = json.loads(capsys.readouterr().out)
    assert off["auto_sign_in"] == "off" and off["state"] == "revoked" and "one-use code" in off["note"]
    assert not module.auto_sign_in_enabled(state)
    key = (state / module.KEY_FILE).read_bytes()
    control.main(["flows-ui", "--revoke"])
    revoked = json.loads(capsys.readouterr().out)
    assert revoked["state"] == "revoked" and revoked["auto_sign_in"] == "off" and "one-use code" in revoked["note"]
    assert (state / module.KEY_FILE).read_bytes() != key
    control.main(["flows-ui", "--revoke", "--auto", "on"])
    revoked = json.loads(capsys.readouterr().out)
    assert revoked["auto_sign_in"] == "on" and "by themselves" in revoked["note"]
    assert module.auto_sign_in_enabled(state)


def test_daemon_reports_the_real_socket_peer():
    # uvicorn's default proxy_headers=True would let any loopback caller pick scope["client"]
    # with X-Forwarded-For, and so choose whose connection the sign-in check inspects.
    import ast
    import inspect
    from src.daemon import bootstrap
    calls = [node for node in ast.walk(ast.parse(inspect.getsource(bootstrap)))
             if isinstance(node, ast.Call) and ast.unparse(node.func) == "uvicorn.run"]
    assert len(calls) == 1
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in calls[0].keywords}
    assert keywords.get("proxy_headers") == "False"


@pytest.mark.asyncio
@pytest.mark.parametrize("during_check", ["auto_off", "revoke", "auto_off_and_revoke"])
async def test_no_automatic_session_outlives_a_concurrent_switch_or_revoke(editor, during_check):
    from src.daemon import flows_ui as module
    http, _ = editor
    service = http._transport.app.state.service

    def check(client, server):  # `flows-ui --auto off` / `--revoke` run while the peer is checked
        if "auto_off" in during_check:
            module.set_auto_sign_in(service.directory, False)
        if "revoke" in during_check:
            module.replace_session_key(service.directory)
        return True

    service.flows_ui.peer_check = check
    response = await http.post("/ui/api/session", json={}, headers=UI)
    assert response.status_code == 401 and "set-cookie" not in response.headers
    assert (await http.get("/ui/api/flows")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["revoke", "auto_off"])
async def test_a_command_right_after_the_check_still_ends_the_new_session(editor, monkeypatch, command):
    from src.daemon import flows_ui as module
    http, _ = editor
    service = http._transport.app.state.service
    peer_stub(service, True)
    admitted = service.flows_ui._still_admitted

    def admitted_then_revoked(key):
        result = admitted(key)
        # `--revoke` or `--auto off` lands after the final check, before the cookie is set.
        if command == "revoke":
            module.replace_session_key(service.directory)
        else:
            module.set_auto_sign_in(service.directory, False)
        return result

    monkeypatch.setattr(service.flows_ui, "_still_admitted", admitted_then_revoked)
    assert (await http.post("/ui/api/session", json={}, headers=UI)).status_code == 200
    assert (await http.get("/ui/api/flows")).status_code == 401  # signed with the revoked key


@pytest.fixture
def known_components(monkeypatch):
    import src.component_catalog as catalog
    monkeypatch.setattr(catalog, "known_agents", lambda: {"code_reviewer", "software_engineer"})
    monkeypatch.setattr(catalog, "known_ids", lambda kind: {
        "skills": {"skill-a"}, "implants": {"implant-b"}, "rules": {"truth"}}[kind])


def test_frontmatter_persona_is_flow_metadata(install, tmp_path, known_components):
    (install / "audit.md").write_text(
        "---\npersona:\n  agent: code_reviewer\n  skills: [skill-a.mdc]\n---\n# Audit\n\nSteps.\n",
        encoding="utf-8")
    library = FlowLibrary(FlowCatalog(install), user_dir=tmp_path / "lib")
    flow = library.resolve("audit").metadata()
    assert flow["title"] == "Audit"
    assert flow["persona"] == {"agent": "code_reviewer", "skills": ["skill-a"]}
    assert flow["persona_source"] == "frontmatter"
    review = library.resolve("review").metadata()
    assert review["persona"] is None and review["persona_source"] is None


def test_overlay_chooses_persona_for_builtin_without_copying(install, tmp_path, known_components):
    library = FlowLibrary(FlowCatalog(install), user_dir=tmp_path / "lib")
    spec = {"agent": "code_reviewer", "skills": [], "implants": ["implant-b"], "rules": ["truth"]}
    result = library.set_persona("review", spec)
    assert result["flow"]["id"] == "builtin:review"
    assert result["flow"]["persona"] == spec
    assert result["flow"]["persona_source"] == "overlay"
    assert (install / "review.md").read_text(encoding="utf-8") == BUILTIN
    assert not (tmp_path / "lib" / "common").exists()  # No local copy of the text.
    assert library.set_persona("builtin:review", None)["flow"]["persona"] is None
    reset = library.set_persona("review", None, reset=True)["flow"]
    assert reset["persona"] is None and reset["persona_source"] is None


def test_overlay_replaces_frontmatter_and_reset_restores_it(install, tmp_path, known_components):
    library = FlowLibrary(FlowCatalog(install), user_dir=tmp_path / "lib")
    text = "---\npersona:\n  agent: software_engineer\n---\n# Mine\n"
    library.save("mine", text)
    library.set_persona("user:mine", {"agent": "code_reviewer"})
    assert library.resolve("mine").metadata()["persona"] == {"agent": "code_reviewer"}
    library.set_persona("mine", None, reset=True)
    assert library.resolve("mine").metadata()["persona"] == {"agent": "software_engineer"}
    library.set_persona("mine", {"agent": "code_reviewer"})
    library.delete("user:mine", expected_revision=revision(text))
    library.save("mine", text)  # A recreated flow starts from its frontmatter again.
    assert library.resolve("mine").metadata()["persona_source"] == "frontmatter"


@pytest.mark.parametrize("persona, code", [
    ({"agent": "ghost"}, "unknown agent"),
    ({"agent": "code_reviewer", "skills": ["skill-missing"]}, "unknown skills"),
    ({"agent": "code_reviewer", "rules": "truth"}, "must be a list"),
    ({"agent": "code_reviewer", "extra": 1}, "unknown persona fields"),
    ({"skills": ["skill-a"]}, "persona.agent"),
    ("code_reviewer", "must be a mapping"),
])
def test_invalid_persona_is_rejected_on_save_and_overlay(install, tmp_path, known_components,
                                                        persona, code):
    library = FlowLibrary(FlowCatalog(install), user_dir=tmp_path / "lib")
    with pytest.raises(FlowError, match=code):
        library.set_persona("review", persona)
    import yaml
    text = f"---\n{yaml.safe_dump({'persona': persona})}---\n# Bad\n"
    with pytest.raises(FlowError, match=code):
        library.save("bad", text)
    assert not (tmp_path / "lib" / "common" / "bad.md").exists()


def test_broken_frontmatter_is_flow_invalid(install, tmp_path):
    library = FlowLibrary(FlowCatalog(install), user_dir=tmp_path / "lib")
    with pytest.raises(FlowError, match="flow_invalid"):
        library.save("bad", "---\npersona: [unclosed\n---\n# Bad\n")


@pytest.mark.asyncio
async def test_editor_chooses_a_flow_persona(editor, install, known_components, monkeypatch):
    monkeypatch.setattr("src.daemon.flows_ui.list_agents", lambda: [
        {"id": "code_reviewer", "display_name": "Code Reviewer", "role": "Review"}])
    http, _ = editor
    await login(http)
    agents = (await http.get("/ui/api/agents")).json()["agents"]
    assert [agent["id"] for agent in agents] == ["code_reviewer"]
    body = {"id": "review", "persona": {"agent": "code_reviewer", "rules": ["truth"]}}
    saved = await http.put("/ui/api/flow/persona", json=body, headers=UI)
    assert saved.status_code == 200
    assert saved.json()["flow"]["persona"] == {"agent": "code_reviewer", "rules": ["truth"]}
    flow = (await http.get("/ui/api/flow", params={"id": "review"})).json()["flow"]
    assert flow["persona_source"] == "overlay"
    bad = await http.put("/ui/api/flow/persona", headers=UI,
                         json={"id": "review", "persona": {"agent": "ghost"}})
    assert bad.status_code == 400 and "unknown agent" in bad.json()["error"]
    reset = await http.put("/ui/api/flow/persona", json={"id": "review", "reset": True}, headers=UI)
    assert reset.json()["flow"]["persona"] is None
    blocked = await http.put("/ui/api/flow/persona", json=body,
                             headers={"X-Agents-UI": "1", "Origin": "https://attacker.example"})
    assert blocked.status_code == 403


def test_flow_opening_with_a_markdown_rule_is_not_frontmatter(install, tmp_path):
    library = FlowLibrary(FlowCatalog(install), user_dir=tmp_path / "lib")
    for text in ("---\n\n# Ruled\n\nSteps.\n", "---\n# Ruled\n\nText\n---\nMore\n"):
        library.save("ruled", text, expected_revision=None if text.endswith("Steps.\n") else
                     revision("---\n\n# Ruled\n\nSteps.\n"))
        flow = library.resolve("ruled").metadata()
        assert flow["title"] == "Ruled" and flow["persona"] is None


def test_broken_persona_stays_listed_and_can_be_repaired(install, tmp_path, known_components):
    (install / "typo.md").write_text("---\npersona:\n  agent: Bad Name\n---\n# Typo\n", encoding="utf-8")
    library = FlowLibrary(FlowCatalog(install), user_dir=tmp_path / "lib")
    listed = {flow["id"]: flow for flow in library.list("builtin")["flows"]}
    assert "persona.agent" in listed["typo"]["persona_error"]
    repaired = library.set_persona("typo", {"agent": "code_reviewer"})["flow"]
    assert repaired["persona"] == {"agent": "code_reviewer"} and "persona_error" not in repaired
    overlay = tmp_path / "lib" / "personas" / "builtin" / "review.json"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("{not json", encoding="utf-8")
    assert "unreadable" in library.resolve("review").metadata()["persona_error"]
    reset = library.set_persona("review", None, reset=True)
    assert reset["status"] == "reset" and not overlay.exists()
    assert "persona_error" not in reset["flow"]


def test_repository_flow_overlay_is_per_repository(install, repo, tmp_path, known_components):
    library = FlowLibrary(FlowCatalog(install), user_dir=tmp_path / "lib", repo_root=repo)
    library.save("repo:local", "# Local\n", scope="repo")
    flow = library.set_persona("local", {"agent": "code_reviewer"})["flow"]
    assert flow["id"] == "repo:local" and flow["persona_source"] == "overlay"
    key = repo_key(repo)[0]
    assert (tmp_path / "lib" / "personas" / "repos" / key / "local.json").is_file()
    with pytest.raises(FlowError, match="flow_not_found"):
        library.set_persona("missing", {"agent": "code_reviewer"})


def test_persona_directory_symlink_cannot_escape(install, tmp_path, known_components):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "personas").symlink_to(outside, target_is_directory=True)
    library = FlowLibrary(FlowCatalog(install), user_dir=tmp_path / "lib")
    with pytest.raises(FlowError, match="escapes"):
        library.set_persona("review", {"agent": "code_reviewer"})
    assert not list(outside.iterdir())


def test_non_string_persona_keys_are_flow_invalid(install, tmp_path):
    (install / "keys.md").write_text("---\npersona:\n  agent: code_reviewer\n  1: typo\n  x: y\n---\n# K\n",
                                     encoding="utf-8")
    library = FlowLibrary(FlowCatalog(install), user_dir=tmp_path / "lib")
    assert "unknown persona fields: 1, x" in library.resolve("keys").metadata()["persona_error"]


def test_symlinked_overlay_is_reported_and_reset_removes_only_the_link(install, tmp_path, known_components):
    target = tmp_path / "elsewhere.json"
    target.write_text('{"persona": null}', encoding="utf-8")
    overlay = tmp_path / "lib" / "personas" / "builtin" / "review.json"
    overlay.parent.mkdir(parents=True)
    overlay.symlink_to(target)
    library = FlowLibrary(FlowCatalog(install), user_dir=tmp_path / "lib")
    assert "symlink" in library.resolve("review").metadata()["persona_error"]
    with pytest.raises(FlowError, match="symlink"):
        library.set_persona("review", {"agent": "code_reviewer"})
    assert library.set_persona("review", None, reset=True)["status"] == "reset"
    assert not overlay.is_symlink() and target.exists()


def test_delete_checks_the_persona_path_before_changing_anything(install, tmp_path):
    library = FlowLibrary(FlowCatalog(install), user_dir=tmp_path / "lib")
    library.save("mine", "# Mine\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "lib" / "personas").symlink_to(outside, target_is_directory=True)
    with pytest.raises(FlowError, match="escapes"):
        library.delete("user:mine", expected_revision=revision("# Mine\n"))
    assert (tmp_path / "lib" / "common" / "mine.md").is_file()
