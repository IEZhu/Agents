"""The full uninstall (#362) on a temporary home with fake client configurations and a fake runner.

Nothing here touches the real account: every path is under ``tmp_path`` and every OS command
(launchctl, systemctl, crontab, schtasks, security) goes to ``FakeRunner``. The platform is
injected, so the macOS, Linux and Windows branches run on any OS.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/_helpers"))

from src import uninstall  # noqa: E402
from src.uninstall import Context, main, remove_json_members, remove_routing_block, remove_toml_server  # noqa: E402
import inject_claude_md  # noqa: E402

IDENT = "0123456789abcdef"
BEGIN, END = uninstall.MARKERS[0]
USER_TEXT = b"# My rules\n\nAlways answer in English.\n"
OTHER_SERVER = '"other": {\n      "command": "/usr/bin/other",\n      "args": ["--x"]\n    }'


class FakeRunner:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, argv, *, input=None):
        argv = list(argv)
        self.calls.append((argv, input))
        for prefix, (code, out) in self.responses.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return subprocess.CompletedProcess(argv, code, out, b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def ran(self, *prefix):
        return [argv for argv, _ in self.calls if tuple(argv[:len(prefix)]) == prefix]


def write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


def routed(text: bytes) -> bytes:
    """``text`` with the managed section, exactly as the installer's injector writes it."""
    template = ROOT / "scripts/templates/routing-protocol-core.md"
    return inject_claude_md.render(text, template.read_bytes(), Path("CLAUDE.md"))


def mcp_config(*ours: str) -> str:
    entries = [OTHER_SERVER] + [f'"{name}": {{\n      "command": "/x/.venv/bin/python",\n      "args": ["/x/src/server.py"]\n    }}'
                                for name in ours]
    return '{\n  "theme": "dark",\n  "mcpServers": {\n    ' + ",\n    ".join(entries) + "\n  }\n}\n"


def checkout(path: Path) -> Path:
    write(path / "scripts/init_repo.sh", "#!/bin/sh\n")
    write(path / "src/server.py", "")
    (path / ".git/objects").mkdir(parents=True)
    obj = write(path / ".git/objects/ab", "x")
    obj.chmod(0o444)  # as git keeps objects
    return path


def snapshot(root: Path) -> dict:
    found = {}
    for directory, folders, files in os.walk(root):
        for name in files + folders:
            path = Path(directory) / name
            found[str(path)] = path.read_bytes() if path.is_file() and not path.is_symlink() else None
    return found


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    # Client registrations with an unrelated server each.
    write(home / ".claude.json", mcp_config("Agents-Core")[:-3] + ',\n  "projects": {\n    "/p": {\n      "mcpServers": {\n'
          '        "Agents-Core": {"type": "http", "url": "http://127.0.0.1:8765/mcp"},\n        "keep": {"url": "https://k"}\n'
          '      }\n    }\n  }\n}\n')
    write(home / ".claude/settings.json", '{\n  "permissions": {\n    "allow": ["Bash(ls)"],\n    "deny": [\n'
          '      "Bash(rm)",\n      "mcp__Agents-Core-Desktop__*",\n      "mcp__Agents_Core_Desktop__*"\n    ]\n  }\n}\n')
    write(home / ".claude/CLAUDE.md", routed(USER_TEXT))
    write(home / ".claude/memory/feedback_agents_core_routing.md",
          (ROOT / "scripts/templates/memory-routing.md").read_bytes())
    write(home / ".claude/memory/MEMORY.md", "- [Mine](mine.md) — personal\n" + uninstall.MEMORY_INDEX_ENTRIES[0] + "\n")
    write(home / ".claude/CLAUDE.md.backup.1234567890123456789", "old")
    write(home / ".codex/config.toml", 'model = "o"\n\n[mcp_servers.other]\ncommand = "o"\n\n[mcp_servers."Agents-Core"]\n'
          '"url" = "http://127.0.0.1:8765/mcp"\n\n[mcp_servers."Agents-Core".http_headers]\nA = "b"\n\n[profiles.x]\nmodel = "y"\n')
    write(home / ".codex/AGENTS.md", routed(b""))
    write(home / ".cursor/mcp.json", '{\n  "mcpServers": {\n    "Agents-Core": {"command": "p"}\n  }\n}\n')
    write(home / ".gemini/config/mcp_config.json", mcp_config("Agents-Core"))
    write(home / ".gemini/config/config.json", '{\n  "plugins": {\n    "agents-core": {"enabled": true},\n'
          '    "theirs": {"enabled": true}\n  }\n}\n')
    write(home / ".gemini/config/plugins/agents-core/plugin.json", "{}")
    # Data, caches, the installation, the autodev clone and credentials.
    write(home / ".cache/fastembed/local/model/file", "weights")
    checkout(home / ".agents-core")
    write(home / ".agents-core/flows/.user/mine.md", "# Mine\n")
    write(home / ".agents-core/.venv/bin/placeholder", "")
    checkout(home / ".local/share/agents-autodev/IEZhu-Agents")
    write(home / ".config/agents-autodev/deploy_key", "PRIVATE")
    write(home / "unrelated.txt", "mine")
    return home


