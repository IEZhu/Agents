"""Registrations this installation wrote are kept in the form migrate writes now (#292)."""
import json
from pathlib import Path
import sys
import tomllib

import pytest

from src.daemon import clients, registrations
from src.daemon.state import atomic_private, write_json
from src.daemon.workspaces import WorkspaceRegistry

TOKEN = "private-token-for-tests-only" * 2
URL = "http://127.0.0.1:8765/mcp"


@pytest.fixture
def setup(tmp_path):
    state = tmp_path / "service"; state.mkdir(mode=0o700)
    home = tmp_path / "home"; home.mkdir()
    # Any existing absolute file stands in for Node: the bridge entry only names it.
    write_json(state / "service.json", {"installation": str(tmp_path / "install"), "port": 8765,
                                        "node": sys.executable})
    atomic_private(state / "token", TOKEN)
    return state, home


def project(tmp_path, state, name):
    root = tmp_path / name; root.mkdir()
    return root, WorkspaceRegistry(state).register(root)


def http_codex(identity=None, extra=""):
    headers = f'Authorization = "Bearer {TOKEN}"\nX-Agents-Client = "codex"\n'
    if identity:
        headers += f'X-Agents-Workspace = "{identity}"\n'
    return (f'model = "test"\n[mcp_servers."Agents-Core"]\n'
            f'"tools" = {{ "log_interaction" = {{ "approval_mode" = "approve" }} }}\n'
            f'url = "{URL}"\n[mcp_servers."Agents-Core".http_headers]\n{headers}{extra}'
            '[mcp_servers.other]\ncommand = "safe"\n')


def http_json(identity=None, **extra):
    headers = {"Authorization": "Bearer " + TOKEN}
    if identity:
        headers["X-Agents-Workspace"] = identity
    return {"url": URL, "headers": headers, **extra}


def entry(path, client="codex"):
    if client == "codex":
        return tomllib.loads(path.read_text())["mcp_servers"]["Agents-Core"]
    return json.loads(path.read_text())["mcpServers"]["Agents-Core"]


def backups(state):
    return sorted((state / "backups").glob("*")) if (state / "backups").exists() else []


def test_every_codex_layer_moves_to_the_bridge_together(setup, tmp_path):
    state, home = setup
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(http_codex())
    roots = []
    for name in ("one", "two"):
        root, identity = project(tmp_path, state, name)
        (root / ".codex").mkdir()
        (root / ".codex/config.toml").write_text(http_codex(identity))
        roots.append((root, identity))

    report = registrations.upgrade(state, home)

    assert report["codex"]["status"] == "upgraded" and len(report["codex"]["files"]) == 3
    user = entry(home / ".codex/config.toml")
    assert user["args"][0].endswith("bridge/stdio.mjs") and "url" not in user
    assert user["tools"] == {"log_interaction": {"approval_mode": "approve"}}  # kept
    assert tomllib.loads((home / ".codex/config.toml").read_text())["mcp_servers"]["other"] == {"command": "safe"}
    for root, identity in roots:
        pinned = entry(root / ".codex/config.toml")
        merged = {**user, **pinned}  # Codex merges a server's fields across its layers
        assert not {"url", "http_headers"} & set(merged)
        assert json.loads(Path(merged["args"][1]).read_text())["headers"]["X-Agents-Workspace"] == identity


def test_a_second_run_finds_everything_current(setup, tmp_path):
    state, home = setup
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(http_codex())
    registrations.upgrade(state, home)
    written = backups(state)
    before = (home / ".codex/config.toml").read_bytes()

    report = registrations.upgrade(state, home)

    assert report["codex"] == {"status": "current", "files": []}
    assert backups(state) == written and (home / ".codex/config.toml").read_bytes() == before


def test_a_hand_edited_layer_leaves_its_client_alone(setup, tmp_path):
    state, home = setup
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(http_codex())
    root, identity = project(tmp_path, state, "edited")
    (root / ".codex").mkdir()
    (root / ".codex/config.toml").write_text(http_codex(identity, extra='X-Team = "mine"\n'))
    write_json(home / ".claude.json", {"mcpServers": {"Agents-Core": {"type": "http", **http_json()}}})
    codex_before = [(home / ".codex/config.toml").read_bytes(), (root / ".codex/config.toml").read_bytes()]

    report = registrations.upgrade(state, home)

    assert report["codex"]["status"] == "skipped"
    assert str(root / ".codex/config.toml") in report["codex"]["reason"]
    assert [(home / ".codex/config.toml").read_bytes(), (root / ".codex/config.toml").read_bytes()] == codex_before
    assert report["claude"]["status"] == "upgraded"  # another client is not held back


