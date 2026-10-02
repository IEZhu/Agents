"""Switches for rules, skills and implants: store, bundle enforcement and the editor API."""
import asyncio
import json
import subprocess

import httpx
import pytest
import pytest_asyncio

from src import component_catalog, component_toggles
from src.component_toggles import ToggleError
from src.daemon.app import create_app
from src.daemon.workspaces import WorkspaceRegistry
from src.engine import enrichment, rules
from src.engine.persona_bundle import build_persona_bundle
from src.user_flows import FlowLibrary
from tests.test_persona_bundle import bundle_tree, write_mdc  # noqa: F401  (fixture)
from tests.test_user_flows import TOKEN, UI, install, login, repo  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def toggle_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "user"))
    return tmp_path / "user"


# --- store ---------------------------------------------------------------------------------

def test_store_round_trip_and_defaults(toggle_dir):
    assert component_toggles.disabled("rules") == frozenset()
    component_toggles.set_enabled("rules", "truth", False)
    component_toggles.set_enabled("skills", "skill-core", False)
    assert component_toggles.disabled("rules") == {"truth"}
    assert not component_toggles.is_enabled("skills", "skill-core")
    component_toggles.set_enabled("rules", "truth", True)
    assert component_toggles.disabled("rules") == frozenset()
    assert json.loads((toggle_dir / "components.json").read_text()) == {
        "disabled": {"rules": [], "skills": ["skill-core"], "implants": []}}
    assert not list(toggle_dir.glob(".tmp-*"))


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"disabled": 5}',
                                     '{"disabled": {"rules": "x", "skills": [1, "../x"]}}',
                                     '{"disabled": {"rules": ["truth"], "skills": [1]}}',
                                     '{"disabled": {"rules": ["truth"], "implants": ["../x"]}}',
                                     '{"disabled": {"rules": ["truth"], "agents": 5}}'])
def test_corrupt_file_means_everything_enabled(toggle_dir, content):
    toggle_dir.mkdir(parents=True)
    (toggle_dir / "components.json").write_text(content)
    for kind in component_toggles.KINDS:
        assert component_toggles.disabled(kind) == frozenset()


def test_store_rejects_bad_input():
    with pytest.raises(ToggleError):
        component_toggles.set_enabled("agents", "x", False)
    with pytest.raises(ToggleError):
        component_toggles.set_enabled("rules", "../x", False)
    with pytest.raises(ToggleError):
        component_toggles.disabled("agents")


def test_state_file_is_git_ignored_in_the_default_location():
    default = component_toggles.Path(component_toggles.FLOWS_DIR) / ".user" / "components.json"
    root = default.parents[2]
    ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", str(default)])
    assert ignored.returncode == 0


# --- bundle enforcement --------------------------------------------------------------------

async def build(agent="engineer"):
    return await build_persona_bundle(agent, "fix the bug", None, "standard")


@pytest.mark.asyncio
async def test_disabling_a_component_changes_the_bundle_and_enabling_restores_it(bundle_tree):
    base = await build()
    assert base.skills_loaded == ["skill-core"] and base.implants_loaded == ["Focus"]
    assert base.rules_loaded == ["truth"]
    cases = (("rules", "truth", "rules_loaded", "rules_block", "truth"),
             ("skills", "skill-core", "skills_loaded", "skills_block", "skill-core"),
             ("implants", "implant-focus", "implants_loaded", "implants_block", "Focus"))
    for kind, component_id, loaded, block, shown in cases:
        component_toggles.set_enabled(kind, component_id, False)
        off = await build()
        assert shown not in getattr(off, loaded)
        assert getattr(off, block) != getattr(base, block)
        assert off.bundle_revision != base.bundle_revision
        component_toggles.set_enabled(kind, component_id, True)
        assert (await build()).bundle_revision == base.bundle_revision


@pytest.mark.asyncio
async def test_disabling_everything_never_fails_an_activation(bundle_tree):
    tree, _ = bundle_tree
    for kind, ids in (("rules", ["truth"]), ("skills", ["skill-core", "skill-extra"]),
                      ("implants", ["implant-focus"])):
        for component_id in ids:
            component_toggles.set_enabled(kind, component_id, False)
    bundle = await build()
    assert bundle.rules_block == "" and bundle.rules_loaded == []
    assert bundle.skills_loaded == [] and bundle.implants_loaded == []
    assert (tree / "rules/rule-truth.mdc").exists()  # files are never touched


