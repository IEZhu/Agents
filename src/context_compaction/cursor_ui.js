/* Native Cursor sidebar driver, invoked only after explicit owner opt-in.
 * No activation, selection, clipboard, private service or chat storage.
 * Values are read only on the source-backed title subtree and focused input.
 * Live selector compatibility is unverified; unsupported layouts fail closed.
 *
 * Source basis: Cursor 3.24.12 workbench.desktop.main.js, updateAgentChatTitle,
 * auxiliary-bar-chat-title, aislash-editor-input, composer-bar, mic-icon-showing.
 * Chromium ax_platform_node_cocoa.mm exposes AXDOMClassList; DOM IDs may be
 * empty. Identity uses AXUIElementRef/CFEqual (SDK AXUIElement.h), never DOM IDs.
 */
var BUNDLE = "com.todesktop.230313mzl4w4u92";
var IDENTITY_KEYS = ["bundle_id", "process_id", "window_id", "window_title",
    "chat_id", "chat_title", "input_id", "sidebar_id", "identity_source"];
var REF_KEYS = ["application", "window", "title", "titleSource", "input", "sidebar", "composer"];
var MAX_NODES = 2400;
var MAX_TITLE_NODES = 16;
var deadline = 0;
var classCache = {};

function refuse(reason) { throw {refusal: reason}; }
function timeCheck() {
    if (deadline && Date.now() >= deadline) refuse("ui_unavailable");
}
function initializeNative() {
    ObjC.import("ApplicationServices");
    ObjC.import("CoreFoundation");
    ObjC.import("AppKit");
    // The SDK specifies that a system-wide ref sets the timeout for this process.
    // No trust prompt, UI read, or mutation occurs here.
    var system = ObjC.castRefToObject($.AXUIElementCreateSystemWide());
    if (Number($.AXUIElementSetMessagingTimeout(system, 0.15)) !== 0)
        refuse("ui_unavailable");
}
function isAX(value) {
    return value !== null && Number($.CFGetTypeID(value)) === Number($.AXUIElementGetTypeID());
}
function ax(value) {
    if (!isAX(value)) refuse("identity_unreadable");
    return value;
}
function equal(left, right) {
    return isAX(left) && isAX(right) && !!$.CFEqual(left, right);
}
function fingerprint(element) {
    // A fingerprint detects many changes between subprocesses. It is not a
    // native identity proof; sameNativeIdentity below always uses CFEqual.
    var hash = Number($.CFHash(ax(element)));
    if (!Number.isSafeInteger(hash) || hash < 0) refuse("identity_unreadable");
    return "cfhash:" + String(hash);
}
function decodeCF(reference) {
    // JXA's C return/out values are Ref wrappers. Passing those wrappers
    // directly to CFEqual/CFGetTypeID compares the wrapper, not the AX object.
    var value = ObjC.castRefToObject(reference);
    var type = Number($.CFGetTypeID(value));
    if (type === Number($.AXUIElementGetTypeID())) return value;
    if (type === Number($.CFStringGetTypeID())) return ObjC.unwrap(value);
    if (type === Number($.CFBooleanGetTypeID())) return ObjC.unwrap(value);
    if (type === Number($.CFNumberGetTypeID())) return Number(ObjC.unwrap(value));
    if (type === Number($.CFArrayGetTypeID())) {
        var count = Number($.CFArrayGetCount(value));
        if (!Number.isInteger(count) || count < 0 || count > MAX_NODES)
            refuse("ui_tree_too_large");
        var result = [];
        for (var index = 0; index < count; index++)
            result.push(decodeCF($.CFArrayGetValueAtIndex(value, index)));
        return result;
    }
    refuse("identity_unreadable");
}
function optionalError(code) { return code === -25205 || code === -25212; }
function attr(element, name) {
    timeCheck();
    var output = Ref();
    var code = Number($.AXUIElementCopyAttributeValue(ax(element), $(name), output));
    if (optionalError(code)) return null; // Unsupported attribute / no value.
    if (code !== 0) refuse("ui_unavailable");
    return decodeCF(output[0]);
}
function children(element, limit) {
    timeCheck();
    var countOut = Ref();
    var code = Number($.AXUIElementGetAttributeValueCount(ax(element), $("AXChildren"), countOut));
    if (optionalError(code)) return [];
    if (code !== 0) refuse("ui_unavailable");
    var count = Number(countOut[0]);
    if (!Number.isInteger(count) || count < 0 || count > limit) refuse("ui_tree_too_large");
    if (count === 0) return [];
    var output = Ref();
    code = Number($.AXUIElementCopyAttributeValues(element, $("AXChildren"), 0, count, output));
    if (code !== 0) refuse("ui_unavailable");
    var result = decodeCF(output[0]);
    if (!Array.isArray(result) || result.length !== count || !result.every(isAX))
        refuse("identity_unreadable");
    return result;
}
function classes(element) {
    var key = fingerprint(element);
    var bucket = classCache[key] || [];
    for (var index = 0; index < bucket.length; index++)
        if (equal(bucket[index].element, element)) return bucket[index].classes;
    var value = attr(element, "AXDOMClassList");
    if (value === null) value = [];
    if (!Array.isArray(value) || !value.every(function (item) { return typeof item === "string"; }))
        refuse("identity_unreadable");
    bucket.push({element: element, classes: value});
    classCache[key] = bucket;
    return value;
}
function hasClass(element, name) { return classes(element).indexOf(name) !== -1; }
function contents(element, maximum) {
    var limit = maximum || MAX_NODES;
    var queue = [ax(element)], result = [], seen = {};
    for (var index = 0; index < queue.length; index++) {
        timeCheck();
        if (queue.length > limit) refuse("ui_tree_too_large");
        var node = queue[index];
        var key = fingerprint(node), bucket = seen[key] || [];
        if (bucket.some(function (previous) { return equal(previous, node); }))
            refuse("identity_unreadable"); // Cyclic/duplicated AX hierarchy.
        bucket.push(node);
        seen[key] = bucket;
        result.push(node);
        queue = queue.concat(children(node, limit - queue.length));
    }
    return result;
}
function one(nodes, predicate, reason) {
    var matches = nodes.filter(predicate);
    if (matches.length !== 1) refuse(reason);
    return matches[0];
}
function ancestor(element, className) {
    var current = element;
    for (var depth = 0; depth < 40; depth++) {
        if (hasClass(current, className)) return current;
        current = attr(current, "AXParent");
        if (current === null) break;
        ax(current);
    }
    refuse("identity_unreadable");
}
function exactTitleValue(element) {
    var title = attr(element, "AXTitle"), value = attr(element, "AXValue");
    if ((title !== null && typeof title !== "string") || (value !== null && typeof value !== "string"))
        refuse("identity_unreadable");
    if (title && value && title !== value) refuse("identity_unreadable");
    return title || value || "";
}
function boundChatTitle(title, expected) {
    // Chromium may name the generic class-bearing group via AXTitle, or expose
    // its span text as a static-text child. Never search arbitrary message text.
    var text = exactTitleValue(title);
    var source = title;
    if (!text) {
        var descendants = contents(title, MAX_TITLE_NODES).slice(1);
        source = one(descendants, function (node) {
            return attr(node, "AXRole") === "AXStaticText";
        }, "identity_unreadable");
        text = exactTitleValue(source);
    }
    if (!text || text !== expected) refuse("identity_unreadable");
    return {text: text, source: source};
}
function sameIdentity(left, right) {
    return IDENTITY_KEYS.every(function (key) { return left[key] === right[key]; });
}
function sameNativeIdentity(left, right) {
    return sameIdentity(left.state, right.state) && REF_KEYS.every(function (key) {
        return equal(left.refs[key], right.refs[key]);
    });
}
function processId(element) {
    var output = Ref();
    if (Number($.AXUIElementGetPid(ax(element), output)) !== 0) refuse("identity_unreadable");
    var pid = Number(output[0]);
    if (!Number.isInteger(pid) || pid <= 0) refuse("identity_unreadable");
    return pid;
}
function frontmost(pid) {
    timeCheck();
    var front = $.NSWorkspace.sharedWorkspace.frontmostApplication;
    if (!front || Number(front.processIdentifier) !== pid
            || ObjC.unwrap(front.bundleIdentifier) !== BUNDLE) refuse("cursor_not_frontmost");
}
function application() {
    timeCheck();
    var running = $.NSRunningApplication.runningApplicationsWithBundleIdentifier($(BUNDLE));
    if (Number(running.count) !== 1) refuse("cursor_not_frontmost");
    var candidate = running.objectAtIndex(0);
    if (ObjC.unwrap(candidate.bundleIdentifier) !== BUNDLE) refuse("cursor_not_frontmost");
    var pid = Number(candidate.processIdentifier);
    frontmost(pid);
    var app = ax(ObjC.castRefToObject($.AXUIElementCreateApplication(pid)));
    if (processId(app) !== pid || attr(app, "AXFrontmost") !== true) refuse("cursor_not_frontmost");
    return {element: app, pid: pid};
}
function snapshot(binding, commandDraft, allowPendingCommand) {
    classCache = {};
    var process = application(), app = process.element;
    var windows = attr(app, "AXWindows");
    if (!Array.isArray(windows) || !windows.every(isAX)) refuse("window_unavailable");
    var matching = windows.filter(function (window) {
        var title = attr(window, "AXTitle");
        if (typeof title !== "string" || title.length === 0) refuse("identity_unreadable");
        return title === binding.window_title;
    });
    if (matching.length !== 1) refuse(matching.length ? "window_ambiguous" : "window_unavailable");
    var window = matching[0];
    if (attr(window, "AXMain") !== true || attr(window, "AXFocused") !== true
            || !equal(attr(app, "AXFocusedWindow"), window)) refuse("window_unavailable");
    var nodes = contents(window);
    var title = one(nodes, function (node) {
        return hasClass(node, "auxiliary-bar-chat-title");
    }, "chat_ambiguous");
    var chatTitle = boundChatTitle(title, binding.chat_title);
    var input = one(nodes, function (node) {
        return hasClass(node, "aislash-editor-input");
    }, "input_ambiguous");
    var sidebar = ancestor(input, "auxiliarybar");
    if (!equal(ancestor(title, "auxiliarybar"), sidebar)) refuse("identity_unreadable");
    var composer = ancestor(input, "composer-bar");
    var composers = nodes.filter(function (node) { return hasClass(node, "composer-bar"); });
    if (composers.length !== 1 || !equal(composers[0], composer)) refuse("chat_ambiguous");
    if (attr(input, "AXFocused") !== true || attr(input, "AXEnabled") !== true
            || !equal(attr(app, "AXFocusedUIElement"), input)) refuse("input_not_focused");
    var role = attr(input, "AXRole");
    if (role !== "AXTextArea" && role !== "AXTextField") refuse("identity_unreadable");
    var value = attr(input, "AXValue");
    if (typeof value !== "string") refuse("draft_unreadable");
    if (value !== commandDraft && !(allowPendingCommand && commandDraft.indexOf(value) === 0))
        refuse(commandDraft ? "command_draft_changed" : "draft_present");
    var composerNodes = contents(composer);
    if (composerNodes.some(function (node) { return hasClass(node, "context-pill"); }))
        refuse("attachments_present");
    if (composerNodes.some(function (node) {
        return hasClass(node, "stop-button") || hasClass(node, "queue-voice-mic-button")
            || hasClass(node, "composer-toolbar-queue-item");
    })) refuse("chat_busy");
    // Positive idle/empty proof is required before typing. After typing, retain
    // that proof and recheck the exact command, identity, focus and busy state.
    if (commandDraft === "" && composerNodes.filter(function (node) {
        return hasClass(node, "mic-icon-showing") && attr(node, "AXEnabled") === true;
    }).length !== 1) refuse("idle_unreadable");
    frontmost(process.pid);
    if (attr(app, "AXFrontmost") !== true || !equal(attr(app, "AXFocusedWindow"), window)
            || !equal(attr(app, "AXFocusedUIElement"), input)) refuse("input_not_focused");
    var state = {
        frontmost: true, bundle_id: BUNDLE, process_id: process.pid,
        window_id: fingerprint(window), window_title: binding.window_title,
        chat_id: fingerprint(title), chat_title: chatTitle.text, input_id: fingerprint(input),
        sidebar_id: fingerprint(sidebar), identity_source: "cursor_ax_sidebar_cf_v2",
        identity_proven: true, selected: true, focused: true, draft_proven: true,
        window_count: 1, chat_count: 1, input_count: 1,
        busy: false, draft: value, attachments: false
    };
    var refs = {application: app, window: window, title: title,
        titleSource: chatTitle.source, input: input, sidebar: sidebar, composer: composer};
    if (!REF_KEYS.every(function (key) { return processId(refs[key]) === process.pid; }))
        refuse("identity_unreadable");
    return {state: state, refs: refs};
}
function run(argv) {
    var typed = false;
    try {
        deadline = Date.now() + 4000; // Python also kills this child after 5s.
        if (argv.length !== 2 || (argv[0] !== "snapshot" && argv[0] !== "summarize"))
            refuse("ui_unavailable");
        var request = JSON.parse(argv[1]);
        var binding = request.binding;
        if (!binding || typeof binding.window_title !== "string"
                || typeof binding.chat_title !== "string") refuse("identity_unreadable");
        initializeNative();
        var current = snapshot(binding, "");
        if (argv[0] === "snapshot") return JSON.stringify(current.state);
        if (!request.expected || !sameIdentity(current.state, request.expected)) refuse("identity_changed");
        // Creating the scripting proxy performs no activation or keyboard action.
        var se = Application("System Events");
        var immediate = snapshot(binding, "");
        if (!sameNativeIdentity(immediate, current)) refuse("identity_changed");
        timeCheck();
        // There is no atomic compare-and-type API; a narrow UI race remains.
        typed = true;
        se.keystroke("/summarize");
        // Keyboard delivery and Chromium's AX cache can lag the scripting reply.
        // Wait only for an empty/prefix value to become this exact command. Every
        // read still checks refs/focus/title/busy/attachments; no clear or retype.
        var confirmationDeadline = Math.min(deadline, Date.now() + 600);
        while (true) {
            var afterTyping = snapshot(binding, "/summarize", true);
            if (!sameNativeIdentity(afterTyping, immediate)) refuse("identity_changed");
            if (afterTyping.state.draft === "/summarize") break;
            if (Date.now() >= confirmationDeadline) refuse("command_draft_changed");
            $.NSThread.sleepForTimeInterval(0.03);
        }
        timeCheck();
        se.keyCode(36); // Return, handled by Cursor's native slash interceptor.
        return JSON.stringify({status: "submitted", reason: "native_command_submitted"});
    } catch (error) {
        // Never clean up, retry or send another key after a refusal/error.
        return JSON.stringify({status: typed ? "error" : "skipped",
            reason: typed ? "submission_unknown" : (error.refusal || "ui_unavailable")});
    }
}
