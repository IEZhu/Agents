"""The Memory tab (#297): the AI clients' memory files, read, written, deleted, copied and moved behind
the UI session and the page header. No model and no network."""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import os
import re
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from src.daemon import memory_ui
from src.daemon.app import create_app
from src.daemon.client_dirs import claude_config_dirs, codex_homes
from src.daemon.memory_ui import links
from src.daemon.state import read_json
from src.daemon.workspaces import WorkspaceRegistry

TOKEN = "s" * 48
UI = {"X-Agents-UI": "1", "Origin": "http://127.0.0.1:8765"}
OLD = calendar.timegm((2020, 1, 1, 0, 0, 0))  # file times older than every frontmatter time


def note(name: str, kind: str, description: str, body: str, modified: str = "2026-10-05T10:00:01.802Z") -> str:
    return (f"---\nname: {name}\ndescription: \"{description}\"\nmetadata:\n  node_type: memory\n  type: {kind}\n"
            f"  modified: {modified}\n---\n\n{body}\n")


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def project_dir(root) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(root))


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="")
    os.utime(path, (OLD, OLD))


@pytest_asyncio.fixture
async def daemon(tmp_path, monkeypatch):
    """A home with two Claude configuration directories, a registered project and a Codex home
    without memories. ``~/.claude-work`` is known only from client-configs.json."""
    home = tmp_path / "home"
    project = tmp_path / "code" / "agents"
    project.mkdir(parents=True)
    registry = WorkspaceRegistry(tmp_path / "service")
    registry.register(project)
    root = next(iter(read_json(registry.path).values()))  # the path as the registry keeps it
    name = project_dir(root)
    main = home / ".claude" / "projects" / name / "memory"
    main.mkdir(parents=True)
    write(main / "issue-workflow.md", note("issue-workflow", "feedback", "restate first", "See [[zsh]]."))
    write(main / "zsh.md", note("zsh", "reference", "arrays start at 1", "Body of zsh.", "2026-10-06T08:00:00Z"))
    write(main / "loose.md", "no frontmatter here\n")
    write(main / "MEMORY.md", "- [Issue workflow](issue-workflow.md) — restate first\n"
                              "- [zsh](zsh.md) — arrays\n"
                              "- [Gone](gone.md) — deleted by hand\n"
                              "Free text stays.\n")
    if os.name == "posix":
        os.chmod(main / "zsh.md", 0o600)
    (home / ".claude" / "memory").mkdir()
    write(home / ".claude" / "memory" / "feedback_agents_core_routing.md",
          note("routing", "feedback", "d", "x", "2026-09-01T00:00:00Z"))
    other = home / ".claude-work" / "projects" / name / "memory"
    other.mkdir(parents=True)
    write(other / "issue-workflow.md", note("issue-workflow", "feedback", "older", "Older text."))
    (home / ".claude-work" / "projects" / "-elsewhere").mkdir()
    (home / ".codex" / "memories").mkdir(parents=True)
    (home / ".claude.json").write_text("{}")
    (registry.directory / "client-configs.json").write_text(json.dumps({"version": 1, "configs": [
        {"client": "claude", "path": str(home / ".claude.json"), "workspace": None},
        {"client": "claude", "path": str(home / ".claude-work" / ".claude.json"), "workspace": None},
        {"client": "claude-deny-desktop", "path": str(home / ".claude-work" / "settings.json"), "workspace": None},
        {"client": "claude-deny-desktop", "path": str(home / ".claude-gone" / "settings.json"), "workspace": None},
    ]}))
    for variable in ("HOME", "USERPROFILE"):  # Path.home() reads USERPROFILE on Windows
        monkeypatch.setenv(variable, str(home))

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
            yield http, {"home": home, "root": root, "name": name, "main": main, "other": other,
                         "service": registry.directory, "tmp": tmp_path}


async def login(http) -> None:
    code = (await http.post("/admin/ui/code", headers={"Authorization": "Bearer " + TOKEN})).json()["code"]
    assert (await http.post("/ui/api/session", json={"code": code}, headers=UI)).status_code == 200


def stores(env):
    return str((env["home"] / ".claude").resolve()), str((env["home"] / ".claude-work").resolve())


# --- where memory is found ---------------------------------------------------------------------

