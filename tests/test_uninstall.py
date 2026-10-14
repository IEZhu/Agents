"""The full uninstall (#362) on a temporary home with fake client configurations and a fake runner.

Nothing here touches the real account: every path is under ``tmp_path`` and every OS command
(launchctl, systemctl, crontab, schtasks, security) goes to ``FakeRunner``. The platform is
injected, so the macOS, Linux and Windows branches run on any OS.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/_helpers"))

from src import uninstall  # noqa: E402
from src.uninstall import Context, main, remove_json_members, remove_routing_block, remove_toml_server  # noqa: E402
from scripts import install_session_readiness as readiness_installer  # noqa: E402
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
        text = path.read_text(encoding="utf-8")
        assert OTHER_SERVER in text and "Agents-Core" not in text, path
    claude = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert claude["theme"] == "dark" and claude["projects"]["/p"]["mcpServers"] == {"keep": {"url": "https://k"}}
    assert (home / ".claude/CLAUDE.md").read_bytes() == USER_TEXT
    assert json.loads((home / ".claude/settings.json").read_text(encoding="utf-8")) == {
        "permissions": {"allow": ["Bash(ls)"], "deny": ["Bash(rm)"]}}
    assert (home / ".claude/memory/MEMORY.md").read_text(encoding="utf-8") == "- [Mine](mine.md) — personal\n"
    assert (home / ".codex/config.toml").read_text(encoding="utf-8") == 'model = "o"\n\n[mcp_servers.other]\ncommand = "o"\n\n[profiles.x]\nmodel = "y"\n'
    assert json.loads((home / ".gemini/config/config.json").read_text(encoding="utf-8")) == {"plugins": {"theirs": {"enabled": True}}}
    assert (home / "unrelated.txt").read_text(encoding="utf-8") == "mine"
    assert (home / "project/history.md").exists()  # a project file: reported, never deleted

    # Every edited file has its backup.
    manifest = json.loads((home / ".agents-core-uninstall-backups/T/manifest.json").read_text(encoding="utf-8"))
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
    assert (home / ".agents-core/flows/.user/mine.md").read_text(encoding="utf-8") == "# Mine\n"
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
    assert target.read_text(encoding="utf-8") == "binary" and not os.path.lexists(link)


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
    write(units / "timers.target.wants/agents-core-sync-mine.timer", "")  # not our exact unit name
    os.symlink(timer, units / f"timers.target.wants/agents-core-sync-{IDENT}.timer") if sys.platform != "win32" else \
        write(units / f"timers.target.wants/agents-core-sync-{IDENT}.timer", "")
    state = home / ".local/state/agents-core" / IDENT
    write(state / "user-sync/user-sync.json", "{}")
    write(home / ".config/Claude/claude_desktop_config.json", mcp_config("Agents-Core-Desktop"))
    crontab = (b"MAILTO=me\n0 * * * * backup.sh\n*/5 * * * * /bin/sh -c 'exec x' agents-core-sync-" + IDENT.encode() + b"\n")
    runner = FakeRunner({("crontab", "-l"): (0, crontab)})
    assert main(["--yes"], ctx=context(home, "linux", runner), interactive=False) == 0
    assert sorted(path.name for path in units.rglob("*")) == ["agents-core-sync-mine.timer", "other.timer",
                                                              "timers.target.wants"]
    assert runner.ran("systemctl", "--user", "disable", "--now", f"agents-core-sync-{IDENT}.timer")
    assert runner.ran("systemctl", "--user", "daemon-reload")
    written = [input for argv, input in runner.calls if argv == ["crontab", "-"]]
    assert written == [b"MAILTO=me\n0 * * * * backup.sh\n"]
    assert not (home / ".local/state/agents-core").exists()
    assert OTHER_SERVER in (home / ".config/Claude/claude_desktop_config.json").read_text(encoding="utf-8")
    assert (home / ".agents-core-uninstall-backups/T/001-crontab").read_bytes() == crontab
    if sys.platform != "win32":  # the crontab can hold tokens: private like every backup
        assert (home / ".agents-core-uninstall-backups/T/001-crontab").stat().st_mode & 0o777 == 0o600


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
        assert OTHER_SERVER in path.read_text(encoding="utf-8") and "Agents-Core" not in path.read_text(encoding="utf-8")
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



# --- review round 1 (#362): F1-F17 ------------------------------------------------------------

def git_repository(path: Path, *, remote: str | None = "https://github.com/IEZhu/Agents.git", bare: bool = False) -> Path:
    config = "[core]\n\tbare = " + ("true" if bare else "false") + "\n"
    if remote:
        config += f'[remote "origin"]\n\turl = {remote}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
    git = path if bare else path / ".git"
    write(git / "config", config)
    write(git / "HEAD", "ref: refs/heads/main\n")
    (git / "objects").mkdir(parents=True, exist_ok=True)
    if bare and not remote:
        write(git / "refs/remotes/origin/main", "a" * 40 + "\n")
    write(path / ("description" if bare else "README.md"), "mine")
    return path


def autodev_config(state: Path, clone: str, repository: str = "IEZhu/Agents") -> None:
    write(state / "schedules/autodev.json", json.dumps({"clone": clone, "repository": repository}))


@pytest.mark.parametrize("where", ["documents", "source", "other-remote", "no-git", "working-tree"])
def test_an_invalid_autodev_clone_is_kept_and_reported(home, tmp_path, capsys, where):
    """F1: a hand-edited or stale autodev.json never deletes a project or the running checkout."""
    state = darwin_home(home)
    source = checkout(home / "code/Agents")
    target = {"documents": home / "Documents/MyProject", "source": source,
              "other-remote": home / "projects/mine", "no-git": home / "projects/plain",
              "working-tree": home / "src/Agents"}[where]
    if where in ("documents", "working-tree"):
        git_repository(target)
    elif where == "other-remote":
        git_repository(target, remote="git@github.com:someone/else.git")
    elif where == "no-git":
        write(target / "notes.txt", "mine")
    autodev_config(state, str(target))
    ctx = context(home, "darwin", FakeRunner())
    ctx.source_root = source
    before = snapshot(target)
    main(["--yes"], ctx=ctx, interactive=False)
    assert target.is_dir() and snapshot(target) == {key: value for key, value in before.items()
                                                    if not key.endswith("data/.shared-service.json")}
    assert f"{target}: named as the autodev clone, but" in capsys.readouterr().out


def test_a_valid_autodev_clone_is_removed(home):
    state = darwin_home(home)
    bare = git_repository(home / ".local/share/runner/IEZhu-Agents.git", remote=None, bare=True)
    autodev_config(state, "~/.local/share/runner/IEZhu-Agents.git")
    other = git_repository(home / "srv/IEZhu-Agents.git", bare=True)
    write(other / "refs/remotes/origin/main", "a" * 40 + "\n")
    state2 = home / "Library/Application Support/Agents-Core/fedcba9876543210"
    write(state2 / "service.json", "{}")
    autodev_config(state2, str(other))
    assert main(["--yes"], ctx=context(home, "darwin", FakeRunner()), interactive=False) == 0
    assert not bare.exists() and not other.exists()


def test_only_the_installers_own_backup_names_are_removed(home, capsys):
    """F2: names a user may have chosen stay; mkstemp-like names are only reported."""
    darwin_home(home)
    user = [write(home / ".claude.json.backup.20261013", "mine"),
            write(home / ".claude/CLAUDE.md.backup.original", "mine"),
            write(home / ".claude/settings.json.backup.1760000000", "mine"),
            write(home / ".claude.json.backup.k3j_x9ab", "mine")]
    ours = [write(home / ".claude/CLAUDE.md.backup.1760000000", "old"),
            write(home / ".codex/AGENTS.md.backup.1234567890123456789", "old")]
    assert main(["--yes"], ctx=context(home, "darwin", FakeRunner()), interactive=False) == 0
    assert all(path.read_text(encoding="utf-8") == "mine" for path in user)
    assert not any(path.exists() for path in ours)
    output = capsys.readouterr().out
    assert f"{home / '.claude.json.backup.k3j_x9ab'}: named like the installer's backup" in output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions and symlinks")
def test_read_only_trees_are_removed_and_link_targets_keep_their_mode(home, tmp_path):
    """F3/F14: no chmod reaches a link's target; a 0555 directory tree (autodev environments) goes."""
    darwin_home(home)
    target = write(tmp_path / "homebrew-python", "binary")
    target.chmod(0o755)
    bin_dir = home / ".agents-core/.venv/bin"
    os.symlink(target, bin_dir / "python")
    write(bin_dir / "pip", "x")
    bin_dir.chmod(0o555)
    lib = home / ".local/share/agents-autodev/venvs/abc/lib"
    write(lib / "site.py", "x")
    outside = write(tmp_path / "outside.txt", "keep")
    outside.chmod(0o644)
    os.symlink(outside, lib / "link")
    for directory in (lib, lib.parent):
        directory.chmod(0o555)
    assert main(["--yes"], ctx=context(home, "darwin", FakeRunner()), interactive=False) == 0
    assert not (home / ".agents-core").exists() and not (home / ".local/share/agents-autodev").exists()
    assert target.stat().st_mode & 0o777 == 0o755 and outside.stat().st_mode & 0o777 == 0o644


