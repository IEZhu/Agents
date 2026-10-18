"""Owner opt-in, cadence and fail-closed Cursor stop hooks without any native UI."""
import json
import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

from src.context_compaction import config, runtime
from src.context_compaction.__main__ import main as settings_cli
from scripts import install_context_compaction as installer


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTS_CONTEXT_COMPACTION_CONFIG", raising=False)
    path = tmp_path / "settings.json"
    document = {**config.DEFAULTS, "enabled": True, "min_turns": 2, "cooldown_seconds": 60,
                "cursor_bindings": [{"conversation_id": "chat", "window_title": "project", "chat_title": "task"}]}
    config.write(path, document)
    return path, document


def event(generation="1", **extra):
    return {"hook_event_name": "stop", "status": "completed", "conversation_id": "chat",
            "generation_id": generation, **extra}


def test_missing_disabled_and_invalid_configuration_never_touch_runtime(tmp_path):
    path = tmp_path / "settings.json"
    def forbidden(*args, **kwargs):
        pytest.fail("disabled configuration reached native adapter")
    for raw in (None, "{}", '{"enabled":false}', '{"enabled":"true"}', '{broken', '[]',
                '{"enabled":true,"unknown":1}', 'x' * 65537, '[' * 10000 + ']' * 10000):
        if raw is not None:
            path.write_text(raw)
        result = runtime.handle(event(), path=path, adapter=forbidden)
        assert result["status"] == "skipped"
        assert not (tmp_path / "context-compaction-state").exists()


@pytest.mark.parametrize("key,value", [("schema_version", True), ("schema_version", 2),
    ("enabled", 1), ("min_context_tokens", 9999), ("min_turns", True), ("min_turns", 1001),
    ("cooldown_seconds", -1), ("cooldown_seconds", 86401), ("cursor_bindings", {}),
    ("cursor_bindings", [{"conversation_id": "x"}])])
def test_config_validation(key, value):
    with pytest.raises(ValueError):
        config.validate({key: value})


def test_config_rejects_ambiguous_bindings(settings):
    _, document = settings
    duplicate = {**document["cursor_bindings"][0], "conversation_id": "other"}
    with pytest.raises(ValueError):
        config.validate({**document, "cursor_bindings": document["cursor_bindings"] + [duplicate]})


def test_environment_path_precedence_and_invalid_override_disable(settings, monkeypatch):
    path, _ = settings
    other = path.with_name("other.json")
    config.write(other, {})
    monkeypatch.setenv("AGENTS_CONTEXT_COMPACTION_CONFIG", str(other))
    assert config.config_path(path) == other and config.load(path)[0]["enabled"] is False
    for raw in ("", "relative.json", "//network/file.json"):
        monkeypatch.setenv("AGENTS_CONTEXT_COMPACTION_CONFIG", raw)
        assert config.load(path)[1] == "invalid_config"


def test_explicit_disable_applies_even_if_native_installer_fails(settings):
    path, _ = settings
    def runner(command, **kwargs):
        assert config.load(path)[0]["enabled"] is False
        return subprocess.CompletedProcess(command, 1, "", "native failure")
    with pytest.raises(ValueError):
        installer.install(clients=("claude",), path=path, enabled=False, runner=runner)
    assert config.load(path)[0]["enabled"] is False


def test_config_cli_only_explicit_enable_turns_on_switch(tmp_path, capsys):
    path = tmp_path / "settings.json"
    assert settings_cli(["--config", str(path), "status"]) == 0
    assert not path.exists()
    assert json.loads(capsys.readouterr().out)["enabled"] is False
    settings_cli(["--config", str(path), "bind-cursor", "--conversation-id", "chat",
                  "--window-title", "project", "--chat-title", "task"])
    assert config.load(path)[0]["enabled"] is False
    settings_cli(["--config", str(path), "enable"])
    assert config.load(path)[0]["enabled"] is True
    settings_cli(["--config", str(path), "disable"])
    assert config.load(path)[0]["enabled"] is False


