"""Offline safety contract tests; no Cursor or System Events calls."""

import copy
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

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
    monkeypatch.setattr(cursor.subprocess, "run", lambda *a, **kw: pytest.fail("subprocess called"))
    ui = FakeUI()
    assert compact(ui) == {"status": "unsupported", "reason": "platform_unsupported"}
    assert ui.actions == []
    assert cursor.MacOSCursorTransport(settings()["cursor_bindings"][0]).snapshot()["status"] == "unsupported"


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


def test_native_transport_uses_fixed_script_and_data_arguments(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "submitted"}))

    monkeypatch.setattr(cursor.subprocess, "run", run)
    binding = settings()["cursor_bindings"][0]
    transport = cursor.MacOSCursorTransport(binding)
    assert transport.summarize(snapshot())["status"] == "submitted"
    command, options = calls[0]
    assert command[:3] == ["/usr/bin/osascript", "-l", "JavaScript"]
    assert Path(command[3]).parts[-2:] == ("context_compaction", "cursor_ui.js")
    assert command[4] == "summarize"
    assert json.loads(command[5]) == {"binding": binding, "expected": snapshot()}
    assert options == {"capture_output": True, "text": True, "check": False, "timeout": 5}


@pytest.mark.parametrize("stdout,returncode", [("not-json", 0), ("[]", 0), ("{}", 1), ("x" * 32769, 0)],
                         ids=["malformed-json", "non-object", "failed-process", "oversized-output"])
def test_native_transport_invalid_output_is_unavailable(monkeypatch, stdout, returncode):
    monkeypatch.setattr(cursor.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=returncode, stdout=stdout,
    ))
    assert cursor.MacOSCursorTransport(settings()["cursor_bindings"][0]).snapshot() == {
        "status": "error", "reason": "ui_unavailable",
    }


