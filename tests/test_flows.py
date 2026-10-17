"""Installed workflow discovery, confinement and fresh instruction delivery."""
import hashlib

import pytest

from src.flows import FlowCatalog, FlowError, execution_bundle


@pytest.fixture
def catalog(tmp_path):
    directory = tmp_path / "install" / "flows"
    directory.mkdir(parents=True)
    (directory / "README.md").write_text("# Catalog\n", encoding="utf-8")
    (directory / "docs.md").write_text("# Refresh docs\n\nCheck the target.\n", encoding="utf-8")
    return FlowCatalog(directory)


def test_discovery_and_fresh_revision(catalog):
    entries = catalog.list()
    assert [entry["id"] for entry in entries] == ["docs"]
    flow = catalog.load("flows/docs.md")
    assert entries == [flow.metadata()]
    assert flow.title == "Refresh docs"
    assert flow.revision == hashlib.sha256(flow.content.encode()).hexdigest()
    flow.source_path.write_text("# Changed\n\nNew steps.\n", encoding="utf-8")
    updated = catalog.load("docs.md")
    assert updated.revision != flow.revision
    assert updated.title == "Changed"
    (catalog.directory / "new-flow.md").write_text("# New flow\n", encoding="utf-8")
    assert [entry["id"] for entry in catalog.list()] == ["docs", "new-flow"]


@pytest.mark.parametrize("name", ["../docs", "/tmp/docs.md", "flows/../docs.md",
    "flows/sub/docs.md", "flows\\docs.md", "docs.md.md", "README", "README.md",
    "readme", "readme.md", "", " docs", "docs\x00"])
def test_invalid_names(catalog, name):
    with pytest.raises(FlowError, match="flow_invalid"):
        catalog.load(name)


def test_missing_flow_and_catalog(catalog, tmp_path):
    with pytest.raises(FlowError, match="flow_not_found"):
        catalog.load("missing")
    with pytest.raises(FlowError, match="flows_unavailable"):
        FlowCatalog(tmp_path / "missing").list()


def test_catalog_ignores_non_flow_files(catalog):
    for name in ("docs.md.md", "UPPER.md", "readme.md", "draft.txt"):
        (catalog.directory / name).write_text("# Not a flow\n", encoding="utf-8")
    nested = catalog.directory / "nested"
    nested.mkdir()
    (nested / "hidden.md").write_text("# Nested\n", encoding="utf-8")
    assert [entry["id"] for entry in catalog.list()] == ["docs"]


