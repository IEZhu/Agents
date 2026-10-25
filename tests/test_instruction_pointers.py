"""Responses point to protocol text the client already holds (#433)."""
import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from src import client_instructions as ci
from src.daemon.app import create_app
from src.daemon.workspaces import WorkspaceRegistry
from src.engine import persona, protocol_text

TOKEN = "x" * 48
BEGIN, END = protocol_text.BEGIN_MARKER, protocol_text.END_MARKER


def managed(section: str, notes: str = "## Repository notes\n") -> str:
    return f"{BEGIN}\n\n{section}\n{END}\n\n{notes}"


CURRENT = managed(protocol_text.template())
OUTDATED = managed("# Agents-Core Persona Protocol — version 2\n\nAn older protocol.\n")
# Taken before tests/conftest.py replaces it for every test with a check of explicit contexts only.
REAL_DRIFT_WARNINGS = ci.drift_warnings
LEGACY = ("# >>> Agents-Core Routing Protocol (managed by init_repo.sh) >>>\n\nOld protocol.\n\n"
          "# <<< Agents-Core Routing Protocol (managed by init_repo.sh) <<<\n")


CACHES = (ci._state_of, ci._has_text, ci._git_pointer)


@pytest.fixture(autouse=True)
def fresh_states():
    for cache in CACHES:
        cache.cache_clear()
    yield
    for cache in CACHES:
        cache.cache_clear()


@pytest.fixture
def bundle(monkeypatch):
    async def build(agent, query, history, tier=None, selection=None):
        return SimpleNamespace(
            agent=agent, bundle_revision=("c" if query == "changed" else "b") * 64, scope=f"Scope of {agent}",
            persona_block=f"Role {agent}", rules_block="Rules", skills_block="Skills", implants_block="Implants",
            skills_loaded=["skill-dev-clean-code"], implants_loaded=[], rules_loaded=["language-match"], tier="standard")
    monkeypatch.setattr(persona, "build_persona_bundle", AsyncMock(side_effect=build))


class Router:
    async def update_cache(self, *args):
        return None

    async def lookup_cache(self, *args):
        return None

    def keyword_veto(self, *args):
        return None

    def get_agent_catalog(self):
        return [{"name": "lawyer", "display_name": "Lawyer", "role": "Legal expert", "trigger_command": "/lawyer"},
                {"name": "dba", "display_name": "Database Admin", "role": "SQL", "trigger_command": "/dba"}]


async def activate(agent, current=None, **options):
    return json.loads(await persona.load_persona(Router(), agent, options.pop("query", "task"), [], current, **options))


@pytest.mark.asyncio
async def test_first_activation_and_restore_send_the_full_instruction_and_a_switch_points_to_it(bundle):
    first = await activate("lawyer")
    assert first["instruction"] == persona.APPLY_INSTRUCTION
    assert first["instruction_revision"] == persona.INSTRUCTION_REVISION == protocol_text.instruction_revision()
    switch = await activate("dba", first["persona"])
    assert switch["status"] == "SUCCESS" and switch["instruction"] == persona.INSTRUCTION_POINTER
    assert switch["instruction_revision"] == first["instruction_revision"]
    restore = await activate("dba", switch["persona"], force_reload=True)
    assert restore["instruction"] == persona.APPLY_INSTRUCTION
    # Only the role name survived: a restore without a descriptor is a first activation.
    nameless = await activate("dba", None, force_reload=True)
    assert nameless["instruction"] == persona.APPLY_INSTRUCTION and nameless["replaces_activation_id"] is None
    refresh = await activate("dba", restore["persona"], refresh=True, query="changed")
    assert refresh["status"] == "SUCCESS" and refresh["instruction"] == persona.INSTRUCTION_POINTER
    same = await activate("dba", refresh["persona"])
    assert same["status"] == "NO_CHANGE"


def test_the_pointer_keeps_the_obligations_of_each_answer():
    pointer = persona.INSTRUCTION_POINTER
    assert pointer == protocol_text.bundle_pointer() and "\n" not in pointer
    assert len(pointer) < len(persona.APPLY_INSTRUCTION) / 5
    for phrase in ("replaces_activation_id", "all four blocks", "footer", "log_interaction", "force_reload=True",
                   "instruction_revision", "or is absent", "run_flow with current_persona"):
        assert phrase in pointer, phrase


