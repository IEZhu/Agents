"""The compact worker exchange: text format, file delivery and validation by reference."""
import json
import os
import stat
import time
from pathlib import Path

import pytest

import src.server as server
from src.engine import subagent_exchange
from src.engine.subagent import prepare_subagent
from src.engine.subagent_text import CONTRACT_PREFIX_CHARS, PAYLOAD_KEYS
from src.engine.subagent_validation import contract_fingerprint, validate_result
from src.schemas.subagent import MAX_ERRORS, PreparedPackage
from tests.test_subagent import codes, prepared, request, worker_tree  # noqa: F401

PAYLOADS = {
    "generic": "## items\n- First item\n- Second item\n  continued",
    "research": "## conclusions\n- The cache is cold.[^e1]\n- Restart helps.[^e1][^e2]",
    "review": "## findings\n- high src/app.py:12 — The token is logged.[^e1]\n  fix: Drop it from the message.",
    "implementation": "## changes\n- src/app.py — Removed the token from the log line",
}


def text_result(package, kind=None, status="completed", payload=None, extra=""):
    kind = kind or package.contract.result_type
    sections = [f"task: {package.task_id}", f"attempt: {package.attempt_id}",
                f"contract: {package.contract_revision[:CONTRACT_PREFIX_CHARS]}", f"status: {status}"]
    if status == "input_required":
        sections.append("question: Which branch?")
    sections += ["", "## summary", "Checked the log path.", "", payload or PAYLOADS[kind], "",
                 "## checks", "- passed: Read the code.[^e1]", "- not_run: Live run.", "",
                 "## evidence", "[^e1]: src/app.py:12 — logger.info prints the header",
                 "[^e2]: pytest tests/test_app.py -q — 12 passed"]
    if status == "partial":
        sections += ["", "## remaining_work", "- Fix the second logger"]
    if status == "failed":
        sections += ["", "## limitations", "- The file could not be read"]
    return "\n".join(sections) + extra


async def call(name, arguments):
    content, structured = await server.mcp.call_tool(name, arguments)
    assert json.loads(content[0].text) == structured
    return structured


@pytest.mark.parametrize("kind", list(PAYLOAD_KEYS))
@pytest.mark.parametrize("status", ["completed", "partial", "input_required", "failed"])
def test_text_results_validate_for_every_payload_and_status(worker_tree, kind, status):
    package = prepared(result_type=kind)
    response = validate_result(package.contract.model_dump(mode="json"), text_result(package, status=status))
    assert response.valid, response.errors
    assert len(response.result_sha256) == 64


def test_text_result_maps_to_the_closed_model(worker_tree):
    from src.engine.subagent_text import parse_result

    package = prepared(result_type="review")
    value, errors, sources = parse_result(text_result(package), package.contract)
    assert errors == []
    assert sources["$.result.result.findings"] == [
        "high src/app.py:12 — The token is logged.[^e1]\nfix: Drop it from the message."]
    assert sources["$.result.evidence"][1] == "[^e2]: pytest tests/test_app.py -q — 12 passed"
    assert value["contract_revision"] == package.contract_revision
    assert value["bundle_revision"] == package.descriptor.bundle_revision
    assert value["result"]["findings"] == [{
        "severity": "high", "location": "src/app.py:12", "explanation": "The token is logged.",
        "recommendation": "Drop it from the message.", "evidence_ids": ["e1"]}]
    assert value["checks"][1] == {"name": "Live run.", "status": "not_run", "evidence_ids": []}
    assert value["evidence"][1]["reference"] == "pytest tests/test_app.py -q"
    assert value["question"] is None


def test_text_package_carries_no_json_contract_or_schema(worker_tree):
    package = prepared()
    assert package.contract.result_format == "text"
    assert "## Assignment\n" in package.worker_context and "## Result format" in package.worker_context
    assert "## Assignment contract" not in package.worker_context
    assert "JSON Schema" not in package.worker_context
    assert package.contract_revision[:CONTRACT_PREFIX_CHARS] in package.worker_context
    assert package.contract_revision not in package.worker_context
    legacy = prepared(result_format="json")
    assert "result_format" not in legacy.contract.model_dump(mode="json")
    assert contract_fingerprint(legacy.contract.model_dump(mode="json")) == legacy.contract_revision
    assert package.context_chars < legacy.context_chars


