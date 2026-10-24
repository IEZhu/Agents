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
from src.schemas.subagent import PreparedPackage
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
    value = parse_result(text_result(package), package.contract)
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
    (lambda text: text.replace("  fix: Drop it from the message.", ""), "string_too_short"),
    (lambda text: text.replace("## checks\n", "## checks\nstray line\n"), "invalid_item"),
    (lambda text: text + "\nstray line", "invalid_evidence"),
])
def test_text_format_errors_name_the_problem(worker_tree, mutate, code):
    package = prepared(result_type="review")
    response = validate_result(package.contract.model_dump(mode="json"), mutate(text_result(package)))
    assert not response.valid and code in codes(response), response.errors


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
