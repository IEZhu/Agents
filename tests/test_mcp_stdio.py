"""Real child processes prove shared ownership, isolation and cleanup."""
import asyncio
import json
import sys

import pytest

from src.daemon import control
from src.daemon.mcp_proxy.upstreams import Supervisor, UpstreamError
from src.mcp_registry import Registry, RegistryError, validate_definition


SERVER = '''import json, os, sys
from pathlib import Path
events = Path(os.environ["PROCESS_EVENTS"])
def record(kind):
    with events.open("a") as stream:
        stream.write(kind + " " + str(os.getpid()) + "\\n")
record("start")
try:
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        if method == "initialize":
            result = {"protocolVersion":"2025-06-18", "capabilities":{"tools":{}}, "serverInfo":{"name":"process-test","version":"1"}}
        elif method == "tools/list":
            result = {"tools":[{"name":"identity", "inputSchema":{"type":"object"}}]}
        elif method == "tools/call":
            result = {"content":[{"type":"text", "text":json.dumps({"pid":os.getpid(), "inherited_secret":"UNRELATED_SECRET" in os.environ, "setting":os.environ.get("READ_ONLY")})}]}
        else:
            result = {}
        print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "result":result}), flush=True)
finally:
    record("stop")
'''


def draft(command=sys.executable):
    return {"alias": "shared", "transport": {"type": "stdio", "command": command}}


def test_stdio_definition_roundtrip_and_exact_local_consent(tmp_path):
    registry = Registry(tmp_path / "library", tmp_path / "state")
    data = registry.save(draft())
    assert data["transport"] == {"type": "stdio", "command": sys.executable, "args": [], "env": {}}
    assert registry.resolved("codex") == []
    registry.enable("shared", ["codex", "claude-code"], data["revision"])
    assert registry.resolved("codex")[0] == data
    assert registry.machines()[0]["servers"]["shared"]["apps"] == ["claude-code", "codex"]


@pytest.mark.parametrize("transport", [
    {"command": "python"}, {"command": "/bin/sh\n"}, {"args": "--help"},
    {"args": ["--token=private"]}, {"args": ["--password", "private"]},
    {"env": {"ACCESS_TOKEN": "private"}}, {"env": {"BAD=NAME": "value"}},
    {"env": {"SETTING": "bad\x00value"}}, {"cwd": "/tmp"}, {"url": "http://localhost"},
])
def test_unsafe_stdio_launch_metadata_is_rejected(transport):
    data = draft()
    data["transport"].update(transport)
    with pytest.raises(RegistryError):
        validate_definition(data)


def test_stdio_header_auth_and_secret_env_are_not_silently_ignored():
    for fields in ({"auth": {"type": "bearer"}}, {"secrets": [{"name": "API_TOKEN", "target": "env"}]}):
        with pytest.raises(RegistryError):
            validate_definition({**draft(), **fields})


class NoSecrets:
    def load(self, alias):
        raise AssertionError("Stdio credentials must not read HTTP secret headers")


def events(path):
    return [line.split() for line in path.read_text().splitlines()] if path.exists() else []


async def wait_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_two_apps_concurrent_calls_share_one_child_then_drain_cleans_up(tmp_path, monkeypatch):
    script = tmp_path / "upstream.py"
    script.write_text(SERVER)
    log = tmp_path / "events"
    registry = Registry(tmp_path / "library", tmp_path / "state")
    data = draft()
    data["transport"].update(args=[str(script)], env={"PROCESS_EVENTS": str(log), "READ_ONLY": "true"})
    saved = registry.save(data)
    registry.enable("shared", ["codex", "claude-code"], saved["revision"])
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-reach-upstream")
    supervisor = Supervisor(registry, NoSecrets(), tmp_path / "state")
    try:
        assert supervisor.cached("codex") == [] and not log.exists()
        await asyncio.gather(supervisor.connect("shared", "codex"), supervisor.connect("shared", "claude-code"))
        replies = await asyncio.gather(*(supervisor.call("shared", "identity", {}, app)
                                        for app in ["codex", "claude-code"] * 5))
        values = [json.loads(reply.content[0].text) for reply in replies]
        assert len({value["pid"] for value in values}) == 1
        assert all(value["inherited_secret"] is False and value["setting"] == "true" for value in values)
        assert [kind for kind, _ in events(log)] == ["start"]
        with pytest.raises(UpstreamError, match="not_enabled"):
            await supervisor.call("shared", "identity", {}, "cursor")
    finally:
        await supervisor.close()
    assert events(log) == [["start", str(values[0]["pid"])], ["stop", str(values[0]["pid"])]]


@pytest.mark.asyncio
async def test_idle_child_exits_before_lazy_replacement(tmp_path):
    script = tmp_path / "upstream.py"
    script.write_text(SERVER)
    log = tmp_path / "events"
    saved = validate_definition(draft())
    saved["transport"].update(args=[str(script)], env={"PROCESS_EVENTS": str(log)})
    saved["limits"]["idle_seconds"] = .03

    class FakeRegistry:
        def get(self, alias): return saved
        def list(self): return [{**saved, "enabled": True, "confirmed": True, "apps": ["codex"]}]
        def resolved(self, app): return [saved]

    supervisor = Supervisor(FakeRegistry(), NoSecrets(), tmp_path / "state")
    try:
        await supervisor.connect("shared", "codex")
        await wait_until(lambda: len(events(log)) == 2)
        assert [kind for kind, _ in events(log)] == ["start", "stop"]
        result = await supervisor.call("shared", "identity", {}, "codex")
        assert json.loads(result.content[0].text)["pid"] != int(events(log)[0][1])
    finally:
        await supervisor.close()
    assert [kind for kind, _ in events(log)] == ["start", "stop", "start", "stop"]


def test_cli_stdio_options_preserve_top_level_dispatch(monkeypatch, capsys):
    bodies = []
    class FakeController:
        def __init__(self, state): pass
        def request(self, path, **kwargs):
            body = kwargs["body"]
            bodies.append(body)
            return {"result": {"revision": "test-revision"}}
    monkeypatch.setattr(control, "Controller", FakeController)
    assert control.main(["mcp", "add", "shared", "--command", sys.executable,
                         "--arg=--help", "--env=READ_ONLY=true", "--apps", "codex"]) == 0
    assert bodies[0]["definition"]["transport"] == {
        "type": "stdio", "command": sys.executable, "args": ["--help"], "env": {"READ_ONLY": "true"}}
    assert [body["action"] for body in bodies] == ["save", "enable", "connect"]
