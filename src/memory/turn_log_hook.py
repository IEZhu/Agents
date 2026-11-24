"""Claude Code Stop hook that logs the turn it ends: ``python -m src.memory.turn_log_hook`` (#734).

A headless run that logs through ``log_interaction`` pays twice for every answer: the model
writes the whole answer again as ``response_content``, and one more model call re-reads the
whole context after the tool returns. With ``AGENTS_TURN_LOG_HOOK=1`` and the headless profile
(``config.turn_log_hook()``) the server tells the model not to log (``finish`` variants ``hook``,
``server-hook`` and ``bundle-hook`` in ``src/engine/protocol/finish.md``), and this hook writes
the entry instead, from the hook input and the session transcript, with no model call and no
network.

The entry is the one ``log_interaction`` writes with its defaults: the prompt that started the
turn as intent, ``Agent: <agent>`` and the persona attribution line as action, the final answer
without a time line as outcome, through the same ``HistoryWriter``, its content-hash
deduplication and the same project resolution. The persona is the descriptor of the last
``SUCCESS`` or ``NO_CHANGE`` of ``route_and_load``, ``get_agent_context`` or
``refresh_persona_context`` in the transcript. Token-like secrets are masked; nothing else is
redacted, because no model reads the text first.

Without both switches, or without a prompt, an answer, a persona or a safe project, nothing is
written. The hook always exits 0 and prints nothing to stdout: a failure leaves the turn
unlogged and never blocks or disturbs the client.
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime
import io
import json
import os
from pathlib import Path
import re
import sys
import uuid

from src.engine import config
from src.engine.config import ClientRootError

# The hook input carries the last assistant message; a larger input is not a Stop event.
MAX_INPUT_BYTES = 8 * 1024 * 1024
# A headless run's transcript is far smaller; a larger one is left unlogged rather than parsed.
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
PERSONA_TOOLS = ("route_and_load", "get_agent_context", "refresh_persona_context")
PERSONA_STATUSES = ("SUCCESS", "NO_CHANGE")
# What Claude Code places before a typed prompt, and user rows it writes itself.
LEADING_REMINDERS = re.compile(r"\s*(?:<system-reminder>.*?</system-reminder>\s*)+", re.DOTALL)
HOST_ROW = re.compile(r"Stop hook (?:feedback|blocking error)|\[Request interrupted by user")
# The line log_interaction's timestamp would add (``YYYY.MM.DD HH:MM:SS``), never part of the outcome.
TIME_LINE = re.compile(r"\A\s*\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}[ \t]*\r?\n")


@dataclass(frozen=True)
class Turn:
    """The turn a Stop event ends: its prompt, final answer, persona and persona action."""

    prompt: str
    answer: str | None
    persona: dict | None
    persona_action: str | None


def _content_text(content) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block["text"] for block in content if isinstance(block, dict)
                       and block.get("type") == "text" and isinstance(block.get("text"), str))
    return None


def prompt_text(record: dict) -> str | None:
    """The text of a main-thread user row that starts a turn, without leading system reminders.

    None for tool results, sidechain, meta and compaction rows, and for the rows Claude Code
    writes itself (Stop hook feedback, an interruption marker).
    """
    if (record.get("type") != "user" or record.get("isSidechain") or record.get("isMeta")
            or record.get("isCompactSummary")):
        return None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "tool_result"
                                         for block in content):
        return None
    text = _content_text(content)
    if text is None:
        return None
    text = LEADING_REMINDERS.sub("", text, count=1).strip()
    return text if text and not HOST_ROW.match(text) else None


def _tool_payload(block: dict) -> dict | None:
    """The JSON object a tool returned; FastMCP wraps a handler's JSON text as ``{"result": text}``."""
    if block.get("is_error"):
        return None
    text = _content_text(block.get("content"))
    try:
        value = json.loads(text) if text else None
        if isinstance(value, dict) and set(value) == {"result"} and isinstance(value["result"], str):
            value = json.loads(value["result"])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _persona_tool(name) -> str | None:
    if not isinstance(name, str) or not name.startswith("mcp__"):
        return None
    tool = name.rsplit("__", 1)[-1]
    return tool if tool in PERSONA_TOOLS else None


def _action(status: str, tool: str, arguments: dict, in_turn: bool) -> str:
    """The persona_action the model would have passed for this turn."""
    if not in_turn or status == "NO_CHANGE":
        return "keep"
    if tool == "refresh_persona_context":
        return "refresh"
    if tool == "get_agent_context" and arguments.get("force_reload") is True:
        return "restore"
    return "switch"


