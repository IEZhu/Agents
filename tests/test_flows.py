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
    (b"a" * (256 * 1024 + 1), "flow_invalid")],
    ids=["empty", "whitespace", "invalid-utf8", "oversized"])
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

    Builds real source components at a fixed tier without initializing a vector
    index or model. Declared selections do not require semantic retrieval.
    """
    from src import flow_persona
    from types import SimpleNamespace
    from src.engine import embedder, enrichment
    from src.engine.skills import SkillRetriever
    from src.engine.implants import ImplantRetriever
    from src.engine.persona_bundle import build_persona_bundle

    monkeypatch.setattr(enrichment, "resolve_profile", lambda query: None)
    monkeypatch.setattr(embedder, "_get_model", lambda: pytest.fail("Flow bundle validation must not load an embedding model"))
    # Exact component selections still use the real formatters, but do not need
    # retriever construction, indexes or an embedding model on a clean runner.
    monkeypatch.setattr(enrichment, "_skill_retriever", SimpleNamespace(
        format_skills_for_prompt=lambda items, **kwargs:
            SkillRetriever.format_skills_for_prompt(None, items, **kwargs),
    ))
    monkeypatch.setattr(enrichment, "_implant_retriever", SimpleNamespace(
        format_implants_for_prompt=lambda items:
            ImplantRetriever.format_implants_for_prompt(None, items),
    ))
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
    instructions = " ".join(server.mcp.instructions.split())
    for phrase in ("When the user names a flow, workflow or ritual, or asks for work by one, in any language",
                   "call list_flows and then run_flow for a matching flow before taking its steps.",
                   "If no flow matches, say so and continue without one.",
                   "Do not improvise a procedure an installed flow covers."):
        assert phrase in instructions, phrase


@pytest.mark.asyncio
async def test_thread_close_audits_every_installed_tool_and_catalogues_every_server_tool():
    """Static tool names stay complete alongside the runtime proxy family."""
    import re

    import src.server as server

    text = FlowCatalog().load("thread-close").content
    audit = " ".join(text.split("## 4. Audit tool usage", 1)[1].split("\n## ", 1)[0].split())
    for phrase in ("Installed tools are everything the client exposed to the thread",
                   "A tool that no instruction names is `no-rule`",
                   "a `missed` finding is `?`, and a `?` never becomes a memory lesson",
                   "Write one patch for each `missed`, `misused` and `uncatalogued` finding",
                   "**Managed blocks.**", "**One list, one yes.**"):
        assert phrase in audit, phrase
    table = text.split("**Catalogue.**", 1)[1].split("\n## ", 1)[0]
    rows = {line.split("|")[1].strip(" `"): line.split("|")[2]
            for line in table.splitlines() if line.startswith("| `")}
    assert "`<alias>__<tool>`" in rows.pop("mcp-proxy")
    proxy_read = rows.pop('proxy-read')
    assert set(re.findall(r"`([^`]+)`", proxy_read)) == {'code_execute_readonly', 'get_status', 'get_stats', 'get_data_range', 'get_today_stats', 'code_search', 'get_settings'}
    proxy_state = rows.pop('proxy-state')
    assert set(re.findall(r"`([^`]+)`", proxy_state)) == {'skip_break', 'pause_timer', 'start_break', 'resume_timer', 'code_execute'}
    cells = rows.values()
    catalogued = {re.sub(r"\(.*", "", name) for cell in cells for name in re.findall(r"`([^`]+)`", cell)}
    registered = {tool.name for tool in await server.mcp.list_tools()}
    assert sorted(registered - catalogued) == [] and sorted(catalogued - registered) == []
    for phrase in ("a saved proxy definition or cached catalogue does not prove a live connection",
                   "access claimed without checking the live connection and the upstream app's state",
                   "Blincho detailed activity spans, call history or names, event IDs, journal answers or camera frames",
                   "queued timer work was reported completed without checking the resulting state",
                   "an unfinished strict break was bypassed"):
        assert phrase in " ".join(table.split()), phrase
    misuse = {line.split("|")[1].strip(" `"): line.split("|")[4]
              for line in table.splitlines() if line.startswith("| `proxy-")}
    for slug in ("proxy-read", "proxy-state"):
        assert "credentials or tokens exposed in output" in misuse[slug], slug


def test_flow_execution_uses_valid_default_tokensave_selector_and_current_ax_targets():
    """Shared guidance preserves selector validity and the observation/action dependency."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    execution = " ".join((root / "docs/flow-execution.md").read_text(encoding="utf-8").split())
    safety = " ".join((root / "docs/mcp-tool-safety.md").read_text(encoding="utf-8").split())
    for phrase in ("await tools.mcp__tokensave__tokensave_status({});",
                   "Omit `graph_root` for that default project",
                   "explicitly different, already initialized project",
                   "Do not initialize indexing, add a tracked branch",
                   "verify relevant output against the current file or a pinned Git object"):
        assert phrase in execution, phrase
    for phrase in ("First obtain the latest AX state and inspect its returned tree",
                   "Only then derive the element index from that tree",
                   "Never batch `getAXState({emit:false, disableDiffing:true})` with a `click(index)` chosen from an earlier observation",
                   "After every action, verify the observed state",
                   "These helpers are opt-in orchestration boundaries; they do not intercept other tool calls"):
        assert phrase in safety, phrase


def test_bounded_return_gate_is_stated_once_per_client_class_and_linked():
    """One canonical gate: orchestrated and direct-call rules live in the safety guide only."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    safety = " ".join((root / "docs/mcp-tool-safety.md").read_text(encoding="utf-8").split())
    for phrase in ("## Bounded-return gate by client class",
                   "**Orchestrated hosts**", "**Direct-call clients**",
                   "reach model context unguarded: never report such a result as guarded",
                   "No Agents-Core component installs the guard in any client",
                   "https://github.com/IEZhu/Agents/issues/506",
                   "The helper does not intercept native-client, terminal or third-party responses",
                   "Otherwise the first discovery call is exempt",
                   "A refusal before a usable reference exists", "its `sourceRef` is null",
                   "Exactly four codes do so: `invalid_guard_options`",
                   "The caller can pass each result through the guard, and this gate requires it"):
        assert phrase in safety, phrase
    for gone in ("any client applies it automatically", "before any adapter could run",
                 "A refusal before retention", "so the guard applies"):
        assert gone not in safety, gone
    for name in ("flows/pr-review.md", "docs/flow-execution.md"):
        text = (root / name).read_text(encoding="utf-8")
        assert "mcp-tool-safety.md#bounded-return-gate-by-client-class" in text, name
        for restated in ("before any adapter could run", "8,000-character", "Never forward an unfiltered"):
            assert restated not in text, (name, restated)


def test_thread_close_handoff_keeps_required_obligations_unfinished():
    text = " ".join(FlowCatalog().load("thread-close").content.split())
    for phrase in ("use `finishing` (🧹) when required obligations remain",
                   "unless essential user input or required human approval calls for `needs_user`/`review`",
                   "Resume implementation as `working`",
                   "Verify or explicitly resolve every required task before `ready_to_close`",
                   "`closed` requires explicit closure or confirmed archival",
                   "The status grants no additional authority"):
        assert phrase in text
