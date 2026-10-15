"""Explicit URL subscriptions, with validated local snapshots and no hot-path network.

This machine-local store is intentionally separate from the synced flow library:
corporate rule bodies and authentication references must not enter that Git remote.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.engine import config
from src.file_lock import file_lock
from src.rule_sources import RuleSourceError, fetch_rule
from src.user_library import atomic_write
from src.utils.prompt_loader import split_frontmatter

MAX_CONTENT_BYTES = 64 * 1024
MAX_HISTORY = 20
MAX_SUBSCRIPTIONS = 32
MAX_STATE_BYTES = 4 * 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
# Stop at another marker so repeated @ characters cannot rescan the same suffix.
_IMPORT = re.compile(r"@[^@\s`]*\.mdc\b", re.IGNORECASE)
_REVISION = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LOG = logging.getLogger(__name__)


class UserRuleError(ValueError):
    """Invalid subscription, source document or stale optimistic revision."""


class _MetadataLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise UserRuleError("rule_invalid: duplicate or non-text metadata key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse_rule(content: str):
    """Validate one self-contained MDC; never resolve local/remote imports."""
    from src.engine.rules import Rule

    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise UserRuleError("rule_invalid: content exceeds 64 KiB")
    if any(ord(c) < 32 and c not in "\t\n\r" for c in content):
        raise UserRuleError("rule_invalid: control characters are not allowed")
    raw, body = split_frontmatter(content)
    if raw is None or not body.strip():
        raise UserRuleError("rule_invalid: expected MDC frontmatter and a nonempty body")
    if _IMPORT.search(content):
        raise UserRuleError("rule_invalid: imports are not allowed in URL rules")
    try:
        if any(isinstance(token, (yaml.AliasToken, yaml.AnchorToken)) for token in yaml.scan(raw)):
            raise UserRuleError("rule_invalid: YAML aliases and anchors are not allowed")
        metadata = yaml.load(raw, Loader=_MetadataLoader)
    except UserRuleError:
        raise
    except (yaml.YAMLError, RecursionError, ValueError, OverflowError):
        raise UserRuleError("rule_invalid: malformed YAML metadata") from None
    if not isinstance(metadata, dict) or set(metadata) - {"name", "description", "category", "priority"}:
        raise UserRuleError("rule_invalid: allowed metadata is name, description, category, priority")
    name = metadata.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise UserRuleError("rule_invalid: name must be 1-80 letters, digits, underscores or hyphens")
    for key in ("description", "category"):
        if key in metadata and not isinstance(metadata[key], str):
            raise UserRuleError(f"rule_invalid: {key} must be text")
    priority = metadata.get("priority", 50)
    if type(priority) is not int or not 0 <= priority <= 1000:
        raise UserRuleError("rule_invalid: priority must be an integer from 0 to 1000")
    return Rule(name=f"user:{name}", description=metadata.get("description", ""),
                category=metadata.get("category", "user"), priority=priority,
                body=body.strip())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class RuleLibrary:
    def __init__(self, root: Path | None = None):
        configured = os.environ.get("AGENTS_USER_RULES_DIR")
        self.root = (root or (Path(configured).expanduser() if configured else
                             Path(config.INSTALL_DATA_DIR) / "user-rules")).resolve()

    def _path(self, rule_id: str) -> Path:
        if not isinstance(rule_id, str) or not rule_id.startswith("user:") or not _NAME.fullmatch(rule_id[5:]):
            raise UserRuleError("rule_invalid: expected user:<name> ID")
        path = self.root / f"{rule_id[5:]}.json"
        if path.is_symlink():
            raise UserRuleError("rule_invalid: subscription must not be a symlink")
        return path

    def _read(self, rule_id: str, *, validate_current: bool = True) -> dict:
        path = self._path(rule_id)
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise ValueError("too large")
            state = json.loads(raw)
            if (type(state["format"]) is not int or state["format"] != 1 or state["id"] != rule_id
                    or type(state["enabled"]) is not bool
                    or not isinstance(state["revision"], str) or not _REVISION.fullmatch(state["revision"])
                    or not isinstance(state["url"], str)
                    or not isinstance(state["content_sha256"], str) or not _SHA256.fullmatch(state["content_sha256"])
                    or not isinstance(state["history"], list) or not 1 <= len(state["history"]) <= MAX_HISTORY):
                raise ValueError("bad state")
            if any(not isinstance(item, dict) or not isinstance(item.get("sha256"), str)
                   or not _SHA256.fullmatch(item["sha256"]) or not isinstance(item.get("accepted_at"), str)
                   or "content" not in item for item in state["history"]):
                raise ValueError("bad history envelope")
            if validate_current:
                self._current(state)
            return state
        except FileNotFoundError:
            raise UserRuleError(f"rule_not_found: {rule_id}") from None
        except (OSError, KeyError, TypeError, ValueError, UnicodeError, StopIteration, RecursionError):
            raise UserRuleError(f"rule_invalid: corrupt subscription {rule_id}") from None

    def _current(self, state: dict) -> dict:
        try:
            snapshot = next(item for item in state["history"] if item["sha256"] == state["content_sha256"])
            if (not isinstance(snapshot["content"], str) or _digest(snapshot["content"]) != snapshot["sha256"]
                    or parse_rule(snapshot["content"]).name != state["id"]):
                raise ValueError("invalid snapshot")
        except (KeyError, TypeError, ValueError, StopIteration):
            raise UserRuleError("rule_invalid: corrupt subscription snapshot integrity check failed") from None
        return snapshot

    def _metadata(self, state: dict, *, full: bool = False) -> dict:
        current = self._current(state)
        rule = parse_rule(current["content"])
        result = {key: state.get(key) for key in (
            "id", "url", "auth_env", "enabled", "revision", "content_sha256", "created_at",
            "checked_at", "last_error",
        )}
        result.update(description=rule.description, category=rule.category, priority=rule.priority, body=rule.body)
        if full:
            result["content"] = current["content"]
            result["history"] = [{"sha256": item["sha256"], "accepted_at": item["accepted_at"]}
                                 for item in state["history"]]
        return result

    def list(self) -> list[dict]:
        result = []
        for path in sorted(self.root.glob("*.json")):
            if not _NAME.fullmatch(path.stem):
                fingerprint = hashlib.sha256(os.fsencode(path.name)).hexdigest()
                _LOG.warning("Ignoring noncanonical user-rule filename sha256=%s", fingerprint)
                result.append({"id": "invalid-file:" + fingerprint, "file_name": path.name,
                               "status": "error", "error": "rule_invalid: noncanonical subscription filename",
                               "enabled": None, "revision": None})
                continue
            rule_id = f"user:{path.stem}"
            try:
                result.append(self._entry(rule_id))
            except UserRuleError as error:
                # A concurrent explicit deletion need not leave a phantom error row.
                if str(error).startswith("rule_not_found:"):
                    continue
                raise
        return result

    def _entry(self, rule_id: str, *, full: bool = False) -> dict:
        try:
            state = self._read(rule_id, validate_current=False)
        except UserRuleError as error:
            if str(error).startswith("rule_not_found:"):
                raise
            return self._diagnostic(rule_id, error)
        try:
            return self._metadata(state, full=full)
        except UserRuleError as error:
            return self._diagnostic(rule_id, error, state)

    def _diagnostic(self, rule_id: str, error: UserRuleError, state: dict | None = None) -> dict:
        result = {"id": rule_id, "status": "error", "error": str(error),
                  "enabled": None, "revision": None}
        _LOG.warning("Unavailable user rule %s: %s", rule_id, error)
        if state is None:
            return result
        result.update({key: state[key] for key in ("enabled", "revision", "url", "content_sha256")})
        result["history"] = [{"sha256": item["sha256"], "accepted_at": item["accepted_at"]}
                             for item in state["history"]]
        return result

    def get(self, rule_id: str) -> dict:
        self._path(rule_id)  # Invalid IDs and symlinks remain refusals, not usable diagnostics.
        return self._entry(rule_id, full=True)

    def rules(self, names: set[str] | None = None) -> list:
        if names is not None:
            states = [self._read(name) for name in sorted(names)]
            if any(not state["enabled"] for state in states):
                raise UserRuleError("rule_disabled: enable the selected user rule before using it")
        else:
            states = []
            for path in sorted(self.root.glob("*.json")):
                if not _NAME.fullmatch(path.stem):
                    _LOG.warning("Ignoring noncanonical user-rule filename sha256=%s",
                                 hashlib.sha256(os.fsencode(path.name)).hexdigest())
                    continue
                # Only a valid envelope can establish that guidance is switched off.
                states.append(self._read(f"user:{path.stem}", validate_current=False))
        rules = [parse_rule(self._current(state)["content"]) for state in states if state["enabled"]]
        return sorted(rules, key=lambda rule: (rule.priority, rule.name))

    def _write(self, state: dict) -> dict:
        state["revision"] = uuid.uuid4().hex
        metadata = self._metadata(state, full=True)
        raw = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        if len(raw) > MAX_STATE_BYTES:
            raise UserRuleError("rule_invalid: subscription history is too large")
        atomic_write(self._path(state["id"]), raw)
        return metadata

    def _check_revision(self, state: dict, expected_revision: str) -> None:
        if not expected_revision or state["revision"] != expected_revision:
            raise UserRuleError("rule_conflict: read the subscription and retry with its current revision")

    def subscribe(self, url: str, auth_env: str | None = None) -> dict:
        content = fetch_rule(url, auth_env=auth_env)
        rule = parse_rule(content)
        timestamp = _now()
        state = {"format": 1, "id": rule.name, "url": url, "auth_env": auth_env,
                 "enabled": True, "created_at": timestamp, "checked_at": timestamp,
                 "last_error": None, "content_sha256": _digest(content),
                 "history": [{"sha256": _digest(content), "content": content, "accepted_at": timestamp}]}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with file_lock(self.root / ".lock"):
            if self._path(rule.name).exists():
                raise UserRuleError("rule_exists: this user rule name is already subscribed")
            if len(list(self.root.glob("*.json"))) >= MAX_SUBSCRIPTIONS:
                raise UserRuleError("rule_limit: at most 32 subscriptions")
            return self._write(state)

    def refresh(self, rule_id: str, expected_revision: str) -> dict:
        state = self._read(rule_id)
        self._check_revision(state, expected_revision)
        error_message = None
        try:
            content = fetch_rule(state["url"], auth_env=state.get("auth_env"))
            if parse_rule(content).name != rule_id:
                raise UserRuleError("rule_invalid: source changed the rule name")
        except (RuleSourceError, UserRuleError) as error:
            error_message = str(error)
        with file_lock(self.root / ".lock"):
            state = self._read(rule_id)
            self._check_revision(state, expected_revision)
            state["checked_at"] = _now()
            state["last_error"] = error_message
            if error_message is None:
                digest = _digest(content)
                existing = next((item for item in state["history"] if item["sha256"] == digest), None)
                if existing is None:
                    state["history"].append({"sha256": digest, "content": content,
                                             "accepted_at": state["checked_at"]})
                    state["history"] = state["history"][-MAX_HISTORY:]
                else:
                    # A matching stored hash does not establish integrity of retained bytes.
                    existing["content"] = content
                state["content_sha256"] = digest
            result = self._write(state)
        if error_message is not None:
            return {"status": "error", "error": error_message, "retained": result}
        return {"status": "updated", "rule": result}

    def set_enabled(self, rule_id: str, enabled: bool, expected_revision: str) -> dict:
        if type(enabled) is not bool:
            raise UserRuleError("rule_invalid: enabled must be boolean")
        with file_lock(self.root / ".lock"):
            state = self._read(rule_id)
            self._check_revision(state, expected_revision)
            state["enabled"] = enabled
            return self._write(state)

    def rollback(self, rule_id: str, content_sha256: str, expected_revision: str) -> dict:
        with file_lock(self.root / ".lock"):
            state = self._read(rule_id, validate_current=False)
            self._check_revision(state, expected_revision)
            if not any(item["sha256"] == content_sha256 for item in state["history"]):
                raise UserRuleError("rule_version_not_found: hash is not in retained history")
            state["content_sha256"] = content_sha256
            self._current(state)
            state["last_error"] = None
            return self._write(state)

    def delete(self, rule_id: str, expected_revision: str) -> dict:
        with file_lock(self.root / ".lock"):
            state = self._read(rule_id, validate_current=False)
            self._check_revision(state, expected_revision)
            self._path(rule_id).unlink()
        return {"status": "deleted", "id": rule_id}