# Execute the actual driver with fake native C APIs. Ref wrappers and native
# objects differ here: omitting ObjC.castRefToObject fails the C boundary checks.
# No part of this VM can access a real application, keyboard or clipboard.
_FAKE_AX = r"""
const fs = require('fs');
const vm = require('vm');
const scenario = process.argv[1];
const request = JSON.parse(process.argv[2]);
const source = fs.readFileSync(process.argv[3], 'utf8');
const keys = [], attributes = [];
let reads = 0, equalCalls = 0, timeoutCalls = 0, expired = false, pendingClock = 0;
const TYPES = {string:7, array:19, bool:21, number:22, ax:81};
class Element {
  constructor(hash, attrs, children = []) {
    this.hash = hash;
    this.attrs = attrs;
    this.children = children;
    children.forEach(child => { child.attrs.AXParent = this; });
  }
}
class Native { constructor(type, value) { this.type = type; this.value = value; } }
class RawRef { constructor(value) { this.value = value; } }
function native(value) {
  if (value instanceof Native) return value;
  if (value instanceof Element) return new Native(TYPES.ax, value);
  if (Array.isArray(value)) return new Native(TYPES.array, value);
  if (typeof value === 'string') return new Native(TYPES.string, value);
  if (typeof value === 'boolean') return new Native(TYPES.bool, value);
  if (typeof value === 'number') return new Native(TYPES.number, value);
  throw new Error('unsupported CF value');
}
function requireNative(value) {
  if (!(value instanceof Native)) throw new Error('raw Ref passed to a C API');
  return value;
}
function entity(value) {
  if (requireNative(value).type !== TYPES.ax) throw new Error('not AXUIElementRef');
  return value.value;
}
const input = new Element(4, {AXDOMClassList:['aislash-editor-input'],
  AXRole:'AXTextArea', AXFocused:true, AXEnabled:true, AXValue:''});
let activeInput = input;
const mic = new Element(6, {AXDOMClassList:['mic-icon-showing'], AXEnabled:true});
const composer = new Element(7, {AXDOMClassList:['composer-bar']}, [input, mic]);
const title = new Element(3, {AXDOMClassList:['auxiliary-bar-chat-title'],
  AXRole:'AXGroup', AXTitle:'Context compaction'});
const sidebar = new Element(5, {AXDOMClassList:['auxiliarybar']}, [title, composer]);
const message = new Element(8, {AXRole:'AXStaticText', AXValue:'private chat message'});
const window = new Element(2, {AXTitle:'Agents — Cursor', AXMain:true, AXFocused:true}, [sidebar, message]);
const app = new Element(1, {AXFrontmost:true, AXWindows:[window],
  AXFocusedWindow:window, AXFocusedUIElement:input});
const system = new Element(0, {});
function replaceInput(sameHash = false) {
  const replacement = new Element(sameHash ? 4 : 404, {...activeInput.attrs});
  replacement.attrs.AXParent = composer;
  composer.children[0] = replacement;
  activeInput = replacement;
  app.attrs.AXFocusedUIElement = replacement;
}
if (scenario === 'title_value') { delete title.attrs.AXTitle; title.attrs.AXValue = 'Context compaction'; }
if (scenario === 'static_title' || scenario === 'static_title_ambiguous') {
  delete title.attrs.AXTitle;
  title.children = [new Element(9, {AXRole:'AXStaticText', AXValue:'Context compaction'})];
  if (scenario === 'static_title_ambiguous') title.children.push(new Element(10,
    {AXRole:'AXStaticText', AXValue:'Context compaction'}));
  title.children.forEach(child => { child.attrs.AXParent = title; });
}
if (scenario === 'title_conflict') title.attrs.AXValue = 'Other conversation';
if (scenario === 'wrong_title') title.attrs.AXTitle = 'Other conversation';
if (scenario === 'message_title_match') {
  title.attrs.AXTitle = 'Other conversation';
  message.attrs.AXValue = 'Context compaction';
}
if (scenario === 'wrong_focus') input.attrs.AXFocused = false;
if (scenario === 'focused_input_mismatch') app.attrs.AXFocusedUIElement = message;
if (scenario === 'focused_window_mismatch') app.attrs.AXFocusedWindow = sidebar;
if (scenario === 'busy') mic.attrs.AXDOMClassList = ['stop-button'];
if (scenario === 'idle_unknown') mic.attrs.AXDOMClassList = [];
if (scenario === 'draft') input.attrs.AXValue = 'private unsent work';
if (scenario === 'attachment') composer.children.push(new Element(11, {AXDOMClassList:['context-pill']}));
if (scenario === 'unknown_classes') title.attrs.AXDOMClassList = [];
if (scenario === 'chat_ambiguous') sidebar.children.push(new Element(12, {...title.attrs}));
if (scenario === 'window_ambiguous') app.attrs.AXWindows = [window, window];
if (scenario === 'input_ambiguous') composer.children.push(new Element(13, {...input.attrs}));
if (scenario === 'composer_ambiguous') sidebar.children.push(new Element(14, {AXDOMClassList:['composer-bar']}));
if (scenario === 'sidebar_mismatch') title.attrs.AXParent = new Element(15, {AXDOMClassList:['auxiliarybar']});
if (scenario === 'cyclic_tree') composer.children.push(sidebar);
if (scenario === 'oversized_tree') window.children = Array(2401).fill(sidebar);
const dollar = value => native(value);
Object.assign(dollar, {
  CFGetTypeID: value => requireNative(value).type,
  AXUIElementGetTypeID: () => TYPES.ax,
  CFStringGetTypeID: () => TYPES.string,
  CFBooleanGetTypeID: () => TYPES.bool,
  CFNumberGetTypeID: () => TYPES.number,
  CFArrayGetTypeID: () => TYPES.array,
  CFArrayGetCount: value => requireNative(value).value.length,
  CFArrayGetValueAtIndex: (value, index) => new RawRef(native(requireNative(value).value[index])),
  CFEqual: (left, right) => {
    equalCalls++;
    return entity(left) === entity(right);
  },
  CFHash: value => entity(value).hash,
  AXUIElementCreateSystemWide: () => new RawRef(native(system)),
  AXUIElementCreateApplication: pid => {
    if (pid !== 123) throw new Error('wrong PID');
    return new RawRef(native(app));
  },
  AXUIElementSetMessagingTimeout: (value, timeout) => {
    entity(value);
    if (timeout !== 0.15) throw new Error('unbounded AX timeout');
    timeoutCalls++;
    return 0;
  },
  AXUIElementGetPid: (value, output) => {
    const node = entity(value);
    output[0] = scenario === 'foreign_input_pid' && node === activeInput ? 124 : 123;
    return 0;
  },
  AXUIElementCopyAttributeValue: (value, key, output) => {
    const node = entity(value), name = requireNative(key).value;
    attributes.push(name);
    if (name === 'AXIdentifier' || name === 'AXDOMIdentifier')
      throw new Error('id-less Chromium nodes have no required identifier');
    if (name === 'AXValue' && node === message) throw new Error('read a chat message');
    if (scenario === 'unreadable' && name === 'AXValue' && node === activeInput) return -25204;
    if (scenario === 'attribute_failure' && name === 'AXDOMClassList' && node === title) return -25204;
    if (!Object.prototype.hasOwnProperty.call(node.attrs, name)) return -25205;
    output[0] = new RawRef(native(node.attrs[name]));
    return 0;
  },
  AXUIElementGetAttributeValueCount: (value, key, output) => {
    const node = entity(value);
    if (requireNative(key).value !== 'AXChildren') throw new Error('unexpected array');
    if (scenario === 'children_count_failure' && node === composer) return -25204;
    output[0] = node.children.length;
    return 0;
  },
  AXUIElementCopyAttributeValues: (value, key, start, count, output) => {
    const node = entity(value);
    if (requireNative(key).value !== 'AXChildren' || start !== 0 || count > 2400)
      throw new Error('unbounded children request');
    output[0] = new RawRef(native(scenario === 'wrong_children_type' && node === composer
      ? ['not-an-AX-ref'] : node.children.slice(start, start + count)));
    return 0;
  },
  NSRunningApplication: {runningApplicationsWithBundleIdentifier: value => {
    if (requireNative(value).value !== 'com.todesktop.230313mzl4w4u92') throw new Error('wrong bundle');
    reads++;
    if (scenario === 'identity_race' && reads === 2) replaceInput();
    if (scenario === 'hash_collision_identity_race' && reads === 2) replaceInput(true);
    if (scenario === 'draft_race' && reads === 2) activeInput.attrs.AXValue = 'user started typing';
    if (scenario === 'busy_race' && reads === 2) mic.attrs.AXDOMClassList = ['stop-button'];
    if (scenario === 'delayed_command' && reads === 4) activeInput.attrs.AXValue = '/sum';
    if (scenario === 'delayed_command' && reads === 5) activeInput.attrs.AXValue = '/summarize';
    if (scenario === 'pending_user_text' && reads === 4) activeInput.attrs.AXValue = 'user draft';
    return {count:scenario === 'frontmost_race' && reads === 2 ? 0 : 1,
      objectAtIndex: () => ({processIdentifier:123, bundleIdentifier:native('com.todesktop.230313mzl4w4u92')})};
  }},
  NSWorkspace: {sharedWorkspace:{frontmostApplication:{processIdentifier:123,
    bundleIdentifier:native('com.todesktop.230313mzl4w4u92')}}},
  NSThread: {sleepForTimeInterval: seconds => {
    if (seconds !== 0.03) throw new Error('unbounded confirmation sleep');
    pendingClock += seconds * 1000;
  }}
});
const se = {
  keystroke: value => {
    keys.push(['keystroke', value]);
    if (scenario === 'keyboard_error') throw new Error('keyboard failed');
    activeInput.attrs.AXValue = value;
    if (['delayed_command', 'pending_user_text', 'pending_timeout'].includes(scenario))
      activeInput.attrs.AXValue = '';
    mic.attrs.AXDOMClassList = [];
    if (scenario === 'after_typing_identity_race') replaceInput();
    if (scenario === 'after_typing_hash_collision_race') replaceInput(true);
    if (scenario === 'after_typing_draft_race') activeInput.attrs.AXValue = value + 'user text';
    if (scenario === 'after_typing_focus_race') app.attrs.AXFocusedUIElement = message;
    if (scenario === 'after_typing_busy_race') mic.attrs.AXDOMClassList = ['stop-button'];
    if (scenario === 'after_typing_deadline') expired = true;
  },
  keyCode: value => {
    keys.push(['keyCode', value]);
    if (scenario === 'return_error') throw new Error('return failed');
  }
};
const sandbox = { Ref:() => [], ObjC:{
  import: () => {},
  castRefToObject: value => {
    if (!(value instanceof RawRef)) throw new Error('not a C Ref wrapper');
    return requireNative(value.value);
  },
  unwrap: value => requireNative(value).value
}, Application: name => {
  if (name !== 'System Events') throw new Error('unexpected application');
  return se;
}};
sandbox.$ = dollar;
if (scenario === 'before_typing_deadline' || scenario === 'after_typing_deadline') {
  let clockReads = 0;
  sandbox.Date = {now: () => {
    clockReads++;
    return expired || (scenario === 'before_typing_deadline' && clockReads > 1) ? 4001 : 0;
  }};
}
if (scenario === 'pending_timeout') sandbox.Date = {now: () => pendingClock};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const outcome = JSON.parse(sandbox.run(['summarize', JSON.stringify(request)]));
process.stdout.write(JSON.stringify({outcome, keys, reads, equalCalls, timeoutCalls, attributes}));
"""