# Each variant the full SUCCESS instruction composes (protocol_text.BUNDLE_PARTS) and the words the
# pointer names it by. A variant added to the instruction fails here until the pointer names it.
BUNDLE_TOPICS = {
    ("worker", "bundle"): "worker mode",
    ("first-work", "compact"): "the first-work repository description",
    ("first-work", "compact-unavailable"): "the first-work repository description",
    ("ownership", "compact"): "work another session owns",
    ("apply", "bundle"): "Apply this bundle only if replaces_activation_id matches",
    ("apply", "bundle-pointer"): "instruction_revision",
    ("finish", "bundle"): "finish each answer with that footer and log_interaction",
    ("finish", "redaction"): "the redactions before logging",
    ("history-errors", "compact"): "history errors",
    ("progress", "compact"): "progress messages",
    ("delegation", "compact-request"): "the file-receipt delegation exchange",
    ("delegation", "compact"): "the file-receipt delegation exchange",
    ("artifacts", "compact"): "artifacts",
    ("output-artifacts", "compact"): "artifacts",
    ("intro", "compact"): "the tool roles",
    ("local-inference", "compact"): "local inference",
    ("cooperation", "compact"): "cooperation",
}


def test_the_pointer_names_every_rule_the_full_instruction_carries():
    assert set(protocol_text.BUNDLE_PARTS) == set(BUNDLE_TOPICS)
    # The instruction is exactly these parts, so a section composed some other way fails too.
    assert protocol_text.bundle_instruction() == " ".join(
        " ".join(protocol_text.part(*name).split()) for name in protocol_text.BUNDLE_PARTS)
    for name, topic in BUNDLE_TOPICS.items():
        assert topic in persona.INSTRUCTION_POINTER, (name, topic)
    # #509: the delegation the pointer names is the file-receipt exchange.
    assert "The default receipt names a private package file" in protocol_text.part("delegation", "compact")


def test_the_full_instruction_says_a_later_success_points_to_it():
    full = persona.APPLY_INSTRUCTION
    for phrase in ("A later SUCCESS of this conversation carries a short instruction", "instruction_revision",
                   "force_reload=True", "warnings about outdated managed routing sections"):
        assert phrase in full, phrase


@pytest.mark.asyncio
async def test_route_required_candidates_omit_the_trigger_command():
    response = json.loads(await persona.route_persona(Router(), "a substantive question", [], None, lambda q: False))
    assert response["status"] == "ROUTE_REQUIRED"
    assert response["candidates"] == [{"name": "lawyer", "display_name": "Lawyer", "role": "Legal expert"},
                                      {"name": "dba", "display_name": "Database Admin", "role": "SQL"}]


@pytest.mark.parametrize("content,state", [
    (CURRENT, "current"), (CURRENT.replace("\n", "\r\n"), "current"), (OUTDATED, "outdated"), (LEGACY, "outdated"),
    ("# My notes\n", None), (f"{BEGIN}\nno end marker\n", None),
])
def test_section_state(tmp_path, content, state):
    path = tmp_path / "CLAUDE.md"
    path.write_bytes(content.encode("utf-8"))
    assert ci.section_state(path) == state


