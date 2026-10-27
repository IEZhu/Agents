"""Thread-status uninstall safety, using fake installations and isolated client profiles."""
import json
from pathlib import Path
import subprocess

import pytest

from scripts import install_thread_status as installer
from src import uninstall


ROOT = Path(__file__).resolve().parents[1]


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


def fake_runner(argv, *, input=None, env=None):
    output = b"0\n" if len(argv) > 3 and argv[0] == "git" and argv[3] == "rev-list" else b""
    return subprocess.CompletedProcess(list(argv), 0, output, b"")


def profiles(tmp_path, monkeypatch, platform="linux"):
    home = tmp_path / "O'Neil home with spaces"
    home.mkdir()
    installation = home / ".agents-core"
    write(installation / "scripts/init_repo.sh", "#!/bin/sh\n")
    write(installation / "src/server.py", "")
    (installation / ".git/objects").mkdir(parents=True)
    for _, template in uninstall.THREAD_STATUS_RUNTIMES:
        write(installation / template, (ROOT / template).read_bytes())
    python = write(installation / (".venv/Scripts/python.exe" if platform == "win32" else ".venv/bin/python"),
                   "fake interpreter; never executed")
    claude, codex = home / "profiles/Claude Code", home / "profiles/Codex"
    paths = {
        "claude": (claude / "settings.json", claude / "hooks" / installer.SCRIPT_NAME),
        "codex": (codex / "hooks.json", codex / "hooks" / installer.SCRIPT_NAME),
        "cursor": (home / ".cursor/hooks.json", home / ".cursor/hooks" / installer.SCRIPT_NAME),
    }
    originals = {
        "claude": {"theme": "keep", "hooks": {"PreToolUse": [{"hooks": [{"command": "other"}]}]}},
        "codex": {"theme": "keep", "hooks": {"PreToolUse": [{"hooks": [{"command": "other"}]}]}},
        "cursor": {"version": 1, "hooks": {"preToolUse": [{"command": "other"}]}},
    }
    monkeypatch.setattr(installer, "ROOT", installation)
    monkeypatch.setattr(installer.sys, "executable", str(python))
    monkeypatch.setattr(installer.helpers, "WINDOWS_HOOKS", platform == "win32")
    monkeypatch.setattr(installer, "client_paths", lambda client: paths[client])
    runtimes = {}
    for client, (target, _) in paths.items():
        write(target, json.dumps(originals[client], indent=2) + "\n")
        runtimes[client] = [Path(path) for path in installer.install(client)["scripts"]]
    # TOML is checked as TOML, rather than mistaken for invalid JSON during reference checks.
    write(codex / "config.toml", 'model = "keep"\n')
    ctx = uninstall.Context(home=home, platform=platform,
                            environ={"CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex)},
                            runner=fake_runner, source_root=installation, stamp="T")
    return ctx, paths, runtimes, originals


def handlers(document, client):
    events = ("sessionStart",) if client == "cursor" else (
        ("SessionStart", "UserPromptSubmit", "Interrupt") if client == "codex" else
        ("SessionStart", "UserPromptSubmit"))
    for event in events:
        entry = document["hooks"][event][0]
        yield entry if client == "cursor" else entry["hooks"][0]
    if client == "claude":
        # The interrupt watcher and its Stop marker, in their own generated groups.
        yield document["hooks"]["UserPromptSubmit"][-1]["hooks"][0]
        yield document["hooks"]["Stop"][0]["hooks"][0]


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_default_installed_thread_status_is_removed_with_backups(tmp_path, monkeypatch, platform):
    ctx, paths, runtimes, originals = profiles(tmp_path, monkeypatch, platform)
    registered = {target: target.read_bytes() for target, _ in paths.values()}
    assert uninstall.main(["--dry-run"], ctx=ctx, interactive=False) == 0
    assert all(target.read_bytes() == before for target, before in registered.items())
    assert all(path.exists() for copies in runtimes.values() for path in copies)
    assert uninstall.main(["--yes"], ctx=ctx, interactive=False) == 0
    for client, (target, _) in paths.items():
        assert json.loads(target.read_bytes()) == originals[client]
        assert all(not path.exists() for path in runtimes[client])
    manifest = json.loads((ctx.backup_root / "manifest.json").read_bytes())
    backed_up = {entry["original"]: Path(entry["backup"]).read_bytes() for entry in manifest}
    assert all(backed_up[str(target)] == before for target, before in registered.items())
    assert not (ctx.home / ".agents-core").exists()
    assert uninstall.main(["--yes"], ctx=ctx, interactive=False) == 0


@pytest.mark.parametrize("client", installer.CLIENTS)
def test_native_windows_commands_with_quoted_profile_paths_are_removed(tmp_path, monkeypatch, client):
    ctx, paths, runtimes, originals = profiles(tmp_path, monkeypatch, "win32")
    target, script = paths[client]
    document = json.loads(target.read_bytes())
    python = str(ctx.source_root / ".venv/Scripts/python.exe")
    modes = {}
    if client == "claude":
        modes = {id(document["hooks"]["UserPromptSubmit"][-1]["hooks"][0]): "--watch",
                 id(document["hooks"]["Stop"][0]["hooks"][0]): "--done"}
    for handler in handlers(document, client):
        mode = modes.get(id(handler))
        handler["command"] = subprocess.list2cmdline(
            [python, str(script.with_name(installer.INTERRUPT_NAME)), mode] if mode
            else [python, str(script), "--client", client])
    target.write_text(json.dumps(document), encoding="utf-8")
    report = uninstall.execute(uninstall.thread_status_items(ctx, uninstall.Backups(ctx), ctx.source_root),
                               credentials=False)
    assert not report["failed"] and not report["refused"]
    assert json.loads(target.read_bytes()) == originals[client]
    assert all(not path.exists() for path in runtimes[client])


@pytest.mark.parametrize("platform", ["linux", "win32"])
@pytest.mark.parametrize("client", installer.CLIENTS)
@pytest.mark.parametrize("change", ["timeout", "command", "foreign", "extra-option", "wrong-client"])
def test_modified_and_foreign_thread_status_handlers_are_preserved(tmp_path, monkeypatch, platform, client, change):
    ctx, paths, runtimes, _ = profiles(tmp_path, monkeypatch, platform)
    target, script = paths[client]
    document = json.loads(target.read_bytes())
    for handler in handlers(document, client):
        if change == "timeout":
            handler["timeout"] = 9
        elif change == "command":
            handler["command"] = installer.command_for(script, "--client", client, "--custom")
        elif change == "extra-option":
            handler["async"] = True
        elif change == "wrong-client":
            other = "cursor" if client != "cursor" else "claude"
            handler["command"] = installer.command_for(script, "--client", other)
        else:
            with monkeypatch.context() as scoped:
                scoped.setattr(installer.sys, "executable", str(ctx.home / "foreign/.venv/bin/python"))
                handler["command"] = installer.command_for(script, "--client", client)
    target.write_text(json.dumps(document), encoding="utf-8")
    before = target.read_bytes()
    report = uninstall.execute(uninstall.thread_status_items(ctx, uninstall.Backups(ctx), ctx.source_root),
                               credentials=False)
    assert not report["failed"] and not report["refused"]
    assert target.read_bytes() == before
    assert all(path.exists() for path in runtimes[client])


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_mixed_groups_keep_modified_handlers_and_their_runtime_bundle(tmp_path, monkeypatch, platform):
    ctx, paths, runtimes, _ = profiles(tmp_path, monkeypatch, platform)
    target, script = paths["codex"]
    document = json.loads(target.read_bytes())
    custom = {"command": installer.command_for(script, "--client", "codex", "--custom"),
              "type": "command", "timeout": 12}
    document["hooks"]["Interrupt"][0]["hooks"].append(custom)
    retained = {"matcher": "user-specified", "hooks": [{"command": "other", "type": "command"}]}
    document["hooks"]["Interrupt"].append(retained)
    target.write_text(json.dumps(document), encoding="utf-8")
    report = uninstall.execute(uninstall.thread_status_items(ctx, uninstall.Backups(ctx), ctx.source_root),
                               credentials=False)
    assert not report["failed"] and not report["refused"]
    updated = json.loads(target.read_bytes())
    assert "SessionStart" not in updated["hooks"] and "UserPromptSubmit" not in updated["hooks"]
    assert updated["hooks"]["Interrupt"] == [{"hooks": [custom]}, retained]
    assert all(path.exists() for path in runtimes["codex"])
    assert json.dumps(custom).encode() in target.read_bytes()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_customized_group_metadata_is_preserved(tmp_path, monkeypatch, platform):
    ctx, paths, runtimes, _ = profiles(tmp_path, monkeypatch, platform)
    target, _ = paths["claude"]
    document = json.loads(target.read_bytes())
    document["hooks"]["SessionStart"][0]["matcher"] = "custom"
    target.write_text(json.dumps(document), encoding="utf-8")
    report = uninstall.execute(uninstall.thread_status_items(ctx, uninstall.Backups(ctx), ctx.source_root),
                               credentials=False)
    assert not report["failed"]
    assert json.loads(target.read_bytes())["hooks"]["SessionStart"] == document["hooks"]["SessionStart"]
    assert all(path.exists() for path in runtimes["claude"])


def test_preexisting_empty_events_are_preserved(tmp_path, monkeypatch):
    ctx, paths, _, _ = profiles(tmp_path, monkeypatch)
    target, script = paths["codex"]
    document = json.loads(target.read_bytes())
    document["hooks"]["UserPromptSubmit"] = []
    target.write_text(json.dumps(document), encoding="utf-8")
    updated = uninstall.remove_thread_status(target.read_bytes(), "codex", script, ctx.source_root, windows=False)
    assert json.loads(updated)["hooks"]["UserPromptSubmit"] == []
    assert b'"UserPromptSubmit": []' in updated


@pytest.mark.parametrize("platform", ["linux", "win32"])
@pytest.mark.parametrize("dependency", [0, 1, 2])
@pytest.mark.parametrize("configuration", ["cursor", "desktop", "codex-toml"])
def test_other_client_references_keep_all_copied_dependencies(tmp_path, monkeypatch, platform, dependency, configuration):
    ctx, paths, runtimes, _ = profiles(tmp_path, monkeypatch, platform)
    runtime = runtimes["claude"][dependency]
    command = installer.command_for(runtime, "--custom")
    if configuration == "cursor":
        target, _ = paths["cursor"]
        document = json.loads(target.read_bytes())
        document["hooks"]["sessionStart"].append({"command": command, "timeout": 12})
        target.write_text(json.dumps(document), encoding="utf-8")
    elif configuration == "desktop":
        target = next(path for client, path in uninstall.client_files(ctx) if client == "desktop")
        write(target, json.dumps({"mcpServers": {"custom": {"command": command}}}))
    else:
        target = paths["codex"][0].with_name("config.toml")
        # JSON strings are valid TOML basic strings for this fixture's escaped Windows paths.
        write(target, '[mcp_servers.custom]\ncommand = ' + json.dumps(command) + "\n")
    report = uninstall.execute(uninstall.thread_status_items(ctx, uninstall.Backups(ctx), ctx.source_root),
                               credentials=False)
    assert not report["failed"] and not report["refused"]
    assert all(path.exists() for path in runtimes["claude"])


@pytest.mark.parametrize("change", ["invalid", "unreadable", "malformed-command", "unknown-wrapper"])
def test_uncertain_other_configuration_keeps_copied_runtimes(tmp_path, monkeypatch, change):
    ctx, paths, runtimes, _ = profiles(tmp_path, monkeypatch)
    target = ctx.home / ".gemini/config/hooks.json"
    if change in ("invalid", "unreadable"):
        write(target, b"{invalid" if change == "invalid" else b"{}")
        if change == "unreadable":
            read_bytes = Path.read_bytes

            def unreadable(path):
                if path == target:
                    raise PermissionError("isolated fixture is unreadable")
                return read_bytes(path)

            monkeypatch.setattr(Path, "read_bytes", unreadable)
    else:
        command = "unclosed 'quote" if change == "malformed-command" else "powershell.exe -EncodedCommand unknown!"
        write(target, json.dumps({"hooks": {"SessionStart": [{"command": command}]}}))
    report = uninstall.execute(uninstall.thread_status_items(ctx, uninstall.Backups(ctx), ctx.source_root),
                               credentials=False)
    assert not report["failed"] and not report["refused"]
    assert all(path.exists() for copies in runtimes.values() for path in copies)


@pytest.mark.parametrize("change", ["reference", "new-config", "invalid", "unreadable", "runtime"])
def test_runtime_removal_rechecks_configuration_and_the_full_bundle(tmp_path, monkeypatch, change):
    ctx, paths, runtimes, _ = profiles(tmp_path, monkeypatch)
    target, _ = paths["cursor"]
    document = json.loads(target.read_bytes())
    if change == "new-config":
        target.unlink()
    plan = uninstall.thread_status_items(ctx, uninstall.Backups(ctx), ctx.source_root)
    if change in ("reference", "new-config"):
        document["hooks"]["sessionStart"].append({"command": installer.command_for(runtimes["claude"][2], "--custom")})
        target.write_text(json.dumps(document), encoding="utf-8")
    elif change == "invalid":
        target.write_bytes(b"{invalid after inventory")
    elif change == "runtime":
        runtimes["claude"][1].write_bytes(b"modified after inventory")
    else:
        read_bytes = Path.read_bytes

        def unreadable(path):
            if path == target:
                raise PermissionError("isolated configuration changed")
            return read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", unreadable)
    report = uninstall.execute(plan, credentials=False)
    assert all(path.exists() for path in runtimes["claude"])
    assert all(any(str(path) in entry for entry in report["refused"]) for path in runtimes["claude"])


@pytest.mark.parametrize("dependency", [0, 1, 2])
def test_modified_copied_bundle_and_unrelated_turn_state_are_retained(tmp_path, monkeypatch, dependency):
    ctx, paths, runtimes, originals = profiles(tmp_path, monkeypatch)
    runtime = runtimes["codex"][dependency]
    runtime.write_bytes(runtime.read_bytes() + b"\n# local customization\n")
    record = write(runtime.parent / "agents-core-thread-status-state/chat.json", b'{"turn_id":"keep"}\n')
    custom = write(runtime.parent / "my-hook.py", b"keep\n")
    items = uninstall.thread_status_items(ctx, uninstall.Backups(ctx), ctx.source_root)
    assert any(item.target == str(record.parent) and item.action == "keep" for item in items)
    report = uninstall.execute(items, credentials=False)
    assert not report["failed"] and not report["refused"]
    assert json.loads(paths["codex"][0].read_bytes()) == originals["codex"]
    assert all(path.exists() for path in runtimes["codex"])
    assert record.read_bytes() == b'{"turn_id":"keep"}\n' and custom.read_bytes() == b"keep\n"


@pytest.mark.parametrize("linked", ["configuration", "profile", "runtime"])
def test_linked_profiles_and_runtime_copies_are_not_changed(tmp_path, monkeypatch, linked):
    ctx, paths, runtimes, _ = profiles(tmp_path, monkeypatch)
    target, script = paths["claude"]
    original = target.read_bytes()
    candidate = target if linked == "configuration" else target.parent if linked == "profile" else script
    moved = candidate.with_name(candidate.name + ".real")
    candidate.rename(moved)
    try:
        candidate.symlink_to(moved, target_is_directory=linked == "profile")
    except OSError:
        pytest.skip("symbolic links are unavailable")
    report = uninstall.execute(uninstall.thread_status_items(ctx, uninstall.Backups(ctx), ctx.source_root),
                               credentials=False)
    assert not report["failed"]
    assert candidate.is_symlink() and moved.exists()
    assert all(path.exists() for path in runtimes["claude"])
    if linked != "runtime":
        assert target.read_bytes() == original


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_owned_hooks_are_removed_beneath_linked_home_ancestor(tmp_path, monkeypatch, platform):
    alias = tmp_path / "system-home-alias"
    alias.mkdir()
    ctx, paths, runtimes, originals = profiles(alias, monkeypatch, platform)
    real = tmp_path / "real-home-layout"
    alias.rename(real)
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    report = uninstall.execute(uninstall.thread_status_items(ctx, uninstall.Backups(ctx), ctx.source_root),
                               credentials=False)
    assert not report["failed"]
    for client, (target, _) in paths.items():
        assert json.loads(target.read_bytes()) == originals[client]
        assert all(not path.exists() for path in runtimes[client])
    assert alias.is_symlink() and real.is_dir()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_claude_interrupt_handlers_go_while_a_foreign_stop_hook_stays(tmp_path, monkeypatch, platform):
    ctx, paths, runtimes, originals = profiles(tmp_path, monkeypatch, platform)
    target, script = paths["claude"]
    assert script.with_name(installer.INTERRUPT_NAME) in runtimes["claude"]
    document = json.loads(target.read_bytes())
    foreign = {"hooks": [{"type": "command", "command": "notify-stop"}]}
    document["hooks"]["Stop"].insert(0, foreign)
    target.write_text(json.dumps(document), encoding="utf-8")
    report = uninstall.execute(uninstall.thread_status_items(ctx, uninstall.Backups(ctx), ctx.source_root),
                               credentials=False)
    assert not report["failed"] and not report["refused"]
    assert json.loads(target.read_bytes()) == {**originals["claude"],
                                               "hooks": {**originals["claude"]["hooks"], "Stop": [foreign]}}
    assert all(not path.exists() for path in runtimes["claude"])