def test_claude_directories_come_from_the_environment_the_default_and_the_records(tmp_path):
    home = tmp_path / "h"
    for name in (".claude", "work", "custom", "denied"):
        (home / name).mkdir(parents=True)
    service = tmp_path / "service"
    service.mkdir()
    (service / "client-configs.json").write_text(json.dumps({"version": 1, "configs": [
        {"client": "claude", "path": str(home / ".claude.json"), "workspace": None},  # belongs to ~/.claude
        {"client": "claude", "path": str(home / "work" / ".claude.json"), "workspace": None},
        {"client": "claude-deny-desktop", "path": str(home / "work" / "settings.json"), "workspace": None},
        {"client": "claude-deny-desktop", "path": str(home / "denied" / "settings.json"), "workspace": None},
        {"client": "claude-deny-desktop", "path": str(home / "missing" / "settings.json"), "workspace": None},
        {"client": "codex", "path": str(home / "work" / ".codex" / "config.toml"), "workspace": str(home / "work")},
    ]}))
    found = claude_config_dirs(service, home=home, environ={"CLAUDE_CONFIG_DIR": str(home / "custom")})
    assert found == [(home / name).resolve() for name in ("custom", ".claude", "work", "denied")]
    assert claude_config_dirs(None, home=home, environ={}) == [(home / ".claude").resolve()]
    # A project-scope Codex configuration names no Codex home; a broken registry keeps the defaults.
    assert codex_homes(service, home=home, environ={}) == []
    (home / ".codex").mkdir()
    assert codex_homes(service, home=home, environ={}) == [(home / ".codex").resolve()]
    (service / "client-configs.json").write_text("{not json")
    assert claude_config_dirs(service, home=home, environ={}) == [(home / ".claude").resolve()]


def test_index_links_name_files_of_the_same_directory_only():
    text = "- [A](a.md) — x\n* [B](b.md)\n- [Web](https://x.example/c.md)\n- [Sub](dir/d.md)\nplain [E](e.md)\n"
    assert links(text) == [{"file": "a.md", "title": "A", "line": 1}, {"file": "b.md", "title": "B", "line": 2}]


# --- reads -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_reads_need_a_session_and_the_page_header(daemon):
    http, env = daemon
    main, _ = stores(env)
    paths = ["/ui/api/memory/projects", f"/ui/api/memory?store={main}&project={env['name']}",
             f"/ui/api/memory/file?store={main}&project={env['name']}&file=zsh.md", "/ui/api/memory/search?q=zsh"]
    for path in paths:
        assert (await http.get(path, headers=UI)).status_code == 401
    await login(http)
    for path in paths:
        for headers in ({}, {"Origin": "http://127.0.0.1:8765"}, {"X-Agents-UI": "1", "Origin": "https://attacker.example"}):
            assert (await http.get(path, headers=headers)).status_code == 403, (path, headers)
        response = await http.get(path, headers=UI)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store", path
    for method, path, allowed in (("POST", "/ui/api/memory/projects", "GET"), ("GET", "/ui/api/memory/delete", "POST"),
                                  ("DELETE", "/ui/api/memory/file", "GET, PUT")):
        response = await http.request(method, path, headers=UI)
        assert response.status_code == 405 and response.headers["allow"] == allowed, path
    assert (await http.get("/ui/api/memory/other", headers=UI)).status_code == 404
    body = {"store": main, "project": env["name"], "file": "zsh.md", "sha256": sha(env["main"] / "zsh.md")}
    assert (await http.post("/ui/api/memory/delete", json=body)).status_code == 403  # no page header
    assert (env["main"] / "zsh.md").exists()