def test_section_state_ignores_missing_oversized_and_unreadable_files(tmp_path):
    assert ci.section_state(tmp_path / "missing.md") is None
    assert ci.section_state(tmp_path) is None
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * (ci.MAX_INSTRUCTION_FILE_BYTES + 1))
    assert ci.section_state(big) is None
    locked = tmp_path / "locked.md"
    locked.write_text(CURRENT, encoding="utf-8")
    os.chmod(locked, 0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("permissions are not enforced for this user")
        assert ci.section_state(locked) is None
    finally:
        os.chmod(locked, 0o644)


def test_a_file_newer_than_the_running_server_asks_for_a_restart(tmp_path, monkeypatch, homes):
    newer = "# Agents-Core Persona Protocol — version 2\n\nA newer committed protocol.\n"
    committed = tmp_path / "committed-template.md"
    committed.write_text(newer, encoding="utf-8")
    monkeypatch.setattr(protocol_text, "TEMPLATE", committed)
    path = tmp_path / "CLAUDE.md"
    path.write_text(managed(newer), encoding="utf-8")
    assert ci.section_state(path) == "daemon-outdated"
    warnings = ci.drift_warnings(context(tmp_path, "claude-code"))
    assert len(warnings) == 1 and "restart the server" in warnings[0]


def context(root, client, error=None, grants=()):
    return SimpleNamespace(root=root, client=client, error=error, cooperation_roots=tuple(grants))


@pytest.mark.parametrize("client,name,content,pointer", [
    ("claude-code", "CLAUDE.md", CURRENT, False),  # Claude Code shows a fixed prefix of either text
    ("codex", "AGENTS.md", CURRENT, True),
    ("codex", "AGENTS.md", OUTDATED, False),
    ("claude-code", "CLAUDE.md", OUTDATED, False),
    ("claude-code", "AGENTS.md", CURRENT, False),  # Claude Code does not load AGENTS.md
    ("cursor", "CLAUDE.md", CURRENT, False),
    ("stdio", "AGENTS.md", CURRENT, False),  # a stdio bridge may be any client
    (None, "AGENTS.md", CURRENT, False),
])
def test_initialization_points_only_to_the_workspace_file_the_client_loads(tmp_path, client, name, content, pointer):
    (tmp_path / name).write_text(content, encoding="utf-8")
    full = protocol_text.server_instructions()
    expected = protocol_text.server_pointer() if pointer else full
    assert ci.initialization_instructions(full, context(tmp_path, client, grants=[tmp_path])) == expected


def test_an_identity_without_a_known_checkout_keeps_the_full_text(tmp_path):
    (tmp_path / "AGENTS.md").write_text(CURRENT, encoding="utf-8")
    full = protocol_text.server_instructions()
    assert ci.initialization_instructions(full, context(tmp_path, "codex")) == full


def test_initialization_keeps_the_full_text_without_a_workspace(tmp_path):
    (tmp_path / "AGENTS.md").write_text(CURRENT, encoding="utf-8")
    full = protocol_text.server_instructions()
    assert ci.initialization_instructions(full, context(tmp_path, "codex", "workspace_required", [tmp_path])) == full
    assert ci.initialization_instructions(full, context(None, "codex", grants=[tmp_path])) == full
    assert ci.initialization_instructions(full) == full  # stdio: no request context


def test_a_linked_worktree_decides_together_with_its_main_checkout(tmp_path):
    main, worktree = tmp_path / "main", tmp_path / "worktree"
    main.mkdir()
    worktree.mkdir()
    (main / "AGENTS.md").write_text(CURRENT, encoding="utf-8")
    (worktree / "AGENTS.md").write_text(OUTDATED, encoding="utf-8")
    full = protocol_text.server_instructions()
    assert ci.initialization_instructions(full, context(main, "codex", grants=[worktree])) == full
    (worktree / "AGENTS.md").write_text(CURRENT, encoding="utf-8")
    assert ci.initialization_instructions(full, context(main, "codex", grants=[worktree])) == \
        protocol_text.server_pointer()


def test_a_codex_project_override_keeps_the_full_text(tmp_path):
    (tmp_path / "AGENTS.md").write_text(CURRENT, encoding="utf-8")
    (tmp_path / "AGENTS.override.md").write_text("Project rules\n", encoding="utf-8")
    full = protocol_text.server_instructions()
    assert ci.initialization_instructions(full, context(tmp_path, "codex", grants=[tmp_path])) == full
    (tmp_path / "AGENTS.override.md").write_text("  \n", encoding="utf-8")  # an empty override does not count
    assert ci.initialization_instructions(full, context(tmp_path, "codex", grants=[tmp_path])) == \
        protocol_text.server_pointer()


def test_a_codex_section_past_the_project_doc_limit_keeps_the_full_text(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("# Notes\n" + "x" * 300 + "\n\n" + CURRENT, encoding="utf-8")
    full = protocol_text.server_instructions()
    assert ci.initialization_instructions(full, context(tmp_path, "codex", grants=[tmp_path])) == \
        protocol_text.server_pointer()
    monkeypatch.setattr(ci, "CODEX_PROJECT_DOC_MAX_BYTES", 1000)
    assert ci.initialization_instructions(full, context(tmp_path, "codex", grants=[tmp_path])) == full


def git(directory, *args):
    """git in ``directory`` without the user's global configuration or hooks."""
    subprocess.run(["git", "-C", str(directory), "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
                   check=True, capture_output=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})


def test_a_fixed_identity_points_only_when_every_linked_worktree_is_current(tmp_path):
    """A project configuration registers its root without the session's own checkout, so the
    identity's only grant is the main checkout; Codex may still run it in a linked worktree,
    whose own AGENTS.md decides too."""
    main, worktree = tmp_path / "main", tmp_path / "worktree"
    main.mkdir()
    git(main, "init", "-q")
    (main / "AGENTS.md").write_text(CURRENT, encoding="utf-8")
    git(main, "add", "AGENTS.md")
    git(main, "commit", "-qm", "managed section")
    git(main, "worktree", "add", "-q", "--detach", str(worktree))
    (worktree / "AGENTS.md").write_text(OUTDATED, encoding="utf-8")
    registry = WorkspaceRegistry(tmp_path / "service")
    identity = registry.register(main)  # as ClientMigration.prepare does for a project layer
    grants = registry.cooperation_roots(identity)
    assert grants == (main.resolve(),)
    fixed = context(registry.resolve(identity), "codex", grants=grants)
    full = protocol_text.server_instructions()
    assert ci.initialization_instructions(full, fixed) == full
    (worktree / "AGENTS.md").unlink()
    assert ci.initialization_instructions(full, fixed) == full
    (worktree / "AGENTS.md").write_text(CURRENT, encoding="utf-8")
    assert ci.initialization_instructions(full, fixed) == protocol_text.server_pointer()
    # From the worktree's side, its main checkout counts too.
    (main / "AGENTS.md").write_text(OUTDATED, encoding="utf-8")
    assert ci.initialization_instructions(full, context(worktree.resolve(), "codex", grants=[worktree.resolve()])) == full


def handmade_repository(tmp_path, relative=False):
    """A main checkout and one linked worktree as git lays them out, both with the current section.

    The worktree's entry names its .git file by an absolute path, or by one relative to the entry
    as ``worktree.useRelativePaths`` writes it.
    """
    main, worktree = tmp_path / "main", tmp_path / "worktree"
    entry = main / ".git" / "worktrees" / "worktree"
    entry.mkdir(parents=True)
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {entry}\n", encoding="utf-8")
    (entry / "commondir").write_text("../..\n", encoding="utf-8")
    (entry / "gitdir").write_text("../../../../worktree/.git\n" if relative else f"{worktree / '.git'}\n",
                                  encoding="utf-8")
    for checkout in (main, worktree):
        (checkout / "AGENTS.md").write_text(CURRENT, encoding="utf-8")
    return main, worktree, entry


def test_linked_worktrees_are_listed_by_absolute_or_relative_gitdir_and_gone_ones_are_skipped(tmp_path):
    main, worktree, entry = handmade_repository(tmp_path, relative=True)
    assert ci._session_checkouts([main]) == [main, worktree.resolve()]
    assert ci._session_checkouts([worktree]) == [worktree, main.resolve()]
    # A subdirectory registered as the root counts with the same subdirectory of each worktree.
    (main / "sub").mkdir()
    (worktree / "sub").mkdir()
    assert ci._session_checkouts([main / "sub"]) == [main / "sub", worktree.resolve() / "sub"]
    (entry / "gitdir").write_text(f"{tmp_path / 'gone' / '.git'}\n", encoding="utf-8")  # a removed worktree
    assert ci._session_checkouts([main]) == [main]
    (main / ".git" / "worktrees" / ".DS_Store").write_bytes(b"not a worktree")
    assert ci._session_checkouts([main]) == [main]
    assert ci._session_checkouts([tmp_path / "plain"]) == [tmp_path / "plain"]  # no repository: itself only


@pytest.mark.parametrize("damage", ["no gitdir", "too many", "bad .git file"])
def test_worktrees_that_cannot_be_listed_keep_the_full_text(tmp_path, monkeypatch, damage):
    main, worktree, entry = handmade_repository(tmp_path)
    full = protocol_text.server_instructions()
    assert ci.initialization_instructions(full, context(main, "codex", grants=[main])) == protocol_text.server_pointer()
    root = main
    if damage == "no gitdir":
        (entry / "gitdir").unlink()
    elif damage == "too many":
        monkeypatch.setattr(ci, "MAX_LINKED_WORKTREES", 0)
    else:
        (worktree / ".git").write_text("not a git pointer\n", encoding="utf-8")
        root = worktree
    assert ci._session_checkouts([root]) is None
    assert ci.initialization_instructions(full, context(root, "codex", grants=[root])) == full


def test_a_quoted_end_marker_before_the_section_does_not_shorten_it(tmp_path):
    """The section ends at the end marker after its begin marker, so Codex's byte limit is checked
    against the real end (#433 review)."""
    text = f"The managed section ends at `{END}`.\n" + "x" * 40_000 + "\n\n" + CURRENT
    path = tmp_path / "AGENTS.md"
    path.write_text(text, encoding="utf-8")
    info = ci.section_info(path)
    data = text.encode("utf-8")
    assert info.state == "current" and info.end == data.rindex(END.encode("utf-8")) + len(END.encode("utf-8"))
    assert info.end > ci.CODEX_PROJECT_DOC_MAX_BYTES
    full = protocol_text.server_instructions()
    assert ci.initialization_instructions(full, context(tmp_path, "codex", grants=[tmp_path])) == full


def test_the_override_check_reads_a_bounded_prefix_once_per_file_version(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "MAX_INSTRUCTION_FILE_BYTES", 64)
    reads = []

    def counting_open(path, *args, **kwargs):
        reads.append(str(path))
        return open(path, *args, **kwargs)
    monkeypatch.setattr(ci, "open", counting_open, raising=False)
    override = tmp_path / "AGENTS.override.md"
    override.write_bytes(b" " * 65)  # larger than a managed file: counts as nonempty, unread
    assert ci._nonempty(override) and reads == []
    override.write_text("Project rules\n", encoding="utf-8")
    assert ci._nonempty(override) and ci._nonempty(override)
    assert reads == [str(override)]
    override.write_text("   \n", encoding="utf-8")  # a new version is read again
    assert not ci._nonempty(override) and reads == [str(override)] * 2
    assert not ci._nonempty(tmp_path / "missing.md")


def test_a_failing_pointer_render_keeps_the_full_text(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text(CURRENT, encoding="utf-8")
    full = protocol_text.server_instructions()

    def broken():
        raise protocol_text.ProtocolSourceError("the protocol source has no variant pointer:server")
    monkeypatch.setattr(protocol_text, "server_pointer", broken)
    session = context(tmp_path, "codex", grants=[tmp_path])
    assert ci.initialization_instructions(full, session) == full
    server = FastMCP("test", instructions=full)
    ci.install_initialization_pointer(server._mcp_server)
    token = ci.REQUEST_CLIENT.set(session)
    try:
        assert server._mcp_server.create_initialization_options().instructions == full
    finally:
        ci.REQUEST_CLIENT.reset(token)


def test_a_failing_check_keeps_the_full_text(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text(CURRENT, encoding="utf-8")
    monkeypatch.setattr(ci, "section_info", lambda path: (_ for _ in ()).throw(RuntimeError("boom")))
    full = protocol_text.server_instructions()
    assert ci.initialization_instructions(full, context(tmp_path, "codex", grants=[tmp_path])) == full


def test_the_server_pointer_keeps_the_opening_and_the_rules_only_the_server_states():
    pointer, full = protocol_text.server_pointer(), protocol_text.server_instructions()
    opening, tail = full.split("\n\n", 1)[0], full.rsplit("\n\n", 1)[1]
    assert pointer.startswith(opening + "\n\n") and pointer.endswith("\n\n" + tail)
    for name in ("tools", "flows", "compatibility"):
        assert protocol_text.part(name, "server") in tail
    assert protocol_text.template_revision() in pointer and "{revision}" not in pointer
    assert "force_reload=True" in pointer and len(pointer) < len(full) / 2
    # Everything between the opening and the tail is in the template, and the pointer names it.
    for name, variant in (("delegation", "compact-request"), ("delegation", "compact"), ("artifacts", "compact"),
                          ("local-inference", "compact"), ("output-artifacts", "compact"),
                          ("cooperation", "compact"), ("progress", "compact"), ("first-work", "compact"),
                          ("ownership", "compact"), ("decide", "server"), ("apply", "server")):
        assert protocol_text.part(name, variant) not in pointer, (name, variant)
    for topic in ("the delegation exchange", "artifacts", "local inference", "cooperation", "the keep/switch decision",
                  "response statuses", "the first-work repository description", "work another session owns",
                  "progress messages"):
        assert topic in pointer, topic
    for heading in ("## Delegated worker mode", "## Cooperating on intersecting work", "## Decide locally",
                    "## Apply a complete response", "## Describe a repository on first work",
                    "## Work that another session owns", "## Progress messages"):
        assert heading in protocol_text.template(), heading


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return home


def test_drift_warnings_name_each_outdated_section_of_the_clients_files(tmp_path, homes):
    root = tmp_path / "project"
    root.mkdir()
    files = {root / "CLAUDE.md": OUTDATED, root / "AGENTS.md": CURRENT,
             homes / ".claude" / "CLAUDE.md": OUTDATED, homes / ".codex" / "AGENTS.md": OUTDATED}
    for path, content in files.items():
        path.write_text(content, encoding="utf-8")
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
    claude = ci.drift_warnings(context(root, "claude-code"))
    assert len(claude) == 2
    assert claude[0].startswith(f"The managed routing section of {root / 'CLAUDE.md'} differs")
    assert "inject_claude_md.py" in claude[0] and str(root / "CLAUDE.md") in claude[0]
    assert claude[1].startswith("The managed routing section of ~/.claude/CLAUDE.md differs")
    assert "install_instructions.py" in claude[1]
    assert protocol_text.template_revision() in claude[0]
    codex = ci.drift_warnings(context(root, "codex"))
    assert len(codex) == 1 and "~/.codex/AGENTS.md" in codex[0]
    assert len(ci.drift_warnings(context(root, None))) == 3
    assert len(ci.drift_warnings(context(root, "stdio"))) == 3  # a stdio bridge may be any client
    assert ci.drift_warnings(context(root, "cursor")) == []
    assert ci.drift_warnings(context(root, "claude-desktop")) == []
    # The check only reads: no file changed.
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files} == before


def test_current_and_missing_sections_give_no_warnings(tmp_path, homes):
    root = tmp_path / "project"
    root.mkdir()
    assert ci.drift_warnings(context(root, "claude-code")) == []  # no files at all
    (root / "CLAUDE.md").write_text("# Notes without a managed section\n", encoding="utf-8")
    (homes / ".claude" / "CLAUDE.md").write_text(CURRENT, encoding="utf-8")
    assert ci.drift_warnings(context(root, "claude-code")) == []


def test_codex_override_file_replaces_its_agents_file_only_when_nonempty(tmp_path, homes):
    (homes / ".codex" / "AGENTS.md").write_text(OUTDATED, encoding="utf-8")
    override = homes / ".codex" / "AGENTS.override.md"
    override.write_text(CURRENT, encoding="utf-8")
    assert ci.drift_warnings(context(None, "codex")) == []
    override.write_text("  \n", encoding="utf-8")
    warnings = ci.drift_warnings(context(None, "codex"))
    assert len(warnings) == 1 and "~/.codex/AGENTS.md" in warnings[0]


def test_warnings_leave_out_other_sessions_worktrees(tmp_path, homes):
    main, other = tmp_path / "main", tmp_path / "other"
    main.mkdir()
    other.mkdir()
    (main / "CLAUDE.md").write_text(CURRENT, encoding="utf-8")
    (other / "CLAUDE.md").write_text(OUTDATED, encoding="utf-8")
    assert ci.drift_warnings(context(main, "claude-code", grants=[other])) == []


def test_a_codex_project_override_is_the_file_checked_for_drift(tmp_path, homes):
    (tmp_path / "AGENTS.md").write_text(CURRENT, encoding="utf-8")
    (tmp_path / "AGENTS.override.md").write_text(OUTDATED, encoding="utf-8")
    warnings = ci.drift_warnings(context(tmp_path, "codex"))
    assert len(warnings) == 1 and "AGENTS.override.md" in warnings[0]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell quoting and interpreter path")
@pytest.mark.parametrize("kind", ["installation", "global", "repository"])
def test_refresh_commands_name_the_installation_and_quote_paths(tmp_path, homes, monkeypatch, kind):
    installation = tmp_path / "my install"
    installation.mkdir()
    monkeypatch.setattr(ci, "installation_root", lambda: installation)
    path = {"installation": installation / "CLAUDE.md", "global": homes / ".claude" / "CLAUDE.md",
            "repository": tmp_path / "a project" / "CLAUDE.md"}[kind]
    command = ci._refresh_command(path)
    argv = shlex.split(command.split(", run in ")[0])
    assert argv[0] == str(installation / ".venv" / "bin" / "python")
    expected = {"installation": ["-m", "src.engine.protocol_text", "--write"],
                "global": [str(installation / "scripts" / "install_instructions.py")],
                "repository": [str(installation / "scripts/_helpers/inject_claude_md.py"), str(path),
                               str(installation / "scripts/templates/routing-protocol-core.md")]}[kind]
    assert argv[1:] == expected
    if kind == "installation":
        assert command.endswith(f", run in {installation}")


@pytest.mark.asyncio
async def test_first_activation_carries_the_warnings_and_later_ones_do_not(monkeypatch):
    from src import server
    monkeypatch.setattr(ci, "drift_warnings", lambda context=None: ["outdated"])
    first = json.dumps({"status": "SUCCESS", "replaces_activation_id": None, "instruction": "x"})
    assert json.loads(await server._with_drift_warnings(first))["warnings"] == ["outdated"]
    later = json.dumps({"status": "SUCCESS", "replaces_activation_id": "a"})
    assert await server._with_drift_warnings(later) == later
    route = json.dumps({"status": "ROUTE_REQUIRED", "replaces_activation_id": None})
    assert await server._with_drift_warnings(route) == route
    assert await server._with_drift_warnings("not json") == "not json"
    monkeypatch.setattr(ci, "drift_warnings", lambda context=None: [])
    assert await server._with_drift_warnings(first) == first
    monkeypatch.setattr(ci, "drift_warnings", lambda context=None: (_ for _ in ()).throw(OSError("boom")))
    assert await server._with_drift_warnings(first) == first


@pytest.mark.asyncio
async def test_the_persona_tools_attach_warnings_to_a_first_activation_only(bundle, monkeypatch):
    from src import server
    monkeypatch.setattr(server, "_readiness_problem", AsyncMock(return_value=None))
    monkeypatch.setattr(server.router, "update_cache", AsyncMock())
    monkeypatch.setattr(ci, "drift_warnings", lambda context=None: ["outdated section"])
    first = json.loads(await server.get_agent_context("lawyer", "task", protocol_version=2))
    assert first["warnings"] == ["outdated section"] and first["instruction"] == persona.APPLY_INSTRUCTION
    switch = json.loads(await server.get_agent_context("dba", "task", protocol_version=2,
                                                       current_persona=first["persona"]))
    assert "warnings" not in switch and switch["instruction"] == persona.INSTRUCTION_POINTER
    restore = json.loads(await server.get_agent_context("dba", "task", protocol_version=2,
                                                        current_persona=switch["persona"], force_reload=True))
    assert "warnings" not in restore and restore["instruction"] == persona.APPLY_INSTRUCTION
    refresh = json.loads(await server.refresh_persona_context("changed", current_persona=restore["persona"]))
    assert refresh["status"] == "SUCCESS" and "warnings" not in refresh
    assert refresh["instruction"] == persona.INSTRUCTION_POINTER
    monkeypatch.setattr(server.router, "lookup_cache", AsyncMock(return_value=None))
    routed = json.loads(await server.route_and_load("a substantive question", protocol_version=2))
    assert routed["status"] == "ROUTE_REQUIRED" and "warnings" not in routed
    assert routed["candidates"] and all(set(candidate) <= set(persona.CANDIDATE_FIELDS)
                                        for candidate in routed["candidates"])


@pytest.mark.asyncio
async def test_a_first_activation_names_the_outdated_file_of_the_request_and_its_command(tmp_path, homes, bundle,
                                                                                        monkeypatch):
    from src import server
    monkeypatch.setattr(ci, "drift_warnings", REAL_DRIFT_WARNINGS)
    monkeypatch.setattr(server, "_readiness_problem", AsyncMock(return_value=None))
    monkeypatch.setattr(server.router, "update_cache", AsyncMock())
    root = tmp_path / "project"
    root.mkdir()
    path = root / "CLAUDE.md"
    path.write_text(OUTDATED, encoding="utf-8")
    before = path.read_bytes(), path.stat().st_mtime_ns
    token = ci.REQUEST_CLIENT.set(context(root, "claude-code"))  # as the daemon sets it for a request
    try:
        first = json.loads(await server.get_agent_context("lawyer", "task", protocol_version=2))
        later = json.loads(await server.get_agent_context("dba", "task", protocol_version=2,
                                                          current_persona=first["persona"]))
    finally:
        ci.REQUEST_CLIENT.reset(token)
    assert first["status"] == "SUCCESS" and len(first["warnings"]) == 1
    warning = first["warnings"][0]
    assert warning.startswith(f"The managed routing section of {path} differs from this installation's protocol")
    assert "inject_claude_md.py" in warning and protocol_text.template_revision() in warning
    assert "warnings" not in later
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.asyncio
async def test_the_ask_prompt_and_a_cached_route_carry_the_warnings(bundle, monkeypatch):
    from src import server
    monkeypatch.setattr(server, "_readiness_problem", AsyncMock(return_value=None))
    monkeypatch.setattr(server.router, "update_cache", AsyncMock())
    monkeypatch.setattr(ci, "drift_warnings", lambda context=None: ["outdated section"])
    monkeypatch.setattr(server.router, "lookup_cache", AsyncMock(return_value=SimpleNamespace(target_agent="lawyer")))
    monkeypatch.setattr(server.router, "keyword_veto", lambda query, agent: None)
    routed = json.loads(await server.route_and_load("a contract question", protocol_version=2))
    assert routed["status"] == "SUCCESS" and routed["warnings"] == ["outdated section"]
    assert routed["instruction"] == persona.APPLY_INSTRUCTION
    other = {**routed["persona"], "agent": "dba"}
    rerouted = json.loads(await server.route_and_load("a contract question", protocol_version=2,
                                                      current_persona=other))
    assert rerouted["instruction"] == persona.INSTRUCTION_POINTER and "warnings" not in rerouted
    messages = await server.ask("a contract question")
    assert "outdated section" in messages[0].content.text
    switched = await server.ask("a contract question", current_persona=json.dumps(other))
    assert "outdated section" not in switched[0].content.text


@pytest.mark.skipif(sys.platform == "win32", reason="the project directory check differs on Windows")
def test_current_client_over_stdio_is_the_process_workspace(tmp_path, monkeypatch, homes):
    """The conftest fixture replaces drift_warnings for calls without a context; this one calls the
    real check, which reads the stdio process's own workspace through current_client."""
    from src.engine import config
    project = tmp_path / "project"
    project.mkdir()
    (project / "CLAUDE.md").write_text(OUTDATED, encoding="utf-8")
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(project))
    config._reset_client_repo_root_cache()
    try:
        current = ci.current_client()
        assert current.transport == "stdio" and current.client is None  # a stdio server may serve any client
        assert current.root == project.resolve() and current.error is None
        warnings = REAL_DRIFT_WARNINGS()
        assert len(warnings) == 1 and warnings[0].startswith(f"The managed routing section of {project.resolve()}")
        monkeypatch.setenv("AGENTS_TRANSPORT", "http")  # the daemon: a call outside a request has no client
        assert ci.current_client() is None
    finally:
        config._reset_client_repo_root_cache()


def pointer_runtime(port):
    mcp = FastMCP("test", host="127.0.0.1", port=port, stateless_http=True, json_response=True,
                  instructions=protocol_text.server_instructions())
    ci.install_initialization_pointer(mcp._mcp_server)

    @mcp.tool()
    def session_client() -> str:
        """The client context a tool call sees, as the persona tools' drift check reads it."""
        current = ci.current_client()
        return json.dumps({"client": current.client, "root": str(current.root)})
    return mcp, None


@pytest.mark.asyncio
async def test_daemon_initialize_answers_with_the_pointer_for_a_current_workspace_file(tmp_path):
    current, outdated = tmp_path / "current", tmp_path / "outdated"
    current.mkdir()
    outdated.mkdir()
    (current / "AGENTS.md").write_text(CURRENT, encoding="utf-8")
    (outdated / "AGENTS.md").write_text(OUTDATED, encoding="utf-8")
    registry = WorkspaceRegistry(tmp_path / "service")
    # Registered as an auto-registering bridge does: with the session's physical checkout.
    ids = {"current": registry.register(current, physical_root=current),
           "outdated": registry.register(outdated, physical_root=outdated)}
    app = create_app(registry.directory, TOKEN, runtime_loader=pointer_runtime)
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "codex", "version": "1"}}}
    async with app.router.lifespan_context(app):
        for _ in range(200):
            if app.state.service.state != "starting":
                break
            await asyncio.sleep(.01)
        assert app.state.service.state == "ready"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as http:
            headers = {"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream"}

            def named(workspace, client):
                return {**headers, "X-Agents-Workspace": ids[workspace],
                        **({"X-Agents-Client": client} if client else {})}

            async def instructions(workspace, client):
                response = await http.post("/mcp", json=initialize, headers=named(workspace, client))
                return response.json()["result"]["instructions"]
            assert await instructions("current", "codex") == protocol_text.server_pointer()
            assert await instructions("outdated", "codex") == protocol_text.server_instructions()
            assert await instructions("current", "claude-code") == protocol_text.server_instructions()
            assert await instructions("current", "cursor") == protocol_text.server_instructions()
            assert await instructions("current", None) == protocol_text.server_instructions()
            call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "session_client", "arguments": {}}}
            response = await http.post("/mcp", json=call, headers=named("current", "codex"))
            seen = json.loads(response.json()["result"]["content"][0]["text"])
            assert seen == {"client": "codex", "root": str(current.resolve())}
    assert ci.REQUEST_CLIENT.get() is None


