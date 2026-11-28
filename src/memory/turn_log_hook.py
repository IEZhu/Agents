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
``persona_activation`` of ``run_flow``, of an Agents-Core server in the transcript (registered as
``Agents-Core`` or ``Agents-Core-Desktop``, also inside a plugin; ``log_interaction`` counts under
any server name); it is held
against the footer the answer ends with, so another agent there gives ``mismatch`` and no footer
``unverified``. Only a canonical agent name counts, the footer's and the descriptor's: any other
text in that place never reaches the entry, and the persona line is cleaned like the turn's text.
A ``SUCCESS`` in the turn decides its persona action, whatever ``NO_CHANGE`` follows it.

One entry per turn, the one delivered last: the entry is written under an idempotency key made of
the session and prompt ids, so a later Stop of the same turn (another Stop hook continued it)
replaces it under the journal lock, wherever rotation has put it, unless the later text is only a
short note beside it. A Stop input without a prompt id ties its answer to the transcript's last
turn only once that turn holds that answer, so a lagging transcript never pairs it with the
previous prompt and replaces that turn's entry (#701 review). A turn whose ``log_interaction``
call recorded it, may have recorded it (``history_timeout``) or has no result in the transcript
yet, and a turn the model kept out of the journal (``journal=false``), belongs to that call: the
hook removes its own earlier entry of the turn and writes nothing. A turn whose own call the server
refused is not logged at all, as it was not before this hook, and neither is one whose
``journal=false`` call a server older than that parameter recorded; both are reported. What the
hook removes is recorded for the user library sync (``src.user_sync.history.record_deletion``),
before the journal changes, so the next reconcile of a process that shares this repository's
history takes it out of what this machine shared; a change that raises takes the record back for
what the journal still holds (``forget_deletion``), and only a hook killed inside the change keeps
a record of an entry still there.

Secrets are cleaned before anything is written (``src.memory.turn_redaction.redact``): the rest
of every line after a word that names a secret or a client's password option, token formats,
private key bodies, random-looking strings, and what the repository's scanners find, the hook's
own environment values under a secret's name among them. A text that cannot be cleaned in time is
withheld and the entry marked. Free-form personal details are not detected: the model's own
redacted ``log_interaction`` call, or ``journal=false``, protects a turn that holds them.

