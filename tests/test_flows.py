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