@pytest.mark.parametrize("extra", [{"status": "aborted"}, {"status": "error"},
    {"hook_event_name": "subagentStop"}, {"is_background_agent": True}, {"agent_id": "sub"},
    {"conversation_id": "unbound"}, {"generation_id": ""}, {"loop_count": 1}])
def test_only_bound_completed_main_turns_count(settings, extra):
    path, _ = settings
    assert runtime.handle(event(**extra), path=path)["status"] == "skipped"
    assert not (path.parent / "context-compaction-state").exists()


def test_cadence_duplicate_replay_cooldown_and_no_success_invention(settings):
    path, _ = settings
    calls = []
    def adapter(payload, settings):
        calls.append(payload["generation_id"])
        return {"status": "submitted", "reason": "native_command"}
    def run(generation, now):
        return runtime.handle(event(generation), path=path, adapter=adapter, now=now)
    assert run("1", 100)["reason"] == "turn_threshold"
    assert run("2", 100)["status"] == "submitted"
    assert run("1", 101)["reason"] == "duplicate_turn"
    assert run("3", 101)["reason"] == "turn_threshold"
    assert run("4", 102)["reason"] == "cooldown"
    assert run("5", 160)["status"] == "submitted"
    assert calls == ["2", "5"]


def test_concurrent_hook_cannot_dispatch(settings):
    path, document = settings
    document["min_turns"] = 1
    config.write(path, document)
    def adapter(payload, settings):
        assert runtime.handle(event("2"), path=path, adapter=lambda *a: pytest.fail("concurrent dispatch"))["reason"] == "busy"
        return {"status": "completed", "reason": "observed"}
    assert runtime.handle(event(), path=path, adapter=adapter)["status"] == "completed"


@pytest.mark.parametrize("raw", ['{', '[]', 'x' * 8193, '{"attempt_at":NaN}',
    '{"turns":true}', '{"recent":["untrusted"]}'])
def test_invalid_cadence_state_never_bypasses_cooldown(settings, raw):
    path, document = settings
    config.write(path, {**document, "min_turns": 1})
    state_dir = path.parent / "context-compaction-state"
    state_dir.mkdir()
    state_path = state_dir / (hashlib.sha256(b"chat").hexdigest() + ".json")
    state_path.write_text(raw)
    result = runtime.handle(event(), path=path, adapter=lambda *a: pytest.fail("corrupt state dispatched"))
    assert result["reason"] == "invalid_state"


