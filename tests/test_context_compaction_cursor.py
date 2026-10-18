"""Offline safety contract tests; no Cursor or System Events calls."""

import copy
import json
import subprocess

import pytest

from src.context_compaction import cursor


def settings():
    return {
        "enabled": True,
        "cursor_bindings": [{
            "conversation_id": "conversation-1", "window_title": "Agents — Cursor",
            "chat_title": "Context compaction",
        }],
    }


def snapshot():
    return {
        "frontmost": True, "bundle_id": cursor.CURSOR_BUNDLE_ID, "process_id": 123,
        "window_id": "cfhash:2", "window_title": "Agents — Cursor",
        "chat_id": "cfhash:3", "chat_title": "Context compaction",
        "input_id": "cfhash:4", "sidebar_id": "cfhash:5",
        "identity_source": cursor.IDENTITY_SOURCE,
        "identity_proven": True, "selected": True, "focused": True, "draft_proven": True,
        "window_count": 1, "chat_count": 1, "input_count": 1,
        "busy": False, "draft": "", "attachments": False,
    }


class FakeUI:
    def __init__(self, snapshots=None, result=None):
        self.snapshots = snapshots if snapshots is not None else [snapshot(), snapshot()]
        self.result = result if result is not None else {"status": "submitted"}
        self.actions = []

    def snapshot(self):
        self.actions.append("snapshot")
        value = self.snapshots.pop(0)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def summarize(self, expected_snapshot):
        self.actions.append(("summarize", expected_snapshot))
        if isinstance(self.result, Exception):
            raise self.result
        return copy.deepcopy(self.result)


@pytest.fixture(autouse=True)
def pretend_macos(monkeypatch):
    monkeypatch.setattr(cursor.sys, "platform", "darwin")


def compact(ui, configured=None, **kwargs):
    return cursor.compact_cursor({"conversation_id": "conversation-1"},
                                 configured if configured is not None else settings(),
                                 transport=ui, **kwargs)


@pytest.mark.parametrize("enabled", [False, None, 0, 1, "true", [], {}])
def test_literal_owner_opt_in_before_ui(enabled):
    configured = settings()
    configured["enabled"] = enabled
    ui = FakeUI()
    assert compact(ui, configured) == {"status": "skipped", "reason": "disabled"}
    assert ui.actions == []


def test_missing_owner_switch_is_disabled():
    ui = FakeUI()
    assert compact(ui, {"cursor_bindings": []})["reason"] == "disabled"
    assert ui.actions == []


@pytest.mark.parametrize("platform", ["linux", "win32", "freebsd14"])
def test_unsupported_platform_has_no_transport_or_subprocess_effect(monkeypatch, platform):
    monkeypatch.setattr(cursor.sys, "platform", platform)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("subprocess called"))
    ui = FakeUI()
    assert compact(ui) == {"status": "unsupported", "reason": "platform_unsupported"}
    assert ui.actions == []


@pytest.mark.parametrize("payload", [{}, {"conversation_id": ""}, {"conversation_id": 1},
                                     {"conversation_id": "conversation-2"}])
def test_missing_or_unbound_hook_identity_never_inspects_ui(payload):
    ui = FakeUI()
    assert cursor.compact_cursor(payload, settings(), transport=ui)["status"] == "skipped"
    assert ui.actions == []


@pytest.mark.parametrize("bindings", [
    None, [], [None], [{"conversation_id": "conversation-1"}],
    [{"conversation_id": "conversation-1", "window_title": " ", "chat_title": "A"}],
    [{"conversation_id": "conversation-1", "window_title": "A\nB", "chat_title": "A"}],
])
def test_invalid_binding_does_not_inspect_ui(bindings):
    configured = settings()
    configured["cursor_bindings"] = bindings
    ui = FakeUI()
    assert compact(ui, configured)["status"] == "skipped"
    assert ui.actions == []


@pytest.mark.parametrize("change", [{}, {"conversation_id": "another"}, {"chat_title": "another"}])
def test_duplicates_in_either_binding_direction_refuse(change):
    configured = settings()
    configured["cursor_bindings"].append({**configured["cursor_bindings"][0], **change})
    ui = FakeUI()
    assert compact(ui, configured)["reason"] == "binding_ambiguous"
    assert ui.actions == []


@pytest.mark.parametrize("patch", [
    {"frontmost": False}, {"frontmost": 1}, {"bundle_id": "other.app"},
    {"window_count": 0}, {"window_count": 2}, {"window_count": True},
    {"chat_count": 2}, {"input_count": 2}, {"identity_proven": False},
    {"identity_proven": 1}, {"selected": False}, {"focused": False},
    {"draft_proven": False}, {"identity_source": "guessed"},
    {"process_id": True}, {"process_id": 0}, {"window_id": ""},
    {"input_id": None}, {"chat_id": ""}, {"sidebar_id": None},
    {"window_title": "Different — Cursor"}, {"chat_title": "Different"},
    {"busy": True}, {"busy": None}, {"busy": 0}, {"draft": "unsent work"},
    {"draft": " "}, {"draft": None}, {"draft": []},
    {"attachments": True}, {"attachments": None}, {"attachments": 0},
])
def test_unverifiable_ambiguous_busy_or_draft_snapshot_refuses(patch):
    ui = FakeUI([{**snapshot(), **patch}])
    assert compact(ui)["status"] == "skipped"
    assert ui.actions == ["snapshot"]