@pytest.mark.parametrize("mutate,code", [
    (lambda text: text.replace("## checks", "## notes"), "unknown_section"),
    (lambda text: text.replace("task: task-1\n", ""), "header_required"),
    (lambda text: text.replace("status: completed", "status: completed\nstatus: failed"), "duplicate_header"),
    (lambda text: "Preamble\n" + text, "invalid_header"),
    (lambda text: text.replace("[^e1]: src/app.py:12 — logger", "[^e1]: src/app.py:12 logger"), "separator_required"),
    (lambda text: text.replace("- high src", "- severe src"), "invalid_severity"),
    (lambda text: text.replace("- passed: Read", "- done: Read"), "invalid_check"),
    (lambda text: text.replace("logged.[^e1]", "logged.[^e9]"), "unknown_evidence_id"),
    (lambda text: text.replace("  fix: Drop it from the message.", ""), "recommendation_required"),
    (lambda text: text.replace("## checks\n", "## checks\nstray line\n"), "invalid_item"),
    (lambda text: text + "\nstray line", "invalid_evidence"),
])
def test_text_format_errors_name_the_problem(worker_tree, mutate, code):
    package = prepared(result_type="review")
    response = validate_result(package.contract.model_dump(mode="json"), mutate(text_result(package)))
    assert not response.valid and code in codes(response), response.errors


def test_one_validation_reports_every_malformed_line_and_the_schema(worker_tree):
    """Three evidence lines without the separator and a too-long summary, in one round."""
    package = prepared(result_type="implementation")
    text = text_result(package).replace(
        "[^e1]: src/app.py:12 — logger.info prints the header\n"
        "[^e2]: pytest tests/test_app.py -q — 12 passed",
        "[^e1]: src/app.py:12 logger.info prints the header\n"
        "[^e2]: pytest tests/test_app.py -q 12 passed\n"
        "[^e3]: src/schemas.py:7 the third line").replace("Checked the log path.", "x" * 4001)
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert not response.valid
    assert [(error.path, error.code) for error in response.errors] == [
        ("$.result.evidence[0]", "separator_required"),
        ("$.result.evidence[1]", "separator_required"),
        ("$.result.evidence[2]", "separator_required"),
        ("$.result.result.summary", "string_too_long")], response.errors
    assert len(response.result_sha256) == 64


def test_a_malformed_item_and_a_malformed_evidence_line_arrive_together(worker_tree):
    """The round that fixed changes[2] only to fail again on evidence[1] (#526)."""
    package = prepared(result_type="implementation")
    payload = ("## changes\n- src/a.py — First change\n- src/b.py — Second change\n"
               "- the directory that holds the environments")
    text = text_result(package, payload=payload).replace(
        "[^e2]: pytest tests/test_app.py -q — 12 passed",
        "[^e2]: src/environments.py:136-146 (managed), 301-311 (complete)")
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert not response.valid
    assert {(error.path, error.code) for error in response.errors} == {
        ("$.result.result.changes[2]", "separator_required"),
        ("$.result.evidence[1]", "separator_required")}, response.errors


def test_a_continuation_of_a_reported_line_is_not_a_second_problem(worker_tree):
    """The indented rest of a line already reported belongs to that one diagnostic."""
    package = prepared(result_type="review")
    evidence = text_result(package).replace(
        "[^e1]: src/app.py:12 — logger.info prints the header",
        "[^e1]: src/app.py:12 logger.info prints the header\n  and again in the body")
    response = validate_result(package.contract.model_dump(mode="json"), evidence)
    assert [(error.path, error.code) for error in response.errors] == [
        ("$.result.evidence[0]", "separator_required")], response.errors

    item = text_result(package).replace("## checks\n", "## checks\nstray line\n  its continuation\n")
    response = validate_result(package.contract.model_dump(mode="json"), item)
    assert [(error.path, error.code) for error in response.errors] == [
        ("$.result.checks[0]", "invalid_item")], response.errors


