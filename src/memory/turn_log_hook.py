"""Claude Code hook that logs the turn it ends: ``python -m src.memory.turn_log_hook`` (#734, #701).

A turn logged through ``log_interaction`` costs a headless run twice: the model writes its whole
answer once more as ``response_content``, and one more model call re-reads the whole context after
the tool returns. An interactive session has a worse problem (#701): the model logs one text and
may deliver another, reworded, translated or corrected after another Stop hook asked for one more
step, so the journal keeps an answer the user never read. This hook writes the entry instead, from
the hook input and the session transcript, with no model call and no network.

Who logs a turn is decided by the session itself, never by settings files the server can read
(#701 review), because a hook that does not run cannot announce anything:

- Interactive: one registration runs ``python -m src.memory.turn_log_hook --interactive`` for both
  ``UserPromptSubmit`` and ``Stop``. On ``UserPromptSubmit`` the hook puts the turn-log notice
  (``protocol_text.turn_log_notice``) beside the prompt, which tells the model that the Stop hook
  records this turn; on ``Stop`` it writes the turn. A session that does not run the hook
  (``--bare``, another ``CLAUDE_CONFIG_DIR``, ``disableAllHooks``, settings sources that leave the
  registration out) gets no notice and logs through ``log_interaction`` as before, and so does a
  session whose project does not resolve.
- Headless (#734): ``AGENTS_TURN_LOG_HOOK=1`` with the headless profile, in the environment of
  both the server and the hook, makes the server tell every client that the Stop hook logs.

The entry is the one ``log_interaction`` writes with its defaults: the prompt that opened the turn
as intent (``[image]`` or ``[no text]`` for a prompt without text), ``Agent: <agent>`` and the
persona attribution line as action, and the last text the turn delivered, without a time line, as
outcome. The persona is the descriptor of the last ``SUCCESS`` or ``NO_CHANGE`` of
``route_and_load``, ``get_agent_context`` or ``refresh_persona_context``, or the
``persona_activation`` of ``run_flow``, in the transcript; it is held against the footer the answer
ends with, so another agent there gives ``mismatch`` and no footer ``unverified``. A ``SUCCESS`` in
the turn decides its persona action, whatever ``NO_CHANGE`` follows it.

One entry per turn, the one delivered last: the entry is written under an idempotency key made of
the session and prompt ids, so a later Stop of the same turn (another Stop hook continued it)
replaces it under the journal lock, wherever rotation has put it, unless the later text is only a
short note beside it. A turn whose ``log_interaction`` call recorded it, may have recorded it
(``history_timeout``) or has no result in the transcript yet, and a turn the model kept out of the
journal (``journal=false``), belongs to that call: the hook removes its own earlier entry of the
turn and writes nothing.

Secrets are cleaned before anything is written, with the detectors the repository already has: the
schedule records' and this module's formats, which keep a token's prefix or a key's name; then,
until a scan finds nothing, the outgoing-text scanner of self-development (``src.schedules.scan``:
token formats, high-entropy strings, and each secret-looking value of the hook's environment as a
known secret, also encoded or split), and the credentials that shared memory and user sync refuse
to serve or publish. A text the scanner cannot clean is withheld and the entry marked. Free-form
personal details are not detected: the model's own redacted ``log_interaction`` call, or
``journal=false``, protects a turn that holds them.

A turn whose project does not resolve goes to the fallback journal that ``log_interaction`` uses
(#292). The journal lock is waited for ``LOCK_TIMEOUT_SECONDS`` at most. A Stop event the hook
could not log exits 1, which Claude Code shows as a non-blocking hook error with the reason; the
hook prints nothing to stdout but the notice.
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

from src.engine import config

# The hook input carries the last assistant message; a larger input is not a hook event.
MAX_INPUT_BYTES = 8 * 1024 * 1024
# Read line by line, memory follows the turn and not the file; the cap bounds the time one turn end
# may take (a 58 MB transcript read in 0.2 s, #701 review). A larger one is a failure, not a skip.
MAX_TRANSCRIPT_BYTES = 1024 * 1024 * 1024
# The transcript is written asynchronously and may lag the hook (Claude Code's hooks reference): a
# missing prompt row, or a log_interaction call whose result is not written yet, is read again for
# this long before the hook decides.
TRANSCRIPT_WAIT_SECONDS = 2.0
TRANSCRIPT_POLL_SECONDS = 0.2
# Below the hook's own timeout (30 s in the documented registration), so a busy journal fails
# visibly instead of the client killing the hook while it waits.
LOCK_TIMEOUT_SECONDS = 10.0
NOTICE_EVENT, STOP_EVENT = "UserPromptSubmit", "Stop"
PERSONA_TOOLS = ("route_and_load", "get_agent_context", "refresh_persona_context")
# run_flow returns a flow persona's activation nested under persona_activation (#734 review).
FLOW_TOOL = "run_flow"
LOG_TOOL = "log_interaction"
# History statuses of a log_interaction call that wrote the turn, found it written, or, from a
# daemon older than this hook, accepted it for a later write.
LOGGED_STATUSES = ("recorded", "duplicate", "queued")
# The history status of a call with journal=false, which keeps its turn out of the journal.
WITHHELD_STATUS = "withheld"
# The agent of an entry whose turn has no applied persona and whose answer names none in a footer.
UNATTRIBUTED = "unattributed"
FOOTER_AGENT = re.compile(r"\*\*Agent\*\*: *([A-Za-z0-9_.-]+)")
INTERACTIVE_ARGUMENT = "--interactive"
PERSONA_STATUSES = ("SUCCESS", "NO_CHANGE")
# A prompt typed by the user; rows that Claude Code writes itself carry another origin or none.
HUMAN_ORIGIN = "human"
# What Claude Code places before a typed prompt, and user rows it writes itself.
REMINDER_OPEN, REMINDER_CLOSE = "<system-reminder>", "</system-reminder>"
HOST_ROW = re.compile(r"Stop hook (?:feedback|blocking error)|\[Request interrupted by user")
# The line log_interaction's timestamp would add (``YYYY.MM.DD HH:MM:SS``), never part of the outcome.
TIME_LINE = re.compile(r"\A\s*\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}[ \t]*\r?\n")
# The outcome of a turn that delivered no text, so the turn is still in the journal.
NO_ANSWER = "[no text answer]"
# What a masked secret becomes. No detector reads it as a value, since it starts with "<": user
# sync and shared memory take <...> for a placeholder, so masking ends when nothing else is found.
MASK = "<secret>"
WITHHELD = "[withheld by the turn-log hook: the text could not be cleaned of secrets]"
# Masking one secret can leave another detector's match, such as a label before the masked value;
# the scan runs again on the masked text this many times at most.
REDACTION_ROUNDS = 4
SECRET_KEYS = "capability|parent_capability|api[_-]?key|secret|token|password|passwd"
# Formats the schedule records' mask() does not know, masked here on top of it (#734 review).
EXTRA_SECRET_PATTERNS = (
    re.compile(r"\b(sk-(?:proj-|svcacct-)?)[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(xox[abposr]-)[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(AIza)[0-9A-Za-z_-]{30,}"),
    re.compile(r"\b(eyJ)[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\b(aws_secret_access_key\s*[=:]\s*[\"']?)[A-Za-z0-9/+=]{30,}"),
    # The whole value under a secret-bearing key, quoted or not, however short (#734 review). A
    # quoted value runs to its own closing quote, across lines: the other quote character and
    # escaped quotes are part of it, and an unclosed one masks the rest of the text.
    re.compile(r"(?i)([\"']?\b(?:%s)[\"']?\s*[=:]\s*\")(?:[^\"\\]|\\[\s\S])+" % SECRET_KEYS),
    re.compile(r"(?i)([\"']?\b(?:%s)[\"']?\s*[=:]\s*')(?:[^'\\]|\\[\s\S])+" % SECRET_KEYS),
    re.compile(r"(?i)(\b(?:%s)\s*[=:]\s*)(?![\"'])[^\s,;}&\"']+" % SECRET_KEYS),
)
# A 43-character URL-safe token (secrets.token_urlsafe(32), the form of a cooperation capability)
# in prose, without a key (#734 review). Any mix of its characters is a valid encoding, so every
# such run is masked; an identifier of exactly this length is masked too, a loss the journal
# accepts. Git object names (40) and SHA-256 digests (64) have other lengths and stay.
RANDOM_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")
# Environment variable names that hold a credential: the schedule sandbox's FORBIDDEN_NAME, plus
# spellings it has no need for, since a sandbox never passes them.
SECRET_ENVIRONMENT_NAME = re.compile(r"PASSWD|PASSPHRASE|CREDENTIAL|AUTH|COOKIE|PRIVATE", re.IGNORECASE)
# How far a match that only detects a credential (user sync's password patterns stop after the
# first character of the value) is widened to mask the whole value.
VALUE_TAIL = re.compile(r"[^\s\"'`<>,;)\]}]*")
# A later Stop's text shorter than this share of the entry's answer, footers aside, is a note
# beside it, not a new answer (25 of 240 real re-deliveries, #701 review), and leaves the entry.
SHORT_NOTE_RATIO = 0.25


class TurnNotLogged(Exception):
    """A Stop event the hook should have logged and could not; ``main`` exits 1 with this reason."""


@dataclass(frozen=True)
class Turn:
    """The turn a Stop event ends, as the transcript shows it."""

    prompt: str
    answer: str | None
    persona: dict | None
    persona_action: str | None
    logged: bool = False  # a log_interaction call of this turn recorded it, or may have
    withheld: bool = False  # a log_interaction call of this turn kept it out of the journal
    pending_log: bool = False  # a log_interaction call of this turn has no result in the transcript
    opener_id: str | None = None  # the promptId, else the uuid, of the row that opened the turn


def _content_text(content) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block["text"] for block in content if isinstance(block, dict)
                       and block.get("type") == "text" and isinstance(block.get("text"), str))
    return None


def prompt_text(record: dict) -> str | None:
    """The text of a main-thread user row that starts a turn, without leading system reminders.

    None for tool results, sidechain and compaction rows, for meta rows other than a turn's
    opener, for the rows Claude Code writes itself (Stop hook feedback, an interruption marker),
    and for a prompt without text.
    """
    return _turn_start(record)[1]


def _turn_start(record: dict) -> tuple[bool, str | None]:
    """Whether a row starts a turn, and its prompt text (None for a prompt with no text, such
    as an image alone, which still ends the previous turn; #734 review).

    A meta row starts a turn only when it carries ``turnOrigin``, as a message from another
    session or a scheduled task does (#701); Stop hook feedback and the client's other meta
    rows carry none and stay inside the turn. A prompt the user typed (``turnOrigin`` ``human``)
    starts a turn even when it begins like such a row (#701 review).
    """
    if (record.get("type") != "user" or record.get("isSidechain") or record.get("isCompactSummary")
            or record.get("isMeta") and not record.get("turnOrigin")):
        return False, None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "tool_result"
                                         for block in content):
        return False, None
    text = _content_text(content)
    if text is None:
        return True, None
    text = _without_leading_reminders(text).strip()
    if text and record.get("turnOrigin") != HUMAN_ORIGIN and HOST_ROW.match(text):
        return False, None
    return True, text or None


def _placeholder(record: dict) -> str:
    """The intent of a prompt without text: ``[image]`` when it holds one (#701 review)."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    images = isinstance(content, list) and any(isinstance(block, dict) and block.get("type") == "image"
                                               for block in content)
    return "[image]" if images else "[no text]"


def _without_leading_reminders(text: str) -> str:
    """``text`` without the reminder blocks the client puts before a prompt.

    A loop over string searches, not a regular expression: an unanchored lazy pattern
    backtracked quadratically on long whitespace and stripped reminders quoted mid-prompt
    (#734 review).
    """
    rest = text.lstrip()
    while rest.startswith(REMINDER_OPEN):
        end = rest.find(REMINDER_CLOSE)
        if end < 0:
            break
        rest = rest[end + len(REMINDER_CLOSE):].lstrip()
    return rest


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
    """The Agents-Core tool the hook reads a result of: a persona tool, run_flow or log_interaction."""
    if not isinstance(name, str) or not name.startswith("mcp__"):
        return None
    tool = name.rsplit("__", 1)[-1]
    return tool if tool in PERSONA_TOOLS or tool in (FLOW_TOOL, LOG_TOOL) else None


def _action(success: tuple[str, dict] | None) -> str:
    """The persona_action the model would have passed: from the turn's last ``SUCCESS`` (its tool
    and arguments), else ``keep``. A ``NO_CHANGE`` after it does not hide it (#701 review)."""
    if success is None:
        return "keep"
    tool, arguments = success
    if tool == "refresh_persona_context":
        return "refresh"
    if tool in ("get_agent_context", FLOW_TOOL) and arguments.get("force_reload") is True:
        return "restore"
    return "switch"


def _history(response) -> dict:
    history = response.get("history") if isinstance(response, dict) else None
    return history if isinstance(history, dict) else {}


def _logged(response) -> bool:
    """Whether a log_interaction result recorded its turn, found it recorded, or may have: an
    ``unknown`` state such as ``history_timeout`` may still commit (#701 review)."""
    history = _history(response)
    return history.get("status") in LOGGED_STATUSES or history.get("state") == "unknown"


def _row_id(record: dict) -> str | None:
    for name in ("promptId", "uuid"):
        value = record.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def read_turn(lines, prompt_id: str | None = None) -> Turn | None:
    """The last turn of a Claude Code transcript, given its lines; None without a prompt.

    With ``prompt_id`` (the hook input's), the turn starts at the first prompt row carrying that
    ``promptId`` and ends before a later prompt; without it, the last prompt row starts it. A
    ``prompt_id`` that no row carries gives None: the hook's own prompt may not be written yet,
    and an earlier prompt must not take its answer (#734 review). Sidechain rows are skipped.
    ``logged``, ``withheld`` and ``pending_log`` tell what the turn's ``log_interaction`` calls did.
    """
    from src.engine.subagent_exchange import final_message

    tools: dict[str, tuple[str, dict]] = {}
    prompt = start = opener = None
    matched = False
    last = None  # the descriptor of the last applied persona result, whichever turn it is in
    success = None  # (tool, arguments) of the last SUCCESS in the current turn
    logged = withheld = False
    pending: set[str] = set()
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
        starts, text = _turn_start(record)
        if starts:
            own = prompt_id is not None and record.get("promptId") == prompt_id
            if matched and not own:
                break  # the hook's own turn ended before this prompt
            if not matched:
                prompt, start, matched, opener = text or _placeholder(record), row, own, _row_id(record)
                answer_lines, logged, withheld, pending, success = [], False, False, set(), None
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if record.get("type") == "assistant":
            answer_lines.append(line)
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    tool = _persona_tool(block.get("name"))
                    if tool is not None:
                        arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                        tools[block["id"]] = (tool, arguments)
                        if tool == LOG_TOOL and start is not None:
                            pending.add(block["id"])
                            withheld |= arguments.get("journal") is False
        elif record.get("type") == "user":
            for block in content if isinstance(content, list) else []:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"
                        and isinstance(block.get("tool_use_id"), str) and block["tool_use_id"] in tools):
                    continue
                tool, arguments = tools[block["tool_use_id"]]
                response = _tool_payload(block)
                if tool == LOG_TOOL:
                    pending.discard(block["tool_use_id"])
                    logged |= _logged(response)
                    withheld |= _history(response).get("status") == WITHHELD_STATUS
                    continue
                if response is not None and tool == FLOW_TOOL:
                    activation = response.get("persona_activation")
                    response = activation if isinstance(activation, dict) else None
                if (response is not None and response.get("status") in PERSONA_STATUSES
                        and isinstance(response.get("persona"), dict)):
                    last = response["persona"]
                    if response["status"] == "SUCCESS" and start is not None:
                        success = (tool, arguments)
    if start is None or prompt_id is not None and not matched:
        return None
    descriptor = action = None
    if last is not None:
        descriptor, action = last, _action(success)
    return Turn(prompt, final_message(answer_lines), descriptor, action, logged, withheld, bool(pending), opener)


def _transcript_lines(path):
    """The transcript's lines that may hold a user or assistant row, decoded one at a time.

    Reading the whole file into one string took about eleven times its size (#701 review). A torn
    last line is skipped like any undecodable one. A transcript over ``MAX_TRANSCRIPT_BYTES`` is a
    failure, so a long session's turns are never dropped without a word.
    """
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise TurnNotLogged("transcript_path must be an absolute path")
    with open(path, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > MAX_TRANSCRIPT_BYTES:
            raise TurnNotLogged(f"the transcript is over {MAX_TRANSCRIPT_BYTES // (1024 * 1024)} MiB")
        for raw in stream:
            if b'"user"' in raw or b'"assistant"' in raw:
                yield raw.decode("utf-8", errors="replace")


def settled_turn(path, prompt_id: str | None) -> Turn | None:
    """``read_turn`` of the transcript at ``path``, read again for ``TRANSCRIPT_WAIT_SECONDS`` at
    most while the turn's prompt row or the result of its ``log_interaction`` call is missing:
    the transcript may lag the hook (#701 review)."""
    deadline = time.monotonic() + TRANSCRIPT_WAIT_SECONDS
    while True:
        turn = read_turn(_transcript_lines(path), prompt_id)
        if turn is not None and not turn.pending_log or time.monotonic() >= deadline:
            return turn
        time.sleep(TRANSCRIPT_POLL_SECONDS)


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
        raise config.ClientRootError("the hook input names no cwd", code="workspace_required")
    # The same check log_interaction makes: no memory path may lead out of the project.
    return ClientContext(str(uuid.uuid4()), "stdio", root=Path(root).resolve(), source=source).require_root()


def _journal(payload: dict):
    """``(root, unresolved)``: the project's journal, or for a project that does not resolve the
    installation's fallback journal that ``log_interaction`` uses (#292), with the error and the
    session's directory as unverified metadata (``unresolved``)."""
    from src.daemon.workspaces import WorkspaceError
    from src.memory import fallback

    try:
        return journal_root(payload), None
    except config.ClientRootError as error:  # looked up now: a test may reload the config module
        reason = f"{error.code}: {error}"
    except WorkspaceError as error:
        reason = str(error)
    cwd = payload.get("cwd")
    unresolved = fallback.unresolved(reason, cwd if isinstance(cwd, str) else None)
    return unresolved.root, unresolved


def turn_notice(payload, interactive: bool = False) -> str | None:
    """What the ``UserPromptSubmit`` handler tells the model: this session's Stop hook records the
    turn (``protocol_text.turn_log_notice``).

    None when the hook is off for this session or its project does not resolve: the model then
    logs the turn through ``log_interaction``, whose fallback journal reports the gap (#292).
    """
    if not (interactive or config.turn_log_hook()):
        return None
    if not isinstance(payload, dict) or payload.get("hook_event_name") != NOTICE_EVENT:
        return None
    try:
        journal_root(payload)
    except (config.ClientRootError, ValueError):  # WorkspaceError is a ValueError
        return None
    from src.engine import protocol_text
    return protocol_text.turn_log_notice()


def _footer_agent(answer: str) -> str | None:
    """The agent the footer of ``answer`` names. The protocol ends every final answer with the
    footer, so only its last non-empty line counts; a footer quoted earlier is no footer."""
    lines = [line for line in answer.splitlines() if line.strip()]
    found = FOOTER_AGENT.findall(lines[-1]) if lines else []
    return found[-1] if found else None


def _attribution(turn: Turn, answer: str) -> tuple[str, str]:
    """The entry's agent and persona line (#701 review). The delivered footer names the agent, and
    the turn's descriptor is reported against it: ``mismatch`` when the footer names another
    agent, ``unverified`` when the answer has no footer. Without a descriptor the footer's agent is
    unverified; without either the turn is ``unattributed``, like a log_interaction call without a
    descriptor."""
    from src.engine.persona import LoggedPersona, parse_persona_for_logging, persona_history_line

    footer = _footer_agent(answer)
    agent = turn.persona.get("agent") if turn.persona is not None else None
    if isinstance(agent, str) and agent.strip():
        logged = parse_persona_for_logging(turn.persona, footer or agent)
        if footer is None:
            logged.missing.append("footer")
            if logged.status == "client-reported":
                logged.status = "unverified"
        return footer or agent, persona_history_line(logged, turn.persona_action)
    if footer:
        return footer, persona_history_line(parse_persona_for_logging({"agent": footer}, footer), None)
    return UNATTRIBUTED, persona_history_line(LoggedPersona(status="unverified", missing=["persona"]), None)


@functools.cache
def _sync_patterns() -> tuple:
    """User sync's credential patterns, which match bytes, compiled for text."""
    from src.user_sync.scope import SECRET_PATTERNS
    return tuple((name, re.compile(pattern.pattern.decode("ascii"))) for name, pattern in SECRET_PATTERNS)


def environment_secrets(environ=None) -> list:
    """The secret-looking values of the hook's environment, as the scanner's known secrets.

    A value counts when its variable's name names a secret (the schedule sandbox's
    ``FORBIDDEN_NAME`` or ``SECRET_ENVIRONMENT_NAME``) or when the value itself scans as one, as the
    schedule sandbox checks a session's environment. A value under ``KNOWN_MIN_LENGTH`` characters,
    or one naming an existing absolute path, is no secret to look for.
    """
    from src.schedules import records, scan
    from src.schedules.sandbox import FORBIDDEN_NAME

    probe, known = scan.Scanner(), []
    for name, value in (os.environ if environ is None else environ).items():
        if not isinstance(value, str) or len(value) < scan.KNOWN_MIN_LENGTH or "\x00" in value:
            continue
        if not (FORBIDDEN_NAME.search(name) or SECRET_ENVIRONMENT_NAME.search(name)
                or probe.scan(value) or any(pattern.search(value) for pattern in records.SECRET_PATTERNS)):
            continue
        if os.path.isabs(value) and os.path.exists(value):
            continue
        try:
            known.append(scan.KnownSecret(f"environment:{name}", value))
        except (TypeError, ValueError):  # a value inside its own label, or too short to fingerprint
            continue
    return known


def _secret_spans(text: str, scanner) -> list[tuple[int, int]] | None:
    """Where the detectors find a secret in ``text``; None when the scanner could not scan it
    whole (too large, too many hits)."""
    from src.memory.shared import MEMORY_SECRETS
    from src.schedules import scan

    spans = []
    for hit in scanner.scan(text):
        if hit.kind == scan.HitKind.LIMIT:
            return None
        spans.append((hit.start, hit.end))
    for _, pattern in MEMORY_SECRETS:
        spans.extend(match.span() for match in pattern.finditer(text))
    for name, pattern in _sync_patterns():
        for match in pattern.finditer(text):
            end = VALUE_TAIL.match(text, match.end()).end() if name == "password" else match.end()
            spans.append((match.start(), end))
    return spans


def _mask_spans(text: str, spans) -> str:
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    pieces, kept = [], 0
    for start, end in merged:
        pieces += [text[kept:start], MASK]
        kept = end
    return "".join(pieces) + text[kept:]


def redact(text: str, known=()) -> tuple[str, bool]:
    """``(text, clean)``: ``text`` with the secrets the repository's detectors find masked, and
    whether a last scan of the result found nothing more (#701, owner's decision 2026-11-27).

    The formats of the schedule records (``records.mask``) and this module keep a token's prefix
    or a key's name; every other find becomes ``MASK``, round after round, until the scan comes
    back empty or ``REDACTION_ROUNDS`` pass. ``known`` are the scanner's known secrets
    (``environment_secrets``).
    """
    from src.schedules import scan
    from src.schedules.records import mask

    text = mask(text)
    for pattern in EXTRA_SECRET_PATTERNS:
        text = pattern.sub(lambda match: match[1] + MASK, text)
    text = RANDOM_TOKEN.sub(MASK, text)
    scanner = scan.Scanner(known)
    for _ in range(REDACTION_ROUNDS):
        spans = _secret_spans(text, scanner)
        if spans is None:
            return text, False
        if not spans:
            return text, True
        text = _mask_spans(text, spans)
    return text, _secret_spans(text, scanner) == []


def _body(text: str) -> str:
    """``text`` up to its last ``**Agent**:``, the footer an answer ends with, whitespace collapsed:
    what a short note is measured by, in a delivered text and in an entry's one-line outcome alike."""
    start = text.rfind("**Agent**:")
    return " ".join((text[:start] if start >= 0 else text).split())


def short_note(later: str, earlier: str) -> bool:
    """Whether a later Stop's text is only a note beside the answer an entry holds (#701 review)."""
    return len(_body(later)) < SHORT_NOTE_RATIO * len(_body(earlier))


def _identifier(value) -> str | None:
    return value if isinstance(value, str) and value.strip() and len(value) <= 128 else None


def turn_key(payload: dict, prompt: str | None) -> str | None:
    """The idempotency key of a turn's entry, from the session's and the prompt's ids (#701
    review): a later Stop of the turn finds the entry wherever rotation moved it, and two sessions
    never share one. None without a prompt id."""
    if prompt is None:
        return None
    session = _identifier(payload.get("session_id"))
    if session is None:
        transcript = str(payload.get("transcript_path"))
        session = hashlib.sha256(transcript.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return f"stop-hook/{session}/{prompt}"


def record_turn(payload, interactive: bool = False) -> dict:
    """Write the turn a Stop event ends to the journal; return what happened.

    ``interactive`` is the hook's ``--interactive`` argument; without it only the headless
    switches (``config.turn_log_hook()``) let the hook write. Raises ``TurnNotLogged``, or the
    journal's error, for a turn it should have logged and could not.
    """
    from src.memory.history import HistoryWriter

    if not (interactive or config.turn_log_hook()):
        return {"status": "skipped", "reason": "disabled"}
    if not isinstance(payload, dict) or payload.get("hook_event_name") != STOP_EVENT:
        return {"status": "skipped", "reason": "not_a_stop_event"}
    root, unresolved = _journal(payload)
    prompt_id = _identifier(payload.get("prompt_id"))
    turn = settled_turn(payload.get("transcript_path"), prompt_id)
    if turn is None:
        raise TurnNotLogged("the turn's prompt is not in the transcript")
    key = turn_key(payload, prompt_id or turn.opener_id)
    writer = HistoryWriter(str(root / "history.md"), str(root / "history"))
    if turn.withheld or turn.logged or turn.pending_log:
        # The model's own call decides this turn: the hook's earlier entry of it goes (#701 review).
        withdrawn = writer.withdraw(key, lock_timeout=LOCK_TIMEOUT_SECONDS) if key else {"status": "absent"}
        return {"status": "skipped", "reason": "not_journaled" if turn.withheld else "logged_by_model",
                "withdrawn": withdrawn["status"] == "withdrawn"}
    if key is None and payload.get("stop_hook_active") is True:
        # Without an id the entry of this turn cannot be found to replace: the first Stop logged it.
        return {"status": "skipped", "reason": "stop_hook_active"}
    given = payload.get("last_assistant_message")
    answer = given if isinstance(given, str) and given.strip() else turn.answer
    answer = TIME_LINE.sub("", answer or "", count=1).strip() or NO_ANSWER
    agent, line = _attribution(turn, answer)
    action = f"Agent: {agent}"
    attributed = action + ("\n" + line if line else "")
    known = environment_secrets()
    intent, intent_clean = redact(turn.prompt, known)
    outcome, outcome_clean = redact(answer, known)
    metadata = {"answer_timestamp": datetime.datetime.now().strftime("%Y.%m.%d %H:%M:%S"),
                "logged_by": "stop_hook", "client": "claude-code"}
    for name, value in (("session_id", _identifier(payload.get("session_id"))), ("prompt_id", prompt_id)):
        if value is not None:
            metadata[name] = value
    withheld = [name for name, clean in (("intent", intent_clean), ("outcome", outcome_clean)) if not clean]
    if withheld:
        metadata["redaction"] = {"withheld": withheld}
        intent, outcome = (WITHHELD if "intent" in withheld else intent), (WITHHELD if "outcome" in withheld else outcome)
    dedupe_action = action
    if unresolved is not None:
        metadata.update(unresolved.metadata())
        dedupe_action = json.dumps([action, unresolved.claimed], ensure_ascii=False)
    if key is None:
        return writer.append_entry(intent, attributed, outcome, None, None, metadata,
                                   dedupe_action=dedupe_action, lock_timeout=LOCK_TIMEOUT_SECONDS)
    # The text decides a replay, not the time in the metadata: the same answer again writes nothing.
    fingerprint = hashlib.sha256(json.dumps([intent, attributed, outcome], ensure_ascii=False)
                                 .encode("utf-8", "surrogatepass")).hexdigest()
    return writer.append_entry(intent, attributed, outcome, None, None, metadata, dedupe_action=dedupe_action,
                               idempotency_key=key, payload_fingerprint=fingerprint,
                               supersede=lambda earlier: not short_note(outcome, earlier.outcome),
                               lock_timeout=LOCK_TIMEOUT_SECONDS)


def _reason(error: BaseException) -> str:
    """What stderr says about a failure: the hook's own reasons and the journal's codes, else the
    error's type, never a message that may quote the turn."""
    from src.memory.history import MemoryWriteRejected

    if isinstance(error, (TurnNotLogged, MemoryWriteRejected)):
        return str(error)
    if isinstance(error, (config.ClientRootError, OSError)):
        return f"{type(error).__name__}: {error}"
    return type(error).__name__


def main(argv=None) -> int:
    """Handle the hook event on stdin; ``--interactive`` switches the hook on for this session.

    ``UserPromptSubmit`` prints the turn-log notice as ``additionalContext`` and always exits 0, so
    a prompt never fails over this hook. ``Stop`` logs the turn; when it could not, it prints the
    reason to stderr and exits 1, which Claude Code shows as a non-blocking hook error, never 2,
    which would keep the turn going. Other events, unreadable input and other arguments exit 0.
    """
    interactive = INTERACTIVE_ARGUMENT in (sys.argv[1:] if argv is None else argv)
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        payload = json.loads(raw) if len(raw) <= MAX_INPUT_BYTES else None
    except ValueError:
        payload = None
    event = payload.get("hook_event_name") if isinstance(payload, dict) else None
    if event == NOTICE_EVENT:
        try:
            notice = turn_notice(payload, interactive)
        except Exception:  # noqa: BLE001 - without the notice the model logs the turn itself
            notice = None
        if notice:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": NOTICE_EVENT, "additionalContext": notice}}))
        return 0
    if event != STOP_EVENT:
        return 0
    try:
        record_turn(payload, interactive)
    except Exception as error:  # noqa: BLE001 - reported to the client, never raised into it
        print(f"agents-core turn log: turn not logged ({_reason(error)})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