def darwin_home(home):
    write(home / "Library/Application Support/Claude/claude_desktop_config.json", mcp_config("Agents-Core-Desktop"))
    state = home / "Library/Application Support/Agents-Core" / IDENT
    write(state / "service.json", "{}")
    write(state / "user-sync/user-sync.json", "{}")
    write(state / "unresolved/history.md", "# turns\n")
    write(state / "workspaces.json", json.dumps({"w": str(home / "project")}))
    write(home / "project/.mcp.json", mcp_config("Agents-Core"))
    write(home / "project/.cursor/mcp.json", mcp_config("Agents-Core"))
    write(home / "project/history.md", "# History\n")
    for suffix in ("", ".updater", ".sync", ".scheduler"):
        write(home / f"Library/LaunchAgents/local.agents-core.{IDENT}{suffix}.plist", "<plist/>")
    write(home / "Library/LaunchAgents/com.other.plist", "<plist/>")
    return state


def context(home, platform, runner, **environ):
    return Context(home=home, platform=platform, environ=environ, runner=runner,
                   source_root=home / ".agents-core", uid=501, stamp="T")


def keychain_runner():
    return FakeRunner({("/usr/bin/security", "find-generic-password"): (0, b"attributes")})


def test_dry_run_changes_nothing_and_groups_by_kind(home, capsys):
    darwin_home(home)
    runner = keychain_runner()
    before = snapshot(home)
    assert main(["--dry-run"], ctx=context(home, "darwin", runner), interactive=False) == 0
    assert snapshot(home) == before
    output = capsys.readouterr().out
    for title in ("Services and scheduled jobs:", "Client registrations:", "Managed instructions:",
                  "State, data and logs:", "Caches:", "Installation:", "Credentials"):
        assert title in output
    assert not runner.ran("/bin/launchctl") and not runner.ran("/usr/bin/security", "delete-generic-password")


