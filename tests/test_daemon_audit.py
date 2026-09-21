import json
from pathlib import Path

import pytest

from src.daemon.audit import inventory


@pytest.mark.parametrize("failure", ["invalid_json", "unreadable"])
def test_unreadable_claude_config_does_not_abort_other_scopes(tmp_path, monkeypatch, failure):
    home = tmp_path / "home"
    home.mkdir()
    claude = home / ".claude.json"
    claude.write_text("{" if failure == "invalid_json" else "{}")
    if failure == "unreadable":
        read_text = Path.read_text

        def read(path, *args, **kwargs):
            if path == claude:
                raise PermissionError("access denied")
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    cursor = home / ".cursor/mcp.json"
    cursor.parent.mkdir()
    cursor.write_text(json.dumps({"mcpServers": {"Agents-Core": {"url": "http://127.0.0.1:8765/mcp"}}}))
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / ".mcp.json").write_text('{"mcpServers": {"example": {"command": "node"}}}')

    results = inventory(home=home, workspace=workspace)

    assert {"path": str(claude), "scope": "claude:user", "error": "unreadable configuration"} in results
    assert any(row.get("server") == "Agents-Core" and row["scope"] == "cursor:user" for row in results)
    assert any(row.get("server") == "example" and row["scope"] == "claude:project" for row in results)


@pytest.mark.parametrize("document", [[], {"mcpServers": []}, {"mcpServers": None},
                                     {"projects": []}, {"projects": {"/example": []}}])
def test_invalid_claude_configuration_shape_keeps_audit_running(tmp_path, document):
    claude = tmp_path / ".claude.json"
    claude.write_text(json.dumps(document))
    cursor = tmp_path / ".cursor/mcp.json"
    cursor.parent.mkdir()
    cursor.write_text('{"mcpServers": {"other": {"command": "node"}}}')

    results = inventory(home=tmp_path)

    assert any(row["path"] == str(claude) and row.get("error") == "unreadable configuration" for row in results)
    assert any(row.get("server") == "other" for row in results)


@pytest.mark.parametrize("document", [[], {"mcpServers": []}, {"mcpServers": None},
                                     {"mcpServers": {"broken": {"headers": None}}},
                                     {"mcpServers": {"broken": {"command": []}}}])
def test_invalid_project_configuration_shape_keeps_other_scopes(tmp_path, document):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text('{"mcpServers": {"other": {"command": "node"}}}')
    project = tmp_path / "project"
    project.mkdir()
    path = project / ".mcp.json"
    path.write_text(json.dumps(document))

    results = inventory(home=home, workspace=project)

    assert any(row["path"] == str(path) and row.get("error") == "unreadable configuration" for row in results)
    assert any(row.get("server") == "other" for row in results)
