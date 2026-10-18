"""Run the sync preview and scope-confirmation renderers against a minimal DOM."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


NODE = shutil.which("node")
PAGE = Path(__file__).resolve().parents[1] / "src/daemon/flows_ui.html"


@pytest.mark.skipif(NODE is None, reason="needs node")
@pytest.mark.parametrize("location", ["blocked", "history_blocked", "scope_confirmation"])
def test_invalid_mcp_record_rendering_separates_repair_from_credentials(location):
    source = PAGE.read_text(encoding="utf-8")
    functions = []
    for name in ("previewList", "previewBlockedList", "renderPreview", "renderScopeList"):
        match = re.search(r"^function " + name + r"\([^\n]*\) \{.*?^\}", source, re.M | re.S)
        if match:
            functions.append(match.group())
    script = r'''
const node = (tag, parent, text = "") => {
  const result = {tag, text, children: [], replaceChildren() { this.children = []; }};
  if (parent) parent.children.push(result);
  return result;
};
const root = node("div");
const $ = () => root;
const keepFocus = () => () => {};
const checkbox = () => {};
const action = (parent, text) => node("button", parent, text);
const plural = (n, word) => n + " " + word;
const PREVIEW_KINDS = {push: "Upload this library"};
const GROUP_NAMES = {};
const items = [
  {path: "mcp/servers/example.json", pattern: "invalid_mcp_record"},
  {path: "common/deploy.md", pattern: "github_token"}
];
const sync = {scopes: {groups: []}, scopeConfirm: {
  target: "history", how: "include", upload: [], history_versions: 1, history_blocked: items
}};
''' + "\n".join(functions) + "\n" + (
        "renderScopeList(root);" if location == "scope_confirmation" else
        "renderPreview(root, " + json.dumps({"kind": "push", location: [
            {"path": "mcp/servers/example.json", "pattern": "invalid_mcp_record"},
            {"path": "common/deploy.md", "pattern": "github_token"}]}) + ");"
    ) + "\nconsole.log(JSON.stringify(root));"
    completed = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=10, check=True)
    root = json.loads(completed.stdout)
    flat = []

    def walk(element):
        flat.append(element)
        for child in element["children"]:
            walk(child)

    walk(root)
    headings = [item["text"] for item in flat if item["tag"] == "h4"]
    assert any("possible credentials" in text for text in headings)
    assert any("invalid MCP records" in text and "fix or re-save" in text.lower() for text in headings)
    assert any("invalid saved versions stay local" in text for text in headings) is (location != "blocked")
    assert all("allow" not in item["text"].lower() for item in flat if item["tag"] == "button")
    lists = [item for item in flat if item["tag"] == "ul" and item["children"]]
    assert any([child["text"] for child in item["children"]] == ["common/deploy.md (github_token)"] for item in lists)
    assert any([child["text"] for child in item["children"]] == ["mcp/servers/example.json (invalid_mcp_record)"] for item in lists)