def run_fake_ax(scenario):
    # The fixture emulates darwin for the adapter on every host. On Windows
    # that suppresses shutil.which's implicit PATHEXT lookup; try the real exe.
    node = shutil.which("node") or shutil.which("node.exe")
    if node is None:
        pytest.skip("Node.js is needed only for the offline fake AX driver tests")
    request = {"binding": settings()["cursor_bindings"][0], "expected": snapshot()}
    completed = subprocess.run(
        [node, "-e", _FAKE_AX, scenario, json.dumps(request), str(cursor._SCRIPT)],
        check=True, capture_output=True, text=True, timeout=5,
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize("scenario", ["success", "title_value", "static_title", "delayed_command"])
def test_native_driver_submits_only_the_literal_command_with_idless_native_refs(scenario):
    result = run_fake_ax(scenario)
    assert result["outcome"] == {"status": "submitted", "reason": "native_command_submitted"}
    assert result["keys"] == [["keystroke", "/summarize"], ["keyCode", 36]]
    assert result["reads"] == (5 if scenario == "delayed_command" else 3)
    assert result["equalCalls"] >= 14  # Includes all retained refs at both action boundaries.
    assert result["timeoutCalls"] == 1
    assert "AXIdentifier" not in result["attributes"]
    assert "AXDOMIdentifier" not in result["attributes"]


@pytest.mark.parametrize("scenario", [
    "wrong_title", "message_title_match", "title_conflict", "static_title_ambiguous", "wrong_focus",
    "focused_input_mismatch", "focused_window_mismatch", "busy", "idle_unknown", "draft",
    "attachment", "unknown_classes", "chat_ambiguous", "window_ambiguous", "input_ambiguous",
    "composer_ambiguous", "sidebar_mismatch", "unreadable", "attribute_failure",
    "children_count_failure", "wrong_children_type", "cyclic_tree", "oversized_tree",
    "foreign_input_pid", "before_typing_deadline",
    "identity_race", "hash_collision_identity_race", "draft_race", "busy_race", "frontmost_race",
])
def test_native_driver_refuses_unproven_target_and_final_race_before_typing(scenario):
    result = run_fake_ax(scenario)
    assert result["outcome"]["status"] == "skipped"
    assert result["keys"] == []


@pytest.mark.parametrize("scenario", [
    "after_typing_identity_race", "after_typing_hash_collision_race", "after_typing_draft_race",
    "after_typing_focus_race", "after_typing_busy_race", "after_typing_deadline", "keyboard_error",
    "pending_user_text", "pending_timeout",
])
def test_native_driver_never_sends_return_after_a_post_typing_error(scenario):
    result = run_fake_ax(scenario)
    assert result["outcome"] == {"status": "error", "reason": "submission_unknown"}
    assert result["keys"] == [["keystroke", "/summarize"]]


def test_native_driver_reports_uncertain_return_as_error_without_retry():
    result = run_fake_ax("return_error")
    assert result["outcome"] == {"status": "error", "reason": "submission_unknown"}
    assert result["keys"] == [["keystroke", "/summarize"], ["keyCode", 36]]
