"""The execution capability table: which effort values each client takes (design 6.4, #347).

A stage session or a delegated step runs with a client (``claude`` or ``codex``), a model and an
effort (design 6.1). This module is the minimal table of what the installed CLIs accept, for the
run sequence (#347), the settings page (#348) and ``set_flow_execution`` (#351):

* **Claude** takes ``--effort <level>``; `parse_claude_efforts` reads the allowed levels from the
  ``--effort`` entry of ``claude --help`` (2.1.296 prints ``(low, medium, high, xhigh, max)`` on
  the continuation line). `CLAUDE_EFFORTS` is that list as last read from the source, used when
  the CLI is missing or its help has no such entry.
* **Codex** takes any non-empty ``-c model_reasoning_effort=<value>`` and sends it to the API, and
  ``codex exec --help`` lists no values, so `CODEX_EFFORTS` is the list codex-cli 0.162.1 knows
  from its source (``ReasoningEffort::from_str``, #344). It counts as ``installed`` only for a
  version the list was derived from (`codex_sandbox.VERIFIED_VERSIONS`).

`installed` builds the table from the CLIs on this machine (``--help`` and ``--version`` only, with
a fresh empty ``HOME``: no login, no configuration, no model turn), `check_effort` refuses a value
the client does not take with ``flow_invalid`` and the allowed values, and `check_model` checks a
model name's form and warns about a Claude alias it does not know. The module imports nothing
from `src.schedules` at load time, so `src.schedules.config` and `worker_launch` take their effort
lists from here (the run's drift check covers this file through those imports).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
import re
import subprocess
import tempfile
from typing import Literal

CLAUDE, CODEX = "claude", "codex"
CLIENTS = (CLAUDE, CODEX)
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")  # claude --help of 2.1.296
# model_reasoning_effort values codex-cli 0.162.1 knows (research, source at tag rust-v0.162.1:
# ReasoningEffort::from_str). Codex itself accepts any non-empty string and sends it to the API.
CODEX_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "persistent")
STATIC_EFFORTS = {CLAUDE: CLAUDE_EFFORTS, CODEX: CODEX_EFFORTS}
# Aliases ``claude --help`` names for the latest models (2.1.296: 'fable', 'opus', 'sonnet'), plus haiku.
CLAUDE_ALIASES = frozenset({"fable", "opus", "sonnet", "haiku"})
# A model name as both CLIs take it on the command line (``sandbox.argv`` checks the same form).
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}")
HELP_TIMEOUT = 30
HELP_LIMIT = 256 * 1024
_OPTION = re.compile(r"^\s{1,8}-")  # an option entry of the help text (``  --x``, ``  -n, --name``)
_EFFORT_ENTRY = re.compile(r"^\s*--effort\b")
_LEVELS = re.compile(r"\(([a-z]+(?:\s*,\s*[a-z]+)+)\)")
_VERSION = re.compile(r"\b(\d+\.\d+\.\d+)\b")

Source = Literal["installed", "static"]
CommandRunner = Callable[..., subprocess.CompletedProcess]


class FlowInvalid(ValueError):
    """An execution value the client does not take; ``code`` is ``flow_invalid`` and ``allowed``
    lists the values it takes (design 6.4)."""

    def __init__(self, message: str, allowed: tuple[str, ...] | list[str] = ()):
        super().__init__(f"flow_invalid: {message}")
        self.code = "flow_invalid"
        self.allowed = list(allowed)


@dataclass(frozen=True)
class Capability:
    """What one client takes on this machine. ``source`` is ``installed`` when the effort list
    was read from (Claude) or derived for (Codex) the installed CLI, ``static`` when it is the
    built-in list because the CLI is missing, failed, or is a version the list does not cover."""

    client: str
    efforts: tuple[str, ...]
    version: str | None
    source: Source

    def to_dict(self) -> dict:
        return {"client": self.client, "efforts": list(self.efforts), "version": self.version, "source": self.source}


def parse_claude_efforts(help_text: str) -> tuple[str, ...] | None:
    """The levels of the ``--effort`` entry of ``claude --help``: the parenthesized list in the
    entry, which may continue over the following indented lines until the next option; None when
    there is no such entry or list."""
    lines = help_text.splitlines()
    for index, line in enumerate(lines):
        if not _EFFORT_ENTRY.match(line):
            continue
        entry = [line]
        for following in lines[index + 1:]:
            if _OPTION.match(following) or not following.strip():
                break
            entry.append(following)
        match = _LEVELS.search(" ".join(part.strip() for part in entry))
        if match is None:
            return None
        levels = tuple(level.strip() for level in match[1].split(","))
        return levels if len(set(levels)) == len(levels) else None
    return None


def _run(argv: list[str], path: str, runner: CommandRunner) -> str | None:
    """Standard output of ``argv`` (``--help`` or ``--version``) with a fresh empty ``HOME``, or None."""
    with tempfile.TemporaryDirectory(prefix="flow-execution-") as home:
        environment = {"PATH": path, "HOME": home, "LC_ALL": "C"}
        try:
            result = runner(argv, capture_output=True, text=True, env=environment, stdin=subprocess.DEVNULL,
                            timeout=HELP_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
    if result.returncode != 0 or not isinstance(result.stdout, str):
        return None
    return result.stdout[:HELP_LIMIT]


def _claude(claude: str | None, path: str, runner: CommandRunner) -> Capability:
    if not claude or not os.path.isabs(claude):
        return Capability(CLAUDE, CLAUDE_EFFORTS, None, "static")
    match = _VERSION.search(_run([claude, "--version"], path, runner) or "")
    version = match[1] if match else None
    help_text = _run([claude, "--help"], path, runner)
    efforts = parse_claude_efforts(help_text) if help_text else None
    if efforts is None:
        return Capability(CLAUDE, CLAUDE_EFFORTS, version, "static")
    return Capability(CLAUDE, efforts, version, "installed")


def _codex(codex: str | None, path: str, runner: CommandRunner) -> Capability:
    if not codex or not os.path.isabs(codex):
        return Capability(CODEX, CODEX_EFFORTS, None, "static")
    # Imported here: src.schedules.config imports this module, and codex_probes imports config.
    from src.schedules import codex_probes
    from src.schedules import codex_sandbox
    version = codex_probes.installed_version(codex, path=path, command_runner=runner)
    source: Source = "installed" if version in codex_sandbox.VERIFIED_VERSIONS else "static"
    return Capability(CODEX, CODEX_EFFORTS, version, source)


def installed(claude: str | None, codex: str | None, *, path: str,
              runner: CommandRunner = subprocess.run) -> dict[str, Capability]:
    """The table for the CLIs at ``claude`` and ``codex`` (absolute paths, or None when absent),
    run with the explicit ``path``; every failure falls back to the built-in lists."""
    return {CLAUDE: _claude(claude, path, runner), CODEX: _codex(codex, path, runner)}


def static() -> dict[str, Capability]:
    """The built-in table, without running anything."""
    return {client: Capability(client, efforts, None, "static") for client, efforts in STATIC_EFFORTS.items()}


def check_effort(client: str, effort: str | None, table: Mapping[str, Capability] | None = None) -> None:
    """Refuse an effort ``client`` does not take (`FlowInvalid` with the allowed values); None
    means the CLI's default and passes. ``table`` defaults to `static`."""
    if client not in CLIENTS:
        raise FlowInvalid(f"unknown client {client!r}", CLIENTS)
    if effort is None:
        return
    capability = (table or static()).get(client) or static()[client]
    if not isinstance(effort, str) or effort not in capability.efforts:
        raise FlowInvalid(f"{client} does not take the effort {effort!r}", capability.efforts)


def check_model(model: str | None, client: str = CLAUDE) -> list[str]:
    """Warnings about a model name (an unknown Claude alias); `FlowInvalid` for a name that is
    not a plain name either CLI takes on its command line. None means the CLI's default."""
    if client not in CLIENTS:
        raise FlowInvalid(f"unknown client {client!r}", CLIENTS)
    if model is None:
        return []
    if not isinstance(model, str) or not MODEL_NAME.fullmatch(model):
        raise FlowInvalid(f"the model {str(model)[:80]!r} is not a plain model name")
    if client == CLAUDE and model not in CLAUDE_ALIASES and not model.startswith("claude-"):
        return [f"{model!r} is neither a known Claude alias ({', '.join(sorted(CLAUDE_ALIASES))}) nor a full "
                "claude-* model name; the CLI decides whether it exists"]
    return []
