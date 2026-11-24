"""Stateless version 2 handlers. Activation state belongs to the caller's dialogue."""

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from src.engine import protocol_text
from src.engine.persona_bundle import ComponentSelection, build_persona_bundle
from src.engine.router import KEYWORD_VETO_ROUTE_REQUIRED
from pydantic import ValidationError

from src.schemas.protocol import PersonaDescriptor, PersonaResponse
from src.version import agents_core_version

logger = logging.getLogger(__name__)

# Sent as the instruction of a conversation's first SUCCESS and of a restore; composed from
# src/engine/protocol/ (#431). A later SUCCESS points to it instead (#433).
APPLY_INSTRUCTION = protocol_text.bundle_instruction()
INSTRUCTION_POINTER = protocol_text.bundle_pointer()
INSTRUCTION_REVISION = protocol_text.instruction_revision()
# What the caller needs to choose a ROUTE_REQUIRED candidate; trigger_command is for people.
CANDIDATE_FIELDS = ("name", "display_name", "role")


PERSONA_KEYS = tuple(PersonaDescriptor.model_fields)
LOGGED_VALUE_MAX = 128


def parse_persona(value: PersonaDescriptor | dict | str | None) -> PersonaDescriptor | None:
    """Strict parse for the bundle tools; the error names what is wrong with the descriptor."""
    if value is None:
        return None
    try:
        return PersonaDescriptor.model_validate(value)
    except ValidationError as error:
        missing, invalid = [], []
        for item in error.errors():
            key = str(item["loc"][0]) if item["loc"] else "persona"
            (missing if item["type"] == "missing" else invalid).append(key)
        detail = "; ".join(part for part in (
            f"missing: {', '.join(dict.fromkeys(missing))}" if missing else "",
            f"invalid: {', '.join(dict.fromkeys(invalid))}" if invalid else "",
        ) if part)
        raise ValueError(
            "current_persona must be the persona object of the last SUCCESS/NO_CHANGE with all "
            f"{len(PERSONA_KEYS)} keys ({', '.join(PERSONA_KEYS)}); {detail}"
        ) from error




@dataclass
class LoggedPersona:
    """What ``log_interaction`` could read from a client-reported ``persona``.

    ``status`` is None without a persona, else ``client-reported`` (complete and
    valid), ``unverified`` (partial or invalid) or ``mismatch`` (names another
    agent than ``agent_name``). ``descriptor`` is set only for a complete, valid one.
    """

    status: str | None = None
    descriptor: PersonaDescriptor | None = None
    fields: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _short(value) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= LOGGED_VALUE_MAX else text[:LOGGED_VALUE_MAX] + "…"


def parse_persona_for_logging(value, agent_name: str) -> LoggedPersona:
    """Never raises: attribution metadata must not cost the logged turn.

    Strict ``parse_persona`` stays authoritative for the bundle tools.
    """
    result = LoggedPersona()
    if value is None:
        return result
    result.status = "unverified"
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            value = parsed
        elif text:
            value = {"agent": text}
            result.warnings.append("persona was a bare string; read as the agent name")
        else:
            result.warnings.append("persona was an empty string")
            return result
    if isinstance(value, PersonaDescriptor):
        value = value.model_dump()
    if not isinstance(value, dict):
        result.warnings.append(f"persona must be an object, got {type(value).__name__}")
        return result

    unknown = sorted(str(k) for k in value if k not in PERSONA_KEYS)
    if unknown:
        result.warnings.append(f"dropped unknown persona keys: {', '.join(unknown)}")
    known = {k: v for k, v in value.items() if k in PERSONA_KEYS}
    try:
        result.descriptor = PersonaDescriptor.model_validate(known)
        result.fields = result.descriptor.model_dump()
        result.status = "unverified" if unknown else "client-reported"
    except ValidationError as error:
        for item in error.errors():
            key = str(item["loc"][0]) if item["loc"] else "persona"
            bucket = result.missing if item["type"] == "missing" else result.invalid
            if key not in bucket:
                bucket.append(key)
        result.fields = {k: v for k, v in known.items() if k not in result.invalid}
        if result.missing:
            result.warnings.append(f"persona is missing: {', '.join(result.missing)}")
        if result.invalid:
            result.warnings.append(f"persona has invalid values: {', '.join(result.invalid)}")
    agent = known.get("agent")
    if isinstance(agent, str) and agent and agent != agent_name:
        result.status = "mismatch"
        result.warnings.append("agent_name does not match persona.agent")
    return result


