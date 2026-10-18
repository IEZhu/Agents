"""User-visible CLI failures must retain safe diagnostics without tracebacks."""
import json
import sys
from urllib.error import URLError

import pytest

from src.daemon import control


@pytest.mark.parametrize("code", ["stale_revision", "not_enabled", "no_secret_store", "credentials_missing"])
def test_service_error_code_reaches_cli(monkeypatch, capsys, code):
    class Controller:
        config = {"port": 8765}

        def __init__(self, state):
            pass

        def request(self, *args, **kwargs):
            return {"error": code}
    monkeypatch.setattr(control, "Controller", Controller)
    assert control.main(["mcp", "connect", "example"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": code}


@pytest.mark.parametrize("records,code", [([], "not_found"), ([{"alias": "example", "status": "invalid"}], "invalid_definition")])
def test_disable_unknown_or_invalid_server(monkeypatch, capsys, records, code):
    class Controller:
        config = {"port": 8765}

        def __init__(self, state):
            pass

        def request(self, *args, **kwargs):
            return {"servers": records}
    monkeypatch.setattr(control, "Controller", Controller)
    assert control.main(["mcp", "disable", "example"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error": code}
    assert "Traceback" not in captured.err


def test_connection_failure_stays_generic(monkeypatch, capsys):
    class Controller:
        config = {"port": 8765}

        def __init__(self, state):
            pass

        def request(self, *args, **kwargs):
            raise URLError("private-transport-detail")
    monkeypatch.setattr(control, "Controller", Controller)
    assert control.main(["mcp", "connect", "example"]) == 1
    captured = capsys.readouterr()
    assert "private-transport-detail" not in captured.out
    assert "check the running service" in json.loads(captured.out)["error"]


@pytest.mark.parametrize("code", ["draining", "starting", "storage"])
def test_disable_propagates_listing_failure_without_traceback(monkeypatch, capsys, code):
    class Controller:
        config = {"port": 8765}

        def __init__(self, state):
            pass

        def request(self, *args, **kwargs):
            return {"error": code}
    monkeypatch.setattr(control, "Controller", Controller)
    assert control.main(["mcp", "disable", "example"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error": code}
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("retained_token", [False, True])
@pytest.mark.parametrize("command", [
    ["list"],
    ["connect", "example"],
    ["disable", "example"],
    ["add", "example", "--url", "http://127.0.0.1:47831/mcp", "--apps", "codex"],
    ["add", "example", "--command", sys.executable, "--apps", "codex"],
])
def test_mcp_without_installed_service_returns_json(tmp_path, capsys, retained_token, command):
    if retained_token:
        token = tmp_path / "token"
        token.write_text("retained-test-token")
        token.chmod(0o600)
    assert control.main(["--state", str(tmp_path), "mcp", *command]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error": "service_not_installed"}
    assert "Traceback" not in captured.err
    assert "retained-test-token" not in captured.out + captured.err