@pytest.mark.parametrize("key", list(snapshot()))
def test_missing_proof_field_refuses(key):
    state = snapshot()
    del state[key]
    ui = FakeUI([state])
    assert compact(ui)["status"] == "skipped"
    assert ui.actions == ["snapshot"]


@pytest.mark.parametrize("patch", [
    {"chat_title": "Another"}, {"window_title": "Another"}, {"process_id": 124},
    {"window_id": "replacement-window"}, {"chat_id": "replacement-chat"},
    {"input_id": "replacement-input"}, {"sidebar_id": "replacement-sidebar"},
    {"frontmost": False}, {"busy": True}, {"draft": "user started typing"},
    {"window_count": 2}, {"chat_count": 2}, {"attachments": True},
])
def test_target_changes_between_reads_never_submit(patch):
    ui = FakeUI([snapshot(), {**snapshot(), **patch}])
    assert compact(ui)["status"] == "skipped"
    assert ui.actions == ["snapshot", "snapshot"]


def test_owner_switch_is_rechecked_at_last_python_action_boundary():
    ui = FakeUI()

    def guard():
        assert ui.actions == ["snapshot", "snapshot"]
        return False

    assert compact(ui, guard=guard)["reason"] == "disabled_before_submit"
    assert ui.actions == ["snapshot", "snapshot"]


@pytest.mark.parametrize("guard", [lambda: None, lambda: 1, lambda: "true"])
def test_owner_switch_recheck_requires_literal_true(guard):
    ui = FakeUI()
    assert compact(ui, guard=guard)["status"] == "skipped"
    assert ui.actions == ["snapshot", "snapshot"]


def test_success_sends_the_rechecked_snapshot_once_without_claiming_completion():
    ui = FakeUI()
    assert compact(ui, guard=lambda: True) == {
        "status": "submitted", "reason": "native_command_submitted",
    }
    assert ui.actions == ["snapshot", "snapshot", ("summarize", snapshot())]


def test_transport_can_refuse_its_own_final_identity_recheck():
    ui = FakeUI(result={"status": "skipped", "reason": "identity_changed"})
    assert compact(ui) == {"status": "skipped", "reason": "identity_changed"}
    assert len(ui.actions) == 3


@pytest.mark.parametrize("outcome", [
    {"status": "completed"}, {"status": "completed", "completion_observed": False},
    {"status": "completed", "completion_observed": True, "identity": {}},
])
def test_unobserved_or_wrong_identity_completion_is_not_success(outcome):
    assert compact(FakeUI(result=outcome))["reason"] == "completion_unverified"


def test_completed_requires_explicit_observation_on_same_identity():
    identity = {key: snapshot()[key] for key in cursor._IDENTITY_KEYS}
    ui = FakeUI(result={"status": "completed", "completion_observed": True, "identity": identity})
    assert compact(ui) == {"status": "completed", "reason": "native_completion_observed"}


@pytest.mark.parametrize("outcome", [None, [], "ok", {"status": "success"}, {}])
def test_unrecognized_transport_outcome_is_error(outcome):
    ui = FakeUI()
    ui.result = outcome
    assert compact(ui)["reason"] == "transport_result_invalid"


@pytest.mark.parametrize("phase", ["snapshot", "summarize", "guard"])
def test_ui_errors_are_sanitized_without_retry_or_draft_cleanup(phase):
    failure = RuntimeError("private draft or authorization detail")
    ui = FakeUI([failure] if phase == "snapshot" else None,
                result=failure if phase == "summarize" else None)

    def guard():
        if phase == "guard":
            raise failure
        return True

    result = compact(ui, guard=guard)
    assert result == {"status": "error", "reason": "ui_unavailable"}
    assert "private" not in json.dumps(result)
    assert sum(isinstance(action, tuple) for action in ui.actions) <= 1


@pytest.mark.parametrize("outcome", [
    {"status": "skipped", "reason": "secret chat text"},
    {"status": "error", "reason": "secret chat text"},
    {"status": "error", "reason": ["unhashable"]},
])
def test_transport_reasons_cannot_leak_arbitrary_user_content(outcome):
    assert compact(FakeUI(result=outcome))["reason"] == "ui_unavailable"


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32", "freebsd14"])
def test_default_adapter_is_unsupported_without_native_or_keyboard_activity(monkeypatch, platform):
    monkeypatch.setattr(cursor.sys, "platform", platform)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("native process invoked"))
    result = cursor.compact_cursor(
        {"conversation_id": "conversation-1"}, settings(),
        guard=lambda: pytest.fail("dispatch guard invoked without a native capability"),
    )
    assert result == {"status": "unsupported", "reason": "native_compaction_unavailable"}