def persona_history_line(logged: LoggedPersona, persona_action) -> str | None:
    """The attribution line appended to a history entry's action; None without persona data."""
    action = _short(persona_action) if persona_action else "unspecified"
    if logged.status is None and persona_action is None:
        return None
    if logged.status == "client-reported":
        d = logged.descriptor
        return (f"Persona (client-reported): {d.agent}; activation={d.activation_id}; "
                f"revision={d.bundle_revision}; action={action}")
    parts = []
    f = logged.fields
    if f.get("agent") is not None:
        parts.append(_short(f["agent"]))
    if f.get("activation_id") is not None:
        parts.append(f"activation={_short(f['activation_id'])}")
    if f.get("bundle_revision") is not None:
        parts.append(f"revision={_short(f['bundle_revision'])}")
    missing = list(logged.missing) if logged.status else ["persona"]
    if missing:
        parts.append(f"missing={','.join(missing)}")
    if logged.invalid:
        parts.append(f"invalid={','.join(logged.invalid)}")
    parts.append(f"action={action}")
    return f"Persona ({logged.status or 'unverified'}): " + "; ".join(parts)


_ui_port: int | None = None


def configure_ui_port(port: int | None) -> None:
    """Record the port of the daemon's web UI; only the daemon calls this."""
    global _ui_port
    _ui_port = port


def ui_link() -> str | None:
    """The bare UI link, only under the daemon. Never carries a code or token."""
    if os.environ.get("AGENTS_TRANSPORT") != "http" or not _ui_port:
        return None
    return f"http://127.0.0.1:{_ui_port}/ui"


def persona_footer(persona: PersonaDescriptor) -> str:
    # The version segment depends on the installation, not the bundle: it stays out
    # of PersonaDescriptor and bundle_revision. Plain Markdown, no HTML.
    version = f"Agents-Core {agents_core_version()}"
    link = ui_link()
    segment = f"[{version}]({link})" if link else version
    return (
        f"**Agent**: {persona.agent} · **Skills**: {', '.join(persona.skills_loaded) or '—'}"
        f" · **Implants**: {', '.join(persona.implants_loaded) or '—'}"
        f" · **Rules**: {', '.join(persona.rules_loaded) or '—'}"
        f" · {segment}"
    )


BUNDLE_NOT_APPLIED = "Keep the existing activation. The requested bundle was not applied."


def error_response(error: Exception | str, request_id: str | None = None, *,
                   instruction: str = BUNDLE_NOT_APPLIED) -> str:
    return PersonaResponse(
        status="ERROR", request_id=request_id or str(uuid.uuid4()), message=str(error),
        instruction=instruction,
    ).to_json()


def unchanged(persona: PersonaDescriptor, request_id: str) -> str:
    return PersonaResponse(
        status="NO_CHANGE", request_id=request_id, persona=persona,
        replaces_activation_id=persona.activation_id, footer=persona_footer(persona),
        instruction="Keep the existing activation and its instructions.",
    ).to_json()


# --- The repository context of the first activation in a workspace (#459) ---------------

REPO_CONTEXT_FIELD = "repo_context"
# Statuses whose response applies or keeps an activation. ROUTE_REQUIRED is an intermediate
# result and ERROR activated nothing, so neither carries a repository report.
_ACTIVATED_STATUSES = ("SUCCESS", "NO_CHANGE")
# What this process last reported for a workspace: the decision, the repository hash it was
# taken at, and the paths that decision was read from with their stat marks. The record is
# consulted BEFORE anything is hashed, because the lookup walks the tree on every SUCCESS and
# NO_CHANGE otherwise — 4.5 ms on this repository, 420 ms on a 13,779-directory one (#459).
# A pending needs_summary is recorded too and repeated from the record, because it must be
# reported until the summary is written (#600) and hashing again would not change it.
#
# This records the reporting, not the activation: protocol 2 keeps activation state in the
# caller's dialogue, and no block, descriptor, footer or revision depends on this mapping.
# Losing it costs one extra lookup, never a missed report.
_reported_workspaces: dict[str, "_ReportRecord"] = {}
_report_lock = threading.Lock()
# One server process serves the few workspaces its client registered; the bound only keeps a
# long-lived process from growing a report per path it ever resolved.
_MAX_REPORTED_WORKSPACES = 64