def test_darwin_uninstall_removes_every_trace_and_keeps_the_rest(home, capsys):
    state = darwin_home(home)
    runner = keychain_runner()
    settings_before = (home / ".claude/settings.json").read_bytes()
    assert main(["--yes"], ctx=context(home, "darwin", runner), interactive=False) == 0

    for gone in (home / "Library/Application Support/Agents-Core", home / ".agents-core",
                 home / ".local/share/agents-autodev", home / ".cache/fastembed",
                 home / ".gemini/config/plugins/agents-core", home / ".cursor/mcp.json",
                 home / ".codex/AGENTS.md", home / ".claude/memory/feedback_agents_core_routing.md",
                 home / ".claude/CLAUDE.md.backup.1234567890123456789"):
        assert not gone.exists(), gone
    assert not list((home / "Library/LaunchAgents").glob("local.agents-core.*"))
    assert (home / "Library/LaunchAgents/com.other.plist").exists()
    assert [argv[2] for argv in runner.ran("/bin/launchctl", "bootout")] == sorted(
        f"gui/501/local.agents-core.{IDENT}{suffix}" for suffix in ("", ".scheduler", ".sync", ".updater"))

    # Unrelated entries and user text stay byte for byte.
    for path in (home / ".claude.json", home / ".gemini/config/mcp_config.json", home / "project/.mcp.json",
                 home / "Library/Application Support/Claude/claude_desktop_config.json"):
        text = path.read_text()
        assert OTHER_SERVER in text and "Agents-Core" not in text, path
    claude = json.loads((home / ".claude.json").read_text())
    assert claude["theme"] == "dark" and claude["projects"]["/p"]["mcpServers"] == {"keep": {"url": "https://k"}}
    assert (home / ".claude/CLAUDE.md").read_bytes() == USER_TEXT
    assert json.loads((home / ".claude/settings.json").read_text()) == {
        "permissions": {"allow": ["Bash(ls)"], "deny": ["Bash(rm)"]}}
    assert (home / ".claude/memory/MEMORY.md").read_text() == "- [Mine](mine.md) — personal\n"
    assert (home / ".codex/config.toml").read_text() == 'model = "o"\n\n[mcp_servers.other]\ncommand = "o"\n\n[profiles.x]\nmodel = "y"\n'
    assert json.loads((home / ".gemini/config/config.json").read_text()) == {"plugins": {"theirs": {"enabled": True}}}
    assert (home / "unrelated.txt").read_text() == "mine"
    assert (home / "project/history.md").exists()  # a project file: reported, never deleted

    # Every edited file has its backup.
    manifest = json.loads((home / ".agents-core-uninstall-backups/T/manifest.json").read_text())
    originals = {record["original"] for record in manifest}
    assert str(home / ".claude/settings.json") in originals and str(home / ".claude/CLAUDE.md") in originals
    backup = next(record["backup"] for record in manifest if record["original"] == str(home / ".claude/settings.json"))
    assert Path(backup).read_bytes() == settings_before

    # Credentials were not confirmed: --yes alone keeps them.
    assert (home / ".config/agents-autodev/deploy_key").exists()
    assert not runner.ran("/usr/bin/security", "delete-generic-password")
    assert "credentials were not confirmed" in capsys.readouterr().out
    assert not state.exists()


def test_credentials_need_include_credentials_with_yes(home):
    darwin_home(home)
    runner = keychain_runner()
    assert main(["--yes", "--include-credentials"], ctx=context(home, "darwin", runner), interactive=False) == 0
    assert not (home / ".config/agents-autodev").exists()
    assert runner.ran("/usr/bin/security", "delete-generic-password", "-s", "agents-autodev-github")


def test_interactive_asks_once_and_separately_for_credentials(home):
    darwin_home(home)
    runner = keychain_runner()
    questions = []

    def ask(question):
        questions.append(question)
        return "y" if len(questions) == 1 else "n"
    assert main([], ctx=context(home, "darwin", runner), ask=ask, interactive=True) == 0
    assert len(questions) == 2 and "credentials" in questions[1]
    assert (home / ".config/agents-autodev").exists() and not (home / ".agents-core").exists()


def test_declined_confirmation_changes_nothing(home):
    darwin_home(home)
    before = snapshot(home)
    assert main([], ctx=context(home, "darwin", keychain_runner()), ask=lambda _: "n", interactive=True) == 1
    assert snapshot(home) == before


def test_without_a_terminal_it_needs_yes(home):
    darwin_home(home)
    before = snapshot(home)
    assert main([], ctx=context(home, "darwin", FakeRunner()), interactive=False) == 2
    assert snapshot(home) == before


def test_keep_data_keeps_library_and_histories(home):
    state = darwin_home(home)
    assert main(["--yes", "--keep-data"], ctx=context(home, "darwin", FakeRunner()), interactive=False) == 0
    assert (home / ".agents-core/flows/.user/mine.md").read_text() == "# Mine\n"
    assert (state / "unresolved/history.md").exists() and (state / "user-sync/user-sync.json").exists()
    assert not (state / "service.json").exists()
    assert not (home / ".agents-core/.git").exists() and not (home / ".agents-core/.venv").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs a privilege on Windows")