A turn whose project does not resolve goes to the fallback journal that ``log_interaction`` uses
(#292). A Stop event has ``DEADLINE_SECONDS`` in all, below the registration's timeout: the
transcript wait, the redaction and the journal lock come out of it. A Stop event the hook could not
log exits 1, which Claude Code shows as a non-blocking hook error with the reason; the hook prints
nothing to stdout but the notice.
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid

from src.engine import config
from src.memory.turn_redaction import environment_secrets, redact

# The hook input carries the last assistant message; a larger input is not a hook event.
MAX_INPUT_BYTES = 8 * 1024 * 1024
# Read line by line, memory follows the turn and not the file; the cap bounds the time one turn end
# may take (a 58 MB transcript read in 0.2 s, #701 review). A larger one is a failure, not a skip.
MAX_TRANSCRIPT_BYTES = 1024 * 1024 * 1024
# The transcript is written asynchronously and may lag the hook (Claude Code's hooks reference): a
# missing prompt row, a log_interaction call whose result is not written yet, or a turn that does
# not hold the Stop's answer yet, is read again for this long before the hook decides.
TRANSCRIPT_WAIT_SECONDS = 2.0
TRANSCRIPT_POLL_SECONDS = 0.2
# The journal lock is waited for this long at most, and never past the deadline below.
LOCK_TIMEOUT_SECONDS = 10.0
# One deadline for a hook event, below the 30-second timeout of the documented registration: a Stop
# that cannot finish in it reports the turn as not logged, rather than being cancelled by the client
# with its output discarded (#701 review). ``WATCHDOG_SECONDS`` ends a command still running then;
# ``HARD_WATCHDOG_SECONDS`` ends it from a thread that needs no interpreter lock, for the case where
# one long computation holds the lock past the first watchdog (#701 review: 70 s on one line).
DEADLINE_SECONDS = 25.0
WATCHDOG_SECONDS = 28.0
HARD_WATCHDOG_SECONDS = 29.0
# What the journal write needs after the redaction: a turn with less time left is not written.
WRITE_RESERVE_SECONDS = 1.0
NOTICE_EVENT, STOP_EVENT = "UserPromptSubmit", "Stop"
PERSONA_TOOLS = ("route_and_load", "get_agent_context", "refresh_persona_context")
# The names Agents-Core's server is registered under (scripts/session_readiness.py): another server's
# persona tool or run_flow of the same name is not read (#701 review). A plugin's server is
# plugin_<plugin>_<server>. log_interaction counts under any server name (``_persona_tool``).
SERVER_NAMES = ("agents-core", "agents-core-desktop")
# run_flow returns a flow persona's activation nested under persona_activation (#734 review).
FLOW_TOOL = "run_flow"
LOG_TOOL = "log_interaction"
# History statuses of a log_interaction call that wrote the turn, found it written, or, from a
# daemon older than this hook, accepted it for a later write.
LOGGED_STATUSES = ("recorded", "duplicate", "queued")
# The history status of a call with journal=false, which keeps its turn out of the journal.
WITHHELD_STATUS = "withheld"
# What pydantic reads as false for a boolean argument such as journal.
FALSE_WORDS = ("0", "off", "f", "false", "n", "no")
# The agent of an entry whose turn has no applied persona and whose answer names none in a footer.
UNATTRIBUTED = "unattributed"
FOOTER_AGENT = re.compile(r"\*\*Agent\*\*: *([A-Za-z0-9_.-]+)")
# An agent's canonical name: the folder of its system prompt under agents/, in the descriptor schema's
# form (``src.schemas.protocol.PersonaDescriptor``: a first letter or digit, as in 3d_print_finder).
AGENT_NAME = re.compile(r"[a-z0-9][a-z0-9_]{0,63}")
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
WITHHELD = "[withheld by the turn-log hook: the text could not be cleaned of secrets]"
# A later Stop's text shorter than this share of the entry's answer, footers aside, is a note
# beside it, not a new answer (25 of 240 real re-deliveries, #701 review), and leaves the entry.
SHORT_NOTE_RATIO = 0.25


class TurnNotLogged(Exception):
    """A Stop event the hook should have logged and could not; ``main`` exits 1 with this reason."""


class JournalFalseIgnored(TurnNotLogged):
    """A ``journal=false`` call came back from a server that wrote the turn anyway (#701 review)."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(
            f"the turn's log_interaction call with journal=false came back history.status {status!r}, not "
            "'withheld': a server older than journal=false wrote the turn; restart every Agents-Core server "
            "process before relying on journal=false")


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
    refused: str | None = None  # what the server answered to a log_interaction call it refused
    unwithheld: tuple[str, str] | None = None  # (outcome, answer) of a journal=false call not withheld
    text: str = ""  # every text the turn's assistant rows hold, whitespace removed


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
    """The tool the hook reads a result of: a persona tool or run_flow of an Agents-Core server under
    any of its registrations, never another server's, and log_interaction under any server name.

    A persona result of another server's tool of the same name must not attribute the turn (#701
    review). log_interaction is read whatever the server is called (``Agents_Core`` in the
    evaluation runner, a name of the user's own choosing), as before that check: a foreign tool of
    that name can only make the hook leave the turn to the model, while missing the model's own call
    wrote a second entry or a turn the model kept out with ``journal=false`` (#701 review)."""
    if not isinstance(name, str) or not name.startswith("mcp__"):
        return None
    server, separator, tool = name[len("mcp__"):].rpartition("__")
    if not separator:
        return None
    if tool == LOG_TOOL:
        return tool
    server = server.lower()
    if not (server in SERVER_NAMES or any(server.endswith("_" + known) for known in SERVER_NAMES)):
        return None
    return tool if tool in PERSONA_TOOLS or tool == FLOW_TOOL else None


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


def _journal_false(arguments: dict) -> bool:
    """Whether a log_interaction call asked to keep its turn out of the journal, as the server reads it."""
    value = arguments.get("journal")
    if isinstance(value, str):
        return value.strip().lower() in FALSE_WORDS
    return value is False or (type(value) is int and value == 0)


def _refusal(block: dict, response) -> str:
    """What a refused log_interaction call answered, for the hook's report: an error code, never text
    of the turn."""
    if block.get("is_error"):
        return "a tool error"
    history = _history(response)
    for value in (history.get("error"), history.get("status"),
                  response.get("status") if isinstance(response, dict) else None):
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
            return value
    return "an unreadable result"


def _row_id(record: dict) -> str | None:
    for name in ("promptId", "uuid"):
        value = record.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _no_space(text: str) -> str:
    return re.sub(r"\s+", "", text)


def read_turn(lines, prompt_id: str | None = None) -> Turn | None:
    """The last turn of a Claude Code transcript, given its lines; None without a prompt.

    With ``prompt_id`` (the hook input's), the turn starts at the first prompt row carrying that
    ``promptId`` and ends before a later prompt; without it, the last prompt row starts it. A
    ``prompt_id`` that no row carries gives None: the hook's own prompt may not be written yet,
    and an earlier prompt must not take its answer (#734 review). Sidechain rows are skipped.
    ``logged``, ``withheld``, ``pending_log``, ``refused`` and ``unwithheld`` tell what the turn's
    ``log_interaction`` calls did.
    """
    from src.engine.subagent_exchange import final_message

    tools: dict[str, tuple[str, dict]] = {}
    prompt = start = opener = None
    matched = False
    last = None  # the descriptor of the last applied persona result, whichever turn it is in
    success = None  # (tool, arguments) of the last SUCCESS in the current turn
    calls: dict[str, list] = {}  # tool_use id -> [journal=false, outcome, what the server answered]
    answer_lines: list[str] = []
    texts: list[str] = []
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
                answer_lines, texts, calls, success = [], [], {}, None
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if record.get("type") == "assistant":
            answer_lines.append(line)
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(_no_space(block["text"]))
                if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    tool = _persona_tool(block.get("name"))
                    if tool is not None:
                        arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                        tools[block["id"]] = (tool, arguments)
                        if tool == LOG_TOOL and start is not None:
                            calls[block["id"]] = [_journal_false(arguments), None, None]
        elif record.get("type") == "user":
            for block in content if isinstance(content, list) else []:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"
                        and isinstance(block.get("tool_use_id"), str) and block["tool_use_id"] in tools):
                    continue
                tool, arguments = tools[block["tool_use_id"]]
                response = _tool_payload(block)
                if tool == LOG_TOOL:
                    call = calls.get(block["tool_use_id"])
                    if call is not None:
                        history = _history(response)
                        if response is not None and history.get("status") == WITHHELD_STATUS:
                            call[1:] = ["withheld", WITHHELD_STATUS]
                        elif response is not None and _logged(response):
                            call[1:] = ["logged", str(history.get("status") or history.get("state"))]
                        else:
                            call[1:] = ["refused", _refusal(block, response)]
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
    outcomes = list(calls.values())
    # A journal=false call that a server answered with anything but "withheld" did not keep the turn out.
    unwithheld = next(((outcome, answer) for false, outcome, answer in outcomes
                       if false and outcome not in (None, "withheld")), None)
    withheld = any(outcome == "withheld" or (false and outcome is None) for false, outcome, _ in outcomes)
    logged = any(outcome == "logged" and not false for false, outcome, _ in outcomes)
    pending = any(outcome is None for _, outcome, _ in outcomes)
    refused = next((answer for false, outcome, answer in outcomes if outcome == "refused" and not false), None)
    return Turn(prompt, final_message(answer_lines), descriptor, action, logged, withheld, pending, opener,
                refused, unwithheld, "".join(texts))


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


def _holds(turn: Turn | None, answer: str | None) -> bool:
    """Whether ``turn`` holds the Stop's ``answer`` among its texts (always, without an answer)."""
    return answer is None or (turn is not None and _no_space(answer) in turn.text)


def settled_turn(path, prompt_id: str | None, answer: str | None = None, deadline: float | None = None) -> Turn | None:
    """``read_turn`` of the transcript at ``path``, read again for ``TRANSCRIPT_WAIT_SECONDS`` at
    most, and never past ``deadline``, while the turn's prompt row or the result of its
    ``log_interaction`` call is missing, or while the turn does not hold ``answer``: the transcript
    may lag the hook (#701 review)."""
    wait_until = time.monotonic() + TRANSCRIPT_WAIT_SECONDS
    if deadline is not None:
        wait_until = min(wait_until, deadline - WRITE_RESERVE_SECONDS)
    while True:
        turn = read_turn(_transcript_lines(path), prompt_id)
        if turn is not None and not turn.pending_log and _holds(turn, answer) or time.monotonic() >= wait_until:
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


def _canonical(name: str | None, descriptor_agent: str | None) -> str | None:
    """``name`` when it is an agent's canonical name: the turn's descriptor's, or the folder of an
    installed agent's system prompt. Any other text in the footer's place, which may be a secret
    there, never reaches the entry (#701 review)."""
    if name is None or name == descriptor_agent:
        return name
    if AGENT_NAME.fullmatch(name) and os.path.isfile(os.path.join(config.AGENTS_DIR, name, "system_prompt.mdc")):
        return name
    return None


def _attribution(turn: Turn, answer: str) -> tuple[str, str]:
    """The entry's agent and persona line (#701 review). The delivered footer names the agent, and
    the turn's descriptor is reported against it: ``mismatch`` when the footer names another
    agent, ``unverified`` when the answer has no footer or names no canonical agent there. Without
    a descriptor the footer's agent is unverified; without either the turn is ``unattributed``,
    like a log_interaction call without a descriptor."""
    from src.engine.persona import LoggedPersona, parse_persona_for_logging, persona_history_line

    agent = turn.persona.get("agent") if turn.persona is not None else None
    # The descriptor's agent counts only as a canonical name, as the footer's does (#701 review).
    agent = agent if isinstance(agent, str) and _canonical(agent, None) else None
    footer = _canonical(_footer_agent(answer), agent)
    if agent is not None:
        logged = parse_persona_for_logging(turn.persona, footer or agent)
        if footer is None:
            logged.missing.append("footer")
            if logged.status == "client-reported":
                logged.status = "unverified"
        return footer or agent, persona_history_line(logged, turn.persona_action)
    if footer:
        return footer, persona_history_line(parse_persona_for_logging({"agent": footer}, footer), None)
    return UNATTRIBUTED, persona_history_line(LoggedPersona(status="unverified", missing=["persona"]), None)


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


class _RemovalsForSync:
    """The history sync integration of the hook process: it shares nothing itself and only records
    the entries the hook removes (a superseded or withdrawn entry), so the next reconcile of a
    process that shares this repository's history takes them out of what this machine shared, and
    merged reads leave them out at once (#701 review). The record is written under the journal's
    locks before the journal changes (``deleting``), so a hook ended right after the change, by
    the client's timeout or its own watchdog, still leaves it, and taken back when the change
    raised with the entry still in the journal (``not_deleting``). The sync module's import and the
    checkout's key are resolved before the locks are taken, so only the state write happens under
    them (#701 review)."""

    def __init__(self, history_path: str):
        from src.user_sync import history as history_sync
        try:
            history_sync.prepare_deletions(history_path)
        except Exception:  # noqa: BLE001 - the record under the locks reports its own failure
            pass

    def appended(self, history_path: str, entry_id: str | None = None) -> None:
        return None

    def machines(self, history_path: str):
        return None

    def deleting(self, history_path: str, entry_id: str, timestamp: str) -> None:
        from src.user_sync import history as history_sync
        history_sync.record_deletion(history_path, entry_id, timestamp)

    def not_deleting(self, history_path: str, entry_id: str, timestamp: str) -> None:
        from src.user_sync import history as history_sync
        history_sync.forget_deletion(history_path, entry_id, timestamp)

    def deleted(self, history_path: str, entry_id: str, timestamp: str) -> None:
        return None  # recorded before the change


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.monotonic()


def record_turn(payload, interactive: bool = False, deadline: float | None = None) -> dict:
    """Write the turn a Stop event ends to the journal; return what happened.

    ``interactive`` is the hook's ``--interactive`` argument; without it only the headless
    switches (``config.turn_log_hook()``) let the hook write. ``deadline`` is the ``time.monotonic``
    value the whole event has to end by. Raises ``TurnNotLogged``, or the journal's error, for a
    turn it should have logged and could not.
    """
    from src.memory import history as journal

    if not (interactive or config.turn_log_hook()):
        return {"status": "skipped", "reason": "disabled"}
    if not isinstance(payload, dict) or payload.get("hook_event_name") != STOP_EVENT:
        return {"status": "skipped", "reason": "not_a_stop_event"}
    root, unresolved = _journal(payload)
    prompt_id = _identifier(payload.get("prompt_id"))
    given = payload.get("last_assistant_message")
    given = given if isinstance(given, str) and given.strip() else None
    # Without a prompt id only the Stop's answer ties it to the transcript's last turn, which a
    # lagging transcript can still hold the previous one of: never replace an entry on a guess.
    expected = given if prompt_id is None else None
    turn = settled_turn(payload.get("transcript_path"), prompt_id, expected, deadline)
    if turn is None:
        raise TurnNotLogged("the turn's prompt is not in the transcript")
    if not _holds(turn, expected):
        raise TurnNotLogged("the transcript's last turn does not hold the Stop's answer yet")
    key = turn_key(payload, prompt_id or turn.opener_id)
    writer = journal.HistoryWriter(str(root / "history.md"), str(root / "history"))
    previous = journal.set_sync(_RemovalsForSync(writer.history_path))
    try:
        return _write_turn(payload, turn, key, given, writer, unresolved, prompt_id, deadline)
    finally:
        journal.set_sync(previous)


def _lock_timeout(deadline: float | None) -> float:
    remaining = _remaining(deadline)
    if remaining is None:
        return LOCK_TIMEOUT_SECONDS
    if remaining <= WRITE_RESERVE_SECONDS / 2:
        raise TurnNotLogged("the hook ran out of time before the journal write")
    return min(LOCK_TIMEOUT_SECONDS, remaining - WRITE_RESERVE_SECONDS / 2)


def _write_turn(payload, turn: Turn, key, given, writer, unresolved, prompt_id, deadline) -> dict:
    if turn.unwithheld is not None or turn.withheld or turn.logged or turn.pending_log or turn.refused is not None:
        # The model's own call decides this turn: the hook's earlier entry of it goes (#701 review).
        withdrawn = writer.withdraw(key, lock_timeout=_lock_timeout(deadline)) if key else {"status": "absent"}
        if turn.unwithheld is not None:
            outcome, answer = turn.unwithheld
            if outcome == "logged":
                raise JournalFalseIgnored(answer)
            raise TurnNotLogged(f"its log_interaction call with journal=false was refused ({answer}), not withheld")
        if turn.withheld or turn.logged or turn.pending_log:
            return {"status": "skipped", "reason": "not_journaled" if turn.withheld else "logged_by_model",
                    "withdrawn": withdrawn["status"] == "withdrawn"}
        # A refused call left no entry before this hook, and the hook must not write the text the
        # call may have been redacting (#701 review).
        raise TurnNotLogged(f"its own log_interaction call was refused ({turn.refused})")
    if key is None and payload.get("stop_hook_active") is True:
        # Without an id the entry of this turn cannot be found to replace: the first Stop logged it.
        return {"status": "skipped", "reason": "stop_hook_active"}
    answer = given if given is not None else turn.answer
    answer = TIME_LINE.sub("", answer or "", count=1).strip() or NO_ANSWER
    agent, line = _attribution(turn, answer)
    action = f"Agent: {agent}"
    known = environment_secrets()
    # Each field may use the time left before the journal write needs its share.
    redaction_deadline = None if deadline is None else deadline - WRITE_RESERVE_SECONDS
    # The persona line carries a client's descriptor fields: it is cleaned as the fields are.
    line, line_clean = redact(line, known, redaction_deadline) if line else (line, True)
    if not line_clean:
        line = WITHHELD
    attributed = action + ("\n" + line if line else "")
    intent, intent_clean = redact(turn.prompt, known, redaction_deadline)
    outcome, outcome_clean = redact(answer, known, redaction_deadline)
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
                                   dedupe_action=dedupe_action, lock_timeout=_lock_timeout(deadline))
    # The text decides a replay, not the time in the metadata: the same answer again writes nothing.
    fingerprint = hashlib.sha256(json.dumps([intent, attributed, outcome], ensure_ascii=False)
                                 .encode("utf-8", "surrogatepass")).hexdigest()
    return writer.append_entry(intent, attributed, outcome, None, None, metadata, dedupe_action=dedupe_action,
                               idempotency_key=key, payload_fingerprint=fingerprint,
                               supersede=lambda earlier: not short_note(outcome, earlier.outcome),
                               lock_timeout=_lock_timeout(deadline))