def test_a_reparse_point_counts_as_a_link(monkeypatch, tmp_path):
    """F6: a junction is not a symlink on Python 3.11 Windows; its reparse attribute is what shows it."""
    import stat as stat_module
    import types
    fake = types.SimpleNamespace(st_mode=stat_module.S_IFDIR | 0o755, st_file_attributes=uninstall.REPARSE_POINT)
    monkeypatch.setattr(uninstall.os, "lstat", lambda path: fake)
    assert uninstall._is_link(tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_windows_junctions_are_never_followed(home, tmp_path, capsys):
    darwin_home(home)
    target = write(tmp_path / "elsewhere/keep.txt", "keep").parent
    junction = home / ".agents-core/data/junction"
    junction.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)], check=True, capture_output=True)
    assert main(["--yes", "--keep-data"], ctx=context(home, "darwin", FakeRunner()), interactive=False) == 0
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep" and not os.path.lexists(junction)
    # A junction as the installation itself is refused, its target kept.
    other = checkout(tmp_path / "checkout")
    shutil.rmtree(home / ".agents-core", onerror=lambda function, path, _: (os.chmod(path, 0o666), function(path)))
    subprocess.run(["cmd", "/c", "mklink", "/J", str(home / ".agents-core"), str(other)], check=True, capture_output=True)
    assert main(["--yes"], ctx=context(home, "darwin", FakeRunner()), interactive=False) == 1
    assert (other / "src/server.py").exists() and "symbolic link or junction" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_backups_are_private(home):
    """F4: the copies hold client configurations with possible tokens."""
    os.umask(0o022)
    darwin_home(home)
    assert main(["--yes"], ctx=context(home, "darwin", FakeRunner()), interactive=False) == 0
    base = home / ".agents-core-uninstall-backups"
    assert base.stat().st_mode & 0o777 == 0o700 and (base / "T").stat().st_mode & 0o777 == 0o700
    files = list((base / "T").iterdir())
    assert files and all(path.stat().st_mode & 0o777 == 0o600 for path in files)


