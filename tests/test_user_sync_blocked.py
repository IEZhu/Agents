"""Blocked MCP records need a repair, while ordinary scanner hits can be allowed."""
import pytest

from src.user_sync import scope, wizard
from src.user_sync.__main__ import _print
from src.user_sync.engine import SyncError, Syncer


@pytest.mark.parametrize("location", ["blocked", "history_blocked"])
@pytest.mark.parametrize("renderer", ["cli", "wizard"])
@pytest.mark.parametrize("ordinary_secret", [False, True], ids=["invalid-only", "mixed"])
def test_terminal_blocked_mcp_records_have_a_repair_remedy(location, renderer, ordinary_secret, capsys):
    invalid = {"path": "mcp/servers/example.json", "pattern": "invalid_mcp_record"}
    items = [invalid]
    if ordinary_secret:
        items.append({"path": "common/deploy.md", "pattern": "github_token"})
    preview = {"status": "confirmation_needed", "kind": "push", location: items}
    if renderer == "cli":
        _print(preview, "preview")
        text = capsys.readouterr().out
    else:
        text = "\n".join(wizard.describe_preview(preview))
    assert "fix or re-save" in text.lower()
    assert "invalid MCP records" in text
    assert "mcp/servers/example.json (invalid_mcp_record)" in text
    assert ("scope --allow-secret" in text) is ordinary_secret
    assert ("invalid saved versions stay local" in text) is (location == "history_blocked")
    if ordinary_secret:
        assert "common/deploy.md (github_token)" in text
        assert text.index("scope --allow-secret") < text.index("common/deploy.md") < text.index("invalid MCP records")


@pytest.mark.parametrize("relative", ["mcp/servers/example.json", "mcp/machines/" + "a" * 32 + ".json"])
@pytest.mark.parametrize("confirm", [None, "old-preview-hash"])
@pytest.mark.parametrize("previously_allowed", [False, True])
def test_invalid_mcp_records_cannot_be_allowed_even_with_confirmation(tmp_path, relative, confirm, previously_allowed):
    library = tmp_path / "library"
    target = library / relative
    target.parent.mkdir(parents=True)
    target.write_text('{"token":"unknown-credential-value"}')
    syncer = Syncer(library, tmp_path / "state")
    allowed = [scope.content_hash(target.read_bytes())] if previously_allowed else []
    if previously_allowed:
        scopes_path = library / scope.SCOPES_PATH
        scopes_path.parent.mkdir(parents=True)
        scopes_path.write_bytes(scope.Scopes(allow=frozenset(allowed)).dump())
    with pytest.raises(SyncError) as error:
        syncer.change_scopes(allow_paths=[relative], confirm=confirm)
    assert error.value.reason == "invalid_mcp_record"
    assert "fix or re-save" in error.value.message.lower()
    assert relative in error.value.message
    assert syncer.scopes()["allowed"] == allowed
    assert relative in scope.snapshot(library, syncer._working_scopes()).blocked


def test_ordinary_secret_allow_still_previews_and_saves_the_content_hash(tmp_path):
    library = tmp_path / "library"
    target = library / "common/deploy.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Deploy\n" + "ghp_" + "A" * 36 + "\n")
    syncer = Syncer(library, tmp_path / "state")
    asked = syncer.change_scopes(allow_paths=["common/deploy.md"])
    assert asked["status"] == "confirmation_needed" and asked["upload"] == ["common/deploy.md"]
    assert syncer.scopes()["allowed"] == []
    saved = syncer.change_scopes(allow_paths=["common/deploy.md"], confirm=asked["hash"])
    assert saved["status"] == "saved" and saved["allowed"] == [scope.content_hash(target.read_bytes())]
    assert "common/deploy.md" in scope.snapshot(library, syncer._working_scopes()).files