def _reason(error: BaseException) -> str:
    """What stderr says about a failure: the hook's own reasons and the journal's codes, else the
    error's type, never a message that may quote the turn."""
    from src.memory.history import MemoryWriteRejected

    if isinstance(error, (TurnNotLogged, MemoryWriteRejected)):
        return str(error)
    if isinstance(error, (config.ClientRootError, OSError)):
        return f"{type(error).__name__}: {error}"
    return type(error).__name__


def _report(error: BaseException) -> str:
    if isinstance(error, JournalFalseIgnored):
        return str(error)
    return f"turn not logged ({_reason(error)})"


def _watchdog(seconds: float, hard_seconds: float | None = None) -> threading.Timer:
    """End the command with exit 1 and a reason once ``seconds`` pass, before the client's own
    timeout cancels it and discards its output (#701 review). Every journal change is one atomic
    replacement or crash-safe in its order, so an exit at any moment leaves the journal readable.

    The timer is a Python thread, which cannot run while one long computation holds the interpreter
    lock; ``faulthandler``'s watchdog thread needs no lock and ends the process at ``hard_seconds``,
    printing where it was (#701 review). It needs a standard error with a file descriptor: without
    one (closed, or a stream without a descriptor in an embedding caller) the timer thread alone
    keeps the bound and the hook still runs (#701 review: the command failed on every event)."""
    def expire():
        try:
            sys.stderr.write("agents-core turn log: turn not logged (the hook ran out of time)\n")
            sys.stderr.flush()
        except (AttributeError, OSError, ValueError):
            pass  # no standard error to say it on: the exit code still does
        os._exit(1)
    if hard_seconds is not None:
        try:
            faulthandler.dump_traceback_later(hard_seconds, exit=True)
        except (AttributeError, OSError, RuntimeError, ValueError):  # io.UnsupportedOperation is both
            pass
    timer = threading.Timer(seconds, expire)
    timer.daemon = True
    timer.start()
    return timer