def test_a_project_without_an_entry_gets_none(setup, tmp_path):
    state, home = setup
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(http_codex())
    root, _ = project(tmp_path, state, "plain")
    (root / ".cursor").mkdir()
    (root / ".cursor/mcp.json").write_text('{"mcpServers": {"other": {"command": "safe"}}}')

    registrations.upgrade(state, home)

    assert not (root / ".codex").exists()
    assert json.loads((root / ".cursor/mcp.json").read_text()) == {"mcpServers": {"other": {"command": "safe"}}}


def test_the_claude_user_entry_becomes_the_session_bridge_and_pins_stay(setup, tmp_path):
    """Claude Code takes an entry whole from one scope, so its HTTP pins can stay as they are."""
    state, home = setup
    root, identity = project(tmp_path, state, "pinned")
    pin = {"type": "http", **http_json(identity)}
    write_json(home / ".claude.json", {
        "mcpServers": {"Agents-Core": {"type": "http", **http_json(), "command": "stale", "args": ["x"]}},
        "projects": {str(root): {"mcpServers": {"Agents-Core": pin}, "otherPolicy": True}},
        "numStartups": 7})

    report = registrations.upgrade(state, home)

    assert report["claude"]["status"] == "upgraded"
    document = json.loads((home / ".claude.json").read_text())
    user = document["mcpServers"]["Agents-Core"]
    assert set(user) == {"command", "args"} and Path(user["args"][1]).name == "claude-code-auto.json"
    assert document["projects"][str(root)] == {"mcpServers": {"Agents-Core": pin}, "otherPolicy": True}
    assert document["numStartups"] == 7


def test_cursor_layers_move_together(setup, tmp_path):
    state, home = setup
    (home / ".cursor").mkdir()
    write_json(home / ".cursor/mcp.json", {"mcpServers": {"Agents-Core": http_json()}})
    root, identity = project(tmp_path, state, "cursor-project")
    (root / ".cursor").mkdir()
    write_json(root / ".cursor/mcp.json", {"mcpServers": {"Agents-Core": http_json(identity)}})

    report = registrations.upgrade(state, home)

    assert report["cursor"]["status"] == "upgraded"
    assert Path(entry(home / ".cursor/mcp.json", "cursor")["args"][1]).name == "cursor-auto.json"
    pinned = entry(root / ".cursor/mcp.json", "cursor")
    assert json.loads(Path(pinned["args"][1]).read_text())["headers"]["X-Agents-Workspace"] == identity


def test_without_node_nothing_changes(setup, tmp_path):
    state, home = setup
    config = json.loads((state / "service.json").read_text())
    write_json(state / "service.json", {**config, "node": str(tmp_path / "missing-node")})
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(http_codex())
    before = (home / ".codex/config.toml").read_bytes()

    report = registrations.upgrade(state, home)

    assert report["codex"]["status"] == "current"
    assert (home / ".codex/config.toml").read_bytes() == before and backups(state) == []


def test_a_concurrent_edit_is_retried(setup, tmp_path, monkeypatch):
    state, home = setup
    write_json(home / ".claude.json", {"mcpServers": {"Agents-Core": {"type": "http", **http_json()}}})
    original_apply = clients.ClientMigration.apply
    calls = []

    def apply(self, changes, **options):
        calls.append(1)
        if len(calls) == 1:  # Claude Code writes its file between our read and our write
            document = json.loads((home / ".claude.json").read_text())
            write_json(home / ".claude.json", {**document, "numStartups": 8})
        return original_apply(self, changes, **options)
    monkeypatch.setattr(clients.ClientMigration, "apply", apply)

    report = registrations.upgrade(state, home)

    assert report["claude"]["status"] == "upgraded" and len(calls) == 2
    document = json.loads((home / ".claude.json").read_text())
    assert document["numStartups"] == 8 and "command" in document["mcpServers"]["Agents-Core"]