@pytest.mark.asyncio
async def test_projects_merge_configuration_directories_and_name_registered_workspaces(daemon):
    http, env = daemon
    await login(http)
    data = (await http.get("/ui/api/memory/projects", headers=UI)).json()
    main, work = stores(env)
    assert [(store["label"], store["client"]) for store in data["stores"]] == [
        ("~/.claude", "claude"), ("~/.claude-work", "claude"), ("~/.codex", "codex")]
    assert [row["key"] for row in data["projects"]] == ["claude:" + env["name"], "claude:"]  # newest first
    project, top = data["projects"]
    assert project["name"] == "agents" and project["root"] == env["root"] and project["workspace"]
    # The newest item's metadata.modified counts, not the file times (all older here).
    assert [(place["store"], place["items"], place["newest"]) for place in project["places"]] == [
        (main, 3, "2026-10-06T08:00:00+00:00"), (work, 1, "2026-10-05T10:00:01+00:00")]
    assert project["items"] == 4 and project["newest"] == "2026-10-06T08:00:00+00:00"
    assert top["name"] == "Outside projects" and top["places"][0]["items"] == 1
    assert data["notes"] == ["Codex: no memory files in $CODEX_HOME/memories (its memories feature may be off).",
                             "Cursor keeps no memory in files."]


@pytest.mark.asyncio
async def test_a_memory_directory_reads_with_its_index_problems_places_and_targets(daemon):
    http, env = daemon
    await login(http)
    main, work = stores(env)
    data = (await http.get(f"/ui/api/memory?store={main}&project={env['name']}", headers=UI)).json()
    assert data["name"] == "agents" and data["label"] == "~/.claude" and data["dir"] == str(env["main"].resolve())
    items = {item["file"]: item for item in data["items"]}
    assert list(items) == ["issue-workflow.md", "loose.md", "zsh.md"]
    workflow = items["issue-workflow.md"]
    assert (workflow["name"], workflow["type"], workflow["description"]) == ("issue-workflow", "feedback", "restate first")
    assert workflow["modified"].startswith("2026-10-05T10:00:01.802") and workflow["editable"]
    assert workflow["changed"] == "2026-10-05T10:00:01+00:00" and workflow["mtime"].startswith("2020-01-01")
    assert workflow["sha256"] == sha(env["main"] / "issue-workflow.md") and "See [[zsh]]." in workflow["text"]
    assert items["loose.md"]["name"] is None and items["loose.md"]["frontmatter"]
    assert items["loose.md"]["changed"] == items["loose.md"]["mtime"]  # no frontmatter time: the file's
    assert [link["file"] for link in data["index"]["links"]] == ["issue-workflow.md", "zsh.md", "gone.md"]
    assert data["not_indexed"] == ["loose.md"]
    assert data["broken"] == [{"file": "gone.md", "title": "Gone", "line": 3}]
    assert data["places"] == [{"store": main, "label": "~/.claude"}, {"store": work, "label": "~/.claude-work"}]
    assert data["targets"] == [{"store": work, "label": "~/.claude-work"}]
    top = (await http.get(f"/ui/api/memory?store={main}&project=", headers=UI)).json()
    assert [item["file"] for item in top["items"]] == ["feedback_agents_core_routing.md"] and not top["index"]["exists"]
    single = (await http.get(f"/ui/api/memory/file?store={main}&project={env['name']}&file=MEMORY.md", headers=UI)).json()
    assert single["text"].startswith("- [Issue workflow]") and single["sha256"] == sha(env["main"] / "MEMORY.md")


@pytest.mark.asyncio
async def test_places_come_only_from_the_discovery(daemon):
    http, env = daemon
    await login(http)
    main, _ = stores(env)
    name = env["name"]
    for query, status, code in (
            (f"store={env['tmp']}&project={name}", 400, "store_unknown"),
            (f"store={main}&project=..", 400, "project_invalid"),
            (f"store={main}&project=a/b", 400, "project_invalid"),
            (f"store={main}&project=-nowhere", 404, "project_not_found")):
        response = await http.get("/ui/api/memory?" + query, headers=UI)
        assert response.status_code == status and response.json()["error"].startswith(code), query
    for file, code in (("../MEMORY.md", "file_invalid"), ("notes.txt", "file_invalid"), (".hidden.md", "file_invalid")):
        response = await http.get(f"/ui/api/memory/file?store={main}&project={name}&file={file}", headers=UI)
        assert response.status_code == 400 and response.json()["error"].startswith(code), file
    body = {"store": main, "project": "..", "file": "x.md", "text": "x", "sha256": None}
    assert (await http.put("/ui/api/memory/file", json=body, headers=UI)).status_code == 400
    assert not (env["home"] / ".claude" / "x.md").exists()


