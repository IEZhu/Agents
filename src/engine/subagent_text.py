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

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
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
@dataclass(frozen=True)
class Separator:
    """One accepted spelling of the separator between the two parts of a line."""
    spelling: str
    pattern: re.Pattern
    write_it: bool
    note: str


# The one source of the separator rule. ``_split`` matches these in order against the copy
# whose code spans are masked, so this tuple is the precedence, and `render_result_format`
# generates the rule and its examples from it. Three review rounds of #595 each corrected
# one of several hand-written statements of this rule and left another contradicting the
# parser; a spelling now reaches the worker's text and the diagnostics from here (#596).
SEPARATORS = (
    Separator(" — ", re.compile(r"\s+—\s+"), write_it=True,
              note="one space, an em dash (U+2014) and one space"),
    Separator(" -- ", re.compile(r"\s+--\s+"), write_it=False,
              note="the spelling accepted before this contract named one"),
)
WRITE_SEPARATOR = next(s for s in SEPARATORS if s.write_it)
_HEADING = re.compile(r"##\s+([A-Za-z_]+)\s*")
_HEADER = re.compile(r"([a-z_]+):[ \t]*(.*)")
_EVIDENCE = re.compile(r"\[\^([^\]]*)\]:[ \t]*(.*)")
_BACKTICK_RUN = re.compile(r"`+")
EXCERPT_CHARS = 120  # enough of a line to find it, short enough for a 240-character diagnostic


def excerpt(text: str) -> str:
    """One of the worker's own lines, collapsed onto one line and shortened for a diagnostic.

    The line is quoted as the worker wrote it: a reference inside it stays a reference and is
    never read, so naming the offending line leaves validation structural (#526).
    """
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= EXCERPT_CHARS else collapsed[:EXCERPT_CHARS - 1] + "\u2026"


class TextFormatError(ValueError):
    """A result that does not follow the text format; ``path`` names the offending part.

    ``line`` is that part as the worker wrote it, so the diagnostic can name the offending
    line and not only the position the parsed result would have kept it in (#526).
    """

    def __init__(self, code: str, message: str, path: str = "$.result", line: str = ""):
        super().__init__(message)
        self.code, self.path, self.line = code, path, excerpt(line)


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


def _payload_template(key: str) -> list[str]:
    """The payload section a worker copies. Built per call, so a test can move the source."""
    separator = WRITE_SEPARATOR.spelling
    return {
        "items": ["## items", "- <one result item>"],
        "conclusions": ["## conclusions", "- <claim>[^e1]"],
        "findings": ["## findings",
                     f"- <critical|high|medium|low|info> <location>{separator}<explanation>[^e1]",
                     "  fix: <recommendation>"],
        "changes": ["## changes", f"- <path>{separator}<what changed>"],
    }[key]


# Every labelled example below is executed by tests/test_subagent_exchange.py against the
# parser it illustrates, so a claim in this text cannot drift from the code without a test
# failing. Generation covers the spellings, which come from SEPARATORS; execution covers the
# conditions, which live in _split's control flow and cannot be read off a pattern (#596).
EXAMPLE_LABEL = re.compile(r" {2}(right|wrong) \(([^)]*)\): (.*)")


def separator_rule() -> list[str]:
    """The sentences that state the separator rule, generated from ``SEPARATORS``.

    Both surfaces that state the rule compose it from here: the rendered package a worker
    reads, below, and the block of ``docs/subagent-contract.md`` that
    ``src.engine.subagent_contract_text`` writes. The conditions in these sentences are not
    derived from the patterns — "spaced" and "outside a code span" live in ``_split``'s
    control flow — so the labelled examples that follow them are executed by
    ``tests/test_subagent_exchange.py`` instead of being described a second time (#596).
    """
    write = WRITE_SEPARATOR.spelling
    sentences = [
        f"Where the form shows '{write}', write exactly that: {WRITE_SEPARATOR.note}. "
        "Not a plain hyphen, not an en dash '–', not nothing at all. It has to stand in "
        "the line's own text, outside any `code span`, because that is where it is "
        "looked for. Every evidence line, finding, changes item and artifacts item "
        "needs one."]
    sentences += [f"'{separator.spelling}' ({separator.note}) still separates a line where no "
                  f"'{write}' appears outside a code span, but do not write it."
                  for separator in SEPARATORS if not separator.write_it]
    sentences.append("A line where none of them appears outside a code span is reported as "
                     "separator_required.")
    return sentences


def _separator_rules() -> list[str]:
    """The rule from ``separator_rule`` and the examples that execute its conditions."""
    tolerated = [s for s in SEPARATORS if not s.write_it]
    write = WRITE_SEPARATOR.spelling
    lines = ["### The separator", *separator_rule(),
             "These examples are the two parts of one line:"]
    lines += [f"  right ('{write}' outside a code span): src/app.py:12{write}The token is logged."]
    lines += [f"  right ('{s.spelling}' outside a code span, with no '{write}' there): "
              f"src/app.py:12{s.spelling}The token is logged." for s in tolerated]
    lines += [f"  right (the em dash sits inside a span, so '{s.spelling}' decides): "
              f"`a — b`{s.spelling}The token is logged." for s in tolerated]
    lines += [f"  right (the em dash has no spaces, so '{s.spelling}' decides): "
              f"a—b{s.spelling}The token is logged." for s in tolerated]
    lines += ["  wrong (no separator at all): src/app.py:12 The token is logged.",
              "  wrong (the only spaced em dash is inside a span): "
              "`src/app.py:12 — the header` is logged."]
    return lines