def test_symlinks_are_refused_and_their_targets_kept(home, tmp_path, capsys):
    darwin_home(home)
    elsewhere = checkout(tmp_path / "elsewhere")
    real_installation = home / ".agents-core"
    os.rename(real_installation, tmp_path / "moved")
    os.symlink(elsewhere, real_installation)
    (tmp_path / "outside-cache").mkdir()
    os.symlink(tmp_path / "outside-cache", home / ".cache-link")
    assert main(["--yes"], ctx=context(home, "darwin", FakeRunner(), FASTEMBED_CACHE_DIR=str(home / ".cache-link")),
                interactive=False) == 1
    assert (elsewhere / "src/server.py").exists() and real_installation.is_symlink()
    assert "symbolic link" in capsys.readouterr().out


def test_a_venv_link_inside_the_installation_is_unlinked_not_followed(home, tmp_path):
    darwin_home(home)
    target = write(tmp_path / "system-python", "binary")
    link = home / ".agents-core/.venv/bin/python"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert main(["--yes"], ctx=context(home, "darwin", FakeRunner()), interactive=False) == 0
    assert target.read_text() == "binary" and not os.path.lexists(link)


def test_source_checkout_is_kept(home, tmp_path):
    darwin_home(home)
    source = checkout(tmp_path / "Documents/Agents")
    write(source / "data/.shared-service.json", "{}")
    ctx = context(home, "darwin", FakeRunner())
    ctx.source_root = source
    assert main(["--yes"], ctx=ctx, interactive=False) == 0
    assert (source / "src/server.py").exists() and not (source / "data/.shared-service.json").exists()
    assert not (home / ".agents-core").exists()


def test_linux_removes_systemd_units_and_only_our_crontab_line(home):
    units = home / ".config/systemd/user"
    timer = write(units / f"agents-core-sync-{IDENT}.timer", "[Timer]\n")
    write(units / f"agents-core-sync-{IDENT}.service", "[Service]\n")
    write(units / "other.timer", "[Timer]\n")
    (units / "timers.target.wants").mkdir()
    os.symlink(timer, units / f"timers.target.wants/agents-core-sync-{IDENT}.timer") if sys.platform != "win32" else \
        write(units / f"timers.target.wants/agents-core-sync-{IDENT}.timer", "")
    state = home / ".local/state/agents-core" / IDENT
    write(state / "user-sync/user-sync.json", "{}")
    write(home / ".config/Claude/claude_desktop_config.json", mcp_config("Agents-Core-Desktop"))
    crontab = (b"MAILTO=me\n0 * * * * backup.sh\n*/5 * * * * /bin/sh -c 'exec x' agents-core-sync-" + IDENT.encode() + b"\n")
    runner = FakeRunner({("crontab", "-l"): (0, crontab)})
    assert main(["--yes"], ctx=context(home, "linux", runner), interactive=False) == 0
    assert sorted(path.name for path in units.rglob("*")) == ["other.timer", "timers.target.wants"]
    assert runner.ran("systemctl", "--user", "disable", "--now", f"agents-core-sync-{IDENT}.timer")
    assert runner.ran("systemctl", "--user", "daemon-reload")
    written = [input for argv, input in runner.calls if argv == ["crontab", "-"]]
    assert written == [b"MAILTO=me\n0 * * * * backup.sh\n"]
    assert not (home / ".local/state/agents-core").exists()
    assert OTHER_SERVER in (home / ".config/Claude/claude_desktop_config.json").read_text()
    assert (home / ".agents-core-uninstall-backups/T/001-crontab").read_bytes() == crontab