@pytest.mark.asyncio
async def test_symlinks_are_refused_and_never_listed(daemon):
    http, env = daemon
    try:
        (env["main"] / "evil.md").symlink_to(env["tmp"] / "outside.md")
        (env["home"] / ".claude" / "projects" / "-linked").symlink_to(env["main"].parent, target_is_directory=True)
        (env["home"] / ".claude" / "projects" / "-only-index" / "memory").mkdir(parents=True)
        (env["home"] / ".claude" / "projects" / "-only-index" / "memory" / "MEMORY.md").symlink_to(env["main"] / "MEMORY.md")
    except OSError as error:  # Windows without the privilege to create symlinks
        pytest.skip(f"symlinks cannot be created here: {error}")
    (env["tmp"] / "outside.md").write_text("secret\n")
    await login(http)
    main, work = stores(env)
    data = (await http.get(f"/ui/api/memory?store={main}&project={env['name']}", headers=UI)).json()
    assert "evil.md" not in [item["file"] for item in data["items"]]
    rows = (await http.get("/ui/api/memory/projects", headers=UI)).json()["projects"]
    assert not {"claude:-linked", "claude:-only-index"} & {row["key"] for row in rows}
    for path in (f"/ui/api/memory/file?store={main}&project={env['name']}&file=evil.md",
                 f"/ui/api/memory?store={main}&project=-linked"):
        response = await http.get(path, headers=UI)
        assert response.status_code == 400 and response.json()["error"].startswith("symlink_refused"), path
    body = {"store": main, "project": env["name"], "file": "evil.md", "text": "x", "sha256": sha(env["tmp"] / "outside.md")}
    response = await http.put("/ui/api/memory/file", json=body, headers=UI)
    assert response.status_code == 400 and (env["tmp"] / "outside.md").read_text(encoding="utf-8") == "secret\n"
    copy = {"store": main, "project": "-linked", "file": "zsh.md", "sha256": sha(env["main"] / "zsh.md"), "to_store": work}
    assert (await http.post("/ui/api/memory/copy", json=copy, headers=UI)).status_code == 400


@pytest.mark.asyncio
async def test_search_counts_items_that_hold_every_term_per_project(daemon):
    http, env = daemon
    await login(http)
    data = (await http.get("/ui/api/memory/search?q=ARRAYS agents", headers=UI)).json()
    assert data["projects"] == [{"key": "claude:" + env["name"], "matches": 1}]
    assert (await http.get("/ui/api/memory/search?q=%20", headers=UI)).json()["projects"] == []


# --- writes ----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_save_carries_the_version_shown_and_keeps_the_mode(daemon):
    http, env = daemon
    await login(http)
    main, _ = stores(env)
    path = env["main"] / "zsh.md"
    body = {"store": main, "project": env["name"], "file": "zsh.md", "text": "new text\n", "sha256": "0" * 64}
    stale = await http.put("/ui/api/memory/file", json=body, headers=UI)
    assert stale.status_code == 409 and stale.json()["error"].startswith("file_changed")
    assert "Body of zsh." in path.read_text(encoding="utf-8")
    saved = await http.put("/ui/api/memory/file", json={**body, "sha256": sha(path)}, headers=UI)
    assert saved.status_code == 200 and saved.json()["status"] == "saved"
    assert path.read_text(encoding="utf-8") == "new text\n" and saved.json()["sha256"] == sha(path)
    if os.name == "posix":
        assert os.stat(path).st_mode & 0o777 == 0o600
    assert not [name for name in os.listdir(env["main"]) if name.endswith(".tmp")]
    too_large = await http.put("/ui/api/memory/file", json={**body, "text": "x" * (256 * 1024 + 1), "sha256": sha(path)},
                               headers=UI)
    assert too_large.status_code == 400 and too_large.json()["error"].startswith("file_too_large")
    write(env["main"] / "big.md", "y" * (256 * 1024 + 1))
    listed = (await http.get(f"/ui/api/memory?store={main}&project={env['name']}", headers=UI)).json()
    big = next(item for item in listed["items"] if item["file"] == "big.md")
    assert not big["editable"] and len(big["text"]) == 256 * 1024 + 1  # shown, not editable
    shrink = await http.put("/ui/api/memory/file", json={**body, "file": "big.md", "text": "small\n",
                                                         "sha256": big["sha256"]}, headers=UI)
    assert shrink.status_code == 400 and shrink.json()["error"].startswith("file_too_large")
    assert (env["main"] / "big.md").stat().st_size == 256 * 1024 + 1
    # MEMORY.md is written like any other file, which is how the page adds an item to it.
    index = env["main"] / "MEMORY.md"
    text = index.read_text(encoding="utf-8") + "- [loose](loose.md)\n"
    added = await http.put("/ui/api/memory/file", json={**body, "file": "MEMORY.md", "text": text, "sha256": sha(index)},
                           headers=UI)
    assert added.status_code == 200 and index.read_text(encoding="utf-8") == text