def library(path: Path) -> Path:
    write(path / ".agents-library.json", "{}")
    write(path / "common/mine.md", "# Mine\n")
    return path


def fastembed(path: Path) -> Path:
    write(path / "models--qdrant--model/blobs/x", "weights")
    return path


def test_a_custom_installation_is_removed_when_its_content_is_ours(home, tmp_path):
    """F5/F11: the directories the variables name are removed after a content check, even outside home."""
    darwin_home(home)
    installation = checkout(home / "agents")
    service = tmp_path / "service-state"
    write(service / "service.json", json.dumps({"installation": str(installation)}))
    cache, flows = fastembed(tmp_path / "models"), library(tmp_path / "library")
    ctx = context(home, "darwin", FakeRunner(), AGENTS_HOME=str(installation), AGENTS_SERVICE_DIR=str(service),
                  FASTEMBED_CACHE_DIR=str(cache), AGENTS_USER_FLOWS_DIR=str(flows))
    ctx.source_root = installation
    assert main(["--yes"], ctx=ctx, interactive=False) == 0
    for gone in (installation, service, cache, flows):
        assert not gone.exists(), gone


def test_directories_named_by_variables_are_kept_without_our_content(home, tmp_path, capsys):
    darwin_home(home)
    projects = write(home / "Projects/notes.md", "mine").parent
    models = write(tmp_path / "models/other-model.bin", "x").parent
    ctx = context(home, "darwin", FakeRunner(), AGENTS_USER_FLOWS_DIR=str(projects), FASTEMBED_CACHE_DIR=str(models),
                  AGENTS_SERVICE_DIR=str(home), AGENTS_HOME=str(home / "Projects"))
    main(["--yes"], ctx=ctx, interactive=False)
    assert (projects / "notes.md").exists() and (models / "other-model.bin").exists()
    output = capsys.readouterr().out
    assert "AGENTS_USER_FLOWS_DIR, but it has no .agents-library.json" in output
    assert "configured as the embedding cache, but it holds more than fastembed models" in output
    assert "named by AGENTS_HOME, but it is not an Agents-Core checkout" in output


def test_filesystem_root_home_and_its_parents_are_never_removed(home):
    """F5: FASTEMBED_CACHE_DIR=/ is listed as kept, and no ownership claim lets a root through."""
    ctx = context(home, "darwin", FakeRunner(), FASTEMBED_CACHE_DIR=os.path.abspath(os.sep),
                  XDG_CONFIG_HOME=str(home / ".config"))
    items = [item for item in uninstall.build_plan(ctx) if item.kind == "cache"]
    assert [(item.target, item.action, item.apply) for item in items] == [(os.path.abspath(os.sep), "keep", None)]
    (home / ".config").mkdir(exist_ok=True)
    for path in (Path(os.path.abspath(os.sep)), home, home.parent, home / "Documents", home / ".config"):
        with pytest.raises(uninstall.Refused):
            uninstall.check_removable(ctx, path, owned=True)
    with pytest.raises(uninstall.Refused):  # outside home without an ownership claim
        uninstall.check_removable(ctx, home.parent / "elsewhere")


def test_windows_tasks_are_derived_from_the_state_directory(home, tmp_path):
    """F12: `--state <dir>` and AGENTS_SERVICE_DIR name tasks after any directory name."""
    state = tmp_path / "my state"
    write(state / "service.json", json.dumps({"installation": str(home / ".agents-core")}))
    tasks = ('"\\agents-core-daemon-my state","N/A","Ready"\r\n"\\agents-core-daemon-my state-updater","x","Ready"\r\n'
             '"\\agents-core-daemon-someone","x","Ready"\r\n').encode()
    runner = FakeRunner({("schtasks", "/Query"): (0, tasks)})
    ctx = context(home, "win32", runner, AGENTS_SERVICE_DIR=str(state), APPDATA=str(home / "AppData/Roaming"),
                  LOCALAPPDATA=str(home / "AppData/Local"), USERPROFILE=str(home))
    assert main(["--yes"], ctx=ctx, interactive=False) == 0
    assert [argv[3] for argv in runner.ran("schtasks", "/Delete")] == [
        "\\agents-core-daemon-my state", "\\agents-core-daemon-my state-updater"]
    assert not state.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs a privilege on Windows")