# Which caller has already been told what, and at which decision: the repository hash for a
# described workspace, the failure for one that could not be resolved. The lookup above is a
# cache of what the tree says and is shared by every session in the process; this is the
# "has this caller been told" decision and must not be, because one process serves several
# sessions — the Claude desktop app starts a single stdio server for all of its Code
# sessions, and the HTTP daemon serves every session on the machine. Keyed per process the
# report fired for the first session and for nobody else (#576 pre-mortem P-C1).
_reported_callers: dict[tuple[str, str], str] = {}
# A few callers per workspace, and a caller is forgotten only to keep a long-lived process
# bounded; forgetting one costs a repeated report, never a missed one.
_MAX_REPORTED_CALLERS = 256
# How long a record may answer on its own. Its stat marks catch a changed top level, a
# rewritten or deleted CLAUDE.md and a new stored hash at once; a directory created deeper
# than they reach is picked up at the next recheck instead of on the next activation.
_RECHECK_SECONDS = 30.0
# What the report embeds of the stored summary. describe_repo, which a caller asks for
# deliberately, keeps returning the stored head whole: an activation must not spend a
# small-context caller's budget on it (a six-paragraph summary yielded 10,301 characters).
MAX_REPORT_PREVIEW_CHARS = 800


@dataclass(frozen=True)
class _ReportRecord:
    """What a workspace's lookup last decided, and the cheap inputs it was decided from.

    ``report`` holds what is true of the checkout and nothing that belongs to one caller: the
    ``workspace_id`` of a pending report (#576 pre-mortem P-C1), the ``registered_workspace`` of
    a caller whose memory root is elsewhere and the instruction that follows from it are all
    added in ``_answer``, per caller. One checkout can be both a session's own memory root and
    another registration's granted checkout, and a record that kept the first caller's advice
    decided what the second was told (CR1 F4). ``write_rejection`` is the checkout's own
    rejection reason, which that instruction needs and which a caller cannot change; None for a
    described checkout, where no write is advised at all.
    """
    repo_hash: str
    described: bool
    report: dict
    write_rejection: str | None
    inputs: tuple[str, ...]
    marks: tuple[tuple[int, int] | None, ...]
    checked_at: float


DESCRIBED_INSTRUCTION = (
    "This repository is described: the preview above is the head of the managed Repository "
    "Memory section in the file named above. Read that section instead of exploring the tree "
    "first; call describe_repo for a subdirectory or with force_refresh.")
UNDESCRIBED_INSTRUCTION = (
    "This repository has no current description. Call describe_repo for its context bundle and "
    "prompt before exploring files, then write_repo_summary with the repo_hash and repo_path "
    "above, and over HTTP the workspace_id above as well, to persist the overview.")
PROTECTED_INSTRUCTION = (
    "This repository has no current description, and a summary cannot be persisted here: "
    "{reason} Call describe_repo for its context bundle and prompt before exploring files, "
    "then follow the instruction it returns instead of writing a summary.")
NO_WORKSPACE_INSTRUCTION = (
    "No workspace resolved, so nothing was described and no repository was assumed: the "
    "installation checkout is never described in a workspace's place. Start the session in the "
    "project or register it, then call describe_repo before exploring files.")
# What a report whose described checkout is not the session's memory root has to say. History
# normalizes a linked worktree to its main checkout, so the memory root of a worktree session
# is that main checkout: a description taken there would be another branch's, and the hash
# with it (#576 pre-mortem P-C2). The report follows the checkout the call named instead and
# says which root its path, hash and preview belong to.
OTHER_CHECKOUT_NOTE = (
    "This report describes {described}, the checkout this call named and this session is "
    "granted, so the path, repo_hash and preview above are that checkout's; this session's "
    "memory is registered for {registered}.")
OTHER_CHECKOUT_REASON = (
    "other_checkout: write_repo_summary resolves its repo_path inside the registered "
    "workspace {registered}, not the described checkout {described}, so the values above "
    "cannot persist a summary of what was described.")