@pytest.mark.asyncio
async def test_concurrent_requests_each_see_their_own_client(tmp_path):
    """Two requests in flight together, with different X-Agents-Client values and workspaces, each
    read their own REQUEST_CLIENT, also from a worker thread, and leave none behind."""
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    registry = WorkspaceRegistry(tmp_path / "service")
    ids = {first: registry.register(first), second: registry.register(second)}
    entered, both = [], asyncio.Event()

    def runtime(port):
        mcp = FastMCP("test", host="127.0.0.1", port=port, stateless_http=True, json_response=True,
                      instructions=protocol_text.server_instructions())
        ci.install_initialization_pointer(mcp._mcp_server)

        @mcp.tool()
        async def who() -> str:
            before = ci.current_client()
            entered.append(before.client)
            if len(entered) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 10)  # both requests are in flight now
            after = ci.current_client()
            in_thread = await asyncio.to_thread(lambda: ci.current_client().client)
            return json.dumps({"before": [before.client, str(before.root)], "after": [after.client, str(after.root)],
                               "thread": in_thread})
        return mcp, None
    app = create_app(registry.directory, TOKEN, runtime_loader=runtime)
    async with app.router.lifespan_context(app):
        for _ in range(200):
            if app.state.service.state != "starting":
                break
            await asyncio.sleep(.01)
        assert app.state.service.state == "ready"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as http:
            async def call(workspace, client):
                headers = {"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream",
                           "X-Agents-Workspace": ids[workspace], "X-Agents-Client": client}
                body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "who", "arguments": {}}}
                response = await http.post("/mcp", json=body, headers=headers)
                return json.loads(response.json()["result"]["content"][0]["text"])
            codex, claude = await asyncio.gather(call(first, "codex"), call(second, "claude-code"))
    assert sorted(entered) == ["claude-code", "codex"]
    assert codex == {"before": ["codex", str(first.resolve())], "after": ["codex", str(first.resolve())],
                     "thread": "codex"}
    assert claude == {"before": ["claude-code", str(second.resolve())],
                      "after": ["claude-code", str(second.resolve())], "thread": "claude-code"}
    assert ci.REQUEST_CLIENT.get() is None