def test_the_combined_diagnostics_stay_within_the_error_cap(worker_tree):
    package = prepared(result_type="implementation")
    broken = "\n".join(f"[^e{index}]: src/app.py:{index} line {index}" for index in range(30))
    text = text_result(package).replace("[^e1]: src/app.py:12 — logger.info prints the header", broken)
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert not response.valid and len(response.errors) == MAX_ERRORS, response.errors
    assert len(response.result_sha256) == 64


def test_a_finding_may_cite_the_same_evidence_twice(worker_tree):
    """A repeated [^id] in one item is one reference; JSON results keep their own check."""
    from src.engine.subagent_text import parse_result

    package = prepared(result_type="review")
    text = text_result(package, payload="## findings\n- high src/app.py:12 — The token is logged.[^e1]\n"
                                        "  fix: Drop it from the message.[^e1]")
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert response.valid, response.errors
    value, errors, _ = parse_result(text, package.contract)
    assert errors == [] and value["result"]["findings"][0]["evidence_ids"] == ["e1"]


def test_the_separator_diagnostic_names_the_offending_line(worker_tree):
    """A path into the parsed model does not say which line to correct; the line does (#526)."""
    package = prepared(result_type="review")
    text = text_result(package).replace("[^e1]: src/app.py:12 — logger", "[^e1]: src/app.py:12 logger")
    response = validate_result(package.contract.model_dump(mode="json"), text)
    error = next(error for error in response.errors if error.code == "separator_required")
    assert error.path == "$.result.evidence[0]"
    assert "'[^id]: reference — description'" in error.message
    assert error.message.endswith("Line: [^e1]: src/app.py:12 logger.info prints the header")
    assert len(error.message) <= 240, error.message


def test_a_finding_without_the_separator_names_its_own_line(worker_tree):
    """One of the two line shapes that discarded six of eight worker runs of #526."""
    package = prepared(result_type="review")
    text = text_result(package, payload="## findings\n- high src/app.py:12 The token is logged.[^e1]\n"
                                        "  fix: Drop it from the message.")
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert [(error.path, error.code) for error in response.errors] == [
        ("$.result.result.findings[0]", "separator_required")], response.errors
    message = response.errors[0].message
    assert "'<severity> <location> — <explanation>'" in message
    assert "Line: high src/app.py:12 The token is logged.[^e1] fix: Drop it from the message." in message


@pytest.mark.parametrize("fix", ["\n  fix:", "\n  fix: [^e1]", ""])
def test_an_empty_or_missing_fix_line_is_rejected_by_its_own_name(worker_tree, fix):
    """The other one: pydantic called it `string_too_short` on `.recommendation` (#526)."""
    package = prepared(result_type="review")
    text = text_result(package, payload="## findings\n- high src/app.py:12 — The token is logged.[^e1]" + fix)
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert [(error.path, error.code) for error in response.errors] == [
        ("$.result.result.findings[0]", "recommendation_required")], response.errors
    message = response.errors[0].message
    assert "two-space-indented 'fix:' line" in message
    assert "Line: high src/app.py:12 — The token is logged.[^e1]" in message


def test_a_schema_error_names_and_shortens_the_line_that_holds_it(worker_tree):
    """A size error on one item also says which line it is, bounded for the 240-char field."""
    package = prepared(result_type="implementation")
    text = text_result(package, payload="## changes\n- src/c.py — " + "verbose " * 600)
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert [(error.path, error.code) for error in response.errors] == [
        ("$.result.result.changes[0].description", "string_too_long")], response.errors
    message = response.errors[0].message
    assert " Line: src/c.py — verbose verbose" in message
    assert message.endswith("\u2026") and len(message) <= 240, message


