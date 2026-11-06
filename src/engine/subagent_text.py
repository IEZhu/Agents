"""Plain-text worker exchange: the assignment a worker reads and the result it returns.

JSON is costly for a model in both directions: every list item repeats its quoted keys,
newlines and quotes are escaped, revisions are 64 characters, and the worker must read a
JSON Schema to write the envelope. The text form carries the same closed result model in
fewer tokens. Code turns it back into the JSON shape `subagent_validation` already checks,
so validation stays in code; nothing about it moves to the model.

A result looks like this (the payload section depends on the result type)::

    task: review-1
    attempt: 1
    contract: 3f2a9c01b7de
    status: completed

    ## summary
    What was done and found.

    ## findings
    - high src/app.py:12 — The token is logged.[^e1]
      fix: Drop the token from the message.

    ## checks
    - passed: Ran the focused tests.[^e2]

    ## evidence
    [^e1]: src/app.py:12 — logger.info prints the header
    [^e2]: pytest tests/test_app.py — 12 passed
"""
from __future__ import annotations

from collections.abc import Callable
import re

from src.schemas.subagent import PROTOCOL, TaskContract

CONTRACT_PREFIX_CHARS = 12  # the worker copies this much of the contract revision
PAYLOAD_KEYS = {"generic": "items", "review": "findings", "implementation": "changes",
                "research": "conclusions"}
HEADER_KEYS = ("task", "attempt", "contract", "status", "question")
TEXT_SECTIONS = ("summary", "question")
LIST_SECTIONS = ("checks", "artifacts", "limitations", "remaining_work")
SEVERITIES = ("critical", "high", "medium", "low", "info")
CHECK_STATES = ("passed", "failed", "not_run")
_REFERENCE = re.compile(r"\[\^([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})\]")
_EM_SEPARATOR = re.compile(r"\s+—\s+")
_SEPARATOR = re.compile(r"\s+(?:—|--)\s+")
_HEADING = re.compile(r"##\s+([A-Za-z_]+)\s*")
_HEADER = re.compile(r"([a-z_]+):[ \t]*(.*)")
_EVIDENCE = re.compile(r"\[\^([^\]]*)\]:[ \t]*(.*)")


class TextFormatError(ValueError):
    """A result that does not follow the text format; ``path`` names the offending part."""

    def __init__(self, code: str, message: str, path: str = "$.result"):
        super().__init__(message)
        self.code, self.path = code, path


# Rendering: what the worker reads.

def _bullets(title: str, values: list[str]) -> list[str]:
    if not values:
        return []
    return [f"### {title}", *(f"- {value}" for value in values), ""]


def render_assignment(contract: dict) -> str:
    """The contract as the worker needs it, without JSON quoting or the parent's budgets."""
    revision = contract["contract_revision"][:CONTRACT_PREFIX_CHARS]
    lines = ["## Assignment",
             f"task: {contract['task_id']} · attempt: {contract['attempt_id']} · contract: {revision}"
             f" · role: {contract['agent']} · result: {contract['result_type']}"]
    execution = contract.get("execution")
    if execution:
        lines.append(f"tier: {execution['tier']} ({execution['complexity']} complexity)")
    lines += ["", "### Task", contract["task"], ""]
    lines += _bullets("Acceptance criteria", [f"{item['id']}: {item['description']}"
                                               for item in contract["acceptance_criteria"]])
    lines += _bullets("Context", contract["context"])
    lines += _bullets("Scope", contract["scope"])
    lines += _bullets("Non-goals", contract["non_goals"])
    manifest = contract.get("artifact_manifest")
    if manifest:
        lines += ["### Pinned artifacts", f"manifest: {manifest['manifest_id']} sha256: {manifest['sha256']}"]
        lines += [f"- {entry['artifact']['artifact_id']} "
                  f"({'required' if entry['required'] else 'optional'}): {entry['purpose']}"
                  for entry in manifest["entries"]]
        lines.append("")
    return "\n".join(lines).rstrip()