def test_launch_agents_are_exact_and_a_linked_plist_is_not_booted_out(home, tmp_path):
    """F9/F12: exact labels, labels derived from a custom state name, the link check before bootout."""
    darwin_home(home)
    state = tmp_path / "my state"
    write(state / "service.json", json.dumps({"installation": str(home / ".agents-core")}))
    agents = home / "Library/LaunchAgents"
    derived = write(agents / "local.agents-core.my state.plist", "<plist/>")
    foreign = write(agents / "local.agents-core.someone.plist", "<plist/>")
    linked_target = write(tmp_path / "nix/plist", "<plist/>")
    (agents / f"local.agents-core.{IDENT}.sync.plist").unlink()
    os.symlink(linked_target, agents / f"local.agents-core.{IDENT}.sync.plist")
    runner = FakeRunner()
    assert main(["--yes"], ctx=context(home, "darwin", runner, AGENTS_SERVICE_DIR=str(state)), interactive=False) == 1
    booted = [argv[2] for argv in runner.ran("/bin/launchctl", "bootout")]
    assert "gui/501/local.agents-core.my state" in booted and not derived.exists()
    assert f"gui/501/local.agents-core.{IDENT}.sync" not in booted
    assert foreign.exists() and linked_target.exists()
    assert "gui/501/local.agents-core.someone" not in booted


def test_settings_are_read_from_dotenv_and_service_json_without_printing_them(home, tmp_path, capsys):
    """F13: the cache and library the server really uses, from .env and service.json model_cache."""
    state = darwin_home(home)
    cache, second, flows = fastembed(tmp_path / "env-models"), fastembed(tmp_path / "service-models"), \
        library(tmp_path / "env-library")
    write(home / ".agents-core/.env", f"LANGFUSE_SECRET_KEY=sk-secret-value\n# FASTEMBED_CACHE_DIR=/nowhere\n"
          f"FASTEMBED_CACHE_DIR=\"{cache}\"\nexport AGENTS_USER_FLOWS_DIR={flows}  # comment\n")
    write(state / "service.json", json.dumps({"installation": str(home / ".agents-core"), "model_cache": str(second)}))
    assert main(["--yes"], ctx=context(home, "darwin", FakeRunner()), interactive=False) == 0
    assert not cache.exists() and not second.exists() and not flows.exists()
    assert "sk-secret-value" not in capsys.readouterr().out


def test_toml_keeps_a_comment_above_the_next_section():
    """F7: comments after our last key belong to what follows."""
    text = (b'model = "o"\n\n[mcp_servers."Agents-Core"]\n# ours\nurl = "x"\n\n# NOTE: keep this server, it talks to prod\n'
            b'[mcp_servers.other]\ncommand = "o"\n')
    assert remove_toml_server(text) == b'model = "o"\n\n# NOTE: keep this server, it talks to prod\n' \
                                       b'[mcp_servers.other]\ncommand = "o"\n'
    trailing = b'a = 1\n\n[mcp_servers."Agents-Core"]\nurl = "x"\n# the end\n'
    assert remove_toml_server(trailing) == b'a = 1\n\n# the end\n'


def test_markers_in_a_code_fence_are_text():
    """F8: an example of the markers inside a fenced block is not the managed section."""
    example = b"# Notes\n\n```markdown\n" + BEGIN + b"\nmy explanation\n" + END + b"\n```\n"
    assert remove_routing_block(example) == example
    assert remove_routing_block(example + routed(b"")) == example  # the real section after the example
    assert not uninstall.marker_lines(b"text " + BEGIN + b"\n")  # not at the start of a line


def test_json_members_are_removed_in_one_parse(monkeypatch):
    """F15: a large ~/.claude.json is parsed once, whatever the number of entries."""
    projects = ",\n".join(f'    "/p{n}": {{"mcpServers": {{"Agents-Core": {{"url": "u"}}, "k": {{}}}}}}' for n in range(200))
    text = ('{\n  "mcpServers": {"Agents-Core": 1, "Agents-Core-Desktop": 2},\n  "projects": {\n' + projects
            + '\n  }\n}\n').encode()
    parses = []
    original = uninstall._Parser.__init__
    monkeypatch.setattr(uninstall._Parser, "__init__", lambda self, text: (parses.append(1), original(self, text))[1])
    updated = remove_json_members(text, uninstall.mcp_removals(projects=True), drop_if_empty=[("mcpServers",)])
    assert len(parses) == 1
    document = json.loads(updated)
    assert "mcpServers" not in document and all(value == {"mcpServers": {"k": {}}} for value in document["projects"].values())
    first = remove_json_members(b'{"mcpServers": {"Agents-Core": 2, "Agents-Core-Desktop": 3, "b": 1}}',
                                uninstall.mcp_removals())
    assert first == b'{"mcpServers": {"b": 1}}'
    last = remove_json_members(b'{"mcpServers": {"a": 1, "Agents-Core": 2, "Agents-Core-Desktop": 3}}',
                               uninstall.mcp_removals())
    assert last == b'{"mcpServers": {"a": 1}}'