def test_changed_configuration_is_rechecked_before_adapter(settings, monkeypatch):
    path, document = settings
    document["min_turns"] = 1
    config.write(path, document)
    calls = 0
    real_load = runtime.load
    def load_changed(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            config.write(path, {**document, "enabled": False})
        return real_load(path)
    monkeypatch.setattr(runtime, "load", load_changed)
    result = runtime.handle(event(), path=path, adapter=lambda *a: pytest.fail("disabled before dispatch"))
    assert result["reason"] == "config_changed"


@pytest.mark.parametrize("result", [None, {"status": "made_up"}, RuntimeError("native failure")])
def test_failures_are_bounded_and_not_retried_on_duplicate(settings, result):
    path, document = settings
    config.write(path, {**document, "min_turns": 1})
    def adapter(*args):
        if isinstance(result, Exception):
            raise result
        return result
    assert runtime.handle(event(), path=path, adapter=adapter)["status"] == "error"
    assert runtime.handle(event(), path=path, adapter=adapter)["reason"] == "duplicate_turn"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks require privileges on Windows")
def test_symlink_configuration_is_disabled_and_not_overwritten(tmp_path):
    source = tmp_path / "source.json"
    config.write(source, {"enabled": True})
    link = tmp_path / "link.json"
    link.symlink_to(source)
    assert config.load(link)[1] == "invalid_config"
    with pytest.raises(ValueError):
        config.write(link, {})
    assert config.load(source)[0]["enabled"] is True


def test_installer_dry_run_creates_nothing_and_never_runs_claude(tmp_path):
    result = installer.install(path=tmp_path / "settings.json", cursor_home=tmp_path / "cursor",
                               dry_run=True, runner=lambda *a, **k: pytest.fail("dry run invoked CLI"))
    assert result["enabled"] is False
    assert not list(tmp_path.iterdir())
    assert result["claude_command"][1:4] == ["plugin", "install", "agents-core-context-compaction"]


def test_installer_preserves_other_hooks_is_idempotent_and_installed_runtime_is_off(tmp_path):
    home = tmp_path / "cursor"
    home.mkdir()
    target = home / "hooks.json"
    original = {"version": 1, "hooks": {"stop": [{"command": "other-hook", "timeout": 5}],
                                       "sessionStart": [{"command": "readiness"}]}, "other": True}
    target.write_text(json.dumps(original))
    path = tmp_path / "settings.json"
    result = installer.install(clients=("cursor",), path=path, cursor_home=home)
    assert len(result["backups"]) == 1
    assert json.loads(Path(result["backups"][0]).read_text()) == original
    first = target.read_bytes()
    again = installer.install(clients=("cursor",), path=path, cursor_home=home)
    assert again["backups"] == [] and target.read_bytes() == first
    document = json.loads(first)
    assert document["hooks"]["stop"][0] == original["hooks"]["stop"][0]
    assert document["hooks"]["sessionStart"] == original["hooks"]["sessionStart"]
    assert document["other"] is True
    script = home / "hooks" / installer.SCRIPT_NAME
    run = subprocess.run([sys.executable, "-B", str(script), "--config", str(path)], input=json.dumps(event()),
                         capture_output=True, text=True, timeout=10)
    assert run.returncode == 0 and json.loads(run.stdout) == {} and run.stderr == ""
    assert not (tmp_path / "context-compaction-state").exists()
    assert config.load(path)[0]["enabled"] is False


def test_failed_claude_install_never_enables_switch(tmp_path):
    path = tmp_path / "settings.json"
    def runner(command, **kwargs):
        assert command[0] == "claude" and "--marketplace" in command
        assert config.load(path)[0]["enabled"] is False
        return subprocess.CompletedProcess(command, 1, "", "unsupported mod version")
    with pytest.raises(ValueError, match="Native Claude"):
        installer.install(clients=("claude",), path=path, enabled=True, runner=runner)
    assert config.load(path)[0]["enabled"] is False


def test_hook_stdout_is_empty_object_for_malformed_oversized_and_failed_input(tmp_path):
    for payload in ("{", "[]", "x" * 65537, '[' * 10000 + ']' * 10000):
        run = subprocess.run([sys.executable, "-B", "-m", "src.context_compaction.runtime", "--config",
                              str(tmp_path / "absent.json")], input=payload, capture_output=True, text=True, timeout=10)
        assert run.returncode == 0 and run.stdout == "{}\n" and run.stderr == ""


def test_explicit_identity_capture_records_only_id_without_compacting(tmp_path):
    target = tmp_path / "identity.json"
    payload = event(prompt="private prompt", user_email="private@example.com", transcript_path="private")
    run = subprocess.run([sys.executable, "-B", "-m", "src.context_compaction.runtime", "--capture-identity",
                          str(target)], input=json.dumps(payload), capture_output=True, text=True, timeout=10)
    assert run.returncode == 0 and run.stdout == "{}\n" and run.stderr == ""
    assert json.loads(target.read_text()) == {"conversation_id": "chat"}
    assert not (tmp_path / "context-compaction-state").exists()


def test_identity_capture_cannot_overwrite_settings_or_unrelated_file(settings, monkeypatch):
    path, document = settings
    monkeypatch.setenv("AGENTS_CONTEXT_COMPACTION_CONFIG", str(path))
    other = path.with_name("unrelated.json")
    other.write_text('{"important":"keep"}')
    for target in (path, other):
        before = target.read_bytes()
        run = subprocess.run([sys.executable, "-B", "-m", "src.context_compaction.runtime", "--capture-identity",
                              str(target)], input=json.dumps(event()), capture_output=True, text=True, timeout=10)
        assert run.returncode == 0 and run.stdout == "{}\n"
        assert target.read_bytes() == before
    assert config.load(path) == (document, None)