# Two line shapes cost most of the discarded worker runs of #526: a findings or evidence
# line without the separator, and a finding whose 'fix:' line is missing or empty. Both are
# stated here with a counter-example, because that is what a worker reads before replying.
def _payload_rules(key: str) -> list[str]:
    """The rules only one payload needs; empty for the others."""
    if key != "findings":
        return []
    separator = WRITE_SEPARATOR.spelling
    return [
        "",
        "### The fix: line",
        "Every finding carries its own 'fix:' line, indented by two spaces, with a non-empty "
        "recommendation on it. A finding that only confirms something takes one too, saying "
        "what to keep; a missing or empty 'fix:' rejects the finding.",
        "  right (a confirming finding names what to keep): "
        f"- info src/app.py:40{separator}The guard is correct.[^e2]",
        "         fix: Keep the guard as it is.",
        "  wrong (the fix: line has no recommendation on it): "
        f"- info src/app.py:40{separator}The guard is correct.[^e2]",
        "         fix:",
    ]


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
        *_payload_template(payload),
        "",
        "## checks",
        "- <passed|failed|not_run>: <check actually run>[^e1]",
        "",
        "## evidence",
        f"[^e1]: <command, path:line or URL>{WRITE_SEPARATOR.spelling}<what it showed>",
        f"[^e2]: src/a.py:10-20, src/b.py:30{WRITE_SEPARATOR.spelling}<what both showed>",
        "",
        "## artifacts",
        f"- <path or artifact:// reference>{WRITE_SEPARATOR.spelling}<purpose>",
        "",
        "## limitations",
        "- <assumption, gap or failure reason>",
        "",
        "## remaining_work",
        "- <unfinished work>",
        "```",
        "Omit a section or the question line when it would be empty; keep summary and "
        f"{payload}. One list item per '- ' line; indent continuation lines by two spaces. "
        "[^id] cites an entry of ## evidence; evidence ids are unique. Every text is at most "
        f"4000 characters; at most 50 {short_lists} and 100 of any other list. "
        "completed: no remaining_work, no question. partial: remaining_work. failed: a "
        "limitation saying why. input_required: question.",
        "",
        *_separator_rules(),
        *_payload_rules(payload),
        "",
        f"Before replying, check every '{WRITE_SEPARATOR.spelling}'"
        + (" and every 'fix:' line" if payload == "findings" else "")
        + " the form shows and the 4000-character limit.",
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


def _outside_code_spans(text: str) -> str:
    """``text`` with every code span's characters replaced, so none of them can match.

    A span is a run of backticks closed by a run of the same length, the rule Markdown
    itself uses, so a ``-delimited span may hold a single backtick and still be one part of
    the line. Lengths are preserved, so a match in the result indexes the original text,
    and a run with no closer masks nothing, because it must not swallow the rest of the
    line. The runs are indexed once and each closer is found by bisection, so a result full
    of stray backticks cannot make this quadratic.
    """
    runs = [match.span() for match in _BACKTICK_RUN.finditer(text)]
    if not runs:
        return text
    by_length: dict[int, list[int]] = {}
    for index, (start, end) in enumerate(runs):
        by_length.setdefault(end - start, []).append(index)
    masked = list(text)
    index = 0
    while index < len(runs):
        start, end = runs[index]
        same_length = by_length[end - start]
        candidate = bisect_right(same_length, index)
        if candidate == len(same_length):
            index += 1  # a run that nothing closes is literal text, not a span
            continue
        closer = same_length[candidate]
        masked[start:runs[closer][1]] = "\x00" * (runs[closer][1] - start)
        index = closer + 1  # runs inside the span are its content, not openers
    return "".join(masked)


def _split(text: str, path: str, shape: str, line: str = "") -> tuple[str, str]:
    # The spellings of ``SEPARATORS`` are tried in order, so the one to write wins wherever
    # it appears and the older one decides only without it. A reference may itself contain
    # ' -- ', as `git log --oneline -- path` does, which is why the order matters.
    # All of them are looked for outside code spans: a worker who writes a location or a
    # command in backticks means one part, and splitting inside it would report neither.
    outside = _outside_code_spans(text)
    found = next((match for separator in SEPARATORS
                  if (match := separator.pattern.search(outside))), None)
    if not found:
        raise TextFormatError("separator_required",
                              f"Expected '{shape}', with '{WRITE_SEPARATOR.spelling}' "
                              "between the two parts.", path, line or text)
    return text[:found.start()].strip(), text[found.end():].strip()


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
                                          f"{path}[{len(items)}]", line))
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


