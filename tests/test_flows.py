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
    assert {entry["id"] for entry in entries} >= {"documentation-refresh", "pr-review", "pr-queue"}


def test_pr_review_reads_the_merge_gating_from_more_than_one_endpoint():
    """A session read classic protection alone and reported a gated branch as ungated."""
    review = " ".join(FlowCatalog().load("pr-review").content.split())
    for phrase in ("gh api --paginate repos/OWNER/REPO/rulesets",
                   "`Branch not protected`",
                   "gh pr view N --json reviewDecision",
                   "can be `MERGEABLE` and `BLOCKED` at the same time",
                   # One page of rulesets, and an omitted bypass list, each read as
                   # an absence of gating.
                   "returns 30 rulesets at a time",
                   "omits `bypass_actors` for a caller without write access",
                   # The REST request answers 200 and queues nothing for this bot.
                   "answers 200 for this bot and registers nothing",
                   "requestCopilotCodeReview(input:{",
                   "no bot node id and so no earlier Copilot review to take one from",
                   "A registered reviewer is still not a review.",
                   "takes `reviewEffort`, whose values are `LITE`, `BALANCED`, `MAX` and `AUTO`",
                   # Two requests were spent on heads CodeRabbit had already picked up.
                   "Read the head's own check runs before asking it for anything",
                   "What suppresses a request is a run still going or a completed review of that head",
                   "a rate-limited, paused or skipped status is a run of a kind and not a review"):
        assert phrase in review, phrase
    assert "on GitLab read that flow's own paragraph on the equivalents" in review


def test_pr_queue_pins_the_steps_a_single_review_cannot_know():
    """#510: a queue was improvised once per item. Each step below had to be rediscovered."""
    catalog = FlowCatalog()
    queue = " ".join(catalog.load("pr-queue").content.split())
    for phrase in (
            # A stored rerere resolution hid a conflict in a local chain simulation.
            "git -c rerere.enabled=false merge --no-ff",
            "a stored resolution from an earlier attempt hides a conflict",
            # Two merge bases: ort resolved the item while GitHub reported a conflict.
            "git merge-base --all",
            "Plan a merge of the target into that item right after its predecessor lands",
            # Every merge invalidates the simulation the queue was planned on.
            "Repeat the simulation after every merge",
            "Re-simulate the remaining items on a fresh throwaway branch",
            # A check red on every item because of the target's own state.
            "workflow_dispatch",
            # The approval requirement and who may bypass it, read rather than assumed.
            "an account cannot approve its own request",
            # pr-review's own rules are not relaxed by the queue.
            "merge no other item's branch into it",
            "a queue run carries `no-merge` into every item it\nstarts",
            "Carrying `no-merge` moves one of those conditions onto this flow.",
            "Never present an item\nas ready with neither.",
            "Being next in a queue is not consent to merge an item,",
            "the merges are the owner's to make",
            # Corrections from the first review round of this flow, each of which a
            # reader would otherwise have to rediscover the way the queue did.
            "Do not\nstop at the first conflict",
            "git refuses to delete the branch that is checked\nout",
            "An item opened from a fork has its\nhead in that fork",
            "declares the `workflow_dispatch` trigger on\n   that repository's default branch",
            "re-simulate and run it again\nwhen a head changes",
            "GitHub's default `pull_request` checkout is the merge of the head into the\nbase",
            # Second review round of this flow.
            "Do not push the simulation to the target repository.",
            "the surface is the whole execution graph and not\n   the YAML",
            "a private target never goes to a public\n   repository",
            "gh workflow run vulnerability-scan.yml --repo <simulation repository>",
            "Resolve every item to its exact current head SHA",
            "Run step 3's checks again on that new simulation",
            "Fetch the target branch from the remote that step 1 verified holds it",
            # Third review round of this flow.
            "the no-push\nrule covers the simulation rather than the flow",
            "The one push this flow does make is the\ntarget merge into an item's own branch",
            "Read `FETCH_HEAD`\nimmediately after the target fetch",
            "each with\nthe same `-c rerere.enabled=false`",
            "a contributor\n   with a branch in the target can add any of that just as a fork can",
            "a head in a fork belongs\n   to its owner",
            "gh api --paginate repos/<owner>/<repo>/rulesets",
            "**This flow does not merge, and it never widens authorization.**",
            "That is one fetch per item: two items on the same remote are two refs",
            "Fetch every remaining item's head from the remote that holds it and re-resolve",
            "Read it from the remote's URL (`git remote -v`),\nnot from its name",
            "A predecessor that lives only in a fork cannot be a base",
            "compare that\ncount with the required one",
            "That is a consequence of the deletion, not of the\nmerge",
            "count the eligible approvers",
            # Running this flow on its own queue: a shortened head SHA made every
            # item report what reads like a conflict and is none.
            "A SHA named in a fetch is the whole forty characters.",
            "git rev-parse --verify <item head>^{commit}",
            # A branch dispatch is not a fork trigger, so the run receives every
            # secret the destination reaches, inherited organization ones included.
            "A `workflow_dispatch` run on a branch is not a fork trigger",
            "inherits an organization\n   secret set to all repositories without anyone",
            "an unread list is not\n   an empty one",
            # Copilot, on that same paragraph: the lists are paginated, the
            # environments were missing, and an ephemeral runner is not the property.
            "gh api --paginate repos/<owner>/<repo>/environments/<name>/secrets",
            "an ephemeral runner is not the property that would",
            "dispatch is the exception rather than the second of two equal options",
            # Second Copilot and CodeRabbit round on that paragraph: the gate sat
            # after the push, the organization list does not exist for a user-owned
            # repository, and two of the requirements were not enforceable at all.
            "Establish it before the push and not before the dispatch",
            "a user-owned repository has no\n   organization list",
            "a rule that counted it as a reachable\n   credential would refuse every dispatch ever",
            "that file comes from the simulation and an item\n   can rewrite it",
            "no identity federation accepts that repository",
            # Third round on that paragraph: a repository runner list hides an
            # organization runner shared through a group, and the token leaves marks
            # in the owner that no branch deletion takes back.
            "orgs/<owner>/actions/runner-groups",
            "a repository's runner list does not show an organization runner shared",
            "a package published with the token lives in the owner's namespace",
            "one of the two events GitHub does not suppress when the token creates them",
            "read the destination's owner as the blast radius",
            "stop extending this list",
            "the run left no package in the owner's namespace"):
        assert " ".join(phrase.split()) in queue, phrase
    review = " ".join(catalog.load("pr-review").content.split())
    assert "run [pr-queue](pr-queue.md) instead" in review


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