@pytest.mark.asyncio
async def test_a_new_item_is_created_once_and_can_join_the_index(daemon):
    http, env = daemon
    await login(http)
    main, work = stores(env)
    body = {"store": main, "project": env["name"], "file": "fresh-note.md", "sha256": None, "index": True,
            "text": note("fresh-note", "project", "a new fact", "Body.")}
    created = await http.put("/ui/api/memory/file", json=body, headers=UI)
    assert created.status_code == 200 and created.json() == {**created.json(), "status": "created", "indexed": True}
    if os.name == "posix":
        assert os.stat(env["main"] / "fresh-note.md").st_mode & 0o777 == 0o644
    assert (env["main"] / "MEMORY.md").read_text(encoding="utf-8").endswith(
        "Free text stays.\n- [fresh-note](fresh-note.md) — a new fact\n")
    again = await http.put("/ui/api/memory/file", json=body, headers=UI)
    assert again.status_code == 409 and again.json()["error"].startswith("file_exists")
    # A project without a memory directory gets none from a save.
    missing = await http.put("/ui/api/memory/file", json={**body, "store": work, "project": "-elsewhere"}, headers=UI)
    assert missing.status_code == 404 and missing.json()["error"].startswith("project_not_found")
    assert not (env["home"] / ".claude-work" / "projects" / "-elsewhere" / "memory").exists()


@pytest.mark.asyncio
async def test_delete_moves_the_file_to_the_trash_and_removes_only_its_index_line(daemon):
    http, env = daemon
    await login(http)
    main, _ = stores(env)
    path = env["main"] / "zsh.md"
    original = path.read_bytes()
    body = {"store": main, "project": env["name"], "file": "zsh.md", "sha256": "1" * 64}
    stale = await http.post("/ui/api/memory/delete", json=body, headers=UI)
    assert stale.status_code == 409 and path.exists()
    done = await http.post("/ui/api/memory/delete", json={**body, "sha256": sha(path)}, headers=UI)
    assert done.status_code == 200 and done.json()["status"] == "deleted" and done.json()["unindexed"]
    assert not path.exists()
    kept = Path(done.json()["trash"])
    assert kept.relative_to(env["service"] / "memory-trash").parts[1:] == ("claude", env["name"], "zsh.md")
    assert kept.read_bytes() == original
    assert (env["main"] / "MEMORY.md").read_text(encoding="utf-8") == ("- [Issue workflow](issue-workflow.md) — restate first\n"
                                                      "- [Gone](gone.md) — deleted by hand\nFree text stays.\n")