def test_the_result_format_states_both_line_shapes_with_a_counter_example(worker_tree):
    """The package, not the worker, is where these two defects live: spell the shapes out."""
    from src.engine.subagent_text import render_result_format

    rendered = render_result_format(prepared(result_type="review").contract.model_dump(mode="json"))
    assert "one space, an em dash (U+2014, '—') and one space" in rendered
    assert "outside any `code span`" in rendered
    assert "  wrong: - high src/app.py:12 The token is logged.[^e1]   (no separator: rejected)" in rendered
    assert "A finding that only confirms something takes one too" in rendered
    assert "  wrong: - info src/app.py:40 — The guard is correct.[^e2]\n         fix:" in rendered
    assert "check every ' — ' and every 'fix:' line the form shows" in rendered

    other = render_result_format(prepared(result_type="research").contract.model_dump(mode="json"))
    assert "### The separator" in other and "### The fix: line" not in other


def test_the_result_format_shows_several_locations_and_a_pre_reply_check(worker_tree):
    from src.engine.subagent_text import render_result_format

    package = prepared(result_type="implementation")
    rendered = render_result_format(package.contract.model_dump(mode="json"))
    assert "[^e2]: src/a.py:10-20, src/b.py:30 — <what both showed>" in rendered
    assert "Before replying, check every ' — ' the form shows and the 4000-character limit." in rendered
    assert rendered in package.worker_context


@pytest.mark.parametrize("section,prefix", [("evidence", "$.result.evidence"),
                                            ("changes", "$.result.result.changes"),
                                            ("checks", "$.result.checks")])
def test_a_schema_error_after_a_skipped_entry_keeps_the_source_position(worker_tree, section, prefix):
    """The over-long entry is the third line, not the second one the shortened list holds."""
    package = prepared(result_type="implementation")
    long_text = "x" * 4001
    if section == "evidence":
        text = text_result(package).replace(
            "[^e1]: src/app.py:12 — logger.info prints the header\n"
            "[^e2]: pytest tests/test_app.py -q — 12 passed",
            "[^e1]: src/app.py:12 logger.info prints the header\n"
            "[^e2]: pytest tests/test_app.py -q — 12 passed\n"
            f"[^e3]: src/schemas.py:7 — {long_text}")
    elif section == "changes":
        text = text_result(package, payload="## changes\n- the directory that holds the environments\n"
                                            "- src/b.py — Second change\n"
                                            f"- src/c.py — {long_text}")
    else:
        text = text_result(package).replace(
            "- passed: Read the code.[^e1]\n- not_run: Live run.",
            "- done: Read the code.[^e1]\n- not_run: Live run.\n"
            f"- passed: {long_text}")
    response = validate_result(package.contract.model_dump(mode="json"), text)
    first = "invalid_check" if section == "checks" else "separator_required"
    field = "name" if section == "checks" else "description"
    assert [(error.path, error.code) for error in response.errors] == [
        (f"{prefix}[0]", first),
        (f"{prefix}[2].{field}", "string_too_long")], response.errors
    assert all(" Line: " in error.message for error in response.errors), response.errors


def test_a_repeated_citation_in_a_check_or_a_conclusion_counts_once(worker_tree):
    from src.engine.subagent_text import parse_result

    package = prepared(result_type="research")
    text = text_result(package, payload="## conclusions\n- The cache is cold.[^e1][^e1]").replace(
        "- passed: Read the code.[^e1]", "- passed: Read the code.[^e1][^e1]")
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert response.valid, response.errors
    value, errors, _ = parse_result(text, package.contract)
    assert errors == []
    assert value["result"]["conclusions"][0]["evidence_ids"] == ["e1"]
    assert value["checks"][0]["evidence_ids"] == ["e1"]


def test_a_json_result_still_rejects_a_repeated_citation(worker_tree):
    from src.engine.subagent_text import parse_result

    package = prepared()
    value, errors, _ = parse_result(text_result(package), package.contract)
    assert errors == []
    value["checks"][0]["evidence_ids"] = ["e1", "e1"]
    response = validate_result(package.contract.model_dump(mode="json"), value)
    assert not response.valid and "duplicate_evidence_reference" in codes(response), response.errors