@pytest.mark.asyncio
async def test_disabled_semantic_hit_is_skipped_not_an_error(bundle_tree):
    component_toggles.set_enabled("skills", "skill-extra", False)
    enrichment.get_skill_retriever().retrieve = lambda *a, **k: [
        {"filename": "skill-core.mdc"}, {"filename": "skill-extra.mdc"}]
    assert (await build()).skills_loaded == ["skill-core"]
    # A skill outside the agent's policy is still an error, even when it is switched off.
    component_toggles.set_enabled("skills", "skill-other", False)
    enrichment.get_skill_retriever().retrieve = lambda *a, **k: [{"filename": "skill-other.mdc"}]
    with pytest.raises(ValueError):
        await build()


def test_per_query_rules_path_ignores_toggles(bundle_tree):
    component_toggles.set_enabled("rules", "truth", False)
    rules.invalidate_cache()
    assert [r.name for r in rules.get_rules()] == ["truth"]
    assert rules.get_rules(fresh=True, strict=True, apply_toggles=True) == []
    assert rules.get_rules(apply_toggles=True) == []  # the cached path filters too
    rules.invalidate_cache()


def test_toggles_survive_a_restart_and_leave_git_clean(toggle_dir):
    component_toggles.set_enabled("skills", "skill-core", False)
    # A new process only has the file.
    assert component_toggles.disabled("skills") == {"skill-core"}


@pytest.mark.asyncio
async def test_load_implants_leaves_out_disabled(monkeypatch):
    import src.server as server
    from types import SimpleNamespace
    items = [{"filename": f"implant-{n}.mdc", "content": "", "metadata": {}} for n in ("a", "b")]
    monkeypatch.setattr(server, "get_implant_retriever", lambda: SimpleNamespace(
        retrieve=lambda **kwargs: items,
        format_implants_for_prompt=lambda found: ",".join(i["filename"] for i in found)))
    component_toggles.set_enabled("implants", "implant-a", False)
    assert await server.load_implants(query="anything") == "implant-b.mdc"


# --- catalog and repositories by key -------------------------------------------------------

def test_catalog_lists_every_component_file(bundle_tree, monkeypatch):
    tree, _ = bundle_tree
    for name in ("RULES_DIR", "SKILLS_DIR", "IMPLANTS_DIR", "AGENTS_DIR"):
        monkeypatch.setattr(component_catalog, name, str(tree / name.split("_")[0].lower()))
    component_toggles.set_enabled("skills", "skill-extra", False)
    skills = {item["id"]: item for item in component_catalog.list_components("skills")}
    assert set(skills) == {"skill-core", "skill-extra"}
    assert skills["skill-core"]["declared_by"] == [{"agent": "engineer", "tier": "core"}]
    assert skills["skill-extra"]["enabled"] is False and skills["skill-core"]["enabled"] is True
    implant = component_catalog.list_components("implants")[0]
    assert implant["short_name"] == "Focus" and implant["body"] == "Focus body"
    assert component_catalog.list_components("rules")[0]["id"] == "truth"


def test_repository_flows_are_reachable_by_key_without_a_workspace(install, repo, toggle_dir):
    from src.flows import FlowCatalog, FlowError
    made = FlowLibrary(FlowCatalog(install), user_dir=toggle_dir, repo_root=repo)
    made.save("repo:notes", "# Notes\n", scope="repo")
    key = made.repo()[0]
    groups = FlowLibrary(FlowCatalog(install), user_dir=toggle_dir).repositories()
    assert [g["key"] for g in groups] == [key]
    assert groups[0]["flows"][0]["id"] == "repo:notes" and groups[0]["flows"][0]["repo_key"] == key
    by_key = FlowLibrary(FlowCatalog(install), user_dir=toggle_dir, repo_key=key)
    flow = by_key.get("repo:notes")
    by_key.save("repo:notes", "# Notes 2\n", scope="repo", expected_revision=flow["flow"]["revision"])
    assert by_key.get("repo:notes")["content"] == "# Notes 2\n"
    # Keys that repo_key() generates, such as one for a workspace named "_project", are valid.
    underscore = toggle_dir / "repos" / "_project-1a2b3c4d"
    underscore.mkdir()
    assert [g["key"] for g in FlowLibrary(FlowCatalog(install), user_dir=toggle_dir).repositories()
            if g["key"] == underscore.name] == []  # empty groups are not listed
    FlowLibrary(FlowCatalog(install), user_dir=toggle_dir, repo_key=underscore.name)
    for bad in ("../etc", "A", ".hidden", "missing-key", ["x"], 5):
        with pytest.raises(FlowError):
            FlowLibrary(FlowCatalog(install), user_dir=toggle_dir, repo_key=bad)
    (toggle_dir / "repos" / "linked").symlink_to(toggle_dir / "repos" / key)
    with pytest.raises(FlowError):
        FlowLibrary(FlowCatalog(install), user_dir=toggle_dir, repo_key="linked")