_PAYLOAD_TEMPLATES = {
    "items": ["## items", "- <one result item>"],
    "conclusions": ["## conclusions", "- <claim>[^e1]"],
    "findings": ["## findings", "- <critical|high|medium|low|info> <location> — <explanation>[^e1]",
                 "  fix: <recommendation>"],
    "changes": ["## changes", "- <path> — <what changed>"],
}


def render_result_format(contract: dict) -> str:
    """A filled-in template of the result; it replaces the JSON Schema for text contracts."""
    payload = PAYLOAD_KEYS[contract["result_type"]]
    revision = contract["contract_revision"][:CONTRACT_PREFIX_CHARS]
    short_lists = "items, limitations or remaining_work items" if payload == "items" else \
        "limitations or remaining_work items"
    return "\n".join([
        "## Result format",
        "Reply with only a plain-text result in this form: no JSON, no code fence, nothing before or after it.",
        "```text",
        f"task: {contract['task_id']}",
        f"attempt: {contract['attempt_id']}",
        f"contract: {revision}",
        "status: <completed|partial|failed|input_required>",
        "question: <input_required only: the exact missing input or decision>",
        "",
        "## summary",
        "<what was done and found>",
        "",
        *_PAYLOAD_TEMPLATES[payload],
        "",
        "## checks",
        "- <passed|failed|not_run>: <check actually run>[^e1]",
        "",
        "## evidence",
        "[^e1]: <command, path:line or URL> — <what it showed>",
        "[^e2]: src/a.py:10-20, src/b.py:30 — <what both showed>",
        "",
        "## artifacts",
        "- <path or artifact:// reference> — <purpose>",
        "",
        "## limitations",
        "- <assumption, gap or failure reason>",
        "",
        "## remaining_work",
        "- <unfinished work>",
        "```",
        "Omit a section or the question line when it would be empty; keep summary and "
        f"{payload}. One list item per '- ' line; indent continuation lines by two spaces. "
        "Where the form shows ' — ', that separator is required: "
        "every evidence line, finding, changes item and artifacts item needs it. "
        "[^id] cites an entry of ## evidence; evidence ids are unique. Every text is at most "
        f"4000 characters; at most 50 {short_lists} and 100 of any other list. "
        "completed: no remaining_work, no question. partial: remaining_work. failed: a "
        "limitation saying why. input_required: question. "
        "Before replying, check every ' — ' the form shows and the 4000-character limit.",
    ])


# Parsing: what the worker returned, back into the validated JSON shape.