def test_an_identity_problem_arrives_with_the_format_errors(worker_tree):
    """A wrong header reads only the header, so it does not wait for the next round."""
    package = prepared()
    text = text_result(package).replace(f"task: {package.task_id}", "task: another-task").replace(
        "[^e1]: src/app.py:12 — logger", "[^e1]: src/app.py:12 logger")
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert not response.valid
    assert codes(response) >= {"identity_mismatch", "separator_required"}, response.errors


def test_a_structural_limit_arrives_with_the_format_errors(worker_tree):
    package = prepared(result_type="implementation")
    bullets = "\n".join(f"- src/app{index}.py — Change {index}" for index in range(101))
    text = text_result(package, payload=f"## changes\n{bullets}\n- the directory of environments")
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert not response.valid
    assert codes(response) >= {"array_too_large", "separator_required"}, response.errors


def test_consecutive_stray_lines_report_one_problem(worker_tree):
    package = prepared(result_type="review")
    text = text_result(package).replace("## checks\n", "## checks\nfirst stray\nsecond stray\nthird stray\n")
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert [(error.path, error.code) for error in response.errors] == [
        ("$.result.checks[0]", "invalid_item")], response.errors


def test_consecutive_lines_that_are_no_evidence_report_one_problem(worker_tree):
    """A prose block under ## evidence is one problem, a second bad [^id]: line is another."""
    package = prepared(result_type="review")
    text = text_result(package).replace(
        "[^e2]: pytest tests/test_app.py -q — 12 passed",
        "first stray\nsecond stray\nthird stray\n[^e2]: pytest tests/test_app.py -q — 12 passed")
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert [(error.path, error.code) for error in response.errors] == [
        ("$.result.evidence[1]", "invalid_evidence")], response.errors

    text = text_result(package).replace(
        "[^e2]: pytest tests/test_app.py -q — 12 passed",
        "a stray line\n[^e2]: pytest tests/test_app.py -q 12 passed")
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert [(error.path, error.code) for error in response.errors] == [
        ("$.result.evidence[1]", "invalid_evidence"),
        ("$.result.evidence[1]", "separator_required")], response.errors


def test_a_line_that_is_no_evidence_line_takes_no_position(worker_tree):
    """The schema error still names the second evidence line, which a stray line precedes."""
    package = prepared(result_type="review")
    text = text_result(package).replace(
        "[^e2]: pytest tests/test_app.py -q — 12 passed",
        f"a stray line\n[^e2]: pytest tests/test_app.py -q — {'y' * 4001}")
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert [(error.path, error.code) for error in response.errors] == [
        ("$.result.evidence[1]", "invalid_evidence"),
        ("$.result.evidence[1].description", "string_too_long")], response.errors


def test_a_continuation_after_a_stray_line_stays_with_its_item(worker_tree):
    """The fix: line belongs to the finding above the unindented line, not to a second problem."""
    package = prepared(result_type="review")
    text = text_result(package, payload="## findings\n- high src/app.py:12 — The token is logged and\n"
                                        "the header too.\n  fix: Drop both.")
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert [(error.path, error.code) for error in response.errors] == [
        ("$.result.result.findings[1]", "invalid_item")], response.errors


def test_a_continuation_still_reaches_the_entry_after_a_skipped_line(worker_tree):
    from src.engine.subagent_text import parse_result

    package = prepared()
    text = text_result(package).replace(
        "[^e1]: src/app.py:12 — logger.info prints the header",
        "[^e1]: src/app.py:12 logger.info prints the header").replace(
        "[^e2]: pytest tests/test_app.py -q — 12 passed",
        "[^e2]: pytest tests/test_app.py -q — 12 passed\n  and 3 skipped")
    value, errors, _ = parse_result(text, package.contract)
    assert [(error.path, error.code) for error in errors] == [
        ("$.result.evidence[0]", "separator_required")]
    assert value["evidence"][0]["description"] == "12 passed\nand 3 skipped"