@pytest.mark.parametrize("status, branches, reason", [
    ((0, b"?? notes.md\n"), (0, b"main\n"), "it has uncommitted changes or untracked files"),
    ((0, b""), (0, b"main\nfeature\n"), "it has local branches besides main (feature)"),
    ((128, b""), (0, b"main\n"), "git could not check it for uncommitted work"),
])
def test_a_development_checkout_named_by_agents_home_is_kept(home, capsys, status, branches, reason):
    """Round 2 N2: AGENTS_HOME naming a checkout with work in it never deletes that work."""
    darwin_home(home)
    installation = checkout(home / "Documents/Agents")
    runner = FakeRunner({("git", "-C", str(installation), "status"): status,
                         ("git", "-C", str(installation), "for-each-ref"): branches})
    ctx = context(home, "darwin", runner, AGENTS_HOME=str(installation))
    ctx.source_root = installation
    assert main(["--yes"], ctx=ctx, interactive=False) == 0
    assert (installation / "src/server.py").exists()
    assert f"named by AGENTS_HOME, but {reason}" in capsys.readouterr().out


def test_windows_states_inside_the_installation_are_found_and_kept_with_keep_data(home):
    """%USERPROFILE%\\.agents-core holds the state directories and can be the installation too."""
    state = home / ".agents-core" / IDENT
    write(state / "service.json", "{}")
    write(state / "user-sync/user-sync.json", "{}")
    write(state / "unresolved/history.md", "# turns\n")
    write(state / "workspaces.json", json.dumps({"w": str(home / "project")}))
    project = write(home / "project/.mcp.json", mcp_config("Agents-Core"))
    ctx = context(home, "win32", FakeRunner(), USERPROFILE=str(home), APPDATA=str(home / "AppData/Roaming"),
                  LOCALAPPDATA=str(home / "AppData/Local"))
    assert main(["--yes", "--keep-data"], ctx=ctx, interactive=False) == 0
    assert (state / "user-sync/user-sync.json").exists() and (state / "unresolved/history.md").exists()
    assert (home / ".agents-core/flows/.user/mine.md").exists()
    assert not (state / "service.json").exists() and not (home / ".agents-core/src/server.py").exists()
    assert "Agents-Core" not in project.read_text(encoding="utf-8")


def test_decoys_are_removed_with_the_data_but_only_real_decoy_files(home):
    state = darwin_home(home)
    marker, other = "Ab1" + "x" * 45, "Cd2" + "y" * 45
    decoy_dir = home / ".cache-0123abcd"
    ssh_decoy = write(home / ".ssh/id_ed25519_5a6b7c8d", ("-----BEGIN\n" + marker + "\n-----END\n").encode())
    write(decoy_dir / "credentials-1234.json", ('{"token": "' + other + '"}').encode())
    foreign = write(home / ".ssh/id_ed25519", b"the owner's key")
    changed = write(home / ".ssh/id_ed25519_00000000", b"no marker any more")
    outside = write(home.parent / "elsewhere.txt", marker.encode())
    service, account = "api-token-0123456789ab", "user-01234567"
    write(state / "schedules/decoys.json", json.dumps({
        "version": 2, "directory": str(decoy_dir), "keychain": {"service": service, "account": account, "marker": other},
        "files": [{"path": str(ssh_decoy), "marker": marker}, {"path": str(foreign), "marker": marker},
                  {"path": str(changed), "marker": marker}, {"path": str(outside), "marker": marker},
                  {"path": str(decoy_dir / "credentials-1234.json"), "marker": other}]}))
    runner = keychain_runner()
    items = uninstall.build_plan(context(home, "darwin", runner))
    decoy_targets = [item.target for item in items if "decoy" in item.detail]
    assert decoy_targets == [str(ssh_decoy), str(decoy_dir), f"keychain item {service}"]
    assert all(item.kind == "data" for item in items if "decoy" in item.detail)
    assert main(["--yes"], ctx=context(home, "darwin", runner), interactive=False) == 0
    assert not ssh_decoy.exists() and not decoy_dir.exists()
    assert foreign.read_bytes() == b"the owner's key" and outside.exists() and changed.exists()
    assert runner.ran("/usr/bin/security", "delete-generic-password", "-s", service, "-a", account)


def test_registry_entries_with_foreign_names_are_never_removed(home):
    state = darwin_home(home)
    write(home / ".config/x", b"kept")  # the owner's own directory: not a decoy name
    write(state / "schedules/decoys.json", json.dumps({
        "directory": str(home / ".config"),
        "keychain": {"service": "agents-autodev-github", "account": "WonderMr", "marker": "Ab1" + "x" * 45},
        "files": []}))
    runner = keychain_runner()
    items = uninstall.decoy_items(context(home, "darwin", runner), [state])
    assert items == [] and not runner.ran("/usr/bin/security", "find-generic-password", "-s", "agents-autodev-github",
                                          "-a", "WonderMr")