def _unfence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```") and "\n" in stripped:
        stripped = stripped[stripped.index("\n") + 1:-3]
    return stripped


def _unique(ids: list[str]) -> list[str]:
    """One item citing the same evidence twice makes one reference, in first-seen order."""
    return list(dict.fromkeys(ids))


def _references(text: str) -> tuple[str, list[str]]:
    ids = _unique(_REFERENCE.findall(text))
    return re.sub(r"[ \t]+\n", "\n", _REFERENCE.sub("", text)).strip(), ids


def _split(text: str, path: str, shape: str) -> tuple[str, str]:
    # A reference may itself contain ' -- ', as `git log --oneline -- path` does, so the
    # documented separator decides wherever it appears; ' -- ' stays accepted without it.
    pattern = _EM_SEPARATOR if _EM_SEPARATOR.search(text) else _SEPARATOR
    parts = pattern.split(text, maxsplit=1)
    if len(parts) != 2:
        raise TextFormatError("separator_required", f"Expected '{shape}' with ' — ' between the parts; "
                              "every evidence line, finding, changes item and artifacts item needs it.",
                              path)
    return parts[0].strip(), parts[1].strip()


def _parsed(items: list[str], path: str, parse: Callable[[str, str], dict],
            errors: list[TextFormatError]) -> list[dict]:
    """Parse every item of a section; a problem is collected and that item is skipped.

    ``path`` is a template the item's own index fills, so a collected problem keeps the
    index of the offending line even though the parsed list is then shorter.
    """
    values: list[dict] = []
    for index, item in enumerate(items):
        try:
            values.append(parse(item, path.format(index)))
        except TextFormatError as error:
            errors.append(error)
    return values


def _list_path(section: str, payload_key: str) -> str:
    """Where a section's items sit in the result model; the payload sits one level deeper."""
    return f"$.result.result.{section}" if section == payload_key else f"$.result.{section}"


def _items(lines: list[str], path: str, errors: list[TextFormatError]) -> list[str]:
    """The '- ' items of one section, each with its indented continuation lines.

    A run of lines that belong to no item is one collected problem, reported at the
    position the next item would take; such a line takes no position of its own.
    """
    items: list[str] = []
    stray = False  # one run of lines that do not belong to the list is one problem
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("- "):
            items.append(line[2:].strip())
            stray = False
        elif line[:1] in (" ", "\t") and items:
            items[-1] += "\n" + line.strip()  # also the fix: line under a stray one
        elif stray:
            continue
        else:
            errors.append(TextFormatError("invalid_item", "List items start with '- '; indent continuation lines.",
                                          f"{path}[{len(items)}]"))
            stray = True
    return items


def dropped_positions(errors: list[TextFormatError]) -> dict[str, list[int]]:
    """The list positions the collected errors removed, by list path.

    A diagnostic names the position of the line that failed, so the positions of the
    entries that did parse have to be translated back from the shorter list (#526).
    """
    positions: dict[str, list[int]] = {}
    for error in errors:
        if error.code in ("invalid_item", "invalid_evidence"):  # a stray line is no entry
            continue
        path, _, index = error.path.rpartition("[")
        if index[:-1].isdigit():
            positions.setdefault(path, []).append(int(index[:-1]))
    return positions


def _evidence(lines: list[str], errors: list[TextFormatError]) -> list[dict]:
    """The entries of ## evidence, each written '[^id]: reference — description'.

    A malformed entry is collected and skipped, keeping the position of its own line.
    A run of lines that are no entry at all is one problem and takes no position.
    """
    entries: list[dict] = []
    index = 0  # the line's own position, kept when an earlier line was skipped
    skipped = False
    for line in lines:
        if not line.strip():
            continue
        match = _EVIDENCE.fullmatch(line.strip()) if not line[:1].isspace() else None
        path = f"$.result.evidence[{index}]"
        if match:
            index += 1
            try:
                reference, description = _split(match.group(2), path, "[^id]: reference — description")
            except TextFormatError as error:
                errors.append(error)
                skipped = True
                continue
            entries.append({"id": match.group(1), "reference": reference, "description": description})
            skipped = False
        elif skipped:
            continue  # a line following one already reported is part of that one problem
        elif line[:1].isspace() and entries:
            entries[-1]["description"] += "\n" + line.strip()
        else:
            errors.append(TextFormatError("invalid_evidence",
                                          "Evidence lines read '[^id]: reference — description'.", path))
            skipped = True  # a line that is no evidence line takes no position of its own
    return entries


def _finding(item: str, path: str) -> dict:
    head, *rest = item.split("\n")
    severity, _, remainder = head.partition(" ")
    if severity not in SEVERITIES:
        raise TextFormatError("invalid_severity", "A finding starts with critical, high, medium, low or info.", path)
    location, explanation = _split(remainder, path, "<severity> <location> — <explanation>")
    recommendation = None
    for line in rest:
        if line.startswith("fix:"):
            recommendation = line[4:].strip()
        elif recommendation is not None:
            recommendation += "\n" + line
        else:
            explanation += "\n" + line
    explanation, explanation_ids = _references(explanation)
    recommendation, recommendation_ids = _references(recommendation or "")
    return {"severity": severity, "location": location, "explanation": explanation,
            "recommendation": recommendation,
            "evidence_ids": _unique(explanation_ids + recommendation_ids)}


def _check(item: str, path: str) -> dict:
    status, separator, name = item.partition(":")
    if not separator or status.strip() not in CHECK_STATES:
        raise TextFormatError("invalid_check", "A check reads '<passed|failed|not_run>: <name>'.", path)
    name, ids = _references(name)
    return {"name": name, "status": status.strip(), "evidence_ids": ids}


def _artifact(item: str, path: str) -> dict:
    """One item of ## artifacts: ``<path> — <purpose>``."""
    return dict(zip(("path", "purpose"), _split(item, path, "<path> — <purpose>")))


def _change(item: str, path: str) -> dict:
    """One item of ## changes: ``<path> — <description>``."""
    return dict(zip(("path", "description"), _split(item, path, "<path> — <description>")))


def _payload(key: str, items: list[str], errors: list[TextFormatError]) -> list:
    if key == "items":
        return items
    if key == "conclusions":
        return [dict(zip(("claim", "evidence_ids"), _references(item))) for item in items]
    if key == "findings":
        return _parsed(items, "$.result.result.findings[{}]", _finding, errors)
    return _parsed(items, "$.result.result.changes[{}]", _change, errors)


def parse_result(text: str, expected: TaskContract) -> tuple[dict, list[TextFormatError]]:
    """The JSON-shaped result a text result stands for, and the per-line problems it carries.

    A problem that leaves the whole result unreadable raises ``TextFormatError``: an unknown
    or repeated section, a malformed, repeated or missing header line, an identity that does
    not match. A problem of one line is collected into the returned list and that line is
    skipped, never guessed at, so one validation can report every one of them (#526).

    The worker writes a prefix of the contract revision; a matching prefix stands for the
    whole revision, and the bundle revision follows from the contract that binds it.
    """
    payload_key = PAYLOAD_KEYS[expected.result_type]
    allowed = {*TEXT_SECTIONS, *LIST_SECTIONS, "evidence", payload_key}
    header: dict[str, str] = {}
    sections: dict[str, list[str]] = {}
    current = None
    for line in _unfence(text).splitlines():
        line = line.rstrip()
        heading = _HEADING.fullmatch(line)
        if heading:
            current = heading.group(1).lower()
            if current not in allowed:
                raise TextFormatError("unknown_section", f"Sections for this result: {', '.join(sorted(allowed))}.")
            if current in sections:
                raise TextFormatError("duplicate_section", "Each section appears once.", f"$.result.{current}")
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
        elif line.strip():
            field = _HEADER.fullmatch(line.strip())
            if not field or field.group(1) not in HEADER_KEYS:
                raise TextFormatError("invalid_header", "Before the first section, only 'key: value' lines for "
                                      "task, attempt, contract, status and question.")
            if field.group(1) in header:
                raise TextFormatError("duplicate_header", "Each header line appears once.", f"$.result.{field.group(1)}")
            header[field.group(1)] = field.group(2).strip()
    for key in ("task", "attempt", "contract", "status"):
        if not header.get(key):
            raise TextFormatError("header_required", f"The result needs a '{key}:' line.", f"$.result.{key}")
    prefix = header["contract"].lower()
    if not (len(prefix) >= CONTRACT_PREFIX_CHARS and re.fullmatch(r"[a-f0-9]{1,64}", prefix)
            and expected.contract_revision.startswith(prefix)):
        raise TextFormatError("identity_mismatch", "Does not match the retained contract.",
                              "$.result.contract_revision")
    if "question" in header and "question" in sections:
        raise TextFormatError("duplicate_header", "Give the question once.", "$.result.question")
    question = header.get("question") or "\n".join(sections.get("question", [])).strip() or None
    errors: list[TextFormatError] = []
    lists = {name: _items(sections.get(name, []), _list_path(name, payload_key), errors)
             for name in (*LIST_SECTIONS, payload_key)}
    return {
        "protocol": PROTOCOL, "task_id": header["task"], "attempt_id": header["attempt"],
        "bundle_revision": expected.bundle_revision, "contract_revision": expected.contract_revision,
        "status": header["status"], "question": question,
        "result": {"summary": "\n".join(sections.get("summary", [])).strip(),
                   payload_key: _payload(payload_key, lists[payload_key], errors)},
        "evidence": _evidence(sections.get("evidence", []), errors),
        "artifacts": _parsed(lists["artifacts"], "$.result.artifacts[{}]", _artifact, errors),
        "checks": _parsed(lists["checks"], "$.result.checks[{}]", _check, errors),
        "limitations": lists["limitations"], "remaining_work": lists["remaining_work"],
    }, errors


def is_text_result(value) -> bool:
    """A string that is not a JSON object is read as a text result. A fenced JSON object stays
    JSON, so the strict JSON parser reports the fence."""
    return isinstance(value, str) and not _unfence(value).lstrip().startswith("{")