# The one file a report reads inside the checkout it describes: `RepoDescriber` derives its
# `claude_md_path` from the root this way, and the report embeds that file's managed section.
SUMMARY_FILE = "CLAUDE.md"
UNSAFE_INSTRUCTION = (
    "Nothing was described: the file above resolves outside the checkout above, and a report "
    "returns its managed section, so it was not read. No other repository was described in its "
    "place. The user makes that file a regular file inside the checkout, or describes the "
    "checkout it belongs to as its own workspace; then call describe_repo.")


async def with_repo_context(response: str, *, ctx=None, workspace=None) -> str:
    """Carry the describe_repo result itself on the first activation in a workspace (#459).

    Sessions keep working in a repository without calling describe_repo although a memory
    lesson, a client instruction and the managed instruction block each asked for it (eight
    recorded repeats in #459), so the signal travels in the response of the call every session
    already makes instead of depending on the model remembering an instruction. A later
    activation in the same unchanged workspace carries nothing: the field is simply absent.

    No failure here may cost an activation. Any error leaves the response exactly as it was, and
    a workspace that was not described is not recorded as described either.
    """
    try:
        payload = json.loads(response)
        if not isinstance(payload, dict) or payload.get("status") not in _ACTIVATED_STATUSES:
            return response
        report = await _repo_context(ctx, workspace)
        if report is None:
            return response
        payload[REPO_CONTEXT_FIELD] = report
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        logger.warning("Could not report the repository context of this workspace", exc_info=True)
        return response


async def _repo_context(ctx, workspace) -> dict | None:
    """The describe_repo result for this activation's workspace, or None with nothing to report."""
    from src.daemon.workspaces import (WorkspaceError, WorkspaceMismatch,
                                       resolve_client_context)
    from src.memory.describer import in_git_work_tree
    if ctx is None and os.environ.get("AGENTS_TRANSPORT") == "http":
        # Over HTTP only the request carries the workspace, and an activation call that does not
        # forward its MCP context cannot see it: report nothing rather than guess a repository.
        return None
    client = None
    try:
        client = await resolve_client_context(ctx, workspace, allow_install_fallback=False)
        # A workspace no authorized root covers is never described in the registered one's place:
        # the caller gets the refusal the repository-scoped tools raise for it (#576).
        client.require_workspace_match()
        registered = client.workspace_root()
        root = client.report_root()
        workspace_id = client.workspace_id
    except WorkspaceMismatch as mismatch:
        return {"status": "workspace_mismatch", "error": str(mismatch), **mismatch.details()}
    except (WorkspaceError, OSError, RuntimeError) as failure:
        return _unavailable_report(client, failure)
    if _summary_file_escapes(root):
        return _unsafe_report(client, root)
    if not in_git_work_tree(root):
        # A plain folder has nothing to describe: no needs_summary, no prompt (#734).
        return None
    return await asyncio.to_thread(_describe_workspace, root, workspace_id,
                                   _caller_identity(client),
                                   None if root == registered else registered)


def _summary_file_escapes(root) -> bool:
    """True when the file a report of *root* reads does not resolve inside it (CR1 F2).

    `ClientContext.require_root` holds a session's memory root to this rule, which is why the
    deliberate `describe_repo` refuses such a workspace. A report follows the authorized
    checkout a call named, which that method never sees, so the root actually read is held to
    the same rule here. Both sides are resolved: a report root reaches this point from the
    registry's strictly resolved records, from a checkout grant and from a stdio resolution, so
    the containment is decided on resolved paths rather than on one of those invariants.
    """
    try:
        root = Path(root).resolve()
        return not (root / SUMMARY_FILE).resolve().is_relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return True


def _unsafe_report(client, root) -> dict | None:
    """The report for a checkout whose summary file leaves it, once per caller (CR1 F2).

    Before this refusal `plan()` opened whatever `CLAUDE.md` resolved to and the activation
    returned that file's managed section in `summary_preview`, so a repository could hand a
    session the description of a checkout outside its grant. The refusal names the path inside
    the checkout and never the target it resolves to, which is what it withholds, and it
    describes nothing in its place: the installation checkout is not substituted either.

    Recorded per caller like an unavailable workspace (#576 pre-mortem P-C3): the condition is
    the user's to fix, so repeating it on every activation of the session would buy nothing.
    """
    path = Path(root) / SUMMARY_FILE
    if not _tell_once(("unsafe", _caller_identity(client)), str(path)):
        return None
    return {"status": "workspace_unsafe", "code": "memory_path_escapes_workspace",
            "error": (f"workspace_unsafe: {path} resolves outside the workspace {root}, so the "
                      "repository was not described"),
            "workspace": str(root), "path": str(path), "instruction": UNSAFE_INSTRUCTION}


