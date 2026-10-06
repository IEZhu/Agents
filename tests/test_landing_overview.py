"""The landing page's overview of the installation (#188): repository links without credentials, sizes
that never follow symlinks, the apps configured for the service, cached sizes and the protected
``GET /ui/api/overview``. No model and no network."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time

import httpx
import pytest
import pytest_asyncio

from src.daemon import overview
from src.daemon.app import create_app
from src.daemon.workspaces import WorkspaceRegistry

TOKEN = "o" * 48
UI = {"X-Agents-UI": "1", "Origin": "http://127.0.0.1:8765"}


@pytest.mark.parametrize("origin, expected", [
    ("https://user:secret-token@github.com/IEZhu/Agents.git", "https://github.com/IEZhu/Agents"),
    ("git@github.com:IEZhu/Agents.git", "https://github.com/IEZhu/Agents"),
    ("ssh://git@GitHub.com:22/IEZhu/Agents.git\n", "https://github.com/IEZhu/Agents"),
    ("https://gitlab.example.com/group/sub/project", "https://gitlab.example.com/group/sub/project"),
    ("https://github.com/IEZhu/Agents?token=x", None),
    ("/srv/git/agents.git", None),
    ("file:///srv/git/agents.git", None),
    ("", None),
])
def test_an_origin_becomes_a_web_link_without_credentials(origin, expected):
    assert overview.web_url(origin) == expected


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_repository_links_lead_to_github_and_carry_no_token(tmp_path):
    git("init", cwd=tmp_path)
    git("remote", "add", "origin", "https://user:secret-token@github.com/IEZhu/Agents.git", cwd=tmp_path)
    links = overview.repository(tmp_path)
    assert links == {"repository": "https://github.com/IEZhu/Agents", "readme": "https://github.com/IEZhu/Agents#readme",
                     "docs": "https://github.com/IEZhu/Agents/blob/HEAD/docs/README.md",
                     "issues": "https://github.com/IEZhu/Agents/issues"}
    git("remote", "set-url", "origin", "git@git.example.com:me/agents.git", cwd=tmp_path)
    assert overview.repository(tmp_path) == {"repository": "https://git.example.com/me/agents"}
    git("remote", "remove", "origin", cwd=tmp_path)
    assert overview.repository(tmp_path) is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_sizes_never_follow_symlinks(tmp_path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x" * 1_000_000)
    inside = tmp_path / "inside"
    (inside / "nested").mkdir(parents=True)
    (inside / "nested" / "file.txt").write_bytes(b"y" * 10)
    (inside / "link.bin").symlink_to(outside)
    (inside / "linked-dir").symlink_to(tmp_path)
    assert 10 <= overview.size_of(inside) < 1_000
    assert overview.size_of(tmp_path / "missing") is None


def test_apps_count_as_configured_by_an_agents_core_entry(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"Agents-Core": {
        "type": "http", "url": "http://127.0.0.1:8765/mcp", "headers": {"Authorization": "Bearer secret"}}}}))
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "npx"}}}))
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text('[mcp_servers."Agents-Core"]\nurl = "http://127.0.0.1:8765/mcp"\n')
    apps = overview.configured_apps(tmp_path / "service", home)
    assert {app: state["configured"] for app, state in apps.items()} == {
        "claude-code": True, "claude-desktop": False, "codex": True, "cursor": False}
    assert apps["claude-code"]["scopes"] == ["claude:user"] and "secret" not in json.dumps(apps)


def test_sizes_are_measured_at_most_once_per_cache_period(tmp_path, monkeypatch):
    measured = []
    monkeypatch.setattr(overview, "model", lambda: measured.append("model") or {"name": "m", "size": 1})
    monkeypatch.setattr(overview, "directories", lambda directory: measured.append("directories") or [])
    monkeypatch.setattr(overview, "counts", lambda: {"agents": 1})
    monkeypatch.setattr(overview, "configured_apps", lambda directory, home: {})
    now = [1000.0]
    service = type("Service", (), {"directory": tmp_path})()
    reader = overview.Overview(service, clock=lambda: now[0])
    first = reader.read()
    now[0] += overview.CACHE_SECONDS - 1
    assert reader.read() == first and measured == ["model", "directories"]
    now[0] += 2
    reader.read()
    assert measured == ["model", "directories", "model", "directories"]
    assert first["version"] and first["model"] == {"name": "m", "size": 1} and first["counts"] == {"agents": 1}


@pytest_asyncio.fixture
async def daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(overview, "model", lambda: {"name": "microsoft/harrier-oss-v1-270m", "size": 2048})
    monkeypatch.setattr(overview, "directories", lambda directory: [
        {"id": "service", "label": "Service state and service.log", "path": str(directory), "size": 512}])
    registry = WorkspaceRegistry(tmp_path / "service")
    (tmp_path / "home").mkdir()

    def runtime(port):
        from mcp.server.fastmcp import FastMCP
        return FastMCP("test", stateless_http=True, json_response=True), None

    app = create_app(registry.directory, TOKEN, runtime_loader=runtime)
    app.state.service.overview.home = tmp_path / "home"
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if app.state.service.state != "starting":
                break
            await asyncio.sleep(.01)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url="http://127.0.0.1:8765") as http:
            yield http


@pytest.mark.asyncio
async def test_the_overview_needs_a_session_and_the_page_header_and_carries_no_token(daemon):
    http = daemon
    service = http._transport.app.state.service
    assert (await http.get("/ui/api/overview", headers=UI)).status_code == 401
    code = (await http.post("/admin/ui/code", headers={"Authorization": "Bearer " + TOKEN})).json()["code"]
    assert (await http.post("/ui/api/session", json={"code": code}, headers=UI)).status_code == 200
    for headers in ({}, {"X-Agents-UI": "1", "Origin": "https://attacker.example"}, {**UI, "Host": "attacker.example"}):
        assert (await http.get("/ui/api/overview", headers=headers)).status_code == 403, headers
    service.last_activity = idle = time.monotonic() - 100
    response = await http.get("/ui/api/overview", headers=UI)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert service.last_activity == idle  # a read of the page is not activity
    body = response.json()
    assert body["model"] == {"name": "microsoft/harrier-oss-v1-270m", "size": 2048}
    assert body["directories"][0]["size"] == 512 and set(body["apps"]) == set(overview.APPS)
    assert set(body["counts"]) == {"agents", "rules", "skills", "implants", "flows"} and body["counts"]["agents"] > 0
    assert TOKEN not in response.text