def test_an_unreadable_layer_fails_its_client(setup, tmp_path):
    state, home = setup
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(http_codex())
    root, _ = project(tmp_path, state, "broken")
    (root / ".codex").mkdir()
    (root / ".codex/config.toml").write_text('[mcp_servers."Agents-Core"\nurl = ')
    before = (home / ".codex/config.toml").read_bytes()

    report = registrations.upgrade(state, home)

    assert report["codex"]["status"] == "failed" and str(root / ".codex/config.toml") in report["codex"]["reason"]
    assert (home / ".codex/config.toml").read_bytes() == before


def test_a_recorded_profile_is_upgraded_too(setup, tmp_path):
    """A Claude profile migrated with CLAUDE_CONFIG_DIR is in client-configs.json."""
    state, home = setup
    profile = tmp_path / "claude-profile" / ".claude.json"; profile.parent.mkdir()
    write_json(profile, {"mcpServers": {"Agents-Core": {"type": "http", **http_json()}}})
    write_json(state / "client-configs.json",
               {"version": 1, "configs": [{"client": "claude", "path": str(profile), "workspace": None}]})

    report = registrations.upgrade(state, home)

    assert str(profile) in report["claude"]["files"]
    assert "command" in entry(profile, "claude")


# --- at every service start, with a switch ---------------------------------------------------

def test_run_saves_its_report_and_the_switch_turns_it_off(setup):
    state, home = setup
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(http_codex())
    before = (home / ".codex/config.toml").read_bytes()

    assert registrations.set_auto(state, False)["auto"] is False
    assert registrations.run(state, home) == {"auto": False}
    assert (home / ".codex/config.toml").read_bytes() == before

    registrations.set_auto(state, True)
    result = registrations.run(state, home)
    assert result["auto"] is True and result["updated_at"]
    assert result["clients"]["codex"]["status"] == "upgraded"
    assert registrations.state(state) == result


def test_run_is_forced_while_turned_off(setup):
    state, home = setup
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(http_codex())
    registrations.set_auto(state, False)

    result = registrations.run(state, home, force=True)

    assert result["auto"] is False and result["clients"]["codex"]["status"] == "upgraded"


def test_keep_current_waits_while_the_controller_holds_its_lock(setup, monkeypatch):
    """An update's probation holds control.lock while the new service starts."""
    from contextlib import ExitStack
    from src.file_lock import file_lock
    state, home = setup
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(http_codex())
    held = ExitStack()
    held.enter_context(file_lock(state / "control.lock"))
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        held.close()  # the controller finishes its probation

    result = registrations.keep_current(state, sleep=sleep)

    assert waits == [registrations.LOCK_RETRY_SECONDS]
    assert result["clients"]["codex"]["status"] == "upgraded"


def test_keep_current_never_raises(setup, monkeypatch):
    state, _ = setup
    monkeypatch.setattr(registrations, "upgrade", lambda *a, **k: 1 / 0)
    assert registrations.keep_current(state) is None


def test_the_registrations_command(setup, monkeypatch, capsys):
    from src.daemon.control import main
    state, home = setup
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(http_codex())

    main(["--state", str(state), "registrations", "disable"])
    assert json.loads(capsys.readouterr().out) == {"auto": False}
    main(["--state", str(state), "registrations", "run"])
    assert json.loads(capsys.readouterr().out)["clients"]["codex"]["status"] == "upgraded"
    main(["--state", str(state), "registrations", "status"])
    status = json.loads(capsys.readouterr().out)
    assert status["auto"] is False and status["clients"]["codex"]["status"] == "upgraded"


def test_the_service_start_runs_it_in_the_background(setup, monkeypatch):
    """`serve` must not wait for it: during an update's probation it waits for control.lock."""
    import threading
    from src.daemon import bootstrap
    state, _ = setup
    called = threading.Event()
    monkeypatch.setattr(registrations, "keep_current", lambda directory: called.set())

    bootstrap.keep_registrations_current(state)

    assert called.wait(5)
    started = [thread for thread in threading.enumerate() if thread.name == "registrations"]
    assert all(thread.daemon for thread in started)
