"""The Memory tab (#297): the AI clients' memory files, read, written, deleted, copied and moved behind
the UI session and the page header. No model and no network."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time

import httpx
import pytest
import pytest_asyncio

from src.daemon.app import create_app
from src.daemon.client_dirs import claude_config_dirs, codex_homes
from src.daemon.memory_ui import links
from src.daemon.workspaces import WorkspaceRegistry

TOKEN = "s" * 48
UI = {"X-Agents-UI": "1", "Origin": "http://127.0.0.1:8765"}


def note(name: str, kind: str, description: str, body: str) -> str:
    return (f"---\nname: {name}\ndescription: \"{description}\"\nmetadata:\n  node_type: memory\n  type: {kind}\n"
            f"  modified: 2026-10-05T10:00:01.802Z\n---\n\n{body}\n")


def sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project_dir(root) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(root))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home with two Claude configuration directories, a registered project and a Codex home
    without memories. ``~/.claude-work`` is known only from client-configs.json."""
    home = tmp_path / "home"
    project = tmp_path / "code" / "agents"
    project.mkdir(parents=True)
    name = project_dir(project)
    main = home / ".claude" / "projects" / name / "memory"
    main.mkdir(parents=True)
    (main / "issue-workflow.md").write_text(note("issue-workflow", "feedback", "restate first", "See [[zsh]]."))
    (main / "zsh.md").write_text(note("zsh", "reference", "arrays start at 1", "Body of zsh."))
    (main / "loose.md").write_text("no frontmatter here\n")
    (main / "MEMORY.md").write_text("- [Issue workflow](issue-workflow.md) — restate first\n"
                                    "- [zsh](zsh.md) — arrays\n"
                                    "- [Gone](gone.md) — deleted by hand\n"
                                    "Free text stays.\n")
    os.chmod(main / "zsh.md", 0o600)
    (main / "evil.md").symlink_to(tmp_path / "outside.md")
    (tmp_path / "outside.md").write_text("secret\n")
    (home / ".claude" / "memory").mkdir()
    (home / ".claude" / "memory" / "feedback_agents_core_routing.md").write_text(note("routing", "feedback", "d", "x"))
    other = home / ".claude-work" / "projects" / name / "memory"
    other.mkdir(parents=True)
    (other / "issue-workflow.md").write_text(note("issue-workflow", "feedback", "older", "Older text."))
    (home / ".claude-work" / "projects" / "-elsewhere").mkdir()
    (home / ".codex" / "memories").mkdir(parents=True)
    (home / ".claude.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    for variable in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(variable, raising=False)
    return {"home": home, "project": project, "name": name, "main": main, "other": other, "tmp": tmp_path}


@pytest_asyncio.fixture
async def daemon(home):
    registry = WorkspaceRegistry(home["tmp"] / "service")
    registry.register(home["project"])
    (registry.directory / "client-configs.json").write_text(json.dumps({"version": 1, "configs": [
        {"client": "claude", "path": str(home["home"] / ".claude.json"), "workspace": None},
        {"client": "claude", "path": str(home["home"] / ".claude-work" / ".claude.json"), "workspace": None},
        {"client": "claude-deny-desktop", "path": str(home["home"] / ".claude-gone" / "settings.json"), "workspace": None},
    ]}))

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
            code = (await http.post("/admin/ui/code", headers={"Authorization": "Bearer " + TOKEN})).json()["code"]
            http.code = code
            yield http, registry.directory


async def login(http) -> None:
    assert (await http.post("/ui/api/session", json={"code": http.code}, headers=UI)).status_code == 200


def stores(home):
    return str((home["home"] / ".claude").resolve()), str((home["home"] / ".claude-work").resolve())


# --- where memory is found ---------------------------------------------------------------------

def test_claude_directories_come_from_the_environment_the_default_and_the_records(tmp_path):
    home = tmp_path / "h"
    for name in (".claude", "work", "custom"):
        (home / name).mkdir(parents=True)
    service = tmp_path / "service"
    service.mkdir()
    (service / "client-configs.json").write_text(json.dumps({"version": 1, "configs": [
        {"client": "claude", "path": str(home / ".claude.json"), "workspace": None},  # belongs to ~/.claude
        {"client": "claude", "path": str(home / "work" / ".claude.json"), "workspace": None},
        {"client": "claude-deny-desktop", "path": str(home / "work" / "settings.json"), "workspace": None},
        {"client": "claude-deny-desktop", "path": str(home / "missing" / "settings.json"), "workspace": None},
        {"client": "codex", "path": str(home / "work" / ".codex" / "config.toml"), "workspace": str(home / "work")},
    ]}))
    found = claude_config_dirs(service, home=home, environ={"CLAUDE_CONFIG_DIR": str(home / "custom")})
    assert found == [(home / "custom").resolve(), (home / ".claude").resolve(), (home / "work").resolve()]
    assert claude_config_dirs(None, home=home, environ={}) == [(home / ".claude").resolve()]
    # A project-scope Codex configuration names no Codex home; a broken registry keeps the defaults.
    (home / ".codex").mkdir()
    assert codex_homes(service, home=home, environ={}) == [(home / ".codex").resolve()]
    (service / "client-configs.json").write_text("{not json")
    assert claude_config_dirs(service, home=home, environ={}) == [(home / ".claude").resolve()]


def test_index_links_name_files_of_the_same_directory_only():
    text = "- [A](a.md) — x\n* [B](b.md)\n- [Web](https://x.example/c.md)\n- [Sub](dir/d.md)\nplain [E](e.md)\n"
    assert links(text) == [{"file": "a.md", "title": "A", "line": 1}, {"file": "b.md", "title": "B", "line": 2}]


# --- reads -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_reads_need_a_session_and_the_page_header(daemon, home):
    http, _ = daemon
    main, _ = stores(home)
    paths = ["/ui/api/memory/projects", f"/ui/api/memory?store={main}&project={home['name']}",
             f"/ui/api/memory/file?store={main}&project={home['name']}&file=zsh.md", "/ui/api/memory/search?q=zsh"]
    for path in paths:
        assert (await http.get(path, headers=UI)).status_code == 401
    await login(http)
    for path in paths:
        for headers in ({}, {"Origin": "http://127.0.0.1:8765"}, {"X-Agents-UI": "1", "Origin": "https://attacker.example"}):
            assert (await http.get(path, headers=headers)).status_code == 403, (path, headers)
        response = await http.get(path, headers=UI)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store", path
    assert (await http.post(paths[0], headers=UI)).status_code == 405
    assert (await http.get("/ui/api/memory/delete", headers=UI)).status_code == 405
    body = {"store": main, "project": home["name"], "file": "zsh.md", "sha256": sha(home["main"] / "zsh.md")}
    assert (await http.post("/ui/api/memory/delete", json=body)).status_code == 403  # no page header


@pytest.mark.asyncio
async def test_projects_merge_configuration_directories_and_name_registered_workspaces(daemon, home):
    http, _ = daemon
    await login(http)
    data = (await http.get("/ui/api/memory/projects", headers=UI)).json()
    main, work = stores(home)
    assert [(store["label"], store["client"]) for store in data["stores"]] == [
        ("~/.claude", "claude"), ("~/.claude-work", "claude"), ("~/.codex", "codex")]
    rows = {row["key"]: row for row in data["projects"]}
    assert set(rows) == {"claude:" + home["name"], "claude:"}  # -elsewhere holds no memory
    project = rows["claude:" + home["name"]]
    assert project["name"] == "agents" and project["root"] == str(home["project"]) and project["workspace"]
    assert [(place["store"], place["items"]) for place in project["places"]] == [(main, 3), (work, 1)]
    assert project["items"] == 4 and project["newest"]
    assert rows["claude:"]["name"] == "Outside projects" and rows["claude:"]["places"][0]["items"] == 1
    assert data["notes"] == ["Codex: no memory files in $CODEX_HOME/memories (its memories feature may be off).",
                             "Cursor keeps no memory in files."]


@pytest.mark.asyncio
async def test_a_memory_directory_reads_with_its_index_problems_places_and_targets(daemon, home):
    http, _ = daemon
    await login(http)
    main, work = stores(home)
    data = (await http.get(f"/ui/api/memory?store={main}&project={home['name']}", headers=UI)).json()
    assert data["name"] == "agents" and data["label"] == "~/.claude" and data["dir"] == str(home["main"].resolve())
    items = {item["file"]: item for item in data["items"]}
    assert list(items) == ["issue-workflow.md", "loose.md", "zsh.md"]  # the symlink is not listed
    workflow = items["issue-workflow.md"]
    assert (workflow["name"], workflow["type"], workflow["description"]) == ("issue-workflow", "feedback", "restate first")
    assert workflow["modified"].startswith("2026-10-05T10:00:01.802") and workflow["editable"]
    assert workflow["sha256"] == sha(home["main"] / "issue-workflow.md") and "See [[zsh]]." in workflow["text"]
    assert items["loose.md"]["name"] is None and items["loose.md"]["frontmatter"]
    assert [link["file"] for link in data["index"]["links"]] == ["issue-workflow.md", "zsh.md", "gone.md"]
    assert data["not_indexed"] == ["loose.md"]
    assert data["broken"] == [{"file": "gone.md", "title": "Gone", "line": 3}]
    assert data["places"] == [{"store": main, "label": "~/.claude"}, {"store": work, "label": "~/.claude-work"}]
    assert data["targets"] == [{"store": work, "label": "~/.claude-work"}]
    top = (await http.get(f"/ui/api/memory?store={main}&project=", headers=UI)).json()
    assert [item["file"] for item in top["items"]] == ["feedback_agents_core_routing.md"] and not top["index"]["exists"]
    single = (await http.get(f"/ui/api/memory/file?store={main}&project={home['name']}&file=MEMORY.md", headers=UI)).json()
    assert single["text"].startswith("- [Issue workflow]") and single["sha256"] == sha(home["main"] / "MEMORY.md")


@pytest.mark.asyncio
async def test_places_come_only_from_the_discovery(daemon, home):
    http, _ = daemon
    await login(http)
    main, _ = stores(home)
    name = home["name"]
    for query, status, code in (
            (f"store={home['tmp']}&project={name}", 400, "store_unknown"),
            (f"store={main}&project=..", 400, "project_invalid"),
            (f"store={main}&project=a/b", 400, "project_invalid"),
            (f"store={main}&project=-nowhere", 404, "project_not_found")):
        response = await http.get("/ui/api/memory?" + query, headers=UI)
        assert response.status_code == status and response.json()["error"].startswith(code), query
    for file, code in (("../MEMORY.md", "file_invalid"), ("notes.txt", "file_invalid"), ("evil.md", "symlink_refused")):
        response = await http.get(f"/ui/api/memory/file?store={main}&project={name}&file={file}", headers=UI)
        assert response.status_code == 400 and response.json()["error"].startswith(code), file


@pytest.mark.asyncio
async def test_search_counts_items_that_hold_every_term_per_project(daemon, home):
    http, _ = daemon
    await login(http)
    data = (await http.get("/ui/api/memory/search?q=ARRAYS agents", headers=UI)).json()
    assert data["projects"] == [{"key": "claude:" + home["name"], "matches": 1}]
    assert (await http.get("/ui/api/memory/search?q=%20", headers=UI)).json()["projects"] == []


# --- writes ----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_save_carries_the_version_shown_and_keeps_the_mode(daemon, home):
    http, _ = daemon
    await login(http)
    main, _ = stores(home)
    path = home["main"] / "zsh.md"
    body = {"store": main, "project": home["name"], "file": "zsh.md", "text": "new text\n", "sha256": "0" * 64}
    stale = await http.put("/ui/api/memory/file", json=body, headers=UI)
    assert stale.status_code == 409 and stale.json()["error"].startswith("file_changed")
    assert "Body of zsh." in path.read_text()
    saved = await http.put("/ui/api/memory/file", json={**body, "sha256": sha(path)}, headers=UI)
    assert saved.status_code == 200 and saved.json()["status"] == "saved"
    assert path.read_text() == "new text\n" and os.stat(path).st_mode & 0o777 == 0o600
    assert saved.json()["sha256"] == sha(path)
    assert not [name for name in os.listdir(home["main"]) if name.endswith(".tmp")]
    too_large = await http.put("/ui/api/memory/file", json={**body, "text": "x" * (256 * 1024 + 1), "sha256": sha(path)},
                               headers=UI)
    assert too_large.status_code == 400 and too_large.json()["error"].startswith("file_too_large")


@pytest.mark.asyncio
async def test_a_new_item_is_created_once_and_can_join_the_index(daemon, home):
    http, _ = daemon
    await login(http)
    main, _ = stores(home)
    body = {"store": main, "project": home["name"], "file": "fresh-note.md", "sha256": None, "index": True,
            "text": note("fresh-note", "project", "a new fact", "Body.")}
    created = await http.put("/ui/api/memory/file", json=body, headers=UI)
    assert created.status_code == 200 and created.json() == {**created.json(), "status": "created", "indexed": True}
    assert os.stat(home["main"] / "fresh-note.md").st_mode & 0o777 == 0o644
    assert (home["main"] / "MEMORY.md").read_text().endswith("Free text stays.\n- [fresh-note](fresh-note.md) — a new fact\n")
    again = await http.put("/ui/api/memory/file", json=body, headers=UI)
    assert again.status_code == 409 and again.json()["error"].startswith("file_exists")
    # A project without a memory directory gets none from a save.
    _, work = stores(home)
    missing = await http.put("/ui/api/memory/file", json={**body, "store": work, "project": "-elsewhere"}, headers=UI)
    assert missing.status_code == 404 and missing.json()["error"].startswith("project_not_found")
    assert not (home["home"] / ".claude-work" / "projects" / "-elsewhere" / "memory").exists()


@pytest.mark.asyncio
async def test_delete_moves_the_file_to_the_trash_and_removes_only_its_index_line(daemon, home):
    http, service = daemon
    await login(http)
    main, _ = stores(home)
    path = home["main"] / "zsh.md"
    original = path.read_bytes()
    body = {"store": main, "project": home["name"], "file": "zsh.md", "sha256": "1" * 64}
    stale = await http.post("/ui/api/memory/delete", json=body, headers=UI)
    assert stale.status_code == 409 and path.exists()
    done = await http.post("/ui/api/memory/delete", json={**body, "sha256": sha(path)}, headers=UI)
    assert done.status_code == 200 and done.json()["status"] == "deleted" and done.json()["unindexed"]
    assert not path.exists()
    kept = done.json()["trash"]
    assert kept.startswith(str(service / "memory-trash")) and kept.endswith(f"/claude/{home['name']}/zsh.md")
    assert open(kept, "rb").read() == original
    assert (home["main"] / "MEMORY.md").read_text() == ("- [Issue workflow](issue-workflow.md) — restate first\n"
                                                       "- [Gone](gone.md) — deleted by hand\nFree text stays.\n")


@pytest.mark.asyncio
async def test_copy_creates_the_target_project_and_appends_the_index_line_once(daemon, home):
    http, _ = daemon
    await login(http)
    main, work = stores(home)
    path = home["main"] / "zsh.md"
    body = {"store": main, "project": home["name"], "file": "zsh.md", "sha256": sha(path), "to_store": work}
    copied = await http.post("/ui/api/memory/copy", json=body, headers=UI)
    assert copied.status_code == 200 and copied.json()["status"] == "copied" and copied.json()["indexed"]
    target = home["other"] / "zsh.md"
    assert target.read_bytes() == path.read_bytes() and path.exists()
    assert (home["other"] / "MEMORY.md").read_text() == "- [zsh](zsh.md) — arrays\n"  # the source's own line
    again = await http.post("/ui/api/memory/copy", json=body, headers=UI)
    assert again.status_code == 409 and again.json()["error"].startswith("target_exists")
    assert again.json()["target"]["sha256"] == sha(target) and "Body of zsh." in again.json()["target"]["text"]
    # Into a project the target directory does not hold yet, with an index line built from the frontmatter.
    top = {"store": main, "project": "", "file": "feedback_agents_core_routing.md", "to_store": work,
           "sha256": sha(home["home"] / ".claude" / "memory" / "feedback_agents_core_routing.md")}
    assert (await http.post("/ui/api/memory/copy", json=top, headers=UI)).status_code == 200
    assert (home["home"] / ".claude-work" / "memory" / "MEMORY.md").read_text() == \
        "- [routing](feedback_agents_core_routing.md) — d\n"
    for wrong, code in (({**body, "to_store": main}, "invalid_request"), ({**body, "to_store": "/nowhere"}, "store_unknown")):
        response = await http.post("/ui/api/memory/copy", json=wrong, headers=UI)
        assert response.status_code == 400 and response.json()["error"].startswith(code)


@pytest.mark.asyncio
async def test_replace_needs_the_targets_version_and_move_deletes_the_source_last(daemon, home):
    http, _ = daemon
    await login(http)
    main, work = stores(home)
    source, target = home["main"] / "issue-workflow.md", home["other"] / "issue-workflow.md"
    body = {"store": main, "project": home["name"], "file": "issue-workflow.md", "sha256": sha(source),
            "to_store": work, "move": True}
    conflict = await http.post("/ui/api/memory/copy", json=body, headers=UI)
    assert conflict.status_code == 409 and source.exists() and "Older text." in target.read_text()
    wrong = await http.post("/ui/api/memory/copy", json={**body, "replace_sha256": "2" * 64}, headers=UI)
    assert wrong.status_code == 409 and wrong.json()["error"].startswith("file_changed") and source.exists()
    moved = await http.post("/ui/api/memory/copy", json={**body, "replace_sha256": sha(target)}, headers=UI)
    assert moved.status_code == 200 and moved.json()["status"] == "moved" and moved.json()["replaced"]
    assert "See [[zsh]]." in target.read_text() and not source.exists()
    assert "issue-workflow.md" not in (home["main"] / "MEMORY.md").read_text()
    assert (home["other"] / "MEMORY.md").read_text() == "- [Issue workflow](issue-workflow.md) — restate first\n"
    assert moved.json()["trash"]
