"""The public instruction installer must leave client connections and runtime state alone."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BEGIN = b"# >>> Agents-Core Routing Protocol (managed by init_repo) >>>"
END = b"# <<< Agents-Core Routing Protocol (managed by init_repo) <<<"
REMINDER = "feedback_agents_core_routing.md"


@pytest.fixture
def instruction_install(tmp_path, monkeypatch):
    checkout = tmp_path / "installation with spaces"
    scripts = checkout / "scripts"
    helpers, templates = scripts / "_helpers", scripts / "templates"
    helpers.mkdir(parents=True)
    (templates / "legacy").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts" / "install_instructions.py", scripts / "install_instructions.py")
    for name in ("inject_claude_md.py", "install_codex_instructions.py", "migrate_routing_memory.py"):
        shutil.copyfile(ROOT / "scripts" / "_helpers" / name, helpers / name)
        monkeypatch.delitem(sys.modules, Path(name).stem, raising=False)
    for name in ("routing-protocol-core.md", "memory-routing.md",
                 "legacy/memory-routing-v1.md", "legacy/memory-routing-v2.md"):
        shutil.copyfile(ROOT / "scripts" / "templates" / name, templates / name)

    profile = tmp_path / "isolated profile"
    profile.mkdir()
    other_cwd = tmp_path / "unrelated working directory"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    monkeypatch.syspath_prepend(str(helpers))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: profile))
    monkeypatch.setattr(shutil, "which", lambda command: None)
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(profile))
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "AGENTS_PERSONA_PROTOCOL", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("PYTHONUTF8", "1")

    spec = importlib.util.spec_from_file_location("public_instruction_installer_test", scripts / "install_instructions.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    yield SimpleNamespace(module=module, checkout=checkout, profile=profile, templates=templates,
                          cli=scripts / "install_instructions.py", cwd=other_cwd)
    for name in ("inject_claude_md", "install_codex_instructions", "migrate_routing_memory"):
        sys.modules.pop(name, None)


def client_files(install):
    codex = install.profile / ".codex" / "AGENTS.md"
    claude = install.profile / ".claude" / "CLAUDE.md"
    for target in (codex, claude):
        target.parent.mkdir(parents=True, exist_ok=True)
    return {"codex": codex, "claude": claude}


def snapshot(paths):
    return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}


def assert_protocol(install, target):
    content = target.read_bytes()
    assert (install.templates / "routing-protocol-core.md").read_bytes().strip() in content
    assert content.count(BEGIN) == content.count(END) == 1


def protected_files(install):
    paths = [
        install.profile / ".codex" / "config.toml",
        install.profile / ".claude.json",
        install.profile / ".claude" / "settings.json",
        install.profile / ".cursor" / "mcp.json",
        install.checkout / ".env",
        install.checkout / "data" / "skills_store.npz",
        install.checkout / "data" / "router_cache.json",
    ]
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"Protected state {index}\r\n".encode())
    return paths


def test_default_updates_detected_clients_and_preserves_runtime_state(instruction_install):
    install = instruction_install
    targets = client_files(install)
    protected = protected_files(install)
    before = snapshot(protected)
    for target in targets.values():
        target.write_bytes(b"# Personal instructions\r\nPreserve these bytes.\r\n")

    assert install.module.main([]) == 0
    for target in targets.values():
        assert_protocol(install, target)
        assert target.read_bytes().startswith(b"# Personal instructions\r\nPreserve these bytes.\r\n")
    assert snapshot(protected) == before
    assert not (install.profile / ".claude" / "memory").exists()


@pytest.mark.parametrize("selected", ["codex", "claude"])
def test_selected_client_is_the_only_client_updated(instruction_install, selected):
    install = instruction_install
    targets = client_files(install)
    for target in targets.values():
        target.write_bytes(b"Unchanged personal rules")
    other = targets["claude" if selected == "codex" else "codex"]
    before = snapshot([other])

    assert install.module.main(["--clients", selected]) == 0
    assert_protocol(install, targets[selected])
    assert snapshot([other]) == before
    assert not list(other.parent.glob(other.name + ".backup.*"))
    assert not (install.profile / ".claude" / "memory").exists()


@pytest.mark.parametrize("environment", [None, "1", "2", "invalid"])
def test_persona_protocol_environment_is_ignored(instruction_install, monkeypatch, environment):
    install = instruction_install
    targets = client_files(install)
    if environment is not None:
        monkeypatch.setenv("AGENTS_PERSONA_PROTOCOL", environment)
    assert install.module.main([]) == 0
    for target in targets.values():
        assert_protocol(install, target)


@pytest.mark.parametrize("arguments", [
    ["--clients", "codex,unknown"], ["--clients", ""], ["--clients", "codex,,claude"],
    ["--protocol", "1"], ["--protocol", "2"],
])
def test_invalid_arguments_are_rejected_before_any_instruction_write(instruction_install, arguments):
    install = instruction_install
    targets = client_files(install)
    for target in targets.values():
        target.write_bytes(b"Personal instructions")
    before = snapshot(targets.values())

    with pytest.raises(SystemExit) as error:
        install.module.main(arguments)
    assert error.value.code == 2
    assert snapshot(targets.values()) == before
    assert not list(install.profile.rglob("*.backup.*"))
    assert not (install.profile / ".claude" / "memory").exists()


def test_missing_clients_are_skipped_without_creating_profiles(instruction_install, capsys):
    install = instruction_install
    assert install.module.main([]) == 0
    assert not list(install.profile.iterdir())
    output = capsys.readouterr().out.lower()
    assert "codex" in output and "claude" in output and "not detected" in output


def test_codex_custom_home_and_override_preserve_inactive_files(instruction_install, tmp_path, monkeypatch):
    install = instruction_install
    targets = client_files(install)
    for target in targets.values():
        target.write_bytes(b"Inactive default profile")
    custom = tmp_path / "custom codex profile"
    custom.mkdir()
    normal, override = custom / "AGENTS.md", custom / "AGENTS.override.md"
    normal.write_bytes(b"Inactive normal instructions")
    override.write_bytes(b"Active custom override\r\n")
    preserved = snapshot([normal, *targets.values()])
    monkeypatch.setenv("CODEX_HOME", str(custom))

    assert install.module.main(["--clients", "codex"]) == 0
    assert_protocol(install, override)
    assert override.read_bytes().startswith(b"Active custom override\r\n")
    assert snapshot(preserved) == preserved
    assert len(list(custom.glob("AGENTS.override.md.backup.*"))) == 1
    assert not list(custom.glob("AGENTS.md.backup.*"))


def test_repeat_is_idempotent_and_migrations_back_up_previous_bytes(instruction_install):
    install = instruction_install
    targets = client_files(install)
    original = b"Personal text without final newline"
    # A managed section written by the removed protocol 1 installer.
    old = original + b"\n" + BEGIN + b"\nBefore answering ANY user query, call route_and_load().\n" + END + b"\n"
    for target in targets.values():
        target.write_bytes(old)
    assert install.module.main([]) == 0
    before = snapshot([*targets.values(), *install.profile.rglob("*.backup.*")])

    assert install.module.main([]) == 0
    assert snapshot(before) == before
    for target in targets.values():
        assert_protocol(install, target)
        assert target.read_bytes().startswith(original + b"\n" + BEGIN)
        assert b"Before answering ANY user query" not in target.read_bytes()
        backups = list(target.parent.glob(target.name + ".backup.*"))
        assert [path.read_bytes() for path in backups] == [old]


@pytest.mark.parametrize("legacy", ["memory-routing-v1.md", "memory-routing-v2.md"])
def test_existing_known_claude_memory_migrates_and_repeat_preserves_mtime(instruction_install, legacy):
    install = instruction_install
    client_files(install)
    directory = install.profile / ".claude" / "memory"
    directory.mkdir()
    reminder, index = directory / REMINDER, directory / "MEMORY.md"
    old = (install.templates / "legacy" / legacy).read_bytes()
    reminder.write_bytes(old)
    memory = sys.modules["migrate_routing_memory"]
    original_index = b"# Personal notes\r\n" + memory.LEGACY_INDEX_ENTRIES[0].encode() + b"\r\nKeep my other notes.\r\n"
    index.write_bytes(original_index)

    assert install.module.main(["--clients", "claude"]) == 0
    assert reminder.read_bytes() == (install.templates / "memory-routing.md").read_bytes()
    assert index.read_bytes() == original_index.replace(memory.LEGACY_INDEX_ENTRIES[0].encode(), memory.INDEX_ENTRY.encode())
    assert next(directory.glob(REMINDER + ".backup.*")).read_bytes() == old
    before = snapshot([reminder, index, *directory.glob("*.backup.*")])
    assert install.module.main(["--clients", "claude"]) == 0
    assert snapshot(before) == before


def test_custom_claude_memory_is_preserved_with_warning(instruction_install, capsys):
    install = instruction_install
    client_files(install)
    directory = install.profile / ".claude" / "memory"
    directory.mkdir()
    reminder, index = directory / REMINDER, directory / "MEMORY.md"
    reminder.write_bytes((install.templates / "legacy" / "memory-routing-v1.md").read_bytes() + b"\nMy exception\n")
    index.write_bytes(b"My custom index\r\n")
    before = snapshot([reminder, index])

    assert install.module.main(["--clients", "claude"]) == 0
    assert snapshot([reminder, index]) == before
    assert not list(directory.glob("*.backup.*"))
    assert str(reminder) in capsys.readouterr().err


@pytest.mark.parametrize("failed", ["codex", "claude"])
def test_client_failure_is_nonzero_and_does_not_block_other_client(instruction_install, capsys, failed):
    install = instruction_install
    targets = client_files(install)
    broken = targets[failed]
    broken.write_bytes(b"Personal text\n" + BEGIN)
    before = snapshot([broken])

    assert install.module.main([]) != 0
    assert snapshot([broken]) == before
    assert not list(broken.parent.glob(broken.name + ".backup.*"))
    assert_protocol(install, targets["claude" if failed == "codex" else "codex"])
    assert str(broken) in capsys.readouterr().err


def test_actual_cli_runs_from_other_cwd_without_touching_connections_or_data(instruction_install):
    install = instruction_install
    targets = client_files(install)
    protected = protected_files(install)
    before = snapshot(protected)
    result = subprocess.run(
        [sys.executable, "-S", str(install.cli), "--clients", "codex,claude"],
        cwd=install.cwd, env=dict(os.environ), text=True, encoding="utf-8",
        capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for target in targets.values():
        assert_protocol(install, target)
        assert str(target) in result.stdout
    assert snapshot(protected) == before
    assert not (install.profile / ".claude" / "memory").exists()