def _caller_identity(client) -> str:
    """Who "already reported" is decided for, within one server process (#576 P-C1).

    Over HTTP that is the registered workspace the request carries; over stdio the origin the
    context was resolved from, with the directory a workspace-less call claimed, which is all
    that distinguishes two sessions of one shared stdio server. Two HTTP sessions registered
    for the same workspace share one identity: nothing in the request separates them, and the
    cost is the report reaching one of them, not none.

    Two sessions sharing one stdio server share an identity for the same reason, and there is
    nothing stable to use instead (CR1 F3). A stdio process runs exactly one `ServerSession`
    over its single pipe pair (`mcp.server.lowlevel.Server.run`), so the Code sessions the
    Claude desktop app multiplexes over one server are not separate MCP sessions, and two
    stdio servers are separate processes that share no record at all. That session carries
    only the initialize handshake — `client_params`, whose fields are `protocolVersion`,
    `capabilities`, `clientInfo` (`name`, `title`, `version`, `icons`, `websiteUrl`), `meta`
    and `task` — and exposes no session identifier; the request carries a per-request
    `request_id` and a `_meta` whose one declared field is `progressToken`. FastMCP's
    `Context.client_id` reads a non-standard `_meta.client_id`, a value the caller chooses for
    itself, so it cannot decide this either. `tests/test_workspace_activation.py` pins that
    reading, so a session identity the SDK gains later is noticed here.
    """
    if client is None:
        return f"{os.environ.get('AGENTS_TRANSPORT') or 'stdio'}:unresolved"
    if client.transport == "http":
        return f"http:{client.workspace_id or ''}"
    return f"{client.transport}:{client.source or ''}:{client.claimed or ''}"


def _unavailable_report(client, failure) -> dict | None:
    """The report for a workspace that did not resolve, once per caller (#576 P-C3).

    This decision is recorded like a described one. Without a record nothing suppressed a
    repeat, so every activation of a workspace-less session carried the same instruction
    again; the failure is the decision token, so a changed error is reported afresh.
    """
    if not _tell_once(("unavailable", _caller_identity(client)), str(failure)):
        return None
    return {"status": "unavailable", "error": str(failure),
            "instruction": NO_WORKSPACE_INSTRUCTION}


def _tell_once(key: tuple[str, str], token: str) -> bool:
    """True when *key* has not been told *token* yet, and record that it now has."""
    with _report_lock:
        if _reported_callers.get(key) == token:
            return False
        _reported_callers[key] = token
        while len(_reported_callers) > _MAX_REPORTED_CALLERS:
            _reported_callers.pop(next(iter(_reported_callers)))
    return True


def _describe_workspace(root, workspace_id=None, caller="", registered=None) -> dict | None:
    """The read-only half of describe_repo for *root*, or None when it was already reported.

    The lookup record of *root* is consulted first, and while its inputs are unchanged the
    answer comes from it without hashing anything: only a stat of the repository root, its
    CLAUDE.md, its stored hash and the manifests the hash reads. ``plan`` hashes the tree and
    reads the managed section; nothing is written, so an activation never starts a repository
    write. That record says what the tree is and serves every session in the process; whether
    *caller* has been told is a separate decision (#576 pre-mortem P-C1), taken in ``_answer``.
    The describer always receives the resolved workspace as its ``repo_path``: without one it
    would describe the installation's own checkout, which is why an unresolved workspace never
    reaches this function. *registered* is this session's memory root when the described
    checkout is not it. The prompt stays with describe_repo, whose instruction names it — a
    context bundle is budgeted at 24,000 characters.
    """
    from src.memory.describer import RepoDescriber
    key = str(root)
    with _report_lock:
        record = _reported_workspaces.get(key)
    if record is not None and time.monotonic() - record.checked_at < _RECHECK_SECONDS \
            and _stat_marks(record.inputs) == record.marks:
        # Nothing the last decision was read from has changed: answer from the record.
        return _answer(key, record, workspace_id, caller, registered)
    describer = RepoDescriber(repo_path=key)
    decision = describer.plan()
    inputs = _report_inputs(describer)
    marks = _stat_marks(inputs)
    if decision.needs_refresh:
        # A pending needs_summary is not completed work: the summary was not written, so every
        # later activation carries it again until it is (#459, #600). The record repeats it
        # instead of walking the tree for an answer that cannot have changed.
        report = {"status": "needs_summary", "workspace": key, "repo_path": describer.repo_path,
                  "repo_hash": decision.current_hash}
        rejection = describer.write_rejection_reason()
    else:
        report = {**describer.up_to_date_response(decision,
                                                  preview_chars=MAX_REPORT_PREVIEW_CHARS),
                  "workspace": key}
        rejection = None
    record = _ReportRecord(decision.current_hash, not decision.needs_refresh, report, rejection,
                           inputs, marks, time.monotonic())
    _remember(key, record)
    return _answer(key, record, workspace_id, caller, registered)