def test_windows_deletes_tasks_and_appdata_state(home):
    profile_state = home / ".agents-core-win"
    appdata, local = home / "AppData/Roaming", home / "AppData/Local"
    write(appdata / "Claude/claude_desktop_config.json", mcp_config("Agents-Core-Desktop"))
    msix = write(local / "Packages/Claude_abc/LocalCache/Roaming/Claude/claude_desktop_config.json",
                 mcp_config("Agents-Core-Desktop"))
    write(profile_state / ".agents-core" / IDENT / "service.json", "{}")
    write(local / "Agents-Core" / IDENT / "user-sync/user-sync.json", "{}")
    tasks = (f'"\\agents-core-daemon-{IDENT}","N/A","Running"\r\n"\\agents-core-daemon-{IDENT}-updater","x","Ready"\r\n'
             f'"\\agents-core-sync-{IDENT}","x","Ready"\r\n"\\OneDrive Update","x","Ready"\r\n').encode()
    runner = FakeRunner({("schtasks", "/Query"): (0, tasks)})
    ctx = context(home, "win32", runner, APPDATA=str(appdata), LOCALAPPDATA=str(local),
                  USERPROFILE=str(profile_state))
    assert main(["--yes"], ctx=ctx, interactive=False) == 0
    deleted = [argv[3] for argv in runner.ran("schtasks", "/Delete")]
    assert deleted == [f"\\agents-core-daemon-{IDENT}", f"\\agents-core-daemon-{IDENT}-updater", f"\\agents-core-sync-{IDENT}"]
    assert not (profile_state / ".agents-core").exists() and not (local / "Agents-Core").exists()
    for path in (appdata / "Claude/claude_desktop_config.json", msix):
        assert OTHER_SERVER in path.read_text() and "Agents-Core" not in path.read_text()
    assert not runner.ran("/bin/launchctl") and not runner.ran("/usr/bin/security")


def test_json_edit_keeps_every_other_byte():
    text = b'{\n  "a": 1,\n  "mcpServers": {"x": 1, "Agents-Core": {"k": [1, "}"]}, "y": "\\"z"},\n  "b": [true, null]\n}'
    updated = remove_json_members(text, uninstall.mcp_removals())
    assert updated == b'{\n  "a": 1,\n  "mcpServers": {"x": 1, "y": "\\"z"},\n  "b": [true, null]\n}'
    last = remove_json_members(b'{"mcpServers": {"x": 1, "Agents-Core": 2}}', uninstall.mcp_removals())
    assert last == b'{"mcpServers": {"x": 1}}'
    only = remove_json_members(b'\xef\xbb\xbf{"mcpServers": {"Agents-Core": 2}, "z": 0}', uninstall.mcp_removals(),
                               drop_if_empty=[("mcpServers",)])
    assert only == b'\xef\xbb\xbf{"z": 0}'
    assert remove_json_members(b'{"mcpServers": {"Agents-Core": 2}}', uninstall.mcp_removals(),
                               drop_if_empty=[("mcpServers",)], delete_when=[{}]) is None


def test_routing_block_removal_restores_the_original_text():
    for original in (b"", USER_TEXT, b"no newline at end\n", b"a\r\nb\r\n"):
        assert remove_routing_block(routed(original)) == (original or None)
    with pytest.raises(ValueError):
        remove_routing_block(BEGIN + b"\n" + BEGIN + b"\n" + END)


def test_markers_match_the_installer():
    assert (inject_claude_md.MARKER_BEGIN.encode(), inject_claude_md.MARKER_END.encode()) == uninstall.MARKERS[0]
    assert (inject_claude_md.LEGACY_MARKER_BEGIN.encode(), inject_claude_md.LEGACY_MARKER_END.encode()) == uninstall.MARKERS[1]
    from src.client_paths import DESKTOP_DENY_RULES, DESKTOP_SERVER
    assert uninstall.DESKTOP_DENY_RULES == DESKTOP_DENY_RULES and uninstall.DESKTOP_SERVER == DESKTOP_SERVER


def test_toml_section_at_the_end_leaves_the_file_as_before_the_migration():
    original = b'model = "o"\n'
    migrated = original + b'\n[mcp_servers."Agents-Core"]\n"url" = "x"\n'
    assert remove_toml_server(migrated) == original


def test_toml_inline_definition_is_refused():
    with pytest.raises(ValueError):
        remove_toml_server(b'mcp_servers = { "Agents-Core" = { url = "x" } }\n')
