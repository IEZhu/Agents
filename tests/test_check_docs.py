"""Documentation checks must detect drift without importing production services."""
from scripts.check_docs import check_links, check_tool_index, declared_tools, parse_document


def test_markdown_navigation_and_rendered_anchors():
    anchors, links = parse_document('''# **First** `call()`
# First call()
Setext heading
==============
<a id="explicit"></a>
[inline](other.md#part)
[reference][guide]
![image](figure.png)
`[example](missing.md)`
```markdown
[example](absent.md)
# Fenced heading
```
[guide]: other.md "Title"
''')
    assert anchors == {"first-call", "first-call-1", "setext-heading", "explicit"}
    assert links == ["other.md#part", "other.md", "figure.png"]


def test_links_resolve_encoded_paths_root_links_and_fragments(tmp_path):
    guide = tmp_path / "guide.md"
    guide.write_text("# Overview\n# Overview\n", encoding="utf-8")
    (tmp_path / "a b.png").write_bytes(b"image")
    doc = tmp_path / "docs" / "readme.md"
    doc.parent.mkdir()
    doc.write_text("[a](/guide.md#overview-1)\n![b](../a%20b.png)\n"
                   "[web](https://example.invalid/missing)\n[x](../guide.md#absent)\n"
                   "[missing](gone.md)\n[outside](../../outside.md)\n", encoding="utf-8")
    errors = check_links(tmp_path, [doc])
    assert len(errors) == 3
    assert any("missing anchor" in error for error in errors)
    assert any("missing path" in error for error in errors)
    assert any("escapes repository" in error for error in errors)


def test_public_tool_inventory_detects_new_and_removed_tools(tmp_path):
    for directory in ["src/local_inference", "src/artifacts", "docs"]:
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "src/server.py").write_text('''raise RuntimeError("never import")
@mcp.tool()
async def registered(): pass
@mcp.prompt()
def prompt_only(): pass
@mcp.tool(name="public_alias")
def internal_name(): pass
''', encoding="utf-8")
    (tmp_path / "src/local_inference/tools.py").write_text('TOOL_NAMES = ("local_tool",)\n')
    (tmp_path / "src/artifacts/service.py").write_text('REQUEST_MODELS = {"artifact_tool": Request}\n')
    assert declared_tools(tmp_path) == {"registered", "public_alias", "local_tool", "artifact_tool"}
    (tmp_path / "docs/mcp-tools.md").write_text(
        "| `registered` | Covered |\n| `removed` | Obsolete |\n", encoding="utf-8")
    errors = check_tool_index(tmp_path)
    assert len(errors) == 4
    assert "Unknown MCP tool in docs/mcp-tools.md: removed" in errors
    assert "MCP tool missing from docs/mcp-tools.md: public_alias" in errors


def test_rendered_html_anchors_exclude_comments_and_accept_valid_attributes(tmp_path):
    doc = tmp_path / "guide.md"
    for html in ['<span id = "valid"></span>', '<span id=valid></span>',
                 '<a name="valid"></a>']:
        doc.write_text(html + '\n\n[section](#valid)\n', encoding="utf-8")
        assert not check_links(tmp_path, [doc])
    doc.write_text('<!-- <a id="removed-section"></a> -->\n\n'
                   '[section](#removed-section)\n', encoding="utf-8")
    assert check_links(tmp_path, [doc]) == [
        'guide.md: #removed-section: missing anchor']


def test_static_inventory_matches_registered_runtime_tools():
    """A new registrar must not silently escape the offline completeness check."""
    import asyncio

    from scripts.check_docs import ROOT
    from src.server import mcp

    assert declared_tools(ROOT) == {tool.name for tool in asyncio.run(mcp.list_tools())}


def test_badge_counts_only_registered_references_and_detects_stale_counts(tmp_path, monkeypatch):
    import xml.etree.ElementTree as ET

    from scripts import check_docs

    monkeypatch.setattr(check_docs, "declared_tools", lambda root: {"one", "two"})
    monkeypatch.setattr(check_docs, "documented_tools", lambda root: {"one", "obsolete"})
    badge = check_docs.tool_badge(tmp_path)
    assert "1 of 2 registered tools" in ET.fromstring(badge)[0].text
    assert check_docs.check_tool_badge(tmp_path)
    path = tmp_path / check_docs.BADGE_PATH
    path.parent.mkdir(parents=True)
    path.write_text(badge, encoding="utf-8")
    assert not check_docs.check_tool_badge(tmp_path)
    monkeypatch.setattr(check_docs, "declared_tools", lambda root: {"one", "two", "three"})
    assert check_docs.check_tool_badge(tmp_path)


def test_explicit_badge_update_precedes_navigation_check(tmp_path, monkeypatch):
    from scripts import check_docs

    monkeypatch.setattr(check_docs, "ROOT", tmp_path)
    monkeypatch.setattr(check_docs, "declared_tools", lambda root: {"one"})
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/mcp-tools.md").write_text('| `one` | Reference |\n', encoding="utf-8")
    (tmp_path / "README.md").write_text('![MCP API docs](docs/badges/mcp-api-docs.svg)\n', encoding="utf-8")
    assert check_docs.main(["README.md", "--update-badge"]) == 0
    path = tmp_path / check_docs.BADGE_PATH
    before = path.stat().st_mtime_ns
    assert check_docs.main(["README.md"]) == 0
    assert path.stat().st_mtime_ns == before