def _answer(key: str, record: "_ReportRecord", workspace_id, caller: str,
            registered=None) -> dict | None:
    """What *caller* is told for the workspace *key*, given its current lookup record.

    A described repository is reported once per caller and per hash, so a second session of
    the same process is told as well, and a described tree that moved is told again. A pending
    needs_summary is reported on every activation until the summary is written (#600), with
    this caller's own ``workspace_id`` rather than the one the record was built for.

    Everything that depends on *registered*, this caller's memory root when the described
    checkout is not it, is composed here and never cached: the record is shared by every
    session in the process, and a checkout can be one session's own memory root and another
    registration's granted checkout at the same time (CR1 F4).
    """
    if record.described:
        if not _tell_once((key, caller), record.repo_hash):
            return None
        report = {**record.report, "instruction": DESCRIBED_INSTRUCTION}
        if registered is not None:
            report["instruction"] += " " + OTHER_CHECKOUT_NOTE.format(described=key,
                                                                      registered=registered)
    else:
        report = {**record.report,
                  "instruction": _undescribed_instruction(key, record.write_rejection, registered)}
        if workspace_id:
            # write_repo_summary refuses an HTTP call without the workspace_id its own
            # needs_summary carries, so the instruction above is followable only with it.
            report["workspace_id"] = workspace_id
    if registered is not None:
        # Which root the path, hash and preview belong to is part of the report, not left to
        # the caller to infer from a workspace it did not name (#576 pre-mortem P-C2).
        report["registered_workspace"] = str(registered)
    return report


def _undescribed_instruction(described: str, rejection: str | None, registered=None) -> str:
    """What to do about a repository with no current description.

    A workspace write_repo_summary always refuses — the running installation and its runtime
    source — is never advised to persist one: describe_repo's own instruction says what to do
    with the overview there instead. That *rejection* is the described checkout's own and is
    read with it. A checkout that is not the calling session's memory root is the same case,
    decided per caller: `workspace_target` resolves a write inside the memory root, so a summary
    written from this report would not be the described checkout's (#576 pre-mortem P-C2).
    """
    if registered is not None:
        return PROTECTED_INSTRUCTION.format(reason=OTHER_CHECKOUT_REASON.format(
            registered=registered, described=described))
    return PROTECTED_INSTRUCTION.format(reason=rejection) if rejection else UNDESCRIBED_INSTRUCTION


def _report_inputs(describer) -> tuple[str, ...]:
    """The paths the decision for a workspace was read from, in a fixed order.

    The repository root covers its top-level entries, which is what the hash reads there;
    CLAUDE.md carries the managed section and the manifests and README head are hashed by
    content. A directory created below the root's own entries is not visible here and waits
    for the recheck.
    """
    from src.memory.describer import _KEY_MANIFEST_FILES
    repo = Path(describer.repo_path)
    return (describer.repo_path, describer.claude_md_path, describer.hash_file,
            *(str(repo / name) for name in (*_KEY_MANIFEST_FILES, "README.md")))


def _stat_marks(paths) -> tuple:
    """A stat mark per path; None for one that does not exist, so its appearance shows."""
    marks = []
    for path in paths:
        try:
            status = os.stat(path)
            marks.append((status.st_mtime_ns, status.st_size))
        except OSError:
            marks.append(None)
    return tuple(marks)


def _remember(key: str, record: "_ReportRecord") -> None:
    with _report_lock:
        _reported_workspaces[key] = record
        while len(_reported_workspaces) > _MAX_REPORTED_WORKSPACES:
            _reported_workspaces.pop(next(iter(_reported_workspaces)))