def readiness_profiles(tmp_path, monkeypatch, platform, home_name="home with spaces"):
    """Install the real adapters into fake profiles using a fake installation's interpreter."""
    home = tmp_path / home_name
    home.mkdir()
    installation = checkout(home / ".agents-core")
    for name in ("session_readiness.py", "session_readiness_mcp.py"):
        write(installation / "scripts" / name, (ROOT / "scripts" / name).read_bytes())
    python = installation / (".venv/Scripts/python.exe" if platform == "win32" else ".venv/bin/python")
    write(python, "fake interpreter; never executed")
    claude, codex = home / "profiles/Claude Code", home / "profiles/Codex"
    env = {"CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex)}
    ctx = context(home, platform, FakeRunner(), **env)
    desktop = next(path for client, path in uninstall.client_files(ctx) if client == "desktop")
    paths = {
        "claude": (claude / "settings.json", claude / "hooks" / readiness_installer.SCRIPT_NAME),
        "codex": (codex / "hooks.json", codex / "hooks" / readiness_installer.SCRIPT_NAME),
        "cursor": (home / ".cursor/hooks.json", home / ".cursor/hooks" / readiness_installer.SCRIPT_NAME),
        "antigravity": (home / ".gemini/config/hooks.json",
                        home / ".gemini/config/hooks" / readiness_installer.SCRIPT_NAME),
        "desktop": (desktop, desktop.parent / "agents-core-readiness/hooks" / readiness_installer.SCRIPT_NAME),
    }
    originals = {
        "claude": {"theme": "keep", "hooks": {"PreToolUse": [{"command": "other"}]}},
        "codex": {"theme": "keep", "hooks": {"PreToolUse": [{"command": "other"}]}},
        "cursor": {"version": 1, "hooks": {"preToolUse": [{"command": "other"}]}},
        "antigravity": {"other-hook": {"enabled": False}},
        "desktop": {"mcpServers": {"other": {"command": "other"}}, "theme": "keep"},
    }
    monkeypatch.setattr(readiness_installer, "ROOT", installation)
    monkeypatch.setattr(readiness_installer.sys, "executable", str(python))
    monkeypatch.setattr(readiness_installer, "WINDOWS_HOOKS", platform == "win32")
    monkeypatch.setattr(readiness_installer, "client_paths", lambda client: paths[client])
    runtimes = {}
    for client, (target, _) in paths.items():
        write(target, json.dumps(originals[client]))
        runtimes[client] = [Path(path) for path in readiness_installer.install(client)["scripts"]]
    return ctx, paths, runtimes, originals


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_uninstall_removes_installed_readiness_adapters(tmp_path, monkeypatch, platform):
    ctx, paths, runtimes, originals = readiness_profiles(tmp_path, monkeypatch, platform)
    before = snapshot(ctx.home)
    assert main(["--dry-run"], ctx=ctx, interactive=False) == 0
    assert snapshot(ctx.home) == before
    assert main(["--yes"], ctx=ctx, interactive=False) == 0
    for client, (target, _) in paths.items():
        assert json.loads(target.read_bytes()) == originals[client], client
        assert all(not path.exists() for path in runtimes[client]), client
    assert not (ctx.home / ".agents-core").exists()
    after = snapshot(ctx.home)
    assert main(["--yes"], ctx=ctx, interactive=False) == 0
    assert snapshot(ctx.home) == after


def readiness_entry(document, client):
    if client == "desktop":
        return document["mcpServers"]["Agents-Readiness"]
    if client == "antigravity":
        return document["agents-core-readiness"]["SessionStart"][0]
    event = "sessionStart" if client == "cursor" else "SessionStart"
    entry = document["hooks"][event][0]
    return entry if client == "cursor" else entry["hooks"][0]


@pytest.mark.parametrize("client", readiness_installer.CLIENTS)
@pytest.mark.parametrize("change", ["modified", "other-installation"])
def test_uninstall_preserves_modified_or_foreign_readiness(tmp_path, monkeypatch, client, change):
    ctx, paths, runtimes, _ = readiness_profiles(tmp_path, monkeypatch, "linux")
    target, _ = paths[client]
    document = json.loads(target.read_bytes())
    entry = readiness_entry(document, client)
    if change == "modified":
        if client == "desktop":
            entry["env"] = {"MY_SETTING": "keep"}
        else:
            entry["timeout"] = 7
    else:
        entry["command"] = entry["command"].replace(str(ctx.home / ".agents-core"), str(ctx.home / "other-installation"))
    raw = (json.dumps(document, indent=2) + "\n").encode()
    target.write_bytes(raw)
    contents = {path: path.read_bytes() for path in runtimes[client]}
    assert main(["--yes"], ctx=ctx, interactive=False) == 0
    assert target.read_bytes() == raw
    assert all(path.read_bytes() == content for path, content in contents.items())


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_uninstall_preserves_other_handlers_and_shared_readiness_runtime(tmp_path, monkeypatch, platform):
    ctx, paths, runtimes, _ = readiness_profiles(tmp_path, monkeypatch, platform)
    target, script = paths["claude"]
    document = json.loads(target.read_bytes())
    group = document["hooks"]["SessionStart"][0]
    other = {"type": "command", "command": readiness_installer.command_for(script, "--custom"), "timeout": 12}
    group["hooks"].append(other)
    untouched = {"matcher": "user-custom", "hooks": [dict(group["hooks"][0])]}
    document["hooks"]["SessionStart"].append(untouched)
    target.write_text(json.dumps(document), encoding="utf-8")
    shared = {path: path.read_bytes() for path in runtimes["claude"]}
    assert main(["--yes"], ctx=ctx, interactive=False) == 0
    updated = json.loads(target.read_bytes())
    assert updated["hooks"]["SessionStart"] == [{"matcher": readiness_installer.MATCHER, "hooks": [other]}, untouched]
    assert all(path.read_bytes() == content for path, content in shared.items())


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_uninstall_preserves_quoted_readiness_runtime_in_modified_mixed_handler(tmp_path, monkeypatch, platform):
    ctx, paths, runtimes, _ = readiness_profiles(tmp_path, monkeypatch, platform, home_name="O'Neil home")
    target, _ = paths["claude"]
    document = json.loads(target.read_bytes())
    group = document["hooks"]["SessionStart"][0]
    modified = {**group["hooks"][0], "timeout": 12}
    group["hooks"].append(modified)
    target.write_text(json.dumps(document), encoding="utf-8")
    before = runtimes["claude"][0].read_bytes()
    plan = uninstall.readiness_items(ctx, uninstall.Backups(ctx), ctx.home / ".agents-core")
    report = uninstall.execute(plan, credentials=False)
    assert not report["failed"] and not report["refused"]
    updated = target.read_bytes()
    assert json.loads(updated)["hooks"]["SessionStart"] == [{"matcher": readiness_installer.MATCHER, "hooks": [modified]}]
    assert json.dumps(modified).encode() in updated and b'"theme": "keep"' in updated
    assert runtimes["claude"][0].read_bytes() == before


@pytest.mark.parametrize("dependency", ["claude", "desktop-runtime", "desktop-helper"])
def test_uninstall_preserves_runtime_referenced_from_another_client(tmp_path, monkeypatch, dependency):
    ctx, paths, runtimes, _ = readiness_profiles(tmp_path, monkeypatch, "linux")
    copied = runtimes["claude"] if dependency == "claude" else runtimes["desktop"]
    referenced = copied[-1] if dependency == "desktop-helper" else copied[0]
    target, _ = paths["cursor"]
    document = json.loads(target.read_bytes())
    modified = {"command": readiness_installer.command_for(referenced, "--custom"), "timeout": 12}
    document["hooks"]["sessionStart"].append(modified)
    target.write_text(json.dumps(document), encoding="utf-8")
    before = {path: path.read_bytes() for path in copied}
    plan = uninstall.readiness_items(ctx, uninstall.Backups(ctx), ctx.home / ".agents-core")
    report = uninstall.execute(plan, credentials=False)
    assert not report["failed"] and not report["refused"]
    assert copied[0].read_bytes() == before[copied[0]]
    if dependency == "desktop-helper":
        assert copied[1].read_bytes() == before[copied[1]]
    elif dependency == "desktop-runtime":
        assert not copied[1].exists()
    updated = target.read_bytes()
    assert json.loads(updated)["hooks"]["sessionStart"] == [modified]
    assert json.dumps(modified).encode() in updated and b'"version": 1' in updated


@pytest.mark.parametrize("change", ["reference", "new-config", "invalid", "unreadable"])
@pytest.mark.parametrize("dependency", ["claude", "desktop-helper"])
def test_uninstall_rechecks_other_client_configs_before_runtime_removal(tmp_path, monkeypatch, change, dependency):
    ctx, paths, runtimes, _ = readiness_profiles(tmp_path, monkeypatch, "linux")
    copied = runtimes["claude"] if dependency == "claude" else runtimes["desktop"]
    referenced = copied[-1] if dependency == "desktop-helper" else copied[0]
    target, _ = paths["cursor"]
    document = json.loads(target.read_bytes())
    if change == "new-config":
        target.unlink()
    plan = uninstall.readiness_items(ctx, uninstall.Backups(ctx), ctx.home / ".agents-core")
    if change in ("reference", "new-config"):
        document["hooks"]["sessionStart"].append({
            "command": readiness_installer.command_for(referenced, "--custom"), "timeout": 12})
        target.write_text(json.dumps(document), encoding="utf-8")
    elif change == "invalid":
        target.write_bytes(b"{invalid after inventory")
    else:
        read_bytes = Path.read_bytes

        def unreadable(path):
            if path == target:
                raise PermissionError("scratch configuration is unreadable")
            return read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", unreadable)
    report = uninstall.execute(plan, credentials=False)
    assert all(path.exists() for path in copied)
    assert all(any(str(path) in entry for entry in report["refused"]) for path in copied)
    if change in ("reference", "new-config"):
        assert not report["failed"]


@pytest.mark.parametrize("change", ["invalid", "unreadable", "malformed-command", "encoded-command", "unknown-wrapper"])
def test_uninstall_preserves_runtimes_when_another_config_is_uncertain(tmp_path, monkeypatch, change):
    ctx, paths, runtimes, _ = readiness_profiles(tmp_path, monkeypatch, "linux")
    target, _ = paths["cursor"]
    if change == "invalid":
        target.write_bytes(b"{invalid before inventory")
    elif change == "unreadable":
        read_bytes = Path.read_bytes

        def unreadable(path):
            if path == target:
                raise PermissionError("scratch configuration is unreadable")
            return read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", unreadable)
    else:
        document = json.loads(target.read_bytes())
        commands = {"malformed-command": "unclosed 'quote",
                    "encoded-command": "powershell.exe -NoProfile -NonInteractive -EncodedCommand unknown!",
                    "unknown-wrapper": "powershell.exe -EncodedCommand unknown!"}
        command = commands[change]
        document["hooks"]["sessionStart"].append({"command": command, "timeout": 12})
        target.write_text(json.dumps(document), encoding="utf-8")
    plan = uninstall.readiness_items(ctx, uninstall.Backups(ctx), ctx.home / ".agents-core")
    candidate = str(runtimes["claude"][0])
    assert any(item.target == candidate and item.action == "keep" for item in plan)
    report = uninstall.execute(plan, credentials=False)
    assert not report["failed"] and runtimes["claude"][0].exists()


def test_uninstall_keeps_modified_runtime_and_unrelated_files(tmp_path, monkeypatch):
    ctx, paths, runtimes, _ = readiness_profiles(tmp_path, monkeypatch, "linux")
    runtime = runtimes["desktop"][0]
    runtime.write_bytes(runtime.read_bytes() + b"\n# user's local change\n")
    kept = write(runtime.parent / "my-hook.py", "keep")
    assert main(["--yes"], ctx=ctx, interactive=False) == 0
    assert runtime.read_bytes().endswith(b"# user's local change\n")
    assert kept.read_text() == "keep" and not runtimes["desktop"][1].exists()
    assert "Agents-Readiness" not in json.loads(paths["desktop"][0].read_bytes())["mcpServers"]


def test_uninstall_finds_readiness_desktop_override_and_msix(tmp_path, monkeypatch):
    ctx, _, _, _ = readiness_profiles(tmp_path, monkeypatch, "win32")
    override = ctx.home / "profiles/Desktop/desktop.json"
    msix = ctx.home / "AppData/Local/Packages/Claude_example/LocalCache/Roaming/Claude/claude_desktop_config.json"
    extra = []
    for target in (override, msix):
        script = target.parent / "agents-core-readiness/hooks" / readiness_installer.SCRIPT_NAME
        monkeypatch.setattr(readiness_installer, "client_paths", lambda client, target=target, script=script: (target, script))
        result = readiness_installer.install("desktop")
        extra += [Path(path) for path in result["scripts"]]
    ctx.environ = {**ctx.environ, "AGENTS_CLAUDE_DESKTOP_CONFIG": str(override)}
    assert main(["--yes"], ctx=ctx, interactive=False) == 0
    assert not override.exists() and not msix.exists()
    assert all(not path.exists() for path in extra)


def test_uninstall_rechecks_readiness_runtime_ownership_before_removing(tmp_path, monkeypatch):
    ctx, paths, runtimes, _ = readiness_profiles(tmp_path, monkeypatch, "linux")
    plan = uninstall.build_plan(ctx)
    target, _ = paths["desktop"]
    document = json.loads(target.read_bytes())
    document["mcpServers"]["Agents-Readiness"]["env"] = {"USER_CHANGED": "keep"}
    target.write_text(json.dumps(document), encoding="utf-8")
    report = uninstall.execute(plan, credentials=False)
    assert len(report["refused"]) == 2 and not report["failed"]
    assert all(path.exists() for path in runtimes["desktop"])
    assert json.loads(target.read_bytes()) == document


@pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs a privilege on Windows")
def test_uninstall_readiness_uses_the_venv_interpreter_path_without_resolving_it(tmp_path, monkeypatch):
    ctx, _, runtimes, _ = readiness_profiles(tmp_path, monkeypatch, "linux")
    python = ctx.home / ".agents-core/.venv/bin/python"
    python.unlink()
    system_python = write(tmp_path / "system/python", "fake system interpreter; never executed")
    os.symlink(system_python, python)
    assert main(["--yes"], ctx=ctx, interactive=False) == 0
    assert all(not path.exists() for files in runtimes.values() for path in files)
    assert system_python.read_text() == "fake system interpreter; never executed"


def test_uninstall_keeps_readiness_runtime_referenced_by_another_desktop_server(tmp_path, monkeypatch):
    ctx, paths, runtimes, _ = readiness_profiles(tmp_path, monkeypatch, "linux")
    target, _ = paths["desktop"]
    document = json.loads(target.read_bytes())
    document["mcpServers"]["user-runtime"] = {"command": "user-python", "args": [str(runtimes["desktop"][0])]}
    target.write_text(json.dumps(document), encoding="utf-8")
    assert main(["--yes"], ctx=ctx, interactive=False) == 0
    assert runtimes["desktop"][0].exists() and not runtimes["desktop"][1].exists()
    updated = json.loads(target.read_bytes())
    assert "Agents-Readiness" not in updated["mcpServers"]
    assert updated["mcpServers"]["user-runtime"] == document["mcpServers"]["user-runtime"]