def test_source_symlink_must_stay_in_catalog(catalog, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (catalog.directory / "escape.md").symlink_to(outside)
    with pytest.raises(FlowError, match="source escapes"):
        catalog.load("escape")
    (catalog.directory / "alias.md").symlink_to(catalog.directory / "docs.md")
    assert catalog.load("alias").content == catalog.load("docs").content


@pytest.mark.parametrize(("raw", "error"), [(b"", "flow_invalid"),
    (b" \n", "flow_invalid"), (b"\xff", "flow_unreadable"),
    (b"a" * (256 * 1024 + 1), "flow_invalid")])
def test_invalid_content(catalog, raw, error):
    (catalog.directory / "docs.md").write_bytes(raw)
    with pytest.raises(FlowError, match=error):
        catalog.load("docs")


def test_bundle_distinguishes_source_target_and_completion(catalog, tmp_path):
    target = tmp_path / "client"
    target.mkdir()
    # A caller's file cannot override an installed flow with the same name.
    (target / "flows").mkdir()
    (target / "flows" / "docs.md").write_text("# Wrong source\n", encoding="utf-8")
    bundle = execution_bundle(catalog.load("docs"), target, "workspace-id", "no-merge")
    assert bundle["status"] == "needs_execution"
    assert bundle["repo_path"] == str(target)
    assert bundle["workspace_id"] == "workspace-id"
    assert bundle["request"] == "no-merge"
    assert bundle["content"] == catalog.load("docs").content
    assert bundle["flow"]["source_path"].startswith(str(catalog.directory))
    assert "has not executed" in bundle["instruction"]


def test_shipped_catalog_is_loadable():
    entries = FlowCatalog().list()
    assert {entry["id"] for entry in entries} >= {"documentation-refresh", "pr-review"}


# The autodev flows (#346): the dispatcher and the gate's rules have no persona; each stage
# and delegated step declares its agent with exact skills and implants (design 8.5, 9.1).
AUTODEV_PERSONAS = {
    "autodev": None,
    "autodev-gate": None,
    "autodev-select": {
        "agent": "product_manager",
        "skills": ["skill-product-frameworks", "skill-decision-frameworks", "skill-analysis-critical",
                   "skill-structured-output"],
        "implants": ["implant-verify-assumptions", "implant-second-order-thinking"]},
    "autodev-implement": {
        "agent": "software_engineer",
        "skills": ["skill-dev-clean-code", "skill-dev-testing", "skill-dev-debugging", "skill-dev-security",
                   "skill-git-conventions", "skill-structured-output"],
        "implants": ["implant-regression-first", "implant-iteration-budget"]},
    "code-review-pass": {
        "agent": "code_reviewer",
        "skills": ["skill-dev-clean-code", "skill-dev-testing", "skill-dev-security", "skill-dev-debugging"],
        "implants": ["implant-chain-of-verification", "implant-regression-first"]},
    "change-premortem": {
        "agent": "sysadmin",
        "skills": ["skill-dev-security", "skill-dev-testing", "skill-error-recovery", "skill-system-design"],
        "implants": ["implant-premortem", "implant-second-order-thinking"]},
}


@pytest.mark.asyncio
async def test_builtin_flow_personas_build_complete_bundles(monkeypatch):
    """A declared persona must activate: unknown or invalid components would fail every run.

    Builds the real bundle at a fixed tier; the intent classifier is skipped
    because it loads an embedding model that is irrelevant here.
    """
    from src import flow_persona
    from src.engine import enrichment
    from src.engine.persona_bundle import build_persona_bundle

    monkeypatch.setattr(enrichment, "resolve_profile", lambda query: None)
    catalog = FlowCatalog()
    declared = {flow_id: flow_persona.declared(catalog.load(flow_id).content)
                for flow_id in catalog.ids()}
    for flow_id, spec in declared.items():
        if spec is None:
            continue
        flow_persona.check_known(spec)
        if not {"skills", "implants"} <= set(spec):
            continue  # Omitted lists retrieve by relevance, which needs the index.
        bundle = await build_persona_bundle(spec["agent"], flow_id, tier="standard",
                                            selection=flow_persona.selection(spec))
        assert bundle.skills_loaded == spec["skills"]
        assert len(bundle.implants_loaded) == len(spec["implants"])
    assert declared["documentation-refresh"]["agent"] == "tech_writer"
    assert declared["thread-close"]["agent"] == "investigative_analyst"
    assert declared["issue-plan"]["agent"] == "system_architect"
    assert declared["issue-implementation"]["agent"] == "software_engineer"
    assert declared["pr-review"]["agent"] == "code_reviewer"
    assert declared["ab-eval"]["agent"] == "ai_senior_engineer"
    assert declared["issue-agent"] is None
    for flow_id, spec in AUTODEV_PERSONAS.items():
        assert declared[flow_id] == spec


def test_list_flows_shows_the_autodev_flows_with_their_personas(tmp_path):
    from src.user_flows import FlowLibrary

    listed = {flow["id"]: flow for flow in
              FlowLibrary(FlowCatalog(), user_dir=tmp_path / "library").list("builtin")["flows"]}
    for flow_id, spec in AUTODEV_PERSONAS.items():
        assert "persona_error" not in listed[flow_id]
        assert listed[flow_id]["persona"] == spec
        assert listed[flow_id]["persona_source"] == ("frontmatter" if spec else None)


def test_delegated_step_flows_defer_to_the_prepared_package():
    """Design 8.5: a prepared worker follows its package, not the flow's persona and activation."""
    catalog = FlowCatalog()
    for flow_id in ("code-review-pass", "change-premortem"):
        text = catalog.load(flow_id).content
        assert "## As a delegated worker" in text
        assert "the package and its worker rules govern" in text
        assert "ReviewPayload" in text
    readme = (catalog.directory / "README.md").read_text(encoding="utf-8")
    delegated = readme.split("## Delegated workers", 1)[1].split("\n## ", 1)[0]
    assert "task material for a prepared worker" in delegated
    assert "role activation do not apply" in delegated


def test_readme_catalog_lists_every_builtin_flow():
    catalog = FlowCatalog()
    readme = (catalog.directory / "README.md").read_text(encoding="utf-8")
    table = readme.split("## Available flows", 1)[1].split("\n## ", 1)[0]
    assert [flow_id for flow_id in catalog.ids() if f"]({flow_id}.md)" not in table] == []


def test_protocol_runs_a_named_flow_before_improvising():
    """A flow the user asked for by name was improvised; the protocol now names that trigger."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for name in ("scripts/templates/routing-protocol-core.md", "CLAUDE.md"):
        flat = " ".join((root / name).read_text(encoding="utf-8").split())
        section = flat.split("## Run a named flow", 1)[1].split(" ## ", 1)[0]
        for phrase in ("call `list_flows` and then `run_flow` for the matching flow before taking any of its steps",
                       '"флоу"', '"по флоу X"', "apply a returned `persona_activation` as a `switch`",
                       "Do not improvise a procedure that an installed flow covers."):
            assert phrase in section, (name, phrase)
    import src.server as server
    assert ("call list_flows and then run_flow before taking its steps; do not improvise a procedure an "
            "installed flow covers.") in server.mcp.instructions


@pytest.mark.asyncio
async def test_thread_close_audits_every_installed_tool_and_catalogues_every_server_tool():
    """The audit covers all installed tools; its Agents-Core catalogue names each tool the server registers."""
    import re

    import src.server as server

    text = FlowCatalog().load("thread-close").content
    audit = " ".join(text.split("## 4. Audit tool usage", 1)[1].split("\n## ", 1)[0].split())
    for phrase in ("Installed tools are everything the client exposed to the thread",
                   "A tool that no instruction names is `no-rule`",
                   "Write one patch for each `missed`, `misused` and `uncatalogued` finding",
                   "**Managed blocks.**", "**One list, one yes.**"):
        assert phrase in audit, phrase
    table = text.split("**Catalogue.**", 1)[1].split("\n## ", 1)[0]
    cells = [line.split("|")[2] for line in table.splitlines() if line.startswith("| `")]
    catalogued = {re.sub(r"\(.*", "", name) for cell in cells for name in re.findall(r"`([^`]+)`", cell)}
    registered = {tool.name for tool in await server.mcp.list_tools()}
    assert sorted(registered - catalogued) == [] and sorted(catalogued - registered) == []