@pytest.mark.asyncio
async def test_copy_creates_the_target_project_and_appends_the_index_line_once(daemon):
    http, env = daemon
    await login(http)
    main, work = stores(env)
    path = env["main"] / "zsh.md"
    body = {"store": main, "project": env["name"], "file": "zsh.md", "sha256": sha(path), "to_store": work}
    copied = await http.post("/ui/api/memory/copy", json=body, headers=UI)
    assert copied.status_code == 200 and copied.json()["status"] == "copied" and copied.json()["indexed"]
    target = env["other"] / "zsh.md"
    assert target.read_bytes() == path.read_bytes() and path.exists()
    assert (env["other"] / "MEMORY.md").read_text(encoding="utf-8") == "- [zsh](zsh.md) — arrays\n"  # the source's own line
    again = await http.post("/ui/api/memory/copy", json=body, headers=UI)
    assert again.status_code == 409 and again.json()["error"].startswith("target_exists")
    assert again.json()["target"]["sha256"] == sha(target) and "Body of zsh." in again.json()["target"]["text"]
    replaced = await http.post("/ui/api/memory/copy", json={**body, "replace_sha256": sha(target)}, headers=UI)
    assert replaced.status_code == 200 and replaced.json()["replaced"] and not replaced.json()["indexed"]
    assert (env["other"] / "MEMORY.md").read_text(encoding="utf-8") == "- [zsh](zsh.md) — arrays\n"  # still once
    # Into a directory the target has no memory in yet, with an index line built from the frontmatter.
    top = {"store": main, "project": "", "file": "feedback_agents_core_routing.md", "to_store": work,
           "sha256": sha(env["home"] / ".claude" / "memory" / "feedback_agents_core_routing.md")}
    assert (await http.post("/ui/api/memory/copy", json=top, headers=UI)).status_code == 200
    assert (env["home"] / ".claude-work" / "memory" / "MEMORY.md").read_text(encoding="utf-8") == \
        "- [routing](feedback_agents_core_routing.md) — d\n"
    fresh = env["home"] / ".claude" / "projects" / "-fresh" / "memory"
    fresh.mkdir(parents=True)
    write(fresh / "a.md", note("a", "user", "first", "A."))
    first = {"store": main, "project": "-fresh", "file": "a.md", "sha256": sha(fresh / "a.md"), "to_store": work}
    assert (await http.post("/ui/api/memory/copy", json=first, headers=UI)).status_code == 200
    assert (env["home"] / ".claude-work" / "projects" / "-fresh" / "memory" / "a.md").read_bytes() == \
        (fresh / "a.md").read_bytes()
    for wrong, code in (({**body, "to_store": main}, "invalid_request"), ({**body, "to_store": "/nowhere"}, "store_unknown")):
        response = await http.post("/ui/api/memory/copy", json=wrong, headers=UI)
        assert response.status_code == 400 and response.json()["error"].startswith(code)


@pytest.mark.asyncio
async def test_replace_needs_the_targets_version_and_move_deletes_the_source_last(daemon):
    http, env = daemon
    await login(http)
    main, work = stores(env)
    source, target = env["main"] / "issue-workflow.md", env["other"] / "issue-workflow.md"
    body = {"store": main, "project": env["name"], "file": "issue-workflow.md", "sha256": sha(source),
            "to_store": work, "move": True}
    conflict = await http.post("/ui/api/memory/copy", json=body, headers=UI)
    assert conflict.status_code == 409 and source.exists() and "Older text." in target.read_text(encoding="utf-8")
    wrong = await http.post("/ui/api/memory/copy", json={**body, "replace_sha256": "2" * 64}, headers=UI)
    assert wrong.status_code == 409 and wrong.json()["error"].startswith("file_changed") and source.exists()
    assert "Older text." in target.read_text(encoding="utf-8")
    moved = await http.post("/ui/api/memory/copy", json={**body, "replace_sha256": sha(target)}, headers=UI)
    assert moved.status_code == 200 and moved.json()["status"] == "moved" and moved.json()["replaced"]
    assert "See [[zsh]]." in target.read_text(encoding="utf-8") and not source.exists()
    assert "issue-workflow.md" not in (env["main"] / "MEMORY.md").read_text(encoding="utf-8")
    assert (env["other"] / "MEMORY.md").read_text(encoding="utf-8") == \
        "- [Issue workflow](issue-workflow.md) — restate first\n"
    assert Path(moved.json()["trash"]).read_text(encoding="utf-8") == target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_junction_counts_as_a_link(daemon, monkeypatch):
    """Windows junctions are not symlinks to Python 3.11; the module asks the reparse-point check."""
    http, env = daemon
    junction = env["home"] / ".claude" / "projects" / "-junction"
    (junction / "memory").mkdir(parents=True)
    write(junction / "memory" / "a.md", note("a", "user", "d", "A."))
    real = memory_ui.is_link
    monkeypatch.setattr(memory_ui, "is_link", lambda path: Path(path) == junction or real(path))
    await login(http)
    main, _ = stores(env)
    rows = (await http.get("/ui/api/memory/projects", headers=UI)).json()["projects"]
    assert "claude:-junction" not in [row["key"] for row in rows]
    response = await http.get(f"/ui/api/memory?store={main}&project=-junction", headers=UI)
    assert response.status_code == 400 and response.json()["error"].startswith("symlink_refused")