def test_the_documented_separator_decides_over_a_double_hyphen_in_the_reference(worker_tree):
    """A reference carries its own ' -- ', as `git log --oneline -- path` does."""
    from src.engine.subagent_text import parse_result

    package = prepared(result_type="review")
    assert "that separator is required" in package.worker_context
    text = text_result(package).replace(
        "[^e1]: src/app.py:12 — logger.info prints the header",
        "[^e1]: git log --oneline -- src/app.py — logger.info prints the header")
    assert validate_result(package.contract.model_dump(mode="json"), text).valid
    evidence = parse_result(text, package.contract)[0]["evidence"][0]
    assert evidence["reference"] == "git log --oneline -- src/app.py"
    assert evidence["description"] == "logger.info prints the header"


def test_a_double_hyphen_still_separates_when_no_em_dash_is_present(worker_tree):
    """Accepted before this contract named one spelling; workers using it keep working."""
    package = prepared(result_type="review")
    text = text_result(package).replace("[^e1]: src/app.py:12 — logger", "[^e1]: src/app.py:12 -- logger")
    assert validate_result(package.contract.model_dump(mode="json"), text).valid


@pytest.mark.parametrize("prefix", ["", "short", "0" * CONTRACT_PREFIX_CHARS])
def test_text_contract_prefix_must_match(worker_tree, prefix):
    package = prepared()
    text = text_result(package).replace(package.contract_revision[:CONTRACT_PREFIX_CHARS], prefix or "", 1)
    response = validate_result(package.contract.model_dump(mode="json"), text)
    assert not response.valid and codes(response) & {"identity_mismatch", "header_required"}


def test_text_result_tolerates_a_fence_but_json_does_not(worker_tree):
    package = prepared()
    contract = package.contract.model_dump(mode="json")
    assert validate_result(contract, "```text\n" + text_result(package) + "\n```").valid
    assert "invalid_json" in codes(validate_result(contract, "```json\n{}\n```"))


def test_text_status_rules_still_apply(worker_tree):
    package = prepared()
    text = text_result(package).replace("status: completed", "status: partial")
    assert "remaining_work_required" in codes(validate_result(package.contract.model_dump(mode="json"), text))