def _evidence(lines: list[str], errors: list[TextFormatError], sources: list[str]) -> list[dict]:
    """The entries of ## evidence, each written '[^id]: reference — description'.

    A malformed entry is collected and skipped, keeping the position of its own line.
    A run of lines that are no entry at all is one problem and takes no position.
    ``sources`` receives the line each position came from, for the diagnostics (#526).
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
            sources.append(line.strip())
            try:
                reference, description = _split(match.group(2), path,
                                                f"[^id]: reference{WRITE_SEPARATOR.spelling}description", line)
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
                                          f"Evidence lines read '[^id]: reference"
                                          f"{WRITE_SEPARATOR.spelling}description'.",
                                          path, line))
            skipped = True  # a line that is no evidence line takes no position of its own
    return entries


def _finding(item: str, path: str) -> dict:
    head, *rest = item.split("\n")
    severity, _, remainder = head.partition(" ")
    if severity not in SEVERITIES:
        raise TextFormatError("invalid_severity", "A finding starts with critical, high, medium, low or info.",
                              path, item)
    location, explanation = _split(remainder, path,
                                  f"<severity> <location>{WRITE_SEPARATOR.spelling}<explanation>", item)
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
    if not recommendation:
        raise TextFormatError("recommendation_required",
                              "A finding needs its own 'fix:' line with a recommendation on it.",
                              path, item)
    return {"severity": severity, "location": location, "explanation": explanation,
            "recommendation": recommendation,
            "evidence_ids": _unique(explanation_ids + recommendation_ids)}


def _check(item: str, path: str) -> dict:
    status, separator, name = item.partition(":")
    if not separator or status.strip() not in CHECK_STATES:
        raise TextFormatError("invalid_check", "A check reads '<passed|failed|not_run>: <name>'.", path, item)
    name, ids = _references(name)
    return {"name": name, "status": status.strip(), "evidence_ids": ids}


def _artifact(item: str, path: str) -> dict:
    """One item of ## artifacts: ``<path> — <purpose>``."""
    return dict(zip(("path", "purpose"),
                    _split(item, path, f"<path>{WRITE_SEPARATOR.spelling}<purpose>")))


def _change(item: str, path: str) -> dict:
    """One item of ## changes: ``<path> — <description>``."""
    return dict(zip(("path", "description"),
                    _split(item, path, f"<path>{WRITE_SEPARATOR.spelling}<description>")))


def _payload(key: str, items: list[str], errors: list[TextFormatError]) -> list:
    if key == "items":
        return items
    if key == "conclusions":
        return [dict(zip(("claim", "evidence_ids"), _references(item))) for item in items]
    if key == "findings":
        return _parsed(items, "$.result.result.findings[{}]", _finding, errors)
    return _parsed(items, "$.result.result.changes[{}]", _change, errors)


def parse_result(text: str, expected: TaskContract
                 ) -> tuple[dict, list[TextFormatError], dict[str, list[str]]]:
    """The JSON-shaped result a text result stands for, and the per-line problems it carries.

    A problem that leaves the whole result unreadable raises ``TextFormatError``: an unknown
    or repeated section, a malformed, repeated or missing header line, an identity that does
    not match. A problem of one line is collected into the returned list and that line is
    skipped, never guessed at, so one validation can report every one of them (#526).

    The third value holds every list's source lines, by list path and in source order, so a
    schema error on one of them can name the line and not only its position (#526).

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
                raise TextFormatError("unknown_section", f"Sections for this result: {', '.join(sorted(allowed))}.",
                                      "$.result", line)
            if current in sections:
                raise TextFormatError("duplicate_section", "Each section appears once.",
                                      f"$.result.{current}", line)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
        elif line.strip():
            field = _HEADER.fullmatch(line.strip())
            if not field or field.group(1) not in HEADER_KEYS:
                raise TextFormatError("invalid_header", "Before the first section, only 'key: value' lines for "
                                      "task, attempt, contract, status and question.", "$.result", line)
            if field.group(1) in header:
                raise TextFormatError("duplicate_header", "Each header line appears once.",
                                      f"$.result.{field.group(1)}", line)
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
    sources = {_list_path(name, payload_key): items for name, items in lists.items()}
    evidence_lines: list[str] = []
    sources["$.result.evidence"] = evidence_lines
    return {
        "protocol": PROTOCOL, "task_id": header["task"], "attempt_id": header["attempt"],
        "bundle_revision": expected.bundle_revision, "contract_revision": expected.contract_revision,
        "status": header["status"], "question": question,
        "result": {"summary": "\n".join(sections.get("summary", [])).strip(),
                   payload_key: _payload(payload_key, lists[payload_key], errors)},
        "evidence": _evidence(sections.get("evidence", []), errors, evidence_lines),
        "artifacts": _parsed(lists["artifacts"], "$.result.artifacts[{}]", _artifact, errors),
        "checks": _parsed(lists["checks"], "$.result.checks[{}]", _check, errors),
        "limitations": lists["limitations"], "remaining_work": lists["remaining_work"],
    }, errors, sources


def is_text_result(value) -> bool:
    """A string that is not a JSON object is read as a text result. A fenced JSON object stays
    JSON, so the strict JSON parser reports the fence."""
    return isinstance(value, str) and not _unfence(value).lstrip().startswith("{")
