"""Reserved Cursor policy boundary; native compaction is currently unsupported.

No chat storage, private Cursor service, credentials or debugging port is used.
The default adapter performs no UI inspection, keyboard events or dispatch.
A supported, target-bound native action must be verified before adding a real
transport. Global keyboard automation and ordinary follow-up prompts are not
fallbacks for native compaction.

An injected transport implements ``snapshot()`` and ``summarize(snapshot)`` for
offline policy tests. It must confine native compaction to the supplied identity.
"""

from __future__ import annotations

import copy
import sys
from typing import Callable


CURSOR_BUNDLE_ID = "com.todesktop.230313mzl4w4u92"
IDENTITY_SOURCE = "cursor_ax_sidebar_cf_v2"
_IDENTITY_KEYS = (
    "bundle_id", "process_id", "window_id", "window_title", "chat_id",
    "chat_title", "input_id", "sidebar_id", "identity_source",
)
_TRUE_PROOFS = ("identity_proven", "selected", "focused", "frontmost", "draft_proven")
_COUNTS = ("window_count", "chat_count", "input_count")
_SAFE_REASONS = frozenset({
    "cursor_not_frontmost", "window_unavailable", "window_ambiguous",
    "identity_unreadable", "chat_ambiguous", "input_ambiguous", "input_not_focused",
    "chat_busy", "idle_unreadable", "draft_present", "draft_unreadable",
    "attachments_present", "identity_changed", "ui_unavailable", "ui_tree_too_large",
    "native_command_submitted", "submission_unknown", "command_draft_changed",
})


def _result(status: str, reason: str, **extra) -> dict:
    return {"status": status, "reason": reason, **extra}


def _label(value) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 1024
            and not any(ord(char) < 32 for char in value))


def _binding(payload, settings) -> tuple[dict | None, str]:
    if not isinstance(payload, dict) or not _label(payload.get("conversation_id")):
        return None, "conversation_id_missing"
    bindings = settings.get("cursor_bindings")
    if not isinstance(bindings, list):
        return None, "binding_missing"
    # Validate the complete collection: malformed or duplicate entries must not
    # conceal another conversation bound to the same visible target.
    conversation_ids, targets = set(), set()
    selected = None
    for item in bindings:
        if not isinstance(item, dict) or not all(
            _label(item.get(key)) for key in ("conversation_id", "window_title", "chat_title")
        ):
            return None, "binding_invalid"
        target = (item["window_title"], item["chat_title"])
        if item["conversation_id"] in conversation_ids or target in targets:
            return None, "binding_ambiguous"
        conversation_ids.add(item["conversation_id"])
        targets.add(target)
        if item["conversation_id"] == payload["conversation_id"]:
            selected = dict(item)
    return (selected, "") if selected is not None else (None, "binding_missing")


def _snapshot_reason(snapshot, binding: dict) -> str:
    if not isinstance(snapshot, dict):
        return "identity_unreadable"
    if snapshot.get("status") in ("skipped", "error"):
        reason = snapshot.get("reason")
        return reason if isinstance(reason, str) and reason in _SAFE_REASONS else "ui_unavailable"
    if snapshot.get("frontmost") is not True or snapshot.get("bundle_id") != CURSOR_BUNDLE_ID:
        return "cursor_not_frontmost"
    if any(type(snapshot.get(key)) is not int or snapshot[key] != 1 for key in _COUNTS):
        return "identity_ambiguous"
    if (snapshot.get("identity_source") != IDENTITY_SOURCE
            or any(snapshot.get(key) is not True for key in _TRUE_PROOFS)):
        return "identity_unreadable"
    if (type(snapshot.get("process_id")) is not int or snapshot["process_id"] <= 0
            or any(not _label(snapshot.get(key)) for key in _IDENTITY_KEYS
                   if key != "process_id")):
        return "identity_unreadable"
    if (snapshot["window_title"] != binding["window_title"]
            or snapshot["chat_title"] != binding["chat_title"]):
        return "binding_mismatch"
    if snapshot.get("busy") is not False:
        return "chat_busy" if snapshot.get("busy") is True else "idle_unreadable"
    if snapshot.get("draft") != "" or type(snapshot.get("draft")) is not str:
        return "draft_present"
    if snapshot.get("attachments") is not False:
        return "attachments_present"
    return ""


def _identity(snapshot: dict) -> dict:
    return {key: snapshot[key] for key in _IDENTITY_KEYS}


def compact_cursor(payload: dict, settings: dict, *, transport=None,
                   guard: Callable[[], bool] | None = None) -> dict:
    """Report unsupported without a verified native transport.

    Injected offline transports exercise binding, idle and identity policy.
    ``guard`` re-reads the owner switch at the last action boundary. A reported
    submission never proves completion. Owner opt-in cannot enable a missing
    native capability, and this module never substitutes a user message.
    """
    if not isinstance(settings, dict) or settings.get("enabled") is not True:
        return _result("skipped", "disabled")
    if transport is None:
        return _result("unsupported", "native_compaction_unavailable")
    # Even an injected transport is never touched on an unsupported platform.
    if sys.platform != "darwin":
        return _result("unsupported", "platform_unsupported")
    binding, reason = _binding(payload, settings)
    if reason:
        return _result("skipped", reason)
    ui = transport
    try:
        first = copy.deepcopy(ui.snapshot())
        reason = _snapshot_reason(first, binding)
        if reason:
            return _result("skipped", reason)
        current = copy.deepcopy(ui.snapshot())
        reason = _snapshot_reason(current, binding)
        if reason:
            return _result("skipped", reason)
        if _identity(current) != _identity(first):
            return _result("skipped", "identity_changed")
        if guard is not None and guard() is not True:
            return _result("skipped", "disabled_before_submit")
        outcome = ui.summarize(copy.deepcopy(current))
    except Exception:
        # An action may have reached Cursor before a timeout or AX exception.
        # Callers must treat this as an unknown outcome, never confirmed success.
        return _result("error", "ui_unavailable")
    if not isinstance(outcome, dict):
        return _result("error", "transport_result_invalid")
    status = outcome.get("status")
    if status == "completed":
        if (outcome.get("completion_observed") is True
                and outcome.get("identity") == _identity(current)):
            return _result("completed", "native_completion_observed")
        return _result("error", "completion_unverified")
    if status == "submitted":
        return _result("submitted", "native_command_submitted")
    if status in ("skipped", "error"):
        reason = outcome.get("reason")
        return _result(status, reason if isinstance(reason, str) and reason in _SAFE_REASONS else "ui_unavailable")
    return _result("error", "transport_result_invalid")