@pytest.mark.asyncio
async def test_a_version_written_while_deleting_is_kept(daemon, monkeypatch):
    http, env = daemon
    await login(http)
    main, _ = stores(env)
    path = env["main"] / "zsh.md"
    original = path.read_bytes()
    trash = memory_ui.MemoryUI._trash

    def write_meanwhile(self, place, project, target, data):
        target.write_text("written by a session meanwhile\n", encoding="utf-8")
        return trash(self, place, project, target, data)

    monkeypatch.setattr(memory_ui.MemoryUI, "_trash", write_meanwhile)
    body = {"store": main, "project": env["name"], "file": "zsh.md", "sha256": sha(path)}
    done = await http.post("/ui/api/memory/delete", json=body, headers=UI)
    assert done.status_code == 200 and Path(done.json()["trash"]).read_bytes() == original
    assert path.read_text(encoding="utf-8") == "written by a session meanwhile\n"
    assert not [name for name in os.listdir(env["main"]) if name.endswith(".deleting")]
    # A refused delete puts the file back as it was and leaves nothing beside it.
    monkeypatch.setattr(memory_ui.MemoryUI, "_trash", trash)
    stale = await http.post("/ui/api/memory/delete", json={**body, "sha256": "3" * 64}, headers=UI)
    assert stale.status_code == 409 and path.read_text(encoding="utf-8") == "written by a session meanwhile\n"
    assert not [name for name in os.listdir(env["main"]) if name.endswith(".deleting")]


@pytest.mark.asyncio
async def test_memory_md_is_no_item_in_claude_and_a_plain_file_in_codex(daemon):
    http, env = daemon
    await login(http)
    main, work = stores(env)
    index = env["main"] / "MEMORY.md"
    before = index.read_bytes()
    body = {"store": main, "project": env["name"], "file": "MEMORY.md", "sha256": sha(index)}
    for path, extra in (("/ui/api/memory/delete", {}), ("/ui/api/memory/copy", {"to_store": work})):
        response = await http.post(path, json={**body, **extra}, headers=UI)
        assert response.status_code == 400 and response.json()["error"].startswith("file_invalid"), path
    assert index.read_bytes() == before and not (env["other"] / "MEMORY.md").exists()
    # Codex keeps no index: its MEMORY.md is listed, read and edited like its other files.
    memories = env["home"] / ".codex" / "memories"
    write(memories / "MEMORY.md", "codex notes\n")
    write(memories / "facts.txt", "plain text\n")
    codex = str((env["home"] / ".codex").resolve())
    rows = {row["key"]: row for row in (await http.get("/ui/api/memory/projects", headers=UI)).json()["projects"]}
    assert rows["codex:"]["items"] == 2 and rows["codex:"]["name"] == "Codex memories"
    data = (await http.get(f"/ui/api/memory?store={codex}&project=", headers=UI)).json()
    assert [item["file"] for item in data["items"]] == ["MEMORY.md", "facts.txt"] and not data["index"]["exists"]
    assert data["targets"] == [] and data["not_indexed"] == ["MEMORY.md", "facts.txt"]
    saved = await http.put("/ui/api/memory/file", json={"store": codex, "project": "", "file": "MEMORY.md",
                                                        "text": "edited\n", "sha256": sha(memories / "MEMORY.md")},
                           headers=UI)
    assert saved.status_code == 200 and (memories / "MEMORY.md").read_text(encoding="utf-8") == "edited\n"


@pytest.mark.asyncio
async def test_a_create_never_replaces_a_file_that_appeared_meanwhile(daemon, monkeypatch):
    http, env = daemon
    await login(http)
    main, _ = stores(env)
    target = env["main"] / "racing.md"
    check = memory_ui._check

    def appears_after_the_check(path, expected):
        check(path, expected)
        if path == target:
            target.write_text("written by a session meanwhile\n", encoding="utf-8")

    monkeypatch.setattr(memory_ui, "_check", appears_after_the_check)
    body = {"store": main, "project": env["name"], "file": "racing.md", "text": "from the page\n", "sha256": None}
    response = await http.put("/ui/api/memory/file", json=body, headers=UI)
    assert response.status_code == 409 and response.json()["error"].startswith("file_exists")
    assert target.read_text(encoding="utf-8") == "written by a session meanwhile\n"
    assert not [name for name in os.listdir(env["main"]) if name.endswith(".tmp")]