def main(argv=None, *, watchdog: bool = False) -> int:
    """Handle the hook event on stdin; ``--interactive`` switches the hook on for this session.

    ``UserPromptSubmit`` prints the turn-log notice as ``additionalContext`` and always exits 0, so
    a prompt never fails over this hook. ``Stop`` logs the turn; when it could not, it prints the
    reason to stderr and exits 1, which Claude Code shows as a non-blocking hook error, never 2,
    which would keep the turn going. Other events, unreadable input and other arguments exit 0.
    ``watchdog`` (the command line) also ends a run still going after ``WATCHDOG_SECONDS``, and one
    whose computation holds the interpreter lock after ``HARD_WATCHDOG_SECONDS``.
    """
    started = time.monotonic()
    timer = _watchdog(WATCHDOG_SECONDS, HARD_WATCHDOG_SECONDS) if watchdog else None
    try:
        return _main(argv, started + DEADLINE_SECONDS)
    finally:
        if timer is not None:
            timer.cancel()
            faulthandler.cancel_dump_traceback_later()  # a no-op when none was armed


def _main(argv, deadline: float) -> int:
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
        record_turn(payload, interactive, deadline)
    except Exception as error:  # noqa: BLE001 - reported to the client, never raised into it
        _say(f"agents-core turn log: {_report(error)}")
        return 1
    return 0


def _say(text: str) -> None:
    """``text`` on standard error when the process has one; print(file=None) would put it on stdout,
    where only the notice belongs."""
    if sys.stderr is None:
        return
    try:
        print(text, file=sys.stderr)
    except (OSError, ValueError):
        pass


if __name__ == "__main__":
    raise SystemExit(main(watchdog=True))