# --- editor API ----------------------------------------------------------------------------

@pytest_asyncio.fixture
async def editor(tmp_path, repo, install, monkeypatch):
    monkeypatch.setattr(component_catalog, "RULES_DIR", str(tmp_path / "rules"))
    monkeypatch.setattr(rules, "RULES_DIR", str(tmp_path / "rules"))
    write_mdc(tmp_path / "rules/rule-truth.mdc", {"name": "truth", "description": "d"}, "Evidence")
    write_mdc(tmp_path / "skills/skill-a.mdc", {"description": "A"}, "A body")
    monkeypatch.setattr(component_catalog, "SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(component_catalog, "IMPLANTS_DIR", str(tmp_path / "implants"))
    monkeypatch.setattr(component_catalog, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr("src.flows.FLOWS_DIR", str(install))
    monkeypatch.setattr("src.daemon.flows_ui.FlowCatalog", lambda: __import__("src.flows", fromlist=["x"]).FlowCatalog(install))
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


@pytest.mark.asyncio
async def test_component_endpoints_need_session_origin_and_header(editor):
    http, _ = editor
    body = {"kind": "skills", "id": "skill-a", "enabled": False}
    assert (await http.get("/ui/api/components", params={"kind": "skills"})).status_code == 401
    assert (await http.put("/ui/api/component", json=body, headers=UI)).status_code == 401
    await login(http)
    for headers in ({"X-Agents-UI": "1", "Origin": "https://attacker.example"},
                    {"Origin": "http://127.0.0.1:8765"}):
        assert (await http.put("/ui/api/component", json=body, headers=headers)).status_code == 403
    assert component_toggles.disabled("skills") == frozenset()
    assert (await http.put("/ui/api/component", json=body, headers=UI)).status_code == 200
    assert component_toggles.disabled("skills") == {"skill-a"}
    listing = (await http.get("/ui/api/components", params={"kind": "skills"})).json()
    assert listing["items"][0]["id"] == "skill-a" and listing["items"][0]["enabled"] is False


@pytest.mark.asyncio
async def test_component_endpoint_validation(editor):
    http, _ = editor
    await login(http)
    assert (await http.get("/ui/api/components", params={"kind": "agents"})).status_code == 400
    for body in ({"kind": "skills", "id": "nope", "enabled": False},
                 {"kind": "skills", "id": "skill-a", "enabled": "no"},
                 {"kind": "agents", "id": "x", "enabled": True}):
        assert (await http.put("/ui/api/component", json=body, headers=UI)).status_code in (400, 404)
    assert component_toggles.disabled("skills") == frozenset()


@pytest.mark.asyncio
async def test_ui_lists_repository_flows_without_workspace_and_edits_by_key(editor, repo):
    http, workspace = editor
    await login(http)
    created = await http.put("/ui/api/flow", headers=UI, json={
        "id": "repo:notes", "content": "# Notes\n", "scope": "repo", "workspace": workspace})
    assert created.status_code == 200
    key = created.json()["flow"]["repo_key"]
    listing = (await http.get("/ui/api/flows")).json()
    assert [g["key"] for g in listing["repositories"]] == [key]
    flow = (await http.get("/ui/api/flow", params={"id": "repo:notes", "repo": key})).json()
    saved = await http.put("/ui/api/flow", headers=UI, json={
        "id": "repo:notes", "content": "# Notes 2\n", "scope": "repo", "repo": key,
        "expected_revision": flow["flow"]["revision"]})
    assert saved.status_code == 200
    assert (await http.get("/ui/api/flow", params={"id": "repo:notes", "repo": "../x"})).status_code == 400
    removed = await http.request("DELETE", "/ui/api/flow", headers=UI, json={
        "id": "repo:notes", "repo": key, "expected_revision": saved.json()["flow"]["revision"]})
    assert removed.status_code == 200


@pytest.mark.asyncio
async def test_switching_off_the_last_preferred_implant_keeps_the_tier_promotion(bundle_tree, monkeypatch):
    seen = []
    monkeypatch.setattr(enrichment, "resolve_profile", lambda query: None)
    monkeypatch.setattr(enrichment, "infer_tier", lambda query: "lite")
    monkeypatch.setattr(enrichment, "_n_results_for_tier",
                        lambda tier: seen.append(tier) or 3)
    await build_persona_bundle("engineer", "hi", None, None)
    component_toggles.set_enabled("implants", "implant-focus", False)
    await build_persona_bundle("engineer", "hi", None, None)
    assert seen == ["standard", "standard"]