@pytest.mark.asyncio
async def test_a_file_turned_into_a_symlink_after_listing_is_not_read(daemon, monkeypatch):
    http, env = daemon
    outside = env["tmp"] / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    swapped = env["main"] / "swap.md"
    write(swapped, "plain\n")
    names = memory_ui.MemoryUI._names

    def swap_after_listing(store, memory):
        found = names(store, memory)
        if swapped.exists() and not swapped.is_symlink():
            swapped.unlink()
            swapped.symlink_to(outside)
        return found

    try:
        (env["tmp"] / "probe").symlink_to(outside)
    except OSError as error:  # Windows without the privilege to create symlinks
        pytest.skip(f"symlinks cannot be created here: {error}")
    monkeypatch.setattr(memory_ui.MemoryUI, "_names", staticmethod(swap_after_listing))
    await login(http)
    main, _ = stores(env)
    data = (await http.get(f"/ui/api/memory?store={main}&project={env['name']}", headers=UI)).json()
    item = next(item for item in data["items"] if item["file"] == "swap.md")
    assert item == {"file": "swap.md", "unreadable": True} and "secret" not in json.dumps(data)


@pytest.mark.asyncio
async def test_long_file_names_up_to_255_characters_are_items(daemon):
    http, env = daemon
    await login(http)
    main, _ = stores(env)
    long_name = "n" * 240 + ".md"
    write(env["main"] / long_name, note("long", "user", "d", "Long."))
    data = (await http.get(f"/ui/api/memory?store={main}&project={env['name']}", headers=UI)).json()
    assert long_name in [item["file"] for item in data["items"]]
    body = {"store": main, "project": env["name"], "file": long_name, "sha256": sha(env["main"] / long_name)}
    saved = await http.put("/ui/api/memory/file", json={**body, "text": "edited\n"}, headers=UI)
    assert saved.status_code == 200
    deleted = await http.post("/ui/api/memory/delete", json={**body, "sha256": sha(env["main"] / long_name)}, headers=UI)
    assert deleted.status_code == 200 and not (env["main"] / long_name).exists()
    too_long = await http.get(f"/ui/api/memory/file?store={main}&project={env['name']}&file={'n' * 254}.md",
                              headers=UI)
    assert too_long.status_code == 400 and too_long.json()["error"].startswith("file_invalid")


@pytest.mark.asyncio
async def test_a_create_or_copy_that_cannot_join_the_index_leaves_no_item(daemon, monkeypatch):
    http, env = daemon
    await login(http)
    main, _ = stores(env)
    body = {"store": main, "project": env["name"], "file": "unindexed.md", "sha256": None, "index": True,
            "text": note("unindexed", "user", "d", "Body.")}

    def refused(self, memory, file, line):
        raise memory_ui.MemoryRefused("file_changed", "MEMORY.md changed since it was shown", 409)

    index_add = memory_ui.MemoryUI._index_add
    monkeypatch.setattr(memory_ui.MemoryUI, "_index_add", refused)
    response = await http.put("/ui/api/memory/file", json=body, headers=UI)
    assert response.status_code == 409 and not (env["main"] / "unindexed.md").exists()
    _, work = stores(env)
    copy = {"store": main, "project": env["name"], "file": "zsh.md", "sha256": sha(env["main"] / "zsh.md"), "to_store": work}
    response = await http.post("/ui/api/memory/copy", json=copy, headers=UI)
    assert response.status_code == 409 and not (env["other"] / "zsh.md").exists()
    monkeypatch.setattr(memory_ui.MemoryUI, "_index_add", index_add)
    try:
        index = env["main"] / "MEMORY.md"
        index.unlink()
        index.symlink_to(env["tmp"] / "elsewhere.md")
    except OSError as error:  # Windows without the privilege to create symlinks
        pytest.skip(f"symlinks cannot be created here: {error}")
    response = await http.put("/ui/api/memory/file", json=body, headers=UI)
    assert response.status_code == 400 and response.json()["error"].startswith("symlink_refused")
    assert not (env["main"] / "unindexed.md").exists()