async def load_persona(
    router, agent_name: str, query: str, history: list[str],
    current_persona: PersonaDescriptor | dict | None = None, *,
    force_reload: bool = False, refresh: bool = False,
    reasoning: str = "Explicit persona selection", request_id: str | None = None,
    selection: ComponentSelection | None = None, ctx=None, workspace: str | None = None,
) -> str:
    """With ``selection`` (a flow's persona) bundles are compared by revision, not
    agent name, and the choice does not train the shared router cache.

    ``ctx`` and ``workspace`` only locate the workspace whose repository context the first
    activation in it reports (#459); neither selects or changes a bundle."""
    request_id = request_id or str(uuid.uuid4())
    try:
        current = parse_persona(current_persona)
        if refresh and (current is None or current.agent != agent_name):
            raise ValueError("Refresh requires the descriptor of the same agent")
        if (current and current.agent == agent_name and not force_reload and not refresh
                and selection is None):
            return await with_repo_context(unchanged(current, request_id),
                                           ctx=ctx, workspace=workspace)

        bundle = await build_persona_bundle(agent_name, query, history, selection=selection)
        if ((refresh or selection is not None) and current and not force_reload
                and current.agent == agent_name
                and current.bundle_revision == bundle.bundle_revision):
            return await with_repo_context(unchanged(current, request_id),
                                           ctx=ctx, workspace=workspace)

        persona = PersonaDescriptor(
            agent=bundle.agent, activation_id=str(uuid.uuid4()),
            bundle_revision=bundle.bundle_revision, scope=bundle.scope,
            skills_loaded=bundle.skills_loaded, implants_loaded=bundle.implants_loaded,
            rules_loaded=bundle.rules_loaded,
        )
        # The conversation holds the full instruction from its first activation, unless this
        # is a first activation or a restore of lost instructions.
        repeat = current is not None and not force_reload
        result = PersonaResponse(
            status="SUCCESS", request_id=request_id, persona=persona,
            replaces_activation_id=current.activation_id if current else None,
            footer=persona_footer(persona), instruction=INSTRUCTION_POINTER if repeat else APPLY_INSTRUCTION,
            instruction_revision=INSTRUCTION_REVISION,
            persona_block=bundle.persona_block, rules_block=bundle.rules_block,
            skills_block=bundle.skills_block, implants_block=bundle.implants_block,
        )
        if not refresh and not force_reload and selection is None:
            # Learning failure cannot invalidate an already assembled bundle.
            try:
                await router.update_cache(query, agent_name, reasoning, request_id)
            except Exception:
                logger.warning("Could not cache persona selection", exc_info=True)
        return await with_repo_context(result.to_json(), ctx=ctx, workspace=workspace)
    except Exception as error:
        logger.warning("Persona bundle not activated: %s", error)
        return error_response(error, request_id)


async def route_persona(router, query: str, history: list[str], current_persona, is_meta, *,
                        ctx=None, workspace: str | None = None) -> str:
    request_id = str(uuid.uuid4())
    try:
        current = parse_persona(current_persona)
        cached = await router.lookup_cache(query, {"history_text": "\n".join(history)})
        agent_name = None
        if cached:
            veto = router.keyword_veto(query, cached.target_agent)
            if veto != KEYWORD_VETO_ROUTE_REQUIRED:
                agent_name = veto or cached.target_agent
        elif is_meta(query):
            # The bundle infers the tier and promotes lite to standard: NO_CHANGE
            # keeps this session-scoped bundle for the whole conversation.
            agent_name = "universal_agent"
        if agent_name is None:
            return PersonaResponse(
                status="ROUTE_REQUIRED", request_id=request_id,
                replaces_activation_id=current.activation_id if current else None,
                candidates=[{key: entry[key] for key in CANDIDATE_FIELDS if key in entry}
                            for entry in router.get_agent_catalog()],
                instruction=("Choose the best agent for the request and relevant conversation, "
                             "then call get_agent_context with protocol_version=2 and the same "
                             "current_persona. Keep the existing activation until SUCCESS."),
            ).to_json()
        return await load_persona(
            router, agent_name, query, history, current, request_id=request_id,
            reasoning="Semantic routing with keyword validation", ctx=ctx, workspace=workspace,
        )
    except Exception as error:
        return error_response(error, request_id)