def read_turn(lines, prompt_id: str | None = None) -> Turn | None:
    """The last turn of a Claude Code transcript, given its lines; None without a prompt.

    With ``prompt_id`` (the hook input's), the turn starts at the first prompt row carrying that
    ``promptId`` and ends before a later prompt; without it, or when no row carries it, the
    last prompt row starts it. Sidechain rows are skipped.
    """
    from src.engine.subagent_exchange import final_message

    tools: dict[str, tuple[str, dict]] = {}
    prompt = start = matched = None
    persona = None  # (descriptor, status, tool, arguments, row)
    answer_lines: list[str] = []
    for row, line in enumerate(lines):
        if '"user"' not in line and '"assistant"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("isSidechain"):
            continue
        text = prompt_text(record)
        if text is not None:
            own = prompt_id is not None and record.get("promptId") == prompt_id
            if matched and not own:
                break  # the hook's own turn ended before this prompt
            if not matched:
                prompt, start, matched, answer_lines = text, row, own, []
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if record.get("type") == "assistant":
            answer_lines.append(line)
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    tool = _persona_tool(block.get("name"))
                    if tool is not None:
                        arguments = block.get("input")
                        tools[block["id"]] = (tool, arguments if isinstance(arguments, dict) else {})
        elif record.get("type") == "user":
            for block in content if isinstance(content, list) else []:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"
                        and isinstance(block.get("tool_use_id"), str) and block["tool_use_id"] in tools):
                    continue
                response = _tool_payload(block)
                if (response is not None and response.get("status") in PERSONA_STATUSES
                        and isinstance(response.get("persona"), dict)):
                    tool, arguments = tools[block["tool_use_id"]]
                    persona = (response["persona"], response["status"], tool, arguments, row)
    if prompt is None:
        return None
    descriptor = action = None
    if persona is not None:
        descriptor, status, tool, arguments, row = persona
        action = _action(status, tool, arguments, row > start)
    return Turn(prompt, final_message(answer_lines), descriptor, action)


def _read_transcript(path) -> list[str]:
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError("transcript_path must be an absolute path")
    with open(path, "rb") as stream:
        raw = stream.read(MAX_TRANSCRIPT_BYTES + 1)
    if len(raw) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("transcript too large")
    # The client may still be appending: a torn last line is skipped like any undecodable one.
    return io.StringIO(raw.decode("utf-8", errors="replace")).readlines()


def journal_root(payload: dict) -> Path:
    """The project whose journal takes the turn, resolved as a stdio server resolves its own.

    ``AGENTS_CLIENT_REPO_ROOT`` decides alone; otherwise an existing ``CLAUDE_PROJECT_DIR``, else
    the session's ``cwd``. The nearest ``.git``, ``CLAUDE.md`` or ``AGENTS.md`` at or above it owns
    the journal, a worktree its main checkout, and an unsafe directory is refused. Raises
    ``ClientRootError`` or ``WorkspaceError``.
    """
    from src.daemon.workspaces import ClientContext

    override = os.environ.get("AGENTS_CLIENT_REPO_ROOT")
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if override:
        root, source = config.client_root_from_override(override), "env"
    elif project and os.path.isdir(project):
        root, source = config.client_root_from_directory(project, launch_directory=False), "CLAUDE_PROJECT_DIR"
    elif isinstance(payload.get("cwd"), str):
        root, source = config.client_root_from_directory(payload["cwd"]), "cwd"
    else:
        raise ClientRootError("the hook input names no cwd", code="workspace_required")
    # The same check log_interaction makes: no memory path may lead out of the project.
    return ClientContext(str(uuid.uuid4()), "stdio", root=Path(root).resolve(), source=source).require_root()


def record_turn(payload) -> dict:
    """Write the turn a Stop event ends to the project journal; return what happened."""
    from src.engine.persona import parse_persona_for_logging, persona_history_line
    from src.memory.history import HistoryWriter
    from src.schedules.records import mask

    if not config.turn_log_hook():
        return {"status": "skipped", "reason": "disabled"}
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "Stop":
        return {"status": "skipped", "reason": "not_a_stop_event"}
    root = journal_root(payload)
    prompt_id = payload.get("prompt_id")
    turn = read_turn(_read_transcript(payload.get("transcript_path")),
                     prompt_id if isinstance(prompt_id, str) and prompt_id else None)
    if turn is None:
        return {"status": "skipped", "reason": "no_prompt"}
    if turn.persona is None:
        return {"status": "skipped", "reason": "no_persona"}
    given = payload.get("last_assistant_message")
    answer = given if isinstance(given, str) and given.strip() else turn.answer
    answer = TIME_LINE.sub("", answer or "", count=1).strip()
    if not answer:
        return {"status": "skipped", "reason": "no_answer"}
    agent = turn.persona.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        return {"status": "skipped", "reason": "no_persona"}
    logged = parse_persona_for_logging(turn.persona, agent)
    action = f"Agent: {agent}"
    attributed = action
    if line := persona_history_line(logged, turn.persona_action):
        attributed += "\n" + line
    metadata = {"answer_timestamp": datetime.datetime.now().strftime("%Y.%m.%d %H:%M:%S"),
                "logged_by": "stop_hook"}
    writer = HistoryWriter(str(root / "history.md"), str(root / "history"))
    return writer.append_entry(mask(turn.prompt), attributed, mask(answer), None, None, metadata,
                               dedupe_action=action)


def main(argv=None) -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) <= MAX_INPUT_BYTES:
            record_turn(json.loads(raw))
    except Exception as error:  # noqa: BLE001 - a Stop hook must never fail or block its client
        print(f"agents-core turn log: turn not logged ({type(error).__name__})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