@pytest.mark.asyncio
async def test_default_delivery_is_a_file_receipt(worker_tree):
    receipt = await call("get_subagent_context", request())
    assert receipt["delivery"] == "file"
    assert not {"worker_context", "contract", "result_schema", "selection"} & receipt.keys()
    path = Path(receipt["context_path"])
    inline = await call("get_subagent_context", request(delivery="inline"))
    assert path.read_text(encoding="utf-8") == inline["worker_context"]
    assert receipt["context_chars"] == len(inline["worker_context"])
    assert str(path) in receipt["launch"] and str(receipt["context_chars"]) in receipt["launch"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    stored = subagent_exchange.load_contract(receipt["contract_revision"])
    assert contract_fingerprint(stored) == receipt["contract_revision"] == inline["contract_revision"]
    assert len(json.dumps(receipt)) < 2000


@pytest.mark.asyncio
async def test_validation_by_revision_and_reference(worker_tree, tmp_path):
    receipt = await call("get_subagent_context", request(result_type="research"))
    package = PreparedPackage.model_validate(await call("get_subagent_context",
                                                         request(result_type="research", delivery="inline")))
    text = text_result(package)
    by_text = await call("validate_subagent_result", {"contract_revision": receipt["contract_revision"],
                                                      "result": text})
    assert by_text["valid"], by_text
    result_file = tmp_path / "result.md"
    result_file.write_text(text, encoding="utf-8")
    by_file = await call("validate_subagent_result", {"contract_revision": receipt["contract_revision"],
                                                      "result_ref": str(result_file)})
    assert by_file["valid"] and by_file["result_sha256"] == by_text["result_sha256"]


def claude_transcript(path, text):
    lines = [
        {"type": "user", "message": {"role": "user", "content": "Read the package"}},
        {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Reading it."}]}},
        {"type": "assistant", "message": {"id": "m2", "content": [{"type": "thinking", "thinking": "..."}]}},
        {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": text}]}},
        {"type": "assistant", "message": {"id": "m3", "content": [{"type": "tool_use", "name": "Read", "input": {}}]}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def test_reference_reads_claude_codex_and_handback_transcripts(worker_tree, tmp_path):
    package = prepared()
    text = text_result(package)
    claude = tmp_path / "agent-a1.jsonl"
    claude_transcript(claude, text)
    assert subagent_exchange.read_result(str(claude)) == text
    handback = tmp_path / "task.output"
    handback.write_text(json.dumps({"type": "assistant", "message": {"id": "h", "content": [
        {"type": "tool_use", "name": "SubagentHandback", "input": {"message": text}}]}}) + "\n", encoding="utf-8")
    assert subagent_exchange.read_result(str(handback)) == text
    codex = tmp_path / "rollout-2026-10-24T10-00-00-x.jsonl"
    codex.write_text("\n".join(json.dumps(line) for line in [
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": []}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "Working."}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": text}]}},
    ]) + "\n", encoding="utf-8")
    assert subagent_exchange.read_result(str(codex)) == text
    assert validate_result(package.contract.model_dump(mode="json"), subagent_exchange.read_result(str(codex))).valid


@pytest.mark.parametrize("name,code", [
    ("relative/result.md", "invalid_result_ref"),
    ("missing.md", "result_ref_missing"),
    ("outside.md", "result_ref_outside"),
    ("nul\x00.md", "result_ref_missing"),
])
def test_reference_policy(worker_tree, tmp_path, monkeypatch, name, code):
    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir(); outside.mkdir()
    (outside / "outside.md").write_text("task: x", encoding="utf-8")
    monkeypatch.setattr(subagent_exchange, "_result_roots", lambda: [allowed.resolve()])
    monkeypatch.setattr(subagent_exchange, "_transcript_roots", lambda: [allowed.resolve()])
    reference = name if name.startswith("relative") else str((allowed if name != "outside.md" else outside) / name)
    with pytest.raises(subagent_exchange.ContractError) as error:
        subagent_exchange.read_result(reference)
    assert error.value.code == code


def test_unencodable_text_is_a_diagnostic_not_a_crash(worker_tree):
    package = prepared()
    response = validate_result(package.contract.model_dump(mode="json"), text_result(package) + "\ud800")
    assert not response.valid and "invalid_text" in codes(response)


def test_client_homes_allow_only_transcripts(worker_tree, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    settings = home / ".claude.json"
    settings.write_text('{"token": "secret"}', encoding="utf-8")
    transcript = home / ".claude" / "projects" / "agent-a1.jsonl"
    claude_transcript(transcript, "task: x")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(subagent_exchange, "_result_roots", lambda: [])
    with pytest.raises(subagent_exchange.ContractError) as error:
        subagent_exchange.read_result(str(settings))
    assert error.value.code == "result_ref_outside"
    assert subagent_exchange.read_result(str(transcript)) == "task: x"


def test_a_stored_contract_must_carry_its_file_revision(worker_tree):
    first = subagent_exchange.store(prepare_subagent(request()))
    second = subagent_exchange.store(prepare_subagent(request(task="Another task")))
    directory = subagent_exchange.exchange_dir()
    (directory / f"{first.contract_revision}.contract.json").write_text(
        (directory / f"{second.contract_revision}.contract.json").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(subagent_exchange.ContractError) as error:
        subagent_exchange.load_contract(first.contract_revision)
    assert error.value.code == "contract_revision_mismatch"


def test_reference_reads_are_bounded_and_decoded_strictly(worker_tree, tmp_path, monkeypatch):
    undecodable = tmp_path / "result.md"
    undecodable.write_bytes(b"task: x\n\xff\xfe")
    with pytest.raises(subagent_exchange.ContractError) as error:
        subagent_exchange.read_result(str(undecodable))
    assert error.value.code == "invalid_text"
    growing = tmp_path / "growing.md"
    growing.write_text("x" * 200, encoding="utf-8")
    monkeypatch.setattr(subagent_exchange, "MAX_RESULT_FILE_BYTES", 128)
    real_stat = Path.stat

    class Earlier:  # the size seen before the worker appended more
        def __init__(self, result):
            self._result = result
        st_size = 64

        def __getattr__(self, name):
            return getattr(self._result, name)
    monkeypatch.setattr(Path, "stat", lambda self, **kwargs: (
        Earlier(real_stat(self, **kwargs)) if self.name == "growing.md" else real_stat(self, **kwargs)))
    with pytest.raises(subagent_exchange.ContractError) as error:
        subagent_exchange.read_result(str(growing))
    assert error.value.code == "result_ref_too_large"


def test_malformed_transcript_records_are_skipped(worker_tree, tmp_path):
    transcript = tmp_path / "agent-odd.jsonl"
    records = [
        {"type": "assistant", "message": {"id": "a", "content": [
            {"type": "tool_use", "name": "SubagentHandback", "input": "unexpected"}]}},
        {"type": "assistant", "message": "not an object"},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "text"}},
        {"type": "assistant", "message": {"id": "c", "content": 5}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": 7}},
        {"type": "assistant", "message": {"id": "b", "content": [{"type": "text", "text": "task: final"}]}},
        {"type": "assistant", "message": {"id": ["late"], "content": [{"type": "text", "text": "late odd id"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "late without id"}]}},
    ]
    transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    assert subagent_exchange.read_result(str(transcript)) == "task: final"


def test_reference_to_an_unfinished_transcript_is_empty(worker_tree, tmp_path):
    transcript = tmp_path / "agent-running.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": "start"}}) + "\n", encoding="utf-8")
    with pytest.raises(subagent_exchange.ContractError) as error:
        subagent_exchange.read_result(str(transcript))
    assert error.value.code == "result_ref_empty"


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,code", [
    ({"result": "x"}, "contract_required"),
    ({"contract": {}, "contract_revision": "0" * 64, "result": "x"}, "contract_required"),
    ({"contract_revision": "0" * 64}, "result_required"),
    ({"contract_revision": "0" * 64, "result": "x", "result_ref": "/tmp/x"}, "result_required"),
    ({"contract_revision": "0" * 64, "result": "x"}, "contract_unknown"),
    ({"contract_revision": "not-hex", "result": "x"}, "invalid_revision"),
])
async def test_validation_argument_errors(worker_tree, arguments, code):
    response = await call("validate_subagent_result", arguments)
    assert not response["valid"] and response["errors"][0]["code"] == code


@pytest.mark.asyncio
async def test_invalid_delivery_is_refused(worker_tree):
    response = await call("get_subagent_context", request(delivery="email"))
    assert response["status"] == "error" and response["errors"][0]["code"] == "invalid_delivery"


@pytest.mark.asyncio
async def test_unwritable_exchange_fails_file_delivery_only(worker_tree, monkeypatch):
    def refuse(package):
        raise OSError("read-only")
    monkeypatch.setattr(subagent_exchange, "store", refuse)
    response = await call("get_subagent_context", request())
    assert response["status"] == "error" and response["errors"][0]["code"] == "exchange_unwritable"
    assert (await call("get_subagent_context", request(delivery="inline")))["status"] == "prepared"


def test_old_exchange_files_are_pruned(worker_tree):
    package = prepare_subagent(request())
    directory = subagent_exchange.exchange_dir()
    directory.mkdir(parents=True, exist_ok=True)
    old = directory / ("0" * 64 + ".md")
    old.write_text("old", encoding="utf-8")
    past = time.time() - subagent_exchange.RETENTION_SECONDS - 60
    os.utime(old, (past, past))
    receipt = subagent_exchange.store(package)
    assert not old.exists() and Path(receipt.context_path).exists()
