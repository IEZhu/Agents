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


def test_builtin_flow_personas_name_parsable_components():
    """A declared persona must activate: unknown or unparsable components would fail every run.

    Reads each file as strictly as a bundle does, without the intent classifier.
    """
    from src import flow_persona
    from src.engine.persona_bundle import _fresh_components
    from src.utils.prompt_loader import read_mdc, resolve_path

    catalog = FlowCatalog()
    declared = {flow_id: flow_persona.declared(catalog.load(flow_id).content)
                for flow_id in catalog.ids()}
    for spec in filter(None, declared.values()):
        flow_persona.check_known(spec)
        read_mdc(resolve_path(f"@agents/{spec['agent']}/system_prompt.mdc"), require_frontmatter=True)
        for kind in ("skills", "implants"):
            assert len(_fresh_components(kind, spec[kind])) == len(spec[kind])
    assert declared["issue-plan"]["agent"] == "system_architect"
    assert declared["issue-implementation"]["agent"] == "software_engineer"
    assert declared["pr-review"]["agent"] == "code_reviewer"
    assert declared["issue-agent"] is None
