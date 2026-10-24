"""Files of the compact worker exchange, so the parent's context carries none of it.

``get_subagent_context`` writes the worker package to a private file the worker reads itself
and keeps the contract here, keyed by its revision; ``validate_subagent_result`` loads that
contract by revision and can read the worker's result from a file or a client transcript
(#453). The parent then holds a short receipt instead of the package, never retypes the
contract or the result, and cannot alter what is validated.

Files live in the installation's ``data/subagent-exchange`` (0700 directory, 0600 files) and
are removed after `RETENTION_SECONDS`. They hold task text, so they stay on this machine.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time

from src.engine.subagent_validation import ContractError, canonical_json, contract_fingerprint
from src.schemas.subagent import MAX_RESULT_CHARS, PreparedPackage, PreparedReceipt

RETENTION_SECONDS = 7 * 24 * 3600
MAX_CONTRACT_BYTES = 256 * 1024
MAX_RESULT_FILE_BYTES = MAX_RESULT_CHARS * 4  # UTF-8 bytes of at most MAX_RESULT_CHARS characters
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
TRANSCRIPT_SUFFIXES = (".jsonl", ".output")  # Claude Code agent transcripts and task outputs, Codex rollouts
_REVISION = re.compile(r"[a-f0-9]{64}")


def exchange_dir() -> Path:
    from src.engine import config  # at call time: tests and installations redirect the data directory

    return Path(config.INSTALL_DATA_DIR) / "subagent-exchange"


def _write_private(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _prune(directory: Path, now: float) -> None:
    for entry in directory.iterdir():
        try:
            if entry.is_file() and now - entry.stat().st_mtime > RETENTION_SECONDS:
                entry.unlink()
        except OSError:
            continue  # another server process may prune the same file


def launch_text(path: Path, chars: int) -> str:
    return (f"You are a fresh worker. Read the file {path} ({chars} characters) completely before doing "
            "anything else and follow it exactly: it is your whole assignment, role, rules and result "
            "format. Reply with only the result it specifies.")


def store(package: PreparedPackage) -> PreparedReceipt:
    """Write the package and its contract; the receipt is what the parent keeps."""
    contract = package.contract.model_dump(mode="json")
    revision = package.contract_revision
    if contract_fingerprint(contract) != revision:  # the stored copy must be the one validation expects
        raise ContractError("contract_revision_mismatch", "Serialized contract does not match its revision.")
    directory = exchange_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _prune(directory, time.time())
    _write_private(directory / f"{revision}.contract.json", canonical_json(contract))
    context_path = directory / f"{revision}.md"
    _write_private(context_path, package.worker_context)
    return PreparedReceipt(
        task_id=package.task_id, attempt_id=package.attempt_id, contract_revision=revision,
        context_path=str(context_path), context_chars=package.context_chars,
        context_sha256=hashlib.sha256(package.worker_context.encode("utf-8")).hexdigest(),
        launch=launch_text(context_path, package.context_chars), descriptor=package.descriptor,
        execution=package.execution)


def load_contract(revision) -> dict:
    """The contract stored at preparation; validation rechecks its fingerprint."""
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise ContractError("invalid_revision", "contract_revision must be 64 lowercase hex characters.",
                            "$.contract_revision")
    path = exchange_dir() / f"{revision}.contract.json"
    try:
        if path.stat().st_size > MAX_CONTRACT_BYTES:
            raise ContractError("contract_unknown", "The stored contract is too large.", "$.contract_revision")
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ContractError("contract_unknown", "No contract is stored for this revision: it expired, was "
                            "prepared by another installation or with delivery inline. Pass the retained "
                            "contract instead.", "$.contract_revision") from None
    if not isinstance(contract, dict) or contract.get("contract_revision") != revision:
        raise ContractError("contract_revision_mismatch", "The stored contract does not carry this revision.",
                            "$.contract_revision")
    return contract


def _resolved(paths) -> list[Path]:
    resolved = []
    for path in paths:
        try:
            if path.is_dir():
                resolved.append(path.resolve())
        except OSError:
            continue
    return resolved


def _result_roots() -> list[Path]:
    """Where a result file may be read: the exchange and the temporary directories."""
    return _resolved([exchange_dir(), Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/folders")])


def _transcript_roots() -> list[Path]:
    """Client homes, where only transcripts may be read: they also hold settings and credentials."""
    home = Path.home()
    homes = [home / ".codex", *home.glob(".claude*")]
    homes += [Path(os.environ[name]) for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME") if os.environ.get(name)]
    return _result_roots() + _resolved(homes)


def _claude_text(record: dict) -> tuple[str | None, str | None]:
    """(message id, text) of a Claude Code assistant record; a background hand-back counts as text."""
    message = record.get("message")
    if record.get("type") != "assistant" or not isinstance(message, dict):
        return None, None
    parts = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif (block.get("type") == "tool_use" and block.get("name") == "SubagentHandback"
              and isinstance((block.get("input") or {}).get("message"), str)):
            parts.append(block["input"]["message"])
    return message.get("id"), ("\n".join(parts) if parts else None)


def _codex_text(record: dict) -> str | None:
    payload = record.get("payload")
    if (record.get("type") != "response_item" or not isinstance(payload, dict)
            or payload.get("type") != "message" or payload.get("role") != "assistant"):
        return None
    parts = [block["text"] for block in payload.get("content") or []
             if isinstance(block, dict) and block.get("type") == "output_text" and isinstance(block.get("text"), str)]
    return "".join(parts) or None


def final_message(lines) -> str | None:
    """The last assistant message of a Claude Code or Codex transcript, given its lines.

    Claude Code writes one line per content block, so the text blocks of one message id are
    joined; a later message without text leaves the earlier text as the final one.
    """
    final, final_id = None, None
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        message_id, text = _claude_text(record)
        if text is None:
            text, message_id = _codex_text(record), None
        if text is None:
            continue
        if message_id is not None and message_id == final_id and final is not None:
            final += "\n" + text
        else:
            final, final_id = text, message_id
    return final.strip() if final and final.strip() else None


def read_result(reference) -> str:
    """The worker's result named by ``result_ref``: a result file, or the final assistant
    message of a Claude Code agent transcript (``agent-<id>.jsonl``, a task ``.output``) or a
    Codex rollout (``rollout-*.jsonl``)."""
    if not isinstance(reference, str) or not reference or not Path(reference).is_absolute():
        raise ContractError("invalid_result_ref", "result_ref must be an absolute file path.", "$.result_ref")
    try:
        path = Path(reference).resolve(strict=True)
        size = path.stat().st_size
    except OSError:
        raise ContractError("result_ref_missing", "result_ref does not name a readable file.", "$.result_ref") from None
    transcript = path.suffix in TRANSCRIPT_SUFFIXES
    if not any(path.is_relative_to(root) for root in (_transcript_roots() if transcript else _result_roots())):
        raise ContractError("result_ref_outside", "result_ref must be a result file under a temporary directory "
                            "or the subagent exchange directory, or a transcript there or in a Claude or Codex "
                            "home.", "$.result_ref")
    if not path.is_file():
        raise ContractError("result_ref_missing", "result_ref does not name a regular file.", "$.result_ref")
    limit = MAX_TRANSCRIPT_BYTES if transcript else MAX_RESULT_FILE_BYTES
    too_large = ContractError("result_ref_too_large", "result_ref is larger than a result can be.", "$.result_ref")
    if size > limit:
        raise too_large
    try:
        with path.open("rb") as handle:  # one bounded read: a worker may still be appending
            raw = handle.read(limit + 1)
    except OSError:
        raise ContractError("result_ref_unreadable", "result_ref could not be read.", "$.result_ref") from None
    if len(raw) > limit:
        raise too_large
    if transcript:  # undecodable bytes in unrelated records are tolerated, as the client wrote them
        text = final_message(io.StringIO(raw.decode("utf-8", errors="replace")))
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ContractError("invalid_text", "The result file is not valid UTF-8 text.", "$.result_ref") from None
    if not text or not text.strip():
        raise ContractError("result_ref_empty", "result_ref holds no result yet: the worker may still be "
                            "running.", "$.result_ref")
    return text
