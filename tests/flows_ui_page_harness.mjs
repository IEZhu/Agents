// Runs the flow editor page script (src/daemon/flows_ui.html) against a stub DOM and
// fetch, for tests/test_flows_ui_page.py. Usage: node flows_ui_page_harness.mjs PAGE SCENARIO
// Prints JSON: the sign-in requests made and whether the sign-in section is shown, or, for the
// "search" scenario, what the list shows after each step of the search sequence, or, for
// "persona_race", the Persona panel's state while a save and a navigation overlap, or, for
// "agents", what the Agents tab lists, finds and shows, and which listings the page requested, or,
// for "ui_panes" and "ui_signout", which pane and header controls show as items open and close, or,
// for "ui_history", how a flow's versions load after its text (#371), or,
// for "place", where itemPlace puts groups of given widths, or, for "ui_place", where the open
// item's group goes as the window is resized, or, for the "sync_*" scenarios (library sync, #170),
// what the header chip, the Sync page, its wizard and the editor's notice show and which
// /ui/api/sync requests the page sent, against a scripted API and timers the scenario fires, or, for
// the "autodev*" scenarios (#348), what the Autodev page shows and which /ui/api/autodev requests it sent, or,
// for the "landing_updates*" scenarios (#381), what the overview's Updates section shows and saves, or,
// for "memory" and "memory_codex" (#297), what the Memory tab lists, shows and sends, or, for
// "workmap_layout", what the Work map tab's pure helpers compute (layers, docking, lanes, the goal's
// connectors, categories, who acts, recorded verdicts, the critical path, level badges and words), or,
// for "workmap", what the Work map tab summarizes in its slots, draws, opens and polls from a #workmap
// link, and when it refits, or, for "workmap_signout", that a revoked session stops it, or, for
// "workmap_routes", how the layout orders and places a map and how its edges run, checked by geometry
// over synthetic boxes.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const [, , pagePath, scenario] = process.argv;
const html = readFileSync(pagePath, "utf8");
const script = html.match(/<script nonce="\{\{NONCE\}\}">([\s\S]*?)<\/script>/)[1];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// POSIX pipes are asynchronous: await the complete JSON before terminating a scenario.
async function finish(value) {
  await new Promise((resolve, reject) => process.stdout.write(JSON.stringify(value) + "\n",
    (error) => error ? reject(error) : resolve()));
  process.exit(0);
}
// Stub elements start with the classes the markup gives them, so what the page hides starts hidden.
const markupClasses = new Map();
for (const [tag] of html.replace(/<script[\s\S]*?<\/script>/g, "").matchAll(/<[a-z][^>]*\sid="[^"]+"[^>]*>/g)) {
  const cls = tag.match(/\sclass="([^"]*)"/);
  markupClasses.set(tag.match(/\sid="([^"]+)"/)[1], cls ? cls[1].split(/\s+/).filter(Boolean) : []);
}

function element(id = "", tag = "") {
  const classes = new Set(markupClasses.get(id) || []);
  const handlers = {};
  const self = {
    id, tag, attrs: {}, classes, children: [], dataset: {}, style: {}, value: "", textContent: "", className: "",
    disabled: false, scrollTop: 0, handlers, focused: 0, scrolled: 0,
    classList: {
      add: (name) => classes.add(name), remove: (name) => classes.delete(name), contains: (name) => classes.has(name),
      toggle: (name, on) => ((on ?? !classes.has(name)) ? classes.add(name) : classes.delete(name)),
    },
    addEventListener(type, handler) { (handlers[type] = handlers[type] || []).push(handler); },
    appendChild(child) { self.children.push(child); },
    append() {},
    replaceChildren() { self.children = []; self.scrollTop = 0; },  // a browser resets scrolling here
    setAttribute(name, value) { self.attrs[name] = String(value); if (name === "id" && !elements.has(value)) elements.set(value, self); },
    scrollIntoView() { self.scrolled += 1; },
    contains: (node) => node === self || self.children.some((child) => child.nodeType !== 3 && child.contains(node)),
    removeAttribute(name) { delete self.attrs[name]; }, getAttribute: (name) => (name in self.attrs ? self.attrs[name] : null),
    querySelector: () => element(), querySelectorAll: () => [], showModal() { self.open = true; }, close() { self.open = false; },
    focus() { self.focused += 1; },
  };
  return self;
}
const elements = new Map();
const byId = (id) => { if (!elements.has(id)) elements.set(id, element(id)); return elements.get(id); };

const posts = [];
const requests = [];  // every path the page fetched, in order
const isUi = scenario.startsWith("ui");
const isSync = scenario.startsWith("sync");
const isAutodev = scenario.startsWith("autodev");
const isWorkmap = scenario.startsWith("workmap");
let signedIn = ["search", "render", "persona_race", "agents", "history", "history_adopt", "memory", "memory_codex"].includes(scenario) || isUi || isSync
  || isAutodev || isWorkmap;
const revisions = {};  // sync_notice: a flow's revision after another machine changed it
const deletedFlows = new Set();  // sync_notice: flows a cycle deleted (GET answers 404)
const confirms = [];  // every question the page asked with confirm()
let confirmAnswer = scenario !== "landing_updates_down_declined";  // what confirm() answers; autodev_races answers no once
const replacedUrls = [];  // every history.replaceState call, as [state, title, url]
const prompts = [];  // every question the page asked with prompt(), and the answer it suggested
const puts = [];
const alerts = [];
let userRuleToggle = { id: "user:team", description: "Team rule", enabled: false, declared_by: [],
                       body: "Accepted A", revision: "r1" };
let putStatus = 200;
const savedText = {};  // what a PUT stored, served back by the next GET
let releasePersonaPut = null;  // persona_race: the test decides when the PUT answers
const deletes = [];
let holdDelete = false, releaseDelete = null;  // ui_panes: a delete that answers late
let holdFlowGet = false, releaseFlowGet = null;  // ui_signout: a flow that loads past the sign-in screen
let refuseSignIn = false;  // workmap_signout: the session was revoked and automatic sign-in is off
let holdHistory = false, failHistory = false;  // ui_history: version lists answer when the scenario releases them, in its order
const historyHolds = [];
const respond = (status, body) => ({ status, ok: status < 400, statusText: "", json: async () => body });

async function fetchStub(path, init = {}) {
  requests.push(path);
  if (path === "/ui/api/session") {
    const body = JSON.parse(init.body || "{}");
    posts.push(body);
    if (posts.length > 5) {  // a sign-in loop: report it instead of spinning forever
      await finish({ looped: true, posts });
    }
    await sleep(10);  // keep the attempt pending while other 401s arrive
    if ("code" in body) return respond(401, { error: "code_invalid" });
    if (scenario === "refused" || scenario === "ui_signout" || refuseSignIn) return respond(401, { error: "sign_in_required" });
    if (scenario !== "cookie_lost") signedIn = true;
    return respond(200, { status: "ok" });
  }
  if (!signedIn) return respond(401, { error: "session_required" });
  if (scenario.startsWith("landing")) return respond(...landingData(path, init));
  if (isSync && path.startsWith("/ui/api/sync")) {
    const method = init.method || "GET";
    const hold = syncHolds[method + " " + path] || syncHolds[method + " " + path.split("?")[0]];
    const answer = syncAnswer(method, path, init.body ? JSON.parse(init.body) : null);
    if (hold) await hold.promise;  // the scenario decides when this request answers
    return respond(...answer);
  }
  if (isSync) return respond(...uiData(path, init));
  if (isAutodev && path.startsWith("/ui/api/autodev")) {
    const method = init.method || "GET";
    const answer = autodevAnswer(method, path, init.body ? JSON.parse(init.body) : null);  // the state when sent
    const hold = autodevHolds[method + " " + path];
    if (hold) {  // the scenario decides when this one request answers
      delete autodevHolds[method + " " + path];
      await hold.promise;
    }
    return respond(...answer);
  }
  if (isAutodev) return respond(...uiData(path, init));
  if (isUi && init.method === "DELETE") {
    deletes.push(JSON.parse(init.body));
    if (holdDelete) await new Promise((resolve) => { releaseDelete = resolve; });
    return respond(200, { status: "deleted" });
  }
  if (isUi && holdFlowGet && path.startsWith("/ui/api/flow?")) await new Promise((resolve) => { releaseFlowGet = resolve; });
  if (isUi && path.startsWith("/ui/api/flow/history?") && (holdHistory || failHistory)) {
    // The list as it was when asked, like a server's: a held answer may be older than a later one.
    const answer = failHistory ? [500, { status: "error", error: "flow_history: Git failed" }] : uiData(path, init);
    if (holdHistory) await new Promise((resolve) => historyHolds.push(resolve));
    return respond(...answer);
  }
  if (isUi) return respond(...uiData(path, init));
  if (scenario === "agents") return respond(...agentsData(path, init));
  if (scenario === "search") {
    if (path.includes("kind=skills")) await sleep(80);  // a slow tab, to type while it loads
    return respond(200, searchData(path));
  }
  if (scenario === "persona_race") return personaRace(path, init);
  if (scenario === "history" || scenario === "history_adopt") return respond(...historyData(path, init));
  if (scenario === "memory" || scenario === "memory_codex") return respond(...memoryData(path, init));
  if (isWorkmap && path.startsWith("/ui/api/workmap")) return respond(...workmapData(path));
  if (path.startsWith("/ui/api/flows")) return respond(200, { flows: [], repositories: [] });
  return respond(200, { workspaces: [], items: [] });
}

// workmap: three maps in two repositories. #workmap opens the open map changed last, though a closed one
// changed later, and says that one other is open; a poll after `workmapRevision` changes sees the build
// validated, and with `workmapExtra` a node added. Times are minutes before now.
let workmapRevision = 9, workmapExtra = false;
const WORKMAP_SHA = "a".repeat(64), WORKMAP_FIX = "f".repeat(64);
const workmapAgo = (minutes) => new Date(Date.now() - minutes * 60000).toISOString();
function workmapNodes() {
  const later = workmapRevision > 9;
  const base = { summary: "", planned_summary: "", risk: "load_bearing", reason: null, needs: "V2", complexity: null,
    attempts: 0, settled: false, ready: false, stale: false, attention: false, result_status: null, checks: null,
    findings: null, limitations: 0, handback: null, question: null, escalation: null, contract_revision: null,
    attempt_id: null, result_sha256: null, inputs: {}, invalid: 0, invalid_codes: [] };
  const done = { result_status: "completed", handback: "line", attempt_id: "1", attempts: 1 };
  return [
    { ...base, id: "goal", kind: "goal", title: "Ship the demo", status: "planned", level: "V0", changed: 1, since: workmapAgo(90) },
    { ...base, ...done, id: "triage", kind: "stage", title: "Triage the issues", status: "accepted", level: "V2", settled: true,
      changed: 3, since: workmapAgo(60), checks: { passed: 3, failed: 0, not_run: 1 }, limitations: 2, contract_revision: "c".repeat(64),
      result_sha256: WORKMAP_SHA, summary: "Three bugs matter", planned_summary: "Triage the open issues" },
    { ...base, id: "decide", kind: "decision", title: "Pick the fixes", status: "planned", level: "V0", ready: true, changed: 4,
      since: workmapAgo(50) },
    { ...base, ...done, id: "fix", kind: "stage", title: "Fix the bug", risk: "critical", status: "validated", level: "V2", needs: "V3",
      attention: true, changed: 6, since: workmapAgo(20), checks: { passed: 5, failed: 0, not_run: 0 }, contract_revision: "d".repeat(64),
      result_sha256: WORKMAP_FIX, summary: "Fixed it", planned_summary: "Fix bug 1" },
    { ...base, id: "check-fix", kind: "stage", title: "Review the fix", status: "prepared", level: "V0", needs: "V3", complexity: "high",
      attempts: 1, changed: 7, since: workmapAgo(10), inputs: { fix: WORKMAP_FIX }, contract_revision: "e".repeat(64), attempt_id: "1" },
    { ...base, ...done, id: "triage-check", kind: "stage", title: "Review the triage", status: "validated", level: "V2", settled: true,
      changed: 3, since: workmapAgo(55), checks: { passed: 2, failed: 0, not_run: 0 }, findings: {}, inputs: { triage: WORKMAP_SHA },
      contract_revision: "1".repeat(64), result_sha256: "2".repeat(64) },
    { ...base, id: "build", kind: "stage", title: "Build the page", status: later ? "validated" : "prepared", level: later ? "V2" : "V0",
      settled: later, attempts: 2, changed: later ? 10 : 8, since: workmapAgo(13), invalid: later ? 0 : 1,
      invalid_codes: later ? [] : ["separator_required"], ...(later ? { result_status: "completed", handback: "line",
      checks: { passed: 2, failed: 0, not_run: 0 }, result_sha256: "b".repeat(64), summary: "Built" } : {}) },
    { ...base, ...done, id: "audit", kind: "stage", title: "Audit the release", status: "needs_attention", level: "V0", attention: true,
      result_status: "partial", changed: 5, since: workmapAgo(40), checks: { passed: 2, failed: 1, not_run: 0 }, result_sha256: "9".repeat(64),
      question: "Ship with the known failure?" },
    { ...base, id: "docs", kind: "stage", title: "Write the docs", risk: "routine", status: "planned", level: "V0", changed: 9,
      since: workmapAgo(30) },
    { ...base, id: "old", kind: "stage", title: "Old approach", status: "superseded", level: "V0", reason: "the docs replace it",
      changed: 9, since: workmapAgo(25) },
    { ...base, id: "port", kind: "question", title: "Which port?", status: "blocked", level: "V0", reason: "waiting for the owner",
      changed: 5, since: workmapAgo(35) },
    ...(workmapExtra ? [{ ...base, id: "extra", kind: "stage", title: "Announce it", status: "planned", level: "V0", changed: 11,
      since: workmapAgo(0) }] : []),
  ];
}
const WORKMAP_EDGES = [
  { from: "decide", to: "triage", type: "depends_on" }, { from: "fix", to: "triage", type: "depends_on" },
  { from: "check-fix", to: "fix", type: "verifies" }, { from: "docs", to: "fix", type: "depends_on" },
  { from: "triage-check", to: "triage", type: "verifies" },
  { from: "docs", to: "build", type: "depends_on" }, { from: "docs", to: "decide", type: "depends_on" },
  { from: "docs", to: "old", type: "supersedes" }, { from: "port", to: "triage", type: "depends_on" },
  { from: "audit", to: "triage", type: "depends_on" }];
// The verdict the store recorded for triage-check's current attempt, on the result triage holds.
const WORKMAP_VERDICTS = [{ verifier: "triage-check", target: "triage", verdict: "clean", result_sha256: WORKMAP_SHA }];
function workmapData(path) {
  const params = new URLSearchParams(path.split("?")[1] || "");
  const edges = workmapExtra ? [...WORKMAP_EDGES, { from: "extra", to: "docs", type: "depends_on" }] : WORKMAP_EDGES;
  const summary = { task_id: "task:1", goal: "Ship the demo", closed: false, revision: workmapRevision + (workmapExtra ? 1 : 0),
    updated: "2026-11-27T10:00:00Z", counts: {}, nodes: 11, edges: 10, running: ["check-fix", "build"] };
  if (path === "/ui/api/workmap/maps") {
    // workmap_picker: the open map is older than the maps its repository lists, so the list leaves it out.
    return [200, { workspaces: [{ workspace: "ws-1", name: "agents", total: 3, skipped: 0, too_large: 0, maps: [
      { ...summary, task_id: "task:old", goal: "Older work", closed: true, updated: "2026-11-27T11:00:00Z" },
      ...(scenario === "workmap_picker" ? [] : [summary])] },
      { workspace: "ws-2", name: "other", total: 1, skipped: 0, too_large: 0, maps: [
        { ...summary, task_id: "task:2", goal: "Other work", updated: "2026-11-27T09:00:00Z" }] }] }];
  }
  if (path.startsWith("/ui/api/workmap/map?")) {
    if (params.get("task_id") !== "task:1") return [404, { status: "error", error: "unknown_map: no work map for task x" }];
    const last = workmapExtra ? ["extra", "docs"] : workmapRevision > 9 ? ["build"] : ["docs", "old"];
    return [200, { ...summary, workspace: "ws-1", name: "agents", nodes: workmapNodes(), edges, verdicts: WORKMAP_VERDICTS,
                   last_change: last, ...(params.get("workspace") ? {} : { candidates: 2 }) }];
  }
  if (path.startsWith("/ui/api/workmap/node?")) {
    const node = workmapNodes().find((item) => item.id === params.get("node"));
    return [200, { workspace: "ws-1", task_id: "task:1", revision: workmapRevision, node,
      history: [{ rev: 3, at: "2026-11-18T09:00:00Z", op: "add_node", by: "parent", status: "planned", level: "V0" },
                { rev: 6, at: new Date().toISOString(), op: "result", by: "server", status: "validated", level: "V2" }],
      relations: {}, artifacts: [{ ref: "artifact://m1/" + "d".repeat(64) + "/a1", purpose: "the report" }],
      has_result: Boolean(node.result_sha256), result_chars: 31 }];
  }
  if (path.startsWith("/ui/api/workmap/result?")) {
    return [200, { workspace: "ws-1", task_id: "task:1", node: params.get("node"), chars: 31, truncated: false,
                   text: "<script>alert(1)</script> <b>x</b>" }];
  }
  return [404, { error: "not_found" }];
}

const DOCS = {
  "user:doc": "---\npersona: tester\n---\n# Doc\n\n## One\n\ntext **bold**\n\n## Two\n\n- item\n",
  "user:plain": "just text, no headings\n",
};
// ui_history: the versions of user:doc, newest first, as /ui/api/flow/history lists them.
const VERSIONS = {
  "user:doc": [{ version: "git-" + "b".repeat(40), revision_prefix: "bbbbbbbbbbbb", deleted: true, time: "2026-10-02T09:30:00.123456+00:00" },
               { version: "git-" + "a".repeat(40), revision_prefix: "aaaaaaaaaaaa", deleted: false, time: "2026-10-01T10:00:00+00:00" }],
};
function uiData(path, init) {
  if (["ui_user_rule_toggle", "ui_user_rule_error"].includes(scenario)) {
    if (path.startsWith("/ui/api/components")) return [200, { items: [{ ...userRuleToggle }] }];
    if (path === "/ui/api/component" && init.method === "PUT") {
      const body = JSON.parse(init.body);
      puts.push(body);
      if (body.expected_revision !== userRuleToggle.revision) return [409, { error: "rule_conflict: stale revision" }];
      userRuleToggle = { ...userRuleToggle, enabled: body.enabled, revision: "r3" };
      return [200, { status: "ok" }];
    }
  }
  if (path.startsWith("/ui/api/flows")) {
    return [200, { flows: Object.keys(DOCS).map((id) => ({ id, source: "user", title: id.slice(5), content: DOCS[id] })), repositories: [] }];
  }
  if (path.startsWith("/ui/api/flow/history?")) {
    const id = new URLSearchParams(path.split("?")[1]).get("id");
    return [200, { status: "success", id, history: VERSIONS[id] || [] }];
  }
  if (path.startsWith("/ui/api/flow?")) {
    const params = new URLSearchParams(path.split("?")[1]), id = params.get("id");
    if (deletedFlows.has(id)) return [404, { status: "error", error: "flow_not_found: use list_flows to discover available flows" }];
    if (id.startsWith("builtin:")) {  // ui_history: a built-in flow, which keeps no versions
      return [200, { flow: { id, source: "builtin", title: id.slice(8), revision: "b1", source_path: "flows/" + id.slice(8) + ".md" },
                     content: "# Built-in\n" }];
    }
    if (params.get("version")) {  // an old version: its text and identity, no versions of its own
      return [200, { flow: { id, source: "user", title: id.slice(5), version: params.get("version"), revision: "old" },
                     content: "# Old " + params.get("version").slice(4, 9) + "\n" }];
    }
    return [200, { flow: { id, source: "user", title: id.slice(5), revision: revisions[id] || "rev1", source_path: "p" },
                   content: savedText[id] ?? DOCS[id] }];
  }
  if (path === "/ui/api/flow" && init.method === "PUT") {
    const body = JSON.parse(init.body);
    puts.push(body);
    if (putStatus !== 200) return [putStatus, { error: "conflict" }];
    savedText[body.id] = body.content;
    if (scenario === "ui_history") {  // the save is the newest version of the flow
      VERSIONS[body.id] = [{ version: "git-" + "c".repeat(40), revision_prefix: "cccccccccccc", deleted: false,
                             time: "2026-10-03T08:00:00+00:00" }, ...(VERSIONS[body.id] || [])];
    }
    return [200, { status: "saved", flow: { id: body.id } }];
  }
  if (path === "/ui/api/history/repos") return [200, { repos: [] }];
  if (path === "/ui/api/memory/projects") return [200, { stores: [], projects: [], notes: [] }];
  if (path.startsWith("/ui/api/components")) {
    return [200, { items: [{ id: "skill-a", short_name: "Skill A", description: "d", enabled: true, declared_by: [],
                             body: "# Skill A\n\n## Use\n\nBody <script>x</script>\n" }] }];
  }
  return [200, {}];
}
// The Agents tab: one agent with aliases, one found only by a routing keyword, one without a role.
const tiers = (core = [], preferred = [], capable = []) => ({ core, preferred, capable });
const AGENTS = [
  { id: "alpha_agent", display_name: "Alpha Agent", role: "Plans releases", tone: "Calm", trigger_command: "/alpha",
    domain_keywords: ["release"], aliases: ["/old_alpha", "/legacy_alpha"], skills: tiers(["skill-a"], [], ["skill-b", "skill-c"]),
    implants: ["implant-x"], body: "## Identity\n\nAlpha body <script>x</script>\n\n## Protocol\n\nSteps\n" },
  { id: "beta_agent", display_name: "Beta Agent", role: "Draws screens", tone: "Kind", trigger_command: "/beta",
    domain_keywords: ["wireframe"], aliases: [], skills: tiers([], ["skill-a"]), implants: [], body: "Beta body\n" },
  { id: "gamma_agent", display_name: "Gamma Agent", role: "", tone: "Dry", trigger_command: "/gamma",
    domain_keywords: [], aliases: [], skills: tiers(), implants: [], body: "" },
];
function agentsData(path, init) {
  if (path === "/ui/api/agents?with_content=1") return [200, { agents: AGENTS }];
  if (path === "/ui/api/agents") {
    return [200, { agents: AGENTS.map(({ id, display_name, role }) => ({ id, display_name, role })) }];
  }
  if (path.startsWith("/ui/api/components")) {
    return [200, { items: [{ id: "skill-a", short_name: "", description: "A skill", enabled: true, body: "# A\n",
                             declared_by: [{ agent: "alpha_agent", tier: "core" }] }] }];
  }
  if (path === "/ui/api/component" && init.method === "PUT") return [200, { status: "ok" }];
  return uiData(path, init);  // the flows
}
// --- History (#189): a scripted /ui/api/history -----------------------------------------------
// history.md of "agents" holds 45 entries: today at noon and an hour before, then one a day, from yesterday.
const NOON = new Date();
NOON.setHours(12, 0, 0, 0);
const historyEntry = (number, time) => ({
  id: number.toString(16).padStart(12, "0"), timestamp: time.toISOString(), agent: number % 2 ? "ux_designer" : "lawyer",
  persona_action: number % 3 ? "keep" : null, intent: "question " + number, action: "Agent: lawyer",
  outcome: "answer **" + number + "**", files: ["src/a.py"], tags: ["#t"], metadata: { answer_timestamp: "x" }, machine: null });
const HISTORY_ENTRIES = Array.from({ length: 45 }, (_, index) =>
  historyEntry(index + 1, new Date(NOON.getTime() - (index === 1 ? 3600000 : Math.max(0, index - 1) * 86400000))));
const ARCHIVE_ENTRIES = [historyEntry(100, new Date("2026-09-30T11:00:00Z")), historyEntry(101, new Date("2026-09-30T10:00:00Z"))];
const HISTORY_REPOS = [
  { workspace: "w-1", name: "agents", root: "/code/agents", available: true, entries: 47, bytes: 600000,
    newest: new Date(Date.now() - 2 * 60000).toISOString(), files: [{ file: "current", entries: 45 }, { file: "2026-09", entries: 2 }] },
  { workspace: "w-2", name: "project-a", root: "/code/project-a", available: true, entries: 1, bytes: 900,
    newest: new Date(Date.now() - 180 * 60000).toISOString(), files: [{ file: "current", entries: 1 }] },
  { workspace: "w-gone", name: "gone", root: "/code/gone", available: false, entries: 0, bytes: 0, newest: null, files: [] },
];
const historyPosts = [];  // every deletion the page asked for
// history_adopt (#292): the fallback journal holds one turn whose session named a folder of project-a.
const UNRESOLVED_ENTRIES = [{ ...historyEntry(200, new Date()), intent: "kept without a project",
  metadata: { unresolved: "workspace_required", claimed_workspace: "/code/project-a/sub" } }];
const UNRESOLVED_REPO = { workspace: "unresolved", name: "Without a project", root: "/state/unresolved", available: true,
  unresolved: true, entries: 1, bytes: 300, newest: new Date().toISOString(), files: [{ file: "current", entries: 1 }] };
const historyAdopts = [];  // every move the page asked for
function historyData(path, init) {
  const url = new URL("http://127.0.0.1" + path), query = url.searchParams;
  if (url.pathname === "/ui/api/history/adopt" && init.method === "POST") {
    const body = JSON.parse(init.body);
    historyAdopts.push(body);
    UNRESOLVED_ENTRIES.splice(0, UNRESOLVED_ENTRIES.length);
    return [200, { status: "adopted", moved: [{ id: body.entries[0] }], already: [], changed: [], missing: [] }];
  }
  if (url.pathname === "/ui/api/history/repos" && scenario === "history_adopt" && UNRESOLVED_ENTRIES.length) {
    return [200, { repos: [UNRESOLVED_REPO, ...HISTORY_REPOS] }];
  }
  if (url.pathname === "/ui/api/history" && query.get("workspace") === "unresolved") {
    return [200, { workspace: "unresolved", name: "Without a project", root: "/state/unresolved", file: "current",
                   files: ["current"], total: UNRESOLVED_ENTRIES.length, bytes: 300, entries: UNRESOLVED_ENTRIES,
                   index: UNRESOLVED_ENTRIES.map(({ id, timestamp, agent, persona_action }) => ({ id, timestamp, agent, persona_action })) }];
  }
  if (url.pathname === "/ui/api/history/delete" && init.method === "POST") {
    const body = JSON.parse(init.body);
    historyPosts.push(body);
    const index = HISTORY_ENTRIES.findIndex((entry) => entry.id === body.entry && entry.timestamp === body.time);
    if (index < 0) return [409, { status: "error", error: "entry_changed: the entry is no longer there as shown" }];
    HISTORY_ENTRIES.splice(index, 1);
    return [200, { status: "deleted" }];
  }
  if (url.pathname === "/ui/api/history/repos") return [200, { repos: HISTORY_REPOS }];
  if (url.pathname === "/ui/api/history/search") {
    return [200, { q: query.get("q"), repos: query.get("q") === "needle" ? [{ workspace: "w-2", matches: 3 }] : [] }];
  }
  if (url.pathname === "/ui/api/history/source") {
    return [200, { workspace: query.get("workspace"), file: query.get("file"), text: "---\nrepo: x\n---\nraw " + query.get("file") }];
  }
  if (url.pathname === "/ui/api/history") {
    const file = query.get("file") || "current", entries = file === "current" ? HISTORY_ENTRIES : ARCHIVE_ENTRIES;
    // `before` counts from the oldest entry, as the daemon's does.
    const before = query.get("before"), first = before === null || query.get("entry");
    let start = first ? 0 : Math.max(0, entries.length - Number(before)), limit = Number(query.get("limit") || 20), focus;
    if (query.get("entry")) {
      focus = entries.findIndex((entry) => entry.id === query.get("entry"));
      limit = Math.max(limit, focus + 1);
    }
    const out = { workspace: query.get("workspace"), name: "agents", root: "/code/agents", file, files: ["current", "2026-09"],
                  total: entries.length, bytes: 600000, entries: entries.slice(start, start + limit) };
    if (first) out.index = entries.map(({ id, timestamp, agent, persona_action }) => ({ id, timestamp, agent, persona_action }));
    if (focus !== undefined) out.focus = focus;
    return [200, out];
  }
  return uiData(path, init);
}

// --- Memory (#297): a scripted /ui/api/memory ------------------------------------------------
// The project "agents" holds three items in ~/.claude, two linked by MEMORY.md, which also links a
// missing file; ~/.claude-work holds one item of it. Writes are recorded in `memoryCalls`.
const MEM_MAIN = "/home/me/.claude", MEM_WORK = "/home/me/.claude-work", MEM_PROJECT = "-home-me-code-agents";
const memNote = (name, type, description, body) =>
  "---\nname: " + name + "\ndescription: \"" + description + "\"\nmetadata:\n  type: " + type + "\n---\n\n" + body + "\n";
const memItem = (file, name, type, description, body) => ({ file, name, type, description, modified: "2026-10-05T10:00:01Z",
  changed: "2026-10-05T10:00:01Z", mtime: new Date(Date.now() - 3600000).toISOString(), bytes: 120, editable: true,
  frontmatter: true, text: memNote(name, type, description, body), sha256: (file.replace(/\W/g, "") + "0".repeat(64)).slice(0, 64) });
const MEMORY_ITEMS = [memItem("workflow.md", "workflow", "feedback", "restate first", "See [[zsh]] and [[nobody]]."),
                      memItem("zsh.md", "zsh", "reference", "arrays start at 1", "Body of zsh."),
                      { ...memItem("loose.md", null, null, null, ""), text: "no frontmatter\n" }];
let memoryIndex = "- [Workflow](workflow.md) — restate first\n- [zsh](zsh.md) — arrays\n- [Gone](gone.md) — deleted\n";
const memLinks = () => memoryIndex.split("\n").map((line, k) => {
  const match = /\[(.*)\]\((.*)\)/.exec(line);
  return match && { title: match[1], file: match[2], line: k + 1 };
}).filter(Boolean);
const MEMORY_ROWS = [
  { key: "claude:" + MEM_PROJECT, client: "claude", project: MEM_PROJECT, name: "agents", root: "/home/me/code/agents", workspace: "w-1",
    places: [{ store: MEM_MAIN, label: "~/.claude", items: 3, newest: new Date(Date.now() - 2 * 60000).toISOString() },
             { store: MEM_WORK, label: "~/.claude-work", items: 1, newest: new Date(Date.now() - 3 * 3600000).toISOString() }],
    items: 4, newest: new Date(Date.now() - 2 * 60000).toISOString() },
  { key: "claude:", client: "claude", project: "", name: "Outside projects", root: null, workspace: null,
    places: [{ store: MEM_MAIN, label: "~/.claude", items: 1, newest: new Date(Date.now() - 86400000).toISOString() }],
    items: 1, newest: new Date(Date.now() - 86400000).toISOString() },
];
const memoryCalls = [];
function memoryData(path, init) {
  const url = new URL("http://127.0.0.1" + path), query = url.searchParams, method = init.method || "GET";
  const body = init.body ? JSON.parse(init.body) : null;
  if (method !== "GET") memoryCalls.push([method, url.pathname, body]);
  if (scenario === "memory_codex") return memoryCodex(url, method);
  if (url.pathname === "/ui/api/memory/projects") {
    return [200, { stores: [{ store: MEM_MAIN, label: "~/.claude", client: "claude" }, { store: MEM_WORK, label: "~/.claude-work", client: "claude" }],
                   projects: MEMORY_ROWS, notes: ["Codex: no memory files.", "Cursor keeps no memory in files."] }];
  }
  if (url.pathname === "/ui/api/memory/search") {
    return [200, { q: query.get("q"), projects: query.get("q") === "needle" ? [{ key: "claude:", matches: 1 }] : [] }];
  }
  if (url.pathname === "/ui/api/memory" && method === "GET") {
    const store = query.get("store"), main = store === MEM_MAIN, project = query.get("project");
    const items = project === "" ? [memItem("routing.md", "routing", "feedback", "d", "x")]
      : main ? MEMORY_ITEMS : [memItem("zsh.md", "zsh", "reference", "older", "Older zsh.")];
    const links = main && project ? memLinks() : [];
    const label = main ? "~/.claude" : "~/.claude-work", other = main ? MEM_WORK : MEM_MAIN;
    return [200, { store, label, client: "claude", project, key: "claude:" + project, name: project ? "agents" : "Outside projects",
                   root: project ? "/home/me/code/agents" : null, dir: store + "/projects/" + project + "/memory", items,
                   index: { exists: links.length > 0, sha256: links.length ? "i".repeat(64) : null, text: links.length ? memoryIndex : null, links },
                   not_indexed: items.filter((item) => !links.some((link) => link.file === item.file)).map((item) => item.file),
                   broken: links.filter((link) => !items.some((item) => item.file === link.file)),
                   places: [{ store: MEM_MAIN, label: "~/.claude" }, { store: MEM_WORK, label: "~/.claude-work" }],
                   targets: [{ store: other, label: main ? "~/.claude-work" : "~/.claude" }] }];
  }
  if (url.pathname === "/ui/api/memory/file" && method === "PUT") {
    if (body.text === "CONFLICT") return [409, { status: "error", error: "file_changed: zsh.md changed since it was shown" }];
    if (body.file === "MEMORY.md") {
      memoryIndex = body.text;
      return [200, { status: "saved", indexed: false }];
    }
    const item = MEMORY_ITEMS.find((entry) => entry.file === body.file);
    if (body.sha256 === null && item) return [409, { status: "error", error: "file_exists: " + body.file + " already exists" }];
    if (item) item.text = body.text;
    else MEMORY_ITEMS.push({ ...memItem(body.file, body.file.slice(0, -3), "feedback", "new", ""), text: body.text });
    return [200, { status: body.sha256 === null ? "created" : "saved", indexed: !!body.index }];
  }
  if (url.pathname === "/ui/api/memory/delete") {
    MEMORY_ITEMS.splice(MEMORY_ITEMS.findIndex((item) => item.file === body.file), 1);
    return [200, { status: "deleted", trash: "/state/memory-trash/x/claude/" + body.project + "/" + body.file }];
  }
  if (url.pathname === "/ui/api/memory/copy") {
    if (!body.move && !body.replace_sha256) {
      return [409, { status: "error", error: "target_exists: ~/.claude-work already has " + body.file,
                     target: { store: MEM_WORK, label: "~/.claude-work", sha256: "t".repeat(64), text: "Older zsh.\n" } }];
    }
    if (body.move) MEMORY_ITEMS.splice(MEMORY_ITEMS.findIndex((item) => item.file === body.file), 1);
    return [200, { status: body.move ? "moved" : "copied", indexed: true, trash: body.move ? "/state/memory-trash/y" : undefined,
                   source_index_warning: body.move ? "file_changed: MEMORY.md changed since it was shown" : undefined }];
  }
  return uiData(path, init);
}

// memory_codex: one Codex store, whose files are plain text and which keeps no index.
const MEM_CODEX = "/home/me/.codex";
function memoryCodex(url, method) {
  const place = { store: MEM_CODEX, label: "~/.codex", items: 1, newest: new Date(Date.now() - 3600000).toISOString() };
  if (url.pathname === "/ui/api/memory/projects") {
    return [200, { stores: [{ store: MEM_CODEX, label: "~/.codex", client: "codex" }], notes: ["Cursor keeps no memory in files."],
                   projects: [{ key: "codex:", client: "codex", project: "", name: "Codex memories", root: null, workspace: null,
                                places: [place], items: 1, newest: place.newest }] }];
  }
  if (url.pathname === "/ui/api/memory" && method === "GET") {
    const item = { ...memItem("facts.md", null, null, null, ""), text: "# not *rendered*\n" };
    return [200, { store: MEM_CODEX, label: "~/.codex", client: "codex", project: "", key: "codex:", name: "Codex memories",
                   root: null, dir: MEM_CODEX + "/memories", items: [item],
                   index: { exists: false, sha256: null, text: null, links: [] }, not_indexed: ["facts.md"], broken: [],
                   places: [{ store: MEM_CODEX, label: "~/.codex" }], targets: [] }];
  }
  if (url.pathname === "/ui/api/memory/file" && method === "PUT") return [200, { status: "created", indexed: false }];
  return [404, { error: "not_found" }];
}

const flow = (id, source, title, content, extra = {}) => ({ id, source, title, content, ...extra });
const rule = (id, short_name, description, body, enabled = true) =>
  ({ id, short_name, description, body, enabled, declared_by: [] });
function searchData(path) {
  if (path.startsWith("/ui/api/flows")) {
    if (!path.includes("with_content=1")) throw new Error("the page must ask for flow content");
    return {
      flows: [flow("user:alpha", "user", "Alpha plan", "deploy steps"),
              flow("user:beta", "user", "Beta", "a mention of ALPHA here, flow chart"),
              flow("review", "builtin", "Review", "the needle phrase", { qualified_id: "builtin:review" })],
      repositories: [{ key: "r", label: "github.com/o/r", issues: [],
                       flows: [flow("repo:x", "repo", "Repo flow", "needle in the text", { repo_key: "r" })] }],
    };
  }
  return {
    items: [rule("rule-a", "Security", "first", "contains logging"), rule("rule-b", "Logging style", "second", "nothing"),
            rule("rule-c", "Third", "third", "Logging and security", false)],
  };
}

const raceFlow = (id, title) => ({ id: `user:${id}`, source: "user", title, revision: id.repeat(64),
                                    persona: null, persona_source: null });
async function personaRace(path, init) {
  if (path.startsWith("/ui/api/flows")) {
    return respond(200, { flows: [raceFlow("a", "Alpha"), raceFlow("b", "Beta")], repositories: [] });
  }
  if (path === "/ui/api/flow/persona") {
    await new Promise((resolve) => { releasePersonaPut = resolve; });
    return respond(200, { status: "saved", flow: raceFlow("a", "Alpha") });
  }
  if (path.startsWith("/ui/api/flow?")) {
    const id = new URLSearchParams(path.split("?")[1]).get("id");
    if (id === "user:b") await sleep(100);  // B is still loading when the save answers
    return respond(200, { flow: raceFlow(id.slice(5), id === "user:b" ? "Beta" : "Alpha"),
                          content: "# T\n", history: [] });
  }
  if (path === "/ui/api/agents") return respond(200, { agents: [] });
  return respond(200, { items: [] });
}

// --- library sync: a scripted /ui/api/sync -------------------------------------------------
// `syncState` answers GET /ui/api/sync; `syncReplies["METHOD /path"]` queues other answers (the
// last one repeats); `syncCalls` records [method, path with query, body] of every sync request. A
// request answers what `syncState` held when it was sent, also when a hold makes it answer later.
const syncCalls = [];
const syncReplies = {};
const PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaFakeKeyForTests agents-core-sync:mac-1a2b";
const SYNC_OFF = { state: "off", reason: null, message: "sync is not set up", conflicts: 0, set_up: false, started: null,
                   github: null, conflict_list: [], suggested_label: "mac-1a2b", repository: null, github_repository: null,
                   loop: { syncing: false, next_fetch_seconds: null } };
let syncState = { ...SYNC_OFF };
function syncAnswer(method, path, body) {
  syncCalls.push([method, path, body]);
  const queue = syncReplies[method + " " + path.split("?")[0]];
  if (queue && queue.length) return queue.length > 1 ? queue.shift() : queue[0];
  if (method === "GET" && path.split("?")[0] === "/ui/api/sync") return [200, syncState];
  return [404, { error: "not_found" }];
}
const reply = (key, ...answers) => { syncReplies[key] = answers; };
const syncHolds = {};  // "METHOD /path" -> { promise, release }: that request answers when released
const hold = (key) => {
  let release;
  syncHolds[key] = { promise: new Promise((resolve) => { release = resolve; }), release: () => { delete syncHolds[key]; release(); } };
  return syncHolds[key];
};
const minutesAgo = (minutes) => new Date(Date.now() - minutes * 60000).toISOString();
const FLOW_CONFLICT = { id: "20261005T120000000000Z-aaaaaaaaaa", path: "common/doc.md", flow: "user:doc", repo_key: null,
                        kind: "both_changed", kept: "remote", machine: "laptop", time: "2026-10-05T12:00:00+00:00",
                        deleted_on: null, reason: "overlapping_changes", overlaps: 2, mine: "version" };
const PERSONA_CONFLICT = { id: "20261005T120100000000Z-bbbbbbbbbb", path: "personas/common/doc.json", flow: "user:doc",
                           repo_key: null, kind: "both_changed", kept: "remote", machine: "laptop",
                           time: "2026-10-05T12:01:00+00:00", deleted_on: null, reason: "not_text", mine: "content" };
const OLD_ENTRY = { time: "2026-10-05T09:00:00+00:00", result: "ok", machine: "mac-1a2b", sent: ["user:old"], received: [],
                    received_from: [], scripts: [], conflicts: 0 };
const started = () => ({
  state: "synced", reason: null, message: null, set_up: true, started: "2026-10-01T10:00:00+00:00", last_success: minutesAgo(2),
  last_attempt: minutesAgo(2), retry_at: null, remote: "github.com/octocat/agents-library",
  repository: "octocat/agents-library", github_repository: "octocat/agents-library", ssh: true, branch: "main",
  label: "mac-1a2b", identity: { name: "Owner", email: "owner@example.com" }, pending: 0, stale: false,
  conflicts: 2, conflict_list: [FLOW_CONFLICT, PERSONA_CONFLICT], fetch_minutes: 5, ask_new_repositories: false,
  activity: [OLD_ENTRY, { time: "2026-10-05T11:00:00+00:00", result: "ok", machine: "mac-1a2b", sent: [],
                          received: ["user:tool"], received_from: ["laptop"], scripts: ["common/tool.py"], conflicts: 0,
                          merged: ["user:doc"] }],
  announced_groups: [{ group: "repos/abc", origin: "github.com/o/r", time: "2026-10-05T11:00:00+00:00" }],
  pending_groups: ["repos/def"], pending_files: [], blocked: [], public_key: PUBLIC_KEY,
  github: { connected: true, login: "octocat", host: "github.com", reconnect_needed: false, warning: null },
  loop: { state: "running", active: true, syncing: false, held: null, next_fetch_seconds: 240, last_cycle: null },
});
if (["sync_dashboard", "sync_notice", "sync_more", "sync_round2"].includes(scenario)) syncState = started();
// Set up over SSH, then the page was reloaded; some setups stopped at the host's fingerprints.
if (["sync_hostkey", "sync_busy_radio", "sync_connection_choice", "sync_connection_recovery",
     "sync_comparison_recovery", "sync_preview_error"].includes(scenario)) {
  syncState = { ...SYNC_OFF, state: "waiting_for_access", set_up: true, started: null, suggested_label: undefined,
                repository: "git.example.com/me/library", ssh: true,
                host_key_unconfirmed: ["sync_hostkey", "sync_busy_radio"].includes(scenario), label: "laptop",
                identity: { name: "Owner", email: "owner@example.com" }, public_key: PUBLIC_KEY };
}
// Set up on GitHub, stopped before GitHub's host keys were stored, then the page was reloaded.
if (scenario === "sync_hostkey_github") {
  syncState = { ...SYNC_OFF, state: "waiting_for_access", set_up: true, started: null, suggested_label: undefined,
                repository: "octocat/agents-library", github_repository: "octocat/agents-library", ssh: true,
                host_key_unconfirmed: true, label: "laptop", identity: { name: "Owner", email: "owner@example.com" },
                public_key: PUBLIC_KEY, github: { connected: true, login: "octocat", host: "github.com" } };
}

// --- the Autodev page (#348): a scripted /ui/api/autodev ----------------------------------------
// The settings are the daemon's defaults, as the test passes them (`editable(DEFAULTS)`); `autodevCalls`
// records [method, path, body] of every request. A save whose slots are not HH:MM is refused as the
// daemon refuses it. As the daemon's revision leaves out enabled and mode, the switch keeps it; a
// scenario changes it, with the settings, for a change made elsewhere. autodev_stored starts with
// stored values the lists lack and a stored run mode; autodev_invalid with an invalid autodev.json;
// autodev_ready with a recent passing `autodev ready` recorded and a current acceptance (#350), so
// run is offered.
const AUTODEV_DEFAULTS = isAutodev ? JSON.parse(process.env.AUTODEV_DEFAULTS) : null;
const CLAUDE_EFFORT_LIST = ["low", "medium", "high", "xhigh", "max"];
const CODEX_EFFORT_LIST = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "persistent"];
const autodevCalls = [];
const autodevState = { revision: "", enabled: false, mode: "dry-run",
                       settings: AUTODEV_DEFAULTS && JSON.parse(JSON.stringify(AUTODEV_DEFAULTS)) };
if (scenario === "autodev_stored") {
  autodevState.enabled = true;
  autodevState.mode = "run";
  autodevState.settings.stages.select.claude.effort = "ultra";
  autodevState.settings.delegates["code-review-pass"].effort_followup = "minimal";
}
let autodevReady = scenario === "autodev_ready";  // what the daemon's check of the recorded result says now
const autodevHolds = {};  // "METHOD path" -> { promise, release }: the next such request answers on release
const holdAutodev = (key) => {
  let release;
  const promise = new Promise((resolve) => { release = resolve; });
  autodevHolds[key] = { promise };
  return release;
};
const COMMITS = { available: false, reason: "codex_stage_commit_unsupported", checked: null,
                  message: "this stage commits, and a Codex stage cannot (workspace-write keeps .git read-only)" };
function autodevPayload() {
  if (scenario === "autodev_invalid") {
    return { ...autodevValidPayload(), revision: "", problems: ["times must be a non-empty list of HH:MM"], settings: null,
             codex: null, status: { ...autodevValidPayload().status, enabled: null, mode: null, next_slot: null } };
  }
  return autodevValidPayload();
}
function autodevValidPayload() {
  const select = scenario === "autodev_codex"
    ? { available: true, reason: null, message: null, checked: "2026-10-11T11:00:00+00:00" }
    : { available: false, reason: "not_checked", checked: null,
        message: "the stage probes have not passed yet: run `python -m src.schedules codex-probe --kind stage --confirm-live-probe`" };
  return {
    revision: autodevState.revision, problems: null, settings: autodevState.settings, defaults: AUTODEV_DEFAULTS,
    choices: { clients: ["claude", "codex"], modes: ["dry-run", "run"], limits: ["max_reviews", "max_bot_rounds", "max_wait_minutes"],
               efforts: { claude: CLAUDE_EFFORT_LIST, codex: CODEX_EFFORT_LIST }, agents: ["code_reviewer", "software_engineer", "sysadmin"] },
    capabilities: { claude: { client: "claude", efforts: CLAUDE_EFFORT_LIST, version: "2.1.296", source: "installed" },
                    codex: { client: "codex", efforts: CODEX_EFFORT_LIST, version: "0.162.1", source: "installed" } },
    codex: { installed: true, version: "0.162.1", stages: { select, implement: COMMITS, submit: COMMITS } },
    status: { enabled: autodevState.enabled, mode: autodevState.mode, next_slot: "2026-10-12T13:47:00+00:00",
              scheduler: { installed: false, supported: true },
              runs: [{ run_id: "20261011T134700Z-bbbbbb", mode: "dry-run", trigger: { kind: "slot", at: "x" }, status: "failed: infra",
                       reason: "network: github.com unreachable", started: "2026-10-11T13:47:05+00:00" },
                     { run_id: "20261011T021700Z-aaaaaa", mode: "dry-run", trigger: { kind: "slot", at: "y" }, status: "completed",
                       decision: "select", started: "2026-10-11T02:17:05+00:00" }],
              automatic: autodevReady ? AUTODEV_READY : AUTODEV_NOT_READY },
  };
}
const AUTODEV_RISKS = [{ row: 1, risk: "A model read the keys", residual_risk: "a sandbox bug" },
                       { row: 2, risk: "An agent decided", residual_risk: "agents act under the owner's account" },
                       { row: 4, risk: "The agent pushed to main", residual_risk: "a broker bug" }];
const AUTODEV_READY = { can_enable: true, missing: [], checked: "2026-10-11T09:05:00+00:00", passed: true,
                        fresh_minutes: 15, accepted: "2026-10-11T09:00:00+00:00", risks: AUTODEV_RISKS,
                        risk_list_sha256: "v1" };
const AUTODEV_NOT_READY = { can_enable: false, checked: "2026-10-11T09:05:00+00:00", passed: false, fresh_minutes: 15,
  accepted: null, risks: AUTODEV_RISKS, risk_list_sha256: "v1",
  missing: ["the last `python -m src.schedules autodev ready` (2026-10-11T09:05:00+00:00) did not pass", "the API token",
            "sandbox probe implement in scheduled mode: no record; run `python -m src.schedules probe "
            + "--mode scheduled --kind implement --confirm-live-probe`"] };
function autodevAnswer(method, path, body) {
  autodevCalls.push([method, path, body]);
  if (method === "GET" && path === "/ui/api/autodev") return [200, autodevPayload()];
  if (method === "PUT" && path === "/ui/api/autodev") {
    if (body.expected_revision !== autodevState.revision) return [409, { status: "error", error: "config_changed",
      message: "autodev.json changed since the page read it; reload to see it" }];
    const wrong = body.settings.times.filter((time) => !/^([01][0-9]|2[0-3]):[0-5][0-9]$/.test(time));
    if (wrong.length) {
      const problems = wrong.map((time) => `times: '${time}' is not a time of day HH:MM`);
      return [400, { status: "error", error: "invalid_settings", message: "not saved: " + problems[0], problems }];
    }
    autodevState.settings = body.settings;
    autodevState.revision = "r" + autodevCalls.length;
    return [200, { ...autodevPayload(), saved: true, warnings: [] }];
  }
  if (method === "POST" && path === "/ui/api/autodev/enable") {
    if (body.mode === "run" && body.accept_shared_account !== true) {
      return [409, { status: "error", error: "acceptance_required", missing: [], risks: AUTODEV_RISKS,
                     message: "run mode starts real runs on the schedule under your GitHub account; turning it on needs "
                              + "accept_shared_account: true" }];
    }
    if (body.mode === "run" && !autodevReady) {
      return [409, { status: "error", error: "not_ready", missing: AUTODEV_NOT_READY.missing,
                     message: "the automatic mode is not ready: 3 condition(s) missing" }];
    }
    autodevState.enabled = true;
    autodevState.mode = body.mode;
    return [200, { ...autodevPayload(), result: { ok: true, enabled: true, mode: body.mode, scheduler_installed: false,
      note: "run `python -m src.schedules autodev install` so that ticks start" } }];
  }
  if (method === "POST" && path === "/ui/api/autodev/disable") {
    autodevState.enabled = false;
    return [200, { ...autodevPayload(), result: { ok: true, enabled: false } }];
  }
  return [404, { error: "not_found" }];
}

// --- the landing page (#188): a scripted /ui/api/overview and /ui/api/stats ---------------
const LANDING_OVERVIEW = {
  version: "26.10.06.1200",
  repository: { repository: "https://github.com/IEZhu/Agents", readme: "https://github.com/IEZhu/Agents#readme",
                docs: "https://github.com/IEZhu/Agents/blob/HEAD/docs/README.md", issues: "https://github.com/IEZhu/Agents/issues" },
  model: { name: "microsoft/harrier-oss-v1-270m", size: 1181116006 },
  counts: { agents: 45, rules: 6, skills: 70, implants: 57, flows: { builtin: 28, personal: 3, repository: 2 } },
  directories: [{ id: "installation", label: "Installation", path: "/opt/agents", size: 2147483648 },
                { id: "logs", label: "Logs of stdio servers", path: "/opt/agents/logs", size: null }],
  apps: { "claude-code": { configured: true, scopes: ["claude:user"] }, "claude-desktop": { configured: false, scopes: [] },
          codex: { configured: true, scopes: ["codex:user"] }, cursor: { configured: false, scopes: [] } },
  registrations: { auto: true, updated_at: "2026-10-11T08:30:00+00:00", clients: {
    claude: { status: "upgraded", files: ["/home/u/.claude.json"], backup: "/state/backups/1" },
    codex: { status: "skipped", files: [], reason: "/home/u/repo/.codex/config.toml: its Agents-Core entry was not written by this installation" },
    cursor: { status: "current", files: [] } },
    warnings: [{ warning: "duplicate_claude_code_server", profile: "/home/u/.claude",
                 message: "Claude Code (/home/u/.claude) has the Agents-Core server of the agents-core@x plugin and also Agents-Core entries in: user (/home/u/.claude.json)." }] },
};
const landingDay = (offset, answers) => ({ date: new Date(Date.now() - offset * 86400000).toISOString().slice(0, 10), answers });
const landingEvent = (number, app, agent, action, repository) => ({
  id: number.toString(16).padStart(12, "0"), time: minutesAgo(number), agent, action, workspace: "w-1", repository, app });
const LANDING_STATS = {
  computed_at: new Date().toISOString(), uptime_seconds: 3 * 3600 + 12 * 60, connected_window_seconds: 300,
  answers: { today: 4, last_7_days: 10, last_30_days: 16,
             per_day: Array.from({ length: 30 }, (_, index) => landingDay(29 - index, index === 29 ? 4 : index === 10 ? 8 : index % 7 === 0 ? 1 : 0)),
             per_agent: [{ agent: "software_engineer", answers: 9 }, { agent: "lawyer", answers: 7 }],
             per_repository: [{ workspace: "w-1", name: "Agents", answers: 12 }, { workspace: "w-2", name: "site", answers: 4 }],
             per_action: { keep: 10, switch: 4, refresh: 1, restore: 0, other: 0, none: 1 },
             per_app: [{ app: "claude-code", answers: 12 }, { app: "unknown", answers: 4 }] },
  events: [landingEvent(1, "claude-code", "ux_designer", "switch", "Agents"), landingEvent(2, "codex", "lawyer", "keep", "site"),
           landingEvent(3, null, "software_engineer", null, "Agents"), landingEvent(4, "cursor", "lawyer", "keep", "site")],
  skipped: [{ workspace: "w-3", name: "gone", reason: "missing" }],
  apps: [{ app: "claude-code", connected: true, streams: 1, last_seen: minutesAgo(1), requests_today: 12, client: "claude-code",
           version: "2.0.14", initialized: minutesAgo(50), workspace: "Agents" },
         { app: "unknown", connected: false, streams: 0, last_seen: minutesAgo(30), requests_today: 3, client: null, version: null,
           initialized: null, workspace: null }],
};
const LANDING_EMPTY = { ...LANDING_STATS, uptime_seconds: 42, events: [], skipped: [], apps: [],
  answers: { today: 0, last_7_days: 0, last_30_days: 0, per_day: Array.from({ length: 30 }, (_, index) => landingDay(29 - index, 0)),
             per_agent: [], per_repository: [], per_action: {}, per_app: [] } };
// The Updates section (#381): the channel as /ui/api/overview and /ui/api/updates answer it.
const downgrading = scenario.startsWith("landing_updates_down");
let updatesNow = {
  configured_branch: downgrading ? "dev" : "main", source: downgrading ? ".env" : "default",
  checked_out_branch: downgrading ? "dev" : "main", action: "follow", revision: "r1", auto_update: true, interval: 900,
  last_run: { time: "2026-10-14T10:00:00+00:00", state: "up_to_date" }, choices: ["main", "dev"],
  update_command: ".venv/bin/python -m src.daemon update",
  when: "Updates fast-forward " + (downgrading ? "dev" : "main") + " at the next scheduled run (every 15 min, when the service is idle), or now with `.venv/bin/python -m src.daemon update`. A saved change applies the same way.",
};
const updatesPuts = [];
function updatesAnswer(method, body) {
  if (method === "GET") return [200, { version: LANDING_OVERVIEW.version, updates: updatesNow }];
  updatesPuts.push(body);
  if (scenario === "landing_updates_conflict") {
    updatesNow = { ...updatesNow, configured_branch: "dev", source: ".env", revision: "r2", action: "switch",
                   when: "The switch from main to dev happens at the next scheduled run." };
    return [409, { status: "error", error: "conflict", message: "the update channel changed since it was read; reload and try again" }];
  }
  if (body.expected_revision !== updatesNow.revision) return [409, { status: "error", error: "conflict", message: "stale" }];
  if (downgrading && !body.allow_downgrade) {
    return [409, { status: "error", error: "downgrade", message: "origin/main does not contain HEAD 0123456789ab of dev: "
      + "switching would go back to older code, and state or .env changes that newer code made may not be read by it; "
      + "confirm with --allow-downgrade" }];
  }
  const from = updatesNow.checked_out_branch;
  updatesNow = { ...updatesNow, configured_branch: body.branch, source: ".env", revision: "r2",
                 action: body.branch === from ? "follow" : "switch",
                 when: "The switch from " + from + " to " + body.branch + " happens at the next scheduled run (every 15 min, when the service is idle), or now with `.venv/bin/python -m src.daemon update`." };
  return [200, { version: LANDING_OVERVIEW.version, updates: updatesNow,
                 saved: { branch: body.branch, downgrade: !!body.allow_downgrade } }];
}
function landingData(path, init) {
  if (path === "/ui/api/overview") return [200, { ...LANDING_OVERVIEW, updates: updatesNow }];
  if (path === "/ui/api/updates") return updatesAnswer(init.method || "GET", init.body ? JSON.parse(init.body) : null);
  if (path === "/ui/api/stats") return [200, scenario === "landing_empty" ? LANDING_EMPTY : LANDING_STATS];
  return agentsData(path, init);  // the flows, the other tabs and the sync status
}

const docHandlers = {};
const windowHandlers = {};
let narrowNow = false;  // ui_place: the stylesheet's narrow layout applies
const storage = new Map(scenario === "ui_narrow_stored" ? [["agents-ui-toc-hidden", "0"]] : []);
// The landing page (#188) opens /ui unless its choice says otherwise; the other scenarios start where
// they always did, on Flows, and their reports leave the choice out.
const LANDING_OFF = "agents-ui-landing-off";
if (!scenario.startsWith("landing") || scenario === "landing_stored_off") storage.set(LANDING_OFF, "1");
const stored = () => [...storage.entries()].filter(([key]) => key !== LANDING_OFF || scenario.startsWith("landing"));
// The page's timers of a second or more (the 30 s status poll, the sign-in poll) never run by
// themselves: a scenario fires them, and none keeps Node alive at the end.
const timers = [];
const fakeTimers = new WeakSet();
function pageSetTimeout(handler, delay, ...args) {
  if (!(delay >= 1000)) return setTimeout(handler, delay, ...args);
  const timer = { handler, delay, args, cleared: false };
  fakeTimers.add(timer);
  timers.push(timer);
  return timer;
}
function pageClearTimeout(timer) {
  if (timer && fakeTimers.has(timer)) timer.cleared = true;
  else clearTimeout(timer);
}
const pendingTimers = (delay) => timers.filter((timer) => !timer.cleared && !timer.fired && (delay === undefined || timer.delay === delay));
async function fireTimers(delay) {
  for (const timer of pendingTimers(delay)) {
    timer.fired = true;
    timer.handler(...timer.args);
  }
  await sleep(30);
}
// The Work map scenarios' viewport width (Present's readable floor scales with it), their tab's
// sessionStorage (workmap_present starts presenting), and a ResizeObserver whose callback a scenario
// runs itself, as a browser would after a box changed size.
let viewportWidth = 1920;
const session = new Map(scenario === "workmap_present" ? [["agents-core.workmap-present", "1"]] : []);
const resizeObservers = [];
class StubResizeObserver {
  constructor(callback) { this.callback = callback; this.observed = new Set(); resizeObservers.push(this); }
  observe(target) { this.observed.add(target); }
  disconnect() { this.observed.clear(); }
}
// The map select as a browser keeps it, an HTML single-select of display size 1: one option is always
// selected once there is one. An option inserted into a select with none selected is selected; selecting
// one deselects the others; deselecting the selected one selects the first again, so `selected` read
// back is not what was assigned. `value` is the selected option's.
function browserSelect(select) {
  select.tag = "select";
  const options = () => select.children.flatMap((child) => (child.tag === "optgroup" ? child.children : [child]))
    .filter((child) => child.tag === "option");
  const settle = () => {
    const all = options();
    if (all.length && !all.some((option) => option.chosen)) all[0].chosen = true;
  };
  const adopt = (option) => {
    if (option.tag !== "option" || "chosen" in option) return;
    option.chosen = false;
    Object.defineProperty(option, "selected", {
      get: () => option.chosen,
      set: (value) => {
        if (value) for (const other of options()) other.chosen = false;
        option.chosen = Boolean(value);
        settle();
      },
    });
  };
  const watch = (parent) => {
    const append = parent.appendChild;
    parent.appendChild = (child) => {
      append(child);
      if (child.tag === "optgroup") watch(child);
      adopt(child);
      settle();
    };
  };
  watch(select);
  Object.defineProperty(select, "value", {
    get: () => (options().find((option) => option.chosen) || { value: "" }).value, set: () => {} });
}
if (isWorkmap) browserSelect(byId("wm-map"));
const documentStub = {
  getElementById: byId, createElement: (tag) => element("", tag),
  createElementNS: (namespace, tag) => Object.assign(element("", tag), { namespace }),
  createTextNode: (text) => ({ nodeType: 3, textContent: text }),
  addEventListener(type, handler) { (docHandlers[type] = docHandlers[type] || []).push(handler); },
  visibilityState: "visible",
  documentElement: element("", "html"),  // the theme switch sets data-theme on it
};
const context = vm.createContext({
  document: documentStub,
  window: {
    addEventListener(type, handler) { (windowHandlers[type] = windowHandlers[type] || []).push(handler); },
    matchMedia: (query) => ({ matches: query.includes("reduced-motion") ? scenario.includes("reduced")
                                                                    : scenario.includes("narrow") || narrowNow }),
    get innerWidth() { return viewportWidth; },
  },
  sessionStorage: {
    getItem: (key) => (session.has(key) ? session.get(key) : null), setItem: (key, value) => session.set(key, String(value)),
    removeItem: (key) => session.delete(key),
  },
  ...(isWorkmap ? { ResizeObserver: StubResizeObserver } : {}),
  // ui_place measures stub boxes; the page reads the header gap, the pane's corner and the joint.
  getComputedStyle: () => ({ columnGap: "8px", borderTopLeftRadius: "22px", width: "13px" }),
  localStorage: {
    getItem: (key) => { if (scenario.endsWith("nostorage")) throw new Error("denied"); return storage.has(key) ? storage.get(key) : null; },
    setItem: (key, value) => { if (scenario.endsWith("nostorage")) throw new Error("denied"); storage.set(key, value); },
  },
  location: { hash: scenario === "used_code" ? "#code=used-code" : scenario === "autodev_link" ? "#autodev"
                : ["workmap", "workmap_refit", "workmap_present"].includes(scenario) ? "#workmap"
                : ["workmap_signout", "workmap_picker"].includes(scenario) ? "#workmap/ws-1/task%3A1" : "",
              pathname: "/ui" },
  history: { replaceState(...args) { replacedUrls.push(args); } },
  fetch: fetchStub, alert(message) { alerts.push(message); }, confirm: (question) => { confirms.push(question); return confirmAnswer; },
  prompt: (question, suggested) => { prompts.push([question, suggested]); return suggested; },
  setTimeout: pageSetTimeout, clearTimeout: pageClearTimeout,
  console, URLSearchParams,
  Option: function Option(text, value) { return Object.assign(element(), { text, value }); },
});
for (const [index, tab] of ["flows", "agents", "rules", "skills", "implants", "history", "memory",
                            ...(isWorkmap ? ["workmap"] : [])].entries()) {
  const button = element();
  button.dataset.tab = tab;
  if (tab === "flows") button.classes.add("active");  // as the markup has it
  byId("tabs").children[index] = button;
}
for (const theme of ["auto", "light", "dark"]) {  // the header's theme switch, as the markup has it
  const button = element("", "button");
  button.dataset.theme = theme;
  byId("theme").children.push(button);
}
vm.runInContext(script, context);
await sleep(100);  // the page's start-up sequence

const fire = (target, type, event = {}) => {
  for (const handler of target.handlers[type] || []) handler({ target, ...event });
};
const press = (key, target) => {
  const event = { key, target, prevented: false, preventDefault() { this.prevented = true; } };
  for (const handler of docHandlers.keydown || []) handler(event);
  return event;
};
const search = byId("search");
const type = async (value) => { search.value = value; fire(search, "input"); await sleep(0); };
// The list as the user sees it: headings, entries with their "in text" chips, and the count.
const shown = () => ({
  list: byId("items").children.map((node) =>
    node.className === "item" || node.className.startsWith("item ")
      ? node.textContent + (node.children.some((c) => c.textContent === "in text") ? " [in text]" : "")
      : node.textContent),
  count: byId("search-count").textContent, query: search.value,
});
const openTab = async (tab) => {
  const button = byId("tabs").children.find((b) => b.dataset.tab === tab);
  fire(button, "click");
  await sleep(50);
};
const buttons = () => byId("items").children.filter((node) => node.className.startsWith("item"));

if (scenario === "search") {
  const steps = {};
  steps.initial = shown();
  await type("alpha");
  steps.alpha = shown();
  await type("  ALPHA   plan ");
  steps.and_terms = shown();
  await type("");
  steps.cleared = shown();
  await type("needle");
  steps.needle_user = shown();
  fire(byId("items").children[0].children[1], "click");  // the System switch
  steps.needle_system = shown();
  fire(byId("items").children[0].children[0], "click");  // back to User
  await type("nothing-matches");
  steps.none = shown();
  await type("flow");  // a name hit of a later group comes before a text-only hit of an earlier one
  steps.global_order = shown();
  fire(byId("items").children[0].children[1], "click");  // System
  await type("zzz");
  steps.system_none = shown();
  fire(byId("items").children[0].children[0], "click");  // back to User
  await type("user");  // a scope prefix of an ID is not part of the name
  steps.prefix = shown();
  await openTab("rules");
  steps.rules_query_empty = shown().query;
  await type("logging");
  steps.rules_logging = shown();
  await type("logging security");
  steps.rules_and = shown();
  await type("logging");
  byId("items").scrollTop = 120;
  fire(buttons()[1], "click");
  steps.scroll_kept = byId("items").scrollTop;
  steps.opened = { title: byId("c-title").textContent, hidden: byId("component").classList.contains("hidden") };
  await type("zzz");
  steps.filtered_out = { ...shown(), title: byId("c-title").textContent,
                         hidden: byId("component").classList.contains("hidden") };
  byId("items").scrollTop = 120;
  await type("log");
  steps.scroll_on_query = byId("items").scrollTop;
  await type("zzz");
  const skills = byId("tabs").children.find((b) => b.dataset.tab === "skills");
  fire(skills, "click");
  await type("sec");  // typed while the skills list is still loading
  steps.while_loading = shown();
  await sleep(150);
  steps.after_loading = shown();
  fire(byId("tabs").children.find((b) => b.dataset.tab === "rules"), "click");
  await sleep(50);
  await openTab("flows");
  steps.flows_query_kept = shown().query;
  await openTab("rules");
  steps.rules_query_kept = shown().query;
  const outside = press("/", { tagName: "BODY" });
  const typing = press("/", { tagName: "TEXTAREA" });
  steps.slash = { prevented: outside.prevented, focused: search.focused, typing_prevented: typing.prevented };
  fire(search, "keydown", { key: "Escape" });
  steps.escape = shown();
  await finish(steps);
}

const esc = (text) => text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const ser = (node) => {
  if (node.nodeType === 3) return esc(node.textContent);
  const attrs = Object.entries(node.attrs).map(([name, value]) => ` ${name}="${value}"`).join("");
  const cls = node.className ? ` class="${node.className}"` : "";
  if (node.tag === "br" || node.tag === "hr") return `<${node.tag}>`;
  return `<${node.tag}${cls}${attrs}>${esc(node.textContent || "")}${node.children.map(ser).join("")}</${node.tag}>`;
};
const findAll = (node, test, found = []) => {
  if (node.nodeType === 3) return found;
  if (test(node)) found.push(node);
  for (const child of node.children) findAll(child, test, found);
  return found;
};
const hasClass = (name) => (node) => (node.className || "").split(" ").includes(name);
// A rendered view as the user sees it: its classes, contents entries and document.
const snap = (id) => {
  const root = byId(id), doc = findAll(root, hasClass("md"))[0];
  return {
    hidden: root.classes.has("hidden"), toc_hidden: root.classes.has("toc-hidden"), no_toc: root.classes.has("no-toc"),
    dismissed: root.classes.has("toc-dismissed"),
    toc: findAll(root, hasClass("md-toc-item")).map((n) => n.className + ":" + n.textContent),
    hide_label: findAll(root, hasClass("md-toc-hide"))[0].textContent, html: doc.children.map(ser).join(""),
  };
};
const open = async (index) => { fire(buttons()[index], "click"); await sleep(40); };
const seg = (id, index) => byId(id).children[index];
// Which pane shows, and which controls the header shows for it.
const panes = () => ({
  shown: ["welcome", "editor", "component"].filter((id) => !byId(id).classes.has("hidden")),
  group: !byId("item-actions").classes.has("hidden"), pane: byId("item-actions").dataset.pane ?? null,
  e_actions: !byId("e-actions").classes.has("hidden"), c_actions: !byId("c-actions").classes.has("hidden"),
});

if (scenario === "render") {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const host = element("", "div");
  const rendered = context.renderMarkdown(Buffer.concat(chunks).toString("utf8"), host);
  const json = JSON.stringify({
    html: host.children.map(ser).join(""), meta: rendered.meta,
    headings: rendered.headings.map((h) => ({ level: h.level, text: h.text, id: h.id })),
  });
  await new Promise((resolve) => process.stdout.write(json + "\n", resolve));  // a pipe may hold a large result back
  process.exit(0);
}

if (isUi) {
  const out = {};
  if (scenario === "ui_user_rule_error") {
    userRuleToggle = { id: "user:team", description: "Unavailable cached user rule", declared_by: [], body: "",
                       enabled: null, revision: null, status: "error", error: "rule_invalid: corrupt subscription" };
    await openTab("rules");
    await open(0);
    await vm.runInContext("toggleItem()", context);
    out.damaged = { disabled: byId("c-toggle").disabled, writes: [...puts],
                    description: byId("c-description").textContent, body: byId("c-body").value };
    userRuleToggle = { id: "user:team", description: "Team rule", declared_by: [], body: "Repaired guidance",
                       enabled: true, revision: "r2" };
    await vm.runInContext("loadComponents('rules', 'user:team')", context);
    out.recovered = { disabled: byId("c-toggle").disabled, body: byId("c-body").value };
    await finish(out);
  }
  if (scenario === "ui_user_rule_toggle") {
    await openTab("rules");
    await open(0);
    userRuleToggle = { ...userRuleToggle, body: "Accepted B", revision: "r2" };
    await vm.runInContext("toggleItem()", context);
    out.conflict = { puts: [...puts], alerts: [...alerts], enabled: userRuleToggle.enabled,
                     displayed: vm.runInContext("({body: state.item.body, revision: state.item.revision})", context) };
    await vm.runInContext("toggleItem()", context);
    out.retry = { puts: [...puts], enabled: userRuleToggle.enabled };
    await finish(out);
  }
  if (scenario === "ui_place") {
    // A personal flow's group, 456 px wide, beside a version (the sync chip sits under it) ending at
    // 222 px and a pane whose straight top edge starts at 304 + 22 + 13 px; the section tabs move as
    // the window narrows.
    const layout = { tabsLeft: 806 };
    const box = (rect) => () => ({ width: rect.right - rect.left, ...rect });
    byId("brand").getBoundingClientRect = box({ left: 12, right: 222 });
    byId("e-actions").getBoundingClientRect = () => ({ width: 456 });
    byId("editor").getBoundingClientRect = box({ left: 304, right: 1238 });
    byId("tabs").getBoundingClientRect = () => ({ left: layout.tabsLeft });
    byId("item-actions").getBoundingClientRect = () => ({});
    const place = () => [byId("item-actions").dataset.place, byId("main").dataset.place];
    const resize = async (tabsLeft, narrow = false) => {
      layout.tabsLeft = tabsLeft; narrowNow = narrow;
      for (const handler of windowHandlers.resize || []) handler({});
      await sleep(0);
      return place();
    };
    await open(0);
    out.opened = place();  // 806 - 8 - 456 = 342 px: on the straight edge
    out.inline = await resize(706);  // 242 px: past the corner, after the version
    out.row = await resize(665);  // 201 px: not even after the version
    out.stack = await resize(665, true);
    out.back = await resize(806);
    await finish(out);
  }
  if (scenario === "ui_panes") {
    const steps = { start: panes() };
    await open(0);
    steps.flow = panes();
    steps.flow_described_by = byId("item-actions").attrs["aria-describedby"];
    await openTab("rules");
    steps.rules = panes();
    await open(0);
    steps.rule = { ...panes(), title: byId("c-title").textContent };
    steps.rule_described_by = byId("item-actions").attrs["aria-describedby"];
    fire(byId("c-toggle"), "click");  // the switch, now in the header, still sends its request
    await sleep(40);
    steps.switched = requests.filter((path) => path === "/ui/api/component").length;
    await openTab("flows");
    steps.back_to_flows = panes();
    await open(0);
    fire(byId("delete"), "click");
    await sleep(40);
    steps.deleted = { ...panes(), deletes: deletes.length };
    // A delete that answers late closes the deleted flow, even one opened again meanwhile…
    holdDelete = true;
    await open(0);
    fire(byId("delete"), "click");
    await sleep(5);
    await open(0);
    releaseDelete();
    await sleep(40);
    steps.late_delete_same_flow = panes();
    // …and leaves anything else the user opened meanwhile open.
    await open(0);
    fire(byId("delete"), "click");
    await sleep(5);
    await open(1);
    releaseDelete();
    await sleep(40);
    steps.late_delete_other_flow = { ...panes(), title: byId("title").textContent };
    await open(1);
    fire(byId("delete"), "click");
    await sleep(5);
    await openTab("rules");
    await open(0);
    releaseDelete();
    await sleep(40);
    steps.late_delete_rule = { ...panes(), title: byId("c-title").textContent, deletes: deletes.map((d) => d.id) };
    await finish(steps);
  }
  if (scenario === "ui_history") {
    // The text shows before the versions answer; the list then fills in and opens an old version.
    const history = () => ({ head: byId("history").children[0]?.textContent ?? null, disabled: byId("history").disabled,
                             options: byId("history").children.slice(1).map((o) => [o.value.slice(0, 9), o.textContent]),
                             busy: byId("history").getAttribute("aria-busy"), head_title: byId("history").children[0]?.title ?? null });
    holdHistory = true;
    await open(0);
    const flowGet = requests.findIndex((path) => path.startsWith("/ui/api/flow?"));
    const historyGet = requests.findIndex((path) => path.startsWith("/ui/api/flow/history?"));
    out.while_loading = { title: byId("title").textContent, content: byId("content").value, panes: panes(), ...history(),
                          flow_get_first: flowGet >= 0 && historyGet > flowGet, history_request: requests[historyGet] };
    historyHolds.shift()();
    await sleep(20);
    out.loaded = history();
    const select = byId("history");
    select.value = VERSIONS["user:doc"][1].version;
    fire(select, "change");
    await sleep(40);
    out.version = { content: byId("content").value, notice: byId("notice").textContent, select_value: select.value,
                    request: requests.filter((path) => path.startsWith("/ui/api/flow?")).at(-1) };
    // A late list of a flow the user left does not replace the list of the flow now open.
    await open(0);
    await open(1);
    historyHolds.pop()();  // user:plain's list answers first…
    await sleep(20);
    out.other_flow = { title: byId("title").textContent, ...history() };
    historyHolds.shift()();  // …then user:doc's
    await sleep(20);
    out.late_answer = { title: byId("title").textContent, ...history() };
    // A list that cannot be read says so; the text stays usable.
    holdHistory = false;
    failHistory = true;
    await open(0);
    out.failed = { title: byId("title").textContent, ...history(), content: byId("content").value,
                   read_only: !!byId("content").readOnly };
    // A save reopens the flow: two lists of the same flow are in flight, and the newer one stays
    // even when the older answers last.
    failHistory = false;
    holdHistory = true;
    await open(0);  // asks for the list before the save
    byId("content").value = "# Doc saved\n";
    fire(byId("content"), "input");
    fire(byId("save"), "click");
    await sleep(80);  // the save reopened the flow, which asks again: the saved version is listed too
    out.saved_requests = { pending: historyHolds.length, put: puts.at(-1)?.id ?? null };
    historyHolds.pop()();  // the newer list answers first…
    await sleep(20);
    out.saved = { title: byId("title").textContent, ...history() };
    historyHolds.shift()();  // …then the one asked before the save
    await sleep(20);
    out.saved_late = { title: byId("title").textContent, ...history() };
    // A built-in flow keeps no versions: nothing is asked, and the list of the flow left meanwhile stays out.
    await open(0);
    const asked = requests.length;
    await context.openFlow("builtin:review");
    await sleep(20);
    out.builtin = { title: byId("title").textContent, ...history(),
                    asked: requests.slice(asked).filter((path) => path.startsWith("/ui/api/flow/history?")) };
    historyHolds.shift()();  // user:doc's list answers after the built-in flow opened
    await sleep(20);
    out.builtin_late = { title: byId("title").textContent, ...history() };
    await finish(out);
  }
  if (scenario === "ui_signout") {
    holdFlowGet = true;
    const opening = context.openFlow("user:doc");  // its GET was sent while still signed in
    await sleep(5);
    signedIn = false;  // the session ends while that GET is pending
    await context.api("/ui/api/workspaces").catch(() => {});  // automatic sign-in is refused
    const signedOut = { signin: !byId("signin").classes.has("hidden"), main_hidden: byId("main").classes.has("hidden") };
    holdFlowGet = false;
    releaseFlowGet();
    await opening.catch(() => {});
    // Before a later request can fail and hide the controls again, as the persona catalog's does here.
    const late = { ...panes(), title: byId("title").textContent };
    await sleep(40);
    await finish({ signed_out: signedOut, late, after: panes() });
  }
  out.stored_at_start = stored();
  out.initial_toc_hidden = byId("e-view").classes.has("toc-hidden");
  if (scenario === "ui_nostorage" || scenario.startsWith("ui_narrow")) {
    await open(0);
    out.opened = snap("e-view");
    fire(byId("e-toc"), "click");
    out.after_header_button = snap("e-view");
    out.stored_after = stored();
    await finish(out);
  }
  await open(0);
  out.opened = { ...snap("e-view"), panes_hidden: byId("panes").classes.has("hidden"),
                 toc_button_hidden: byId("e-toc").classes.has("hidden"), seg: byId("e-seg").children.map((b) => b.classes.has("active")) };
  fire(findAll(byId("e-view"), hasClass("md-toc-item"))[1], "click");
  out.heading_scrolled = findAll(byId("e-view"), (n) => n.tag === "h2").map((n) => n.scrolled);
  fire(findAll(byId("e-view"), hasClass("md-toc-hide"))[0], "click");
  out.hidden = { ...snap("e-view"), stored: stored(), focus_moved: byId("e-toc").focused };
  // The panel's own button leaves the pointer on the panel: the hover reveal waits until it is over something else.
  const part = (root, name) => findAll(root, hasClass(name))[0];
  const dismissed = (root) => root.classes.has("toc-dismissed");
  const pointerOver = (root, name) => { fire(root, "pointerover", { target: part(root, name) }); return dismissed(root); };
  const eView = byId("e-view"), eHide = part(eView, "md-toc-hide");
  out.dismissal = { over_panel: pointerOver(eView, "md-toc-hide") };
  out.dismissal.over_document = pointerOver(eView, "md");
  fire(eHide, "click");  // Keep open
  fire(eHide, "click");  // Hide contents
  out.dismissal.hidden_again = dismissed(eView);
  fire(eHide, "click");  // Keep open from the keyboard: focus reveals the dismissed panel, the pointer cannot
  out.dismissal.kept_open = { toc_hidden: eView.classes.has("toc-hidden"), dismissed: dismissed(eView) };
  fire(byId("e-toc"), "click");
  out.dismissal.header_hidden = { toc_hidden: eView.classes.has("toc-hidden"), dismissed: dismissed(eView) };
  await open(1);
  out.plain = { ...snap("e-view"), toc_button_hidden: byId("e-toc").classes.has("hidden") };
  await open(0);
  out.remembered = snap("e-view");
  fire(byId("e-toc"), "click");
  out.revealed = { ...snap("e-view"), stored: stored() };
  // Source: edit, see the edit rendered, then save from Source.
  fire(seg("e-seg", 1), "click");
  out.source = { panes_hidden: byId("panes").classes.has("hidden"), view_hidden: byId("e-view").classes.has("hidden"), seg: byId("e-seg").children.map((b) => b.classes.has("active")) };
  byId("content").value = "# Changed\n\n## Fresh\n\n<b>x</b>\n";
  fire(byId("content"), "input");
  out.dirty_save_disabled = byId("save").disabled;
  fire(seg("e-seg", 0), "click");
  out.edited = { ...snap("e-view"), save_disabled: byId("save").disabled };
  fire(seg("e-seg", 1), "click");
  fire(byId("save"), "click");
  await sleep(80);
  out.put = puts[0];
  out.after_save = { panes_hidden: byId("panes").classes.has("hidden"), save_disabled: byId("save").disabled, content: byId("content").value };
  // A conflict keeps the text and shows both sides in Source.
  putStatus = 409;
  fire(seg("e-seg", 0), "click");
  byId("content").value = "# Mine\n";
  fire(byId("content"), "input");
  fire(byId("save"), "click");
  await sleep(80);
  out.conflict = { panes_hidden: byId("panes").classes.has("hidden"), side_hidden: byId("side").classes.has("hidden"),
                   notice: byId("notice").textContent, content: byId("content").value, save_disabled: byId("save").disabled };
  // A component body is rendered, with Source on demand.
  await openTab("skills");
  await open(0);
  out.skill = { ...snap("c-view"), source_hidden: byId("c-source").classes.has("hidden"), body: byId("c-body").value.length > 0,
                toc_button_hidden: byId("c-toc").classes.has("hidden") };
  const cView = byId("c-view");
  fire(part(cView, "md-toc-hide"), "click");
  out.skill_dismissal = { toc_hidden: cView.classes.has("toc-hidden"), dismissed: dismissed(cView) };
  out.skill_dismissal.over_panel = pointerOver(cView, "md-toc-hide");
  out.skill_dismissal.over_edge = pointerOver(cView, "md-edge");
  fire(seg("c-seg", 1), "click");
  out.skill_source = { view_hidden: byId("c-view").classes.has("hidden"), source_hidden: byId("c-source").classes.has("hidden") };
  await finish(out);
}

if (scenario.startsWith("landing_updates")) {
  const textOf = (node) => (node.nodeType === 3 ? node.textContent
    : (node.textContent || "") + node.children.map(textOf).join(""));
  const body = () => byId("updates-body");
  const radios = () => findAll(body(), (n) => n.tag === "input" && n.type === "radio");
  const saveButton = () => findAll(body(), (n) => n.tag === "button")[0];
  const view = () => ({ text: textOf(body()), radios: radios().map((r) => [r.value, r.checked, r.disabled]),
                        save: saveButton() ? [saveButton().textContent, saveButton().disabled] : null,
                        alert: byId("landing-alert").textContent });
  const out = { start: view() };
  const first = radios()[0];
  await fireTimers(30000);  // the statistics' poll re-renders the landing page
  out.kept_after_poll = radios()[0] === first;
  const target = downgrading ? "main" : "dev";
  const pick = radios().find((r) => r.value === target);
  pick.checked = true;
  fire(pick, "change");
  await sleep(0);
  out.picked = view();
  fire(saveButton(), "click");
  await sleep(50);
  out.saved = view();
  out.puts = updatesPuts;
  out.confirms = confirms;
  out.reads = requests.filter((path) => path === "/ui/api/updates");
  await finish(out);
}

if (scenario.startsWith("landing")) {
  const textOf = (node) => (node.nodeType === 3 ? node.textContent
    : (node.textContent || "") + node.children.map(textOf).join(""));
  const view = () => ({
    landing: !byId("landing").classes.has("hidden"), main: !byId("main").classes.has("hidden"),
    active_tabs: byId("tabs").children.filter((button) => button.classes.has("active")).map((button) => button.dataset.tab),
    item_actions: !byId("item-actions").classes.has("hidden"), new_flow: !byId("new").classes.has("hidden"),
  });
  const reads = () => requests.filter((path) => ["/ui/api/overview", "/ui/api/stats"].includes(path));
  const steps = () => findAll(byId("how-body"), hasClass("step"));
  const lit = () => steps().map((step, index) => (step.classes.has("lit") ? index : -1)).filter((index) => index >= 0);
  const eventLine = () => findAll(byId("how-body"), hasClass("event-line")).map(textOf)[0] ?? null;
  const out = { start: { ...view(), reads: reads() } };
  if (out.start.landing) {
    out.cards = Object.fromEntries(["about", "stats", "how", "apps", "system"].map((name) => [name, textOf(byId(name + "-body"))]));
    out.links = findAll(byId("about-body"), (n) => n.tag === "a").map((a) => [a.textContent, a.attrs.href, a.attrs.rel]);
    out.chart = (findAll(byId("stats-body"), hasClass("chart"))[0] || { children: [] }).children.map((bar) => bar.dataset.level);
    out.app_states = findAll(byId("apps-body"), hasClass("app-state")).map((status) => status.dataset.state);
    out.steps = steps().length;
    out.lit = lit();
    out.event_line = eventLine();
    out.events_list = findAll(byId("how-body"), hasClass("events")).flatMap((list) => list.children.map(textOf));
    out.event_links = findAll(byId("how-body"), (n) => n.tag === "a").map((a) => a.attrs.href);
    out.step_timers = pendingTimers(1200).length;
    await fireTimers(1200);
    out.lit_after_a_step = lit();
    out.poll_timers = pendingTimers(30000).length;
    await fireTimers(30000);
    out.reads_after_a_poll = reads();
  }
  // Every tab leaves the landing page, and the version opens it again from there.
  out.tabs = [];
  for (const tab of ["flows", "agents", "rules", "skills", "implants", "history", "memory"]) {
    await openTab(tab);
    const left = view();
    fire(byId("version-link"), "click", { preventDefault() {} });
    await sleep(30);
    const back = view();
    out.tabs.push({ tab, left: !left.landing && left.main && left.active_tabs.join() === tab,
                    back: back.landing && !back.main && !back.active_tabs.length && !back.item_actions && !back.new_flow });
  }
  out.reads_at_end = reads();
  const box = byId("landing-off");
  box.checked = true;
  fire(box, "change", { target: box });
  out.stored = stored();
  await finish(out);
}

if (isSync) {
  // The Sync page as the user sees it, found by labels: its steps, headings, alert and buttons.
  const textOf = (node) => (node.nodeType === 3 ? node.textContent
    : (node.textContent || "") + node.children.map(textOf).join(""));
  const root = () => byId("sync-body");
  const buttonsNamed = (label, where = root()) => findAll(where, (n) => n.tag === "button" && n.textContent === label);
  const click = async (label, index = 0, where = root()) => {
    const button = buttonsNamed(label, where)[index];
    if (!button) throw new Error(`no button ${label}: ${JSON.stringify(snapshot())}`);
    if (button.disabled) return "disabled";
    fire(button, "click");
    await sleep(25);
    return "clicked";
  };
  const field = (id) => findAll(root(), (n) => n.attrs.id === id)[0];
  const typeInto = async (id, value) => { const input = field(id); input.value = value; fire(input, "input"); await sleep(0); };
  const choose = async (value) => {
    const input = findAll(root(), (n) => n.tag === "input" && n.type === "radio" && n.value === value)[0];
    input.checked = true;
    fire(input, "change");
    await sleep(0);
  };
  const boxNamed = (name) => findAll(root(), (n) => n.tag === "label"
    && n.children.some((c) => c.nodeType === 3 && c.textContent === name))[0]?.children[0];
  const toggle = async (name, on) => { const box = boxNamed(name); box.checked = on; fire(box, "change"); await sleep(25); };
  const alertText = () => ({ text: byId("sync-alert").textContent, className: byId("sync-alert").className });
  const snapshot = () => {
    const steps = findAll(root(), hasClass("sync-steps"))[0];
    return {
      steps: steps ? steps.children.map((li) => (li.attrs["aria-current"] === "step" ? "*" : "") + li.textContent
        + (li.className === "skipped" ? " (skipped)" : "")) : null,
      headings: findAll(root(), (n) => n.tag === "h3").map((n) => n.textContent),
      buttons: findAll(root(), (n) => n.tag === "button").map((b) => b.textContent + (b.disabled ? " (disabled)" : "")),
      alert: alertText().text,
    };
  };
  const isStatus = ([method, path]) => method === "GET" && path.split("?")[0] === "/ui/api/sync";
  const calls = (from = 0) => syncCalls.slice(from).filter((call) => !isStatus(call));
  const statusReads = () => syncCalls.filter(isStatus).length;
  const freshReads = () => syncCalls.filter((call) => isStatus(call) && call[1].endsWith("?fresh=1")).length;
  const chip = () => ({ text: byId("sync-chip").textContent, hidden: byId("sync-chip").classes.has("hidden"),
                        tone: byId("sync-chip").dataset.tone, pressed: byId("sync-chip").attrs["aria-pressed"],
                        label: byId("sync-chip").attrs["aria-label"] });
  const openPage = async () => { fire(byId("sync-chip"), "click"); await sleep(30); };
  const out = {};

  if (scenario === "sync_chip") {
    const now = Date.parse("2026-10-05T14:00:00Z");
    const at = (status) => context.syncChip(status, now);
    const base = { state: "synced", last_success: "2026-10-05T13:58:00Z", conflicts: 0, pending: 0, loop: {} };
    out.states = {
      none: at(null), unknown: at({}), off: at({ state: "off" }), synced: at(base),
      just_now: at({ ...base, last_success: "2026-10-05T13:59:30Z" }), hours: at({ ...base, last_success: "2026-10-05T11:00:00Z" }),
      stale: at({ ...base, last_success: "2026-10-03T13:00:00Z", stale: true }),
      pending: at({ ...base, state: "pending", pending: 3 }), syncing: at({ ...base, state: "syncing" }),
      loop_syncing: at({ ...base, loop: { syncing: true } }),
      offline: at({ ...base, state: "offline", retry_at: "2026-10-05T14:05:00+00:00" }),
      paused: at({ ...base, state: "paused", conflicts: 2 }), attention: at({ ...base, state: "attention", reason: "auth" }),
      waiting: at({ state: "waiting_for_access" }), conflicts: at({ ...base, conflicts: 2 }),
      one_conflict: at({ ...base, state: "pending", pending: 1, conflicts: 1 }),
    };
    out.started_off = chip();  // the startup status: sync is off
    await openPage();
    out.opened = { chip: chip(), page_shown: !byId("sync-page").classes.has("hidden"),
                   main_hidden: byId("main").classes.has("hidden"), title_focused: byId("sync-title").focused,
                   tabs: byId("tabs").children.map((b) => b.attrs["aria-pressed"]) };
    await openPage();  // the chip again: back to the list
    out.closed = { chip: chip(), page_hidden: byId("sync-page").classes.has("hidden"),
                   main_hidden: byId("main").classes.has("hidden"), tabs: byId("tabs").children.map((b) => b.attrs["aria-pressed"]) };
    await openPage();
    fire(byId("tabs").children[2], "click");  // a tab leaves the Sync page too
    await sleep(40);
    out.tab = { page_hidden: byId("sync-page").classes.has("hidden"), main_hidden: byId("main").classes.has("hidden"),
                tabs: byId("tabs").children.map((b) => b.attrs["aria-pressed"]) };
    await finish(out);
  }

  if (scenario === "sync_poll") {
    out.start = { reads: statusReads(), timers: pendingTimers(30000).length };
    await fireTimers(30000);
    out.after_30s = { reads: statusReads(), timers: pendingTimers(30000).length };
    documentStub.visibilityState = "hidden";
    for (const handler of docHandlers.visibilitychange) handler({});
    out.hidden = { reads: statusReads(), timers: pendingTimers(30000).length };
    await fireTimers(30000);
    out.hidden_after_30s = { reads: statusReads() };
    documentStub.visibilityState = "visible";
    for (const handler of docHandlers.visibilitychange) handler({});
    await sleep(30);
    out.visible = { reads: statusReads(), timers: pendingTimers(30000).length };
    await fireTimers(30000);
    out.visible_after_30s = { reads: statusReads(), timers: pendingTimers(30000).length };
    await finish(out);
  }

  if (scenario === "sync_wizard") {
    await openPage();
    out.connect = snapshot();
    reply("POST /ui/api/sync/github/device", [200, { attempt: "a1", user_code: "WDJB-MJHT", interval: 5, expires_in: 900,
                                                    verification_uri: "https://github.com/login/device" }]);
    await click("Sign in to GitHub");
    const link = findAll(root(), (n) => n.tag === "a")[0];
    out.code = { code: findAll(root(), hasClass("sync-code"))[0].textContent, href: link.attrs.href, rel: link.attrs.rel,
                 target: link.attrs.target, polls_waiting: pendingTimers(5000).length };
    reply("GET /ui/api/sync/github/device", [200, { state: "pending", interval: 5 }],
          [200, { state: "connected", github: { connected: true, login: "octocat", host: "github.com" } }]);
    reply("GET /ui/api/sync/github/libraries", [200, { repositories: [{ full_name: "octocat/agents-library" }], checked: 3,
                                                        truncated: false }]);
    await fireTimers(5000);  // pending: another poll is scheduled
    out.still_waiting = { polls_waiting: pendingTimers(5000).length, code_shown: findAll(root(), hasClass("sync-code")).length };
    syncState = { ...SYNC_OFF, github: { connected: true, login: "octocat", host: "github.com" } };
    await fireTimers(5000);  // connected: on to the repository, whose list loads
    await sleep(30);
    out.repository = { ...snapshot(), checked: findAll(root(), (n) => n.type === "radio" && n.checked).map((n) => n.value) };
    await choose("create");
    await typeInto("sync-new-name", "my-library");
    reply("POST /ui/api/sync/github/create", [200, { status: "created", repository: "octocat/my-library" }]);
    await click("Continue");
    out.identity = { ...snapshot(), label: field("sync-label").value };
    await click("Set up sync");  // no name yet
    out.identity_refused = { alert: alertText(), calls: calls().length };
    await typeInto("sync-name", "Owner");
    await typeInto("sync-email", "owner@example.com");
    reply("POST /ui/api/sync/setup", [200, { status: "waiting_for_access", label: "mac-1a2b", deploy_key: "added",
                                              public_key: PUBLIC_KEY, steps: ["octocat/my-library is private"] }]);
    reply("POST /ui/api/sync/check",
          [409, { status: "waiting_for_access", reason: "auth", deploy_key: "missing", repository: "octocat/my-library",
                  message: "the remote refused this machine's key. GitHub has no deploy key of this machine" }],
          [200, { status: "ok", remote_state: "empty", remote_head: null }]);
    await click("Set up sync");
    await sleep(30);
    out.access = snapshot();
    reply("POST /ui/api/sync/github/add-key", [200, { status: "added", message: "added this machine's deploy key to octocat/my-library" }]);
    await click("Add this machine's key on GitHub");
    out.key_added = alertText();
    reply("GET /ui/api/sync/scopes", [200, { groups: [
      { group: "common", syncs: true }, { group: "personas", syncs: true }, { group: "history", syncs: true },
      { group: "components", syncs: true }, { group: "repos/abc", origin: "github.com/o/r", syncs: true, reason: null },
      { group: "repos/home-1f2e", origin: null, syncs: false, reason: "local" }], excluded_files: [], allowed: [],
      ask_new_repositories: false }]);
    await click("Check access");
    await sleep(30);
    const boxes = () => findAll(root(), (n) => n.tag === "input" && n.type === "checkbox")
      .map((n) => [textOf(findAll(root(), (l) => l.tag === "label" && l.children[0] === n)[0]), n.checked, n.disabled]);
    out.scope = { ...snapshot(), boxes: boxes() };
    reply("PUT /ui/api/sync/scopes", [200, { status: "saved", groups: [{ group: "common", syncs: true },
      { group: "personas", syncs: true }, { group: "history", syncs: false }, { group: "components", syncs: true }] }],
          [200, { status: "confirmation_needed", hash: "inc1", upload: [], history_versions: 2 }],
          [200, { status: "saved", groups: [{ group: "common", syncs: true }, { group: "history", syncs: true }] }]);
    await toggle("Saved versions (history)", false);
    out.excluded = { alert: alertText(), boxes: boxes() };
    await toggle("Saved versions (history)", true);
    out.include_asks = { ...snapshot(), text: textOf(root()) };
    await click("Upload them");
    out.included = { alert: alertText(), boxes: boxes() };
    reply("PUT /ui/api/sync/settings", [200, { state: "waiting_for_access" }]);
    await toggle("Ask before uploading the flows of a repository that is new to the library", true);
    reply("GET /ui/api/sync/preview", [200, {
      kind: "push", hash: "h123", privacy: "private", private_confirmed: false, upload_bytes: 120,
      upload: [{ path: "common/a.md", flow: "user:a", size: 120 }], download: [], delete_local: [],
      remove_from_remote: [], conflicts: [], held: [], excluded: [], pending_groups: [], pending_files: [],
      blocked: [{ path: "common/secret.md", pattern: "github_token", sha256: "x" }],
      history_versions: 3,
      history_blocked: [{ path: "common/old-secret.md", pattern: "private_key", version: "git-deadbeef" }],
      repository_groups: [{ group: "repos/abc", origin: "github.com/o/r", files: 2, new: true }] }]);
    await click("Continue to the preview");
    await sleep(30);
    out.preview = { ...snapshot(), text: textOf(root()) };
    reply("POST /ui/api/sync/start", [200, { status: "synced", sent: ["common/a.md"], received: [], conflicts: [], pushed: true }]);
    syncState = started();
    reply("GET /ui/api/sync/machines", [200, { available: false, label: "mac-1a2b", machines: [], message: "later" }]);
    await click("Start sync");
    await sleep(40);
    out.started = { headings: snapshot().headings, alert: alertText(), chip: chip() };
    out.calls = calls();
    await finish(out);
  }

  if (scenario === "sync_wizard_ssh") {
    await openPage();
    await typeInto("sync-remote", "git@git.example.com:me/library.git");
    await click("Use this SSH URL");
    out.identity = snapshot();
    await typeInto("sync-name", "Owner");
    await typeInto("sync-email", "owner@example.com");
    await typeInto("sync-label", "Laptop!");
    await click("Set up sync");
    out.bad_label = alertText();
    await typeInto("sync-label", "laptop");
    reply("POST /ui/api/sync/setup",
          [200, { status: "host_key_unconfirmed", host: "git.example.com", label: "laptop",
                  fingerprints: ["SHA256:aaaa", "SHA256:bbbb"], message: "compare a fingerprint" }],
          [200, { status: "waiting_for_access", label: "laptop", public_key: PUBLIC_KEY, remote: "git.example.com/me/library" }]);
    await click("Set up sync");
    out.host_keys = { ...snapshot(), radios: findAll(root(), (n) => n.type === "radio").map((n) => n.value) };
    await click("Trust this key");
    out.no_choice = alertText();
    await choose("SHA256:bbbb");
    await click("Trust this key");
    out.key = { ...snapshot(), key: findAll(root(), hasClass("sync-key"))[0].textContent };
    reply("POST /ui/api/sync/check", [200, { status: "ok", remote_state: "library", remote_head: "abc" }]);
    reply("GET /ui/api/sync/scopes", [200, { groups: [{ group: "common", syncs: true }], ask_new_repositories: false }]);
    await click("Check access");
    await sleep(20);
    reply("GET /ui/api/sync/preview", [200, { kind: "join", hash: "h9", privacy: "unknown", private_confirmed: false,
      upload: [], download: [{ path: "common/b.md", flow: "user:b", size: 10 }], delete_local: [], remove_from_remote: [],
      conflicts: [{ path: "common/a.md", flow: "user:a", kind: "both_changed" }], blocked: [], pending_groups: [],
      pending_files: [], repository_groups: [] }]);
    await click("Continue to the preview");
    await sleep(30);
    out.unsure = { ...snapshot(), text: textOf(root()) };
    await toggle("Only I can read this repository. Agents-Core could not check its privacy on this host.", true);
    out.confirmed = snapshot().buttons;
    reply("POST /ui/api/sync/start", [200, { status: "attention", reason: "confirmation_needed",
                                              message: "the preview changed; review it again and confirm" }]);
    await click("Start sync");
    await sleep(30);
    out.changed = { alert: alertText(), headings: snapshot().headings };
    out.calls = calls();
    await finish(out);
  }

  if (scenario === "sync_connection_choice") {
    const inventory = Array.from({ length: 121 }, (_, index) => ({ path: "common/item-" + index + ".md",
      state: index === 0 ? "different" : "local_only", local: { blob: "local", size: 10 }, remote: null,
      retained_reason: index === 120 ? "excluded" : null,
      merge: { action: index === 120 ? "retain" : "merge", conflict: index === 0 ?
        { kind: "both_changed", reason: "overlapping_changes", overlaps: 2 } : null },
      replace: { action: index === 120 ? "retain" : "delete" } }));
    const preview = (mode, hash) => ({ kind: mode === "replace" ? "replace" : "join", hash,
      privacy: "private", connection_choice_required: !mode, connection_mode: mode, allowed_modes: ["merge", "replace"],
      comparison: { summary: { local_only: 120, different: 1, retained: 1 }, files: inventory,
        history: { local_versions: 3, remote_versions: 4, merge_upload_versions: 3, replace_private_versions: 3 } },
      upload: [], download: [], delete_local: [], conflicts: [], blocked: [], pending_groups: [], pending_files: [] });
    const side = (text) => ({ text, binary: false, too_large: false, size: text.length });
    const detail = (path) => ({ path, hash: "merge1", connection_mode: "merge", entry: inventory[0],
      local: side("# Local <script>literal</script>\n"), remote: side("# Remote\n"),
      result: side("# Exact merged result\n"), base: null });
    reply("POST /ui/api/sync/check", [200, { status: "ok", remote_state: "library", remote_head: "remote1" }]);
    reply("GET /ui/api/sync/scopes", [200, { groups: [{ group: "common", syncs: true }], ask_new_repositories: false }]);
    await openPage();
    await click("Check access");
    reply("GET /ui/api/sync/preview", [200, preview(null, "provisional")]);
    await click("Continue to the preview");
    await sleep(30);
    out.unselected = { ...snapshot(), text: textOf(root()), checked: findAll(root(), (n) => n.type === "radio" && n.checked).length };
    await click("Start sync");
    out.no_start = calls().filter(([method, path]) => method === "POST" && path.endsWith("/start")).length;
    reply("GET /ui/api/sync/preview", [200, preview("merge", "merge1")]);
    await choose("merge");
    await sleep(30);
    out.merge = { ...snapshot(), text: textOf(root()) };
    reply("GET /ui/api/sync/comparison", [200, detail("common/item-0.md")]);
    await click("Inspect common/item-0.md");
    out.detail = textOf(root());
    const stale = hold("GET /ui/api/sync/comparison");
    await click("Inspect common/item-1.md");
    reply("GET /ui/api/sync/preview", [200, preview("replace", "replace1")]);
    await choose("replace");
    await sleep(30);
    stale.release();
    await sleep(10);
    out.replace = { ...snapshot(), text: textOf(root()) };
    await click("Cancel connection");
    out.cancelled = { ...snapshot(), starts: calls().filter((call) => call[1].endsWith("/start")).length };
    await click("Check access");
    reply("GET /ui/api/sync/preview", [200, preview(null, "provisional2")]);
    await click("Continue to the preview");
    reply("GET /ui/api/sync/preview", [200, preview("replace", "replace2")]);
    await choose("replace");
    await sleep(30);
    reply("POST /ui/api/sync/start", [200, { status: "attention", reason: "confirmation_needed", message: "the preview changed" }]);
    reply("GET /ui/api/sync/preview", [200, preview("replace", "replace3")]);
    await click("Replace local and connect");
    await sleep(30);
    out.changed = { ...snapshot(), text: textOf(root()) };
    reply("POST /ui/api/sync/start", [200, { status: "synced", sent: [], received: ["common/remote.md"] }]);
    syncState = started();
    await click("Replace local and connect");
    out.calls = calls();
    await finish(out);
  }

  if (["sync_connection_recovery", "sync_comparison_recovery", "sync_preview_error"].includes(scenario)) {
    const message = "finish the accepted replacement with python -m src.user_sync run before another preview";
    const pending = { status: "attention", reason: "adoption_pending", message };
    reply("POST /ui/api/sync/check", [200, { status: "ok", remote_state: "library", remote_head: "remote1" }]);
    reply("GET /ui/api/sync/scopes", [200, { groups: [{ group: "common", syncs: true }], ask_new_repositories: false }]);
    await openPage();
    await click("Check access");
    if (scenario === "sync_comparison_recovery") {
      const preview = (mode) => ({ kind: "join", hash: mode ? "replace1" : "provisional", privacy: "unknown",
        connection_choice_required: !mode, connection_mode: mode, allowed_modes: ["merge", "replace"],
        comparison: { files: [{ path: "common/a.md", state: "different", merge: { action: "merge" },
                               replace: { action: "replace" } }] } });
      reply("GET /ui/api/sync/preview", [200, preview(null)]);
      await click("Continue to the preview");
      reply("GET /ui/api/sync/preview", [200, preview("replace")]);
      await choose("replace");
      await sleep(30);
      reply("GET /ui/api/sync/comparison", [409, pending]);
      await click("Inspect common/a.md");
    } else {
      reply("GET /ui/api/sync/preview", [409, scenario === "sync_preview_error" ? { ...pending, reason: "auth" } : pending]);
      await click("Continue to the preview");
    }
    await sleep(30);
    out.pending = { ...snapshot(), text: textOf(root()),
      inputs: findAll(root(), (n) => n.tag === "input").length };
    if (scenario === "sync_preview_error") {
      out.calls = calls();
      await finish(out);
    }
    reply("POST /ui/api/sync/run", [500, { status: "attention", reason: "write_failed", message: "could not finish the replacement" }],
          [200, { status: "synced", sent: [], received: ["common/a.md"], conflicts: [] }]);
    await click("Finish replacement");
    out.failed = snapshot();
    const mark = syncCalls.length, fresh = freshReads(), wait = hold("POST /ui/api/sync/run");
    await click("Finish replacement");
    await click("Finish replacement");
    out.waiting = { runs: calls(mark).filter((call) => call[1].endsWith("/run")).length,
      ariaDisabled: buttonsNamed("Finish replacement", root())[0].attrs["aria-disabled"] };
    syncState = started();
    const statusWait = hold("GET /ui/api/sync");
    wait.release();
    await sleep(10);
    await click("Finish replacement");
    out.refreshing = { runs: calls(mark).filter((call) => call[1].endsWith("/run")).length,
      ariaDisabled: buttonsNamed("Finish replacement", root())[0].attrs["aria-disabled"] };
    statusWait.release();
    await sleep(30);
    out.finished = { ...snapshot(), fresh_reads: freshReads() - fresh, chip: chip() };
    out.recovery_calls = syncCalls.slice(mark);
    out.calls = calls();
    await finish(out);
  }

  if (scenario === "sync_dashboard") {
    out.chip = chip();
    reply("GET /ui/api/sync/machines", [200, { available: true, label: "mac-1a2b", repository: "octocat/agents-library",
      machines: [{ id: 1, title: "Agents-Core mac-1a2b", label: "mac-1a2b", read_only: false, this: true, managed: true },
                 { id: 2, title: "Agents-Core linux-9f9f", label: "linux-9f9f", read_only: false, this: false,
                   created_at: "2026-10-01T08:00:00Z", managed: true },
                 { id: 3, title: "CI deploy", label: "CI deploy", read_only: true, this: false, managed: false }] }]);
    reply("GET /ui/api/sync/scopes", [200, { groups: [{ group: "common", syncs: true }, { group: "repos/abc",
      origin: "github.com/o/r", syncs: true }], ask_new_repositories: false }]);
    await openPage();
    await sleep(20);
    const part = (key) => byId("sync-body").children.find((card) => card.attrs["aria-labelledby"] === "sync-part-" + key);
    out.dashboard = { headings: snapshot().headings, status: textOf(part("status")), machines: textOf(part("machines")),
                      machine_buttons: buttonsNamed("Remove", part("machines")).length, activity: textOf(part("activity")),
                      conflicts: textOf(part("conflicts")), scopes: textOf(part("scopes")), access: textOf(part("access")) };
    const mark = syncCalls.length;
    reply("POST /ui/api/sync/machines/remove", [200, { status: "removed", id: 2 }]);
    await click("Remove", 0, part("machines"));
    reply("POST /ui/api/sync/conflicts/resolve", [200, { status: "resolved" }]);
    await click("Keep current", 0, part("conflicts"));
    await click("Use mine", 1, part("conflicts"));
    await click("Dismiss", 0, part("conflicts"));
    reply("GET /ui/api/sync/conflict", [200, { ...PERSONA_CONFLICT, current: "{\"persona\": \"a\"}", mine: "{\"persona\": \"b\"}", binary: false }]);
    await click("Open", 1, part("conflicts"));
    out.compare = findAll(part("conflicts"), (n) => n.tag === "textarea").map((n) => [n.attrs["aria-label"], n.value, n.readOnly]);
    reply("PUT /ui/api/sync/scopes", [200, { status: "saved", groups: [{ group: "common", syncs: true }] }],
          [200, { status: "confirmation_needed", hash: "ap1", upload: ["repos/def/x.md"] }],
          [200, { status: "saved", groups: [{ group: "common", syncs: true }] }]);
    await click("Exclude", 0, part("scopes"));
    await click("Approve", 0, part("scopes"));
    out.approve_asks = textOf(part("scopes"));
    await click("Upload them", 0, part("scopes"));
    reply("PUT /ui/api/sync/settings", [200, { state: "paused" }]);
    await click("Pause", 0, part("status"));
    reply("POST /ui/api/sync/run", [200, { status: "synced", sent: ["common/a.md"], received: [], conflicts: [], pushed: true }]);
    await click("Sync now", 0, part("status"));
    out.sync_now = alertText();
    await typeInto("sync-id-name", "Owner Two");
    await click("Save", 0, part("identity"));
    reply("POST /ui/api/sync/check", [200, { status: "ok", remote_state: "library" }]);
    await click("Check access", 0, part("access"));
    reply("POST /ui/api/sync/key/regenerate", [200, { status: "replaced", deploy_key: "added", public_key: PUBLIC_KEY,
                                                      message: "added the new key to octocat/agents-library and removed the old one" }]);
    await click("Regenerate key", 0, part("access"));
    out.regenerated = alertText();
    reply("POST /ui/api/sync/github/forget", [200, { status: "forgotten", revoke_url: "https://github.com/settings/applications",
                                                     message: "The GitHub authorization is deleted from this machine." }]);
    await click("Forget account", 0, part("access"));
    out.revoke = findAll(part("access"), (n) => n.tag === "a").map((n) => n.attrs.href);
    out.actions = calls(mark).filter(([method, path]) => method !== "GET" || path.startsWith("/ui/api/sync/conflict"));
    // Open a flow's conflict: the editor shows the current text with the kept one in the split pane.
    reply("GET /ui/api/sync/conflict", [200, { ...FLOW_CONFLICT, current: DOCS["user:doc"], mine: "# Mine, kept\n", binary: false }]);
    await click("Open", 0, part("conflicts"));
    await sleep(40);
    out.open_flow = { page_hidden: byId("sync-page").classes.has("hidden"), title: byId("title").textContent,
                      side_hidden: byId("side").classes.has("hidden"), side_label: byId("side-label").textContent,
                      side: byId("side-content").value, content: byId("content").value, view_source: !byId("panes").classes.has("hidden") };
    reply("POST /ui/api/sync/disconnect", [200, { status: "off", deploy_key: "removed",
                                                  deploy_key_message: "Removed this machine's deploy key (Agents-Core mac-1a2b) from octocat/agents-library." }]);
    await openPage();
    syncState = { ...SYNC_OFF };
    await click("Disconnect");
    await sleep(30);
    out.disconnected = { alert: alertText(), steps: snapshot().steps, chip: chip() };
    await finish(out);
  }

  if (scenario === "sync_focus") {
    // keepFocus, as a rebuild calls it: what had the focus gets it back, found by its name.
    const make = (tag, text, attrs = {}) => {
      const made = documentStub.createElement(tag);
      made.tagName = tag.toUpperCase();
      made.textContent = text;
      for (const [name, value] of Object.entries(attrs)) made.setAttribute(name, value);
      return made;
    };
    const host = make("div", ""), fallback = make("h3", "Machines");
    let inside = [];
    host.contains = (node) => node === host || inside.includes(node);
    host.querySelectorAll = () => inside;
    const field = (label) => { const box = make("input", ""); box.parentNode = { textContent: label }; return box; };
    const run = (focused, rebuilt) => {
      documentStub.activeElement = focused;
      const restore = context.keepFocus(host, fallback);
      inside = rebuilt;
      restore();
      return rebuilt.map((node) => node.focused).concat(fallback.focused);
    };
    const oldRemove = make("button", "Remove", { "aria-label": "Remove linux-9f9f" });
    inside = [make("button", "Remove", { "aria-label": "Remove desk-0001" }), oldRemove];
    out.same_name = run(oldRemove, [make("button", "Remove", { "aria-label": "Remove desk-0001" }),
                                    make("button", "Remove", { "aria-label": "Remove linux-9f9f" })]);
    const oldBox = field("Personal flows");
    inside = [oldBox];
    out.same_field = run(oldBox, [field("Persona choices"), field("Personal flows")]);
    const gone = make("button", "Pause");
    inside = [gone];
    out.fallback = run(gone, [make("button", "Resume")]);
    out.outside = run(make("button", "Elsewhere"), [make("button", "Elsewhere")]);
    await finish(out);
  }

  if (scenario === "sync_hostkey") {
    reply("POST /ui/api/sync/setup",
          [200, { status: "host_key_unconfirmed", host: "git.example.com", label: "laptop",
                  fingerprints: ["SHA256:aaaa", "SHA256:bbbb"], message: "compare a fingerprint" }],
          [200, { status: "waiting_for_access", label: "laptop", public_key: PUBLIC_KEY, remote: "git.example.com/me/library" }]);
    await openPage();
    await sleep(30);
    out.fingerprints = { ...snapshot(), radios: findAll(root(), (n) => n.type === "radio").map((n) => n.value) };
    await choose("SHA256:bbbb");
    syncState = { ...syncState, host_key_unconfirmed: false };
    await click("Trust this key");
    await sleep(20);
    out.trusted = { ...snapshot(), key: (findAll(root(), hasClass("sync-key"))[0] || {}).textContent };
    out.calls = calls();
    await finish(out);
  }

  if (scenario === "sync_more") {
    // Set from the command line, not in the list; a second record of one file, seconds later.
    const twin = { ...FLOW_CONFLICT, id: "20261005T120007000000Z-cccccccccc", time: "2026-10-05T12:00:07+00:00" };
    syncState = { ...started(), fetch_minutes: 3, conflict_list: [FLOW_CONFLICT, PERSONA_CONFLICT, twin] };
    reply("GET /ui/api/sync/machines", [200, { available: true, label: "mac-1a2b", repository: "octocat/agents-library",
      machines: [{ id: 1, title: "Agents-Core mac-1a2b", label: "mac-1a2b", this: true, managed: true },
                 { id: 2, title: "Agents-Core linux-9f9f", label: "linux-9f9f", this: false, managed: true },
                 { id: 3, title: "CI deploy", label: "CI deploy", read_only: true, this: false, managed: false }] }]);
    reply("GET /ui/api/sync/scopes", [200, { groups: [{ group: "common", syncs: true }], ask_new_repositories: false }]);
    const part = (key) => byId("sync-body").children.find((card) => card.attrs["aria-labelledby"] === "sync-part-" + key);
    const opened = freshReads();
    await openPage();
    await sleep(20);
    out.fresh_on_open = freshReads() - opened;
    const select = findAll(part("status"), (n) => n.tag === "select")[0];
    out.interval = { options: select.children.map((option) => option.value), value: select.value };
    out.machines = textOf(part("machines"));
    out.remove_buttons = findAll(part("machines"), (n) => n.tag === "button").map((n) => n.attrs["aria-label"]);
    out.names = findAll(root(), (n) => n.tag === "button" && n.attrs["aria-label"]).map((n) => n.attrs["aria-label"]);
    // A request in flight: the clicked control keeps the focus (aria-disabled, never disabled); Pause works.
    const running = hold("POST /ui/api/sync/run");
    reply("POST /ui/api/sync/run", [200, { status: "synced", sent: [], received: [], conflicts: [], pushed: false }]);
    await click("Sync now", 0, part("status"));
    const now = buttonsNamed("Sync now", part("status"))[0], pause = buttonsNamed("Pause", part("status"))[0];
    out.busy = { sync_now: [now.attrs["aria-disabled"] ?? null, now.disabled], pause: [pause.attrs["aria-disabled"] ?? null, pause.disabled] };
    reply("PUT /ui/api/sync/settings", [200, { state: "paused" }]);
    const beforePause = calls().length;
    fire(pause, "click");
    await sleep(25);
    out.paused_while_busy = calls().slice(beforePause).map(([method, path, body]) => [method, path, body]);
    running.release();
    await sleep(40);
    out.after = { sync_now: [buttonsNamed("Sync now", part("status"))[0].attrs["aria-disabled"] ?? null] };
    // Fresh reads after actions only; polls take the scanned status.
    const polled = freshReads();
    await fireTimers(30000);
    out.fresh_on_poll = freshReads() - polled;
    // A poll that fails says so instead of showing the last state.
    reply("GET /ui/api/sync", [500, { error: "boom" }]);
    await fireTimers(30000);
    out.unavailable = chip();
    delete syncReplies["GET /ui/api/sync"];
    await fireTimers(30000);
    out.available_again = chip();
    // A file that is not text is said to be so.
    reply("GET /ui/api/sync/conflict", [200, { ...PERSONA_CONFLICT, current: null, current_binary: true, mine: null, binary: true }]);
    await click("Open", 1, part("conflicts"));
    out.binary = findAll(part("conflicts"), (n) => n.tag === "textarea").map((n) => n.value);
    // Regenerate key says what happens on GitHub; signed out it says why it cannot.
    reply("POST /ui/api/sync/key/regenerate", [200, { status: "replaced", deploy_key: "added", public_key: PUBLIC_KEY, message: "added" }]);
    await click("Regenerate key", 0, part("access"));
    out.regenerate_question = confirms.at(-1);
    syncState = { ...syncState, github: null };
    await fireTimers(30000);
    const regenerations = calls().filter(([, path]) => path === "/ui/api/sync/key/regenerate").length;
    await click("Regenerate key", 0, part("access"));
    out.signed_out = { alert: alertText(), posts: calls().filter(([, path]) => path === "/ui/api/sync/key/regenerate").length - regenerations };
    // Leaving the page with the focus on it puts the focus on the tab that shows.
    documentStub.activeElement = byId("sync-page");
    const focusedBefore = byId("tabs").children[0].focused;
    fire(byId("sync-chip"), "click");
    out.tab_focused = byId("tabs").children[0].focused - focusedBefore;
    await finish(out);
  }

  if (scenario === "sync_round2") {
    const part = (key) => byId("sync-body").children.find((card) => card.attrs["aria-labelledby"] === "sync-part-" + key);
    // The wizard's first step comes from the settings (`set_up`), never from a status a scan old.
    const step = (status) => { const made = context.newWizard(status); return [made.step, made.mode]; };
    out.wizard_from = {
      stale_synced: step({ ...SYNC_OFF, state: "synced", set_up: false }),
      stale_off: step({ ...SYNC_OFF, state: "off", set_up: true, repository: "git.example.com/me/library" }),
      github: step({ ...SYNC_OFF, state: "off", set_up: true, github_repository: "octocat/agents-library" }),
    };
    // A fresh read is pending (opening the page): the 30 s poll neither replaces nor overtakes it.
    const fresh = hold("GET /ui/api/sync?fresh=1");
    reply("GET /ui/api/sync/machines", [200, { available: true, label: "mac-1a2b", repository: "octocat/agents-library",
      machines: [{ id: 5, title: "laptop key", label: "laptop key", read_only: false, this: true, managed: false },
                 { id: 6, title: "CI deploy", label: "CI deploy", read_only: true, this: false, managed: false }] }]);
    reply("GET /ui/api/sync/scopes", [200, { groups: [{ group: "common", syncs: true }], ask_new_repositories: false }]);
    const reads = statusReads();
    out.before = chip().text;
    syncState = { ...started(), conflicts: 0, conflict_list: [] };  // what the fresh read answers, once released
    await openPage();
    syncState = { ...started(), state: "paused" };  // what a poll would answer meanwhile
    out.poll_timers = pendingTimers(30000).length;
    await fireTimers(30000);
    out.while_fresh = { reads: statusReads() - reads, chip: chip().text };
    fresh.release();
    await sleep(40);
    out.after_fresh = { chip: chip().text, timers: pendingTimers(30000).length };
    // This machine's key under a title sync did not give it.
    out.machines = textOf(part("machines"));
    out.remove_buttons = buttonsNamed("Remove", part("machines")).length;
    // A request runs: text fields are read-only until it answers.
    const running = hold("POST /ui/api/sync/run");
    reply("POST /ui/api/sync/run", [200, { status: "synced", sent: [], received: [], conflicts: [], pushed: false }]);
    await click("Sync now", 0, part("status"));
    const name = field("sync-id-name");
    out.busy_field = { read_only: name.readOnly, aria: name.attrs["aria-disabled"] ?? null };
    running.release();
    await sleep(40);
    out.idle_field = { read_only: field("sync-id-name").readOnly, aria: field("sync-id-name").attrs["aria-disabled"] ?? null };
    await finish(out);
  }

  if (scenario === "sync_busy_radio") {
    reply("POST /ui/api/sync/setup",
          [200, { status: "host_key_unconfirmed", host: "git.example.com", label: "laptop",
                  fingerprints: ["SHA256:aaaa", "SHA256:bbbb"], message: "compare a fingerprint" }],
          [200, { status: "waiting_for_access", label: "laptop", public_key: PUBLIC_KEY, remote: "git.example.com/me/library" }]);
    await openPage();
    await sleep(30);
    await choose("SHA256:aaaa");
    const trusting = hold("POST /ui/api/sync/setup");
    await click("Trust this key");
    await choose("SHA256:bbbb");  // while the request runs: the choice goes back
    out.while_busy = findAll(root(), (n) => n.type === "radio").map((n) => [n.value, n.checked]);
    trusting.release();
    await sleep(30);
    out.sent = calls().filter(([, path]) => path === "/ui/api/sync/setup").map(([, , body]) => body);
    await finish(out);
  }

  if (scenario === "sync_hostkey_github") {
    const setting = hold("POST /ui/api/sync/setup");
    reply("POST /ui/api/sync/setup", [200, { status: "waiting_for_access", label: "laptop", public_key: PUBLIC_KEY,
      repository: "octocat/agents-library", deploy_key: "added",
      steps: ["added this machine's deploy key to octocat/agents-library (Agents-Core laptop, with write access)"] }]);
    await openPage();
    await sleep(30);
    out.stopped = { ...snapshot(), text: textOf(root()) };
    setting.release();
    await sleep(30);
    out.again = { ...snapshot(), text: textOf(root()) };
    out.calls = calls();
    await finish(out);
  }

  if (scenario === "sync_notice") {
    const notice = () => ({ hidden: byId("sync-notice").classes.has("hidden"), text: byId("sync-notice-text").textContent });
    await open(0);  // user:doc at rev1
    const receive = (flow, time, from = ["laptop"]) => ({ time, result: "ok", machine: "mac-1a2b", sent: [],
                                                          received: [flow], received_from: from, scripts: [], conflicts: 0 });
    syncState = { ...started(), activity: [...started().activity, receive("user:plain", "2026-10-05T14:01:00+00:00")] };
    await fireTimers(30000);
    out.other_flow = notice();  // another flow was received
    syncState = { ...syncState, activity: [...syncState.activity, receive("user:doc", "2026-10-05T14:01:30+00:00")] };
    await fireTimers(30000);
    out.same_revision = notice();  // the revision did not change: a persona or another repository's flow
    revisions["user:doc"] = "rev2";
    savedText["user:doc"] = "# Doc from laptop\n";
    syncState = { ...syncState, activity: [...syncState.activity, receive("user:doc", "2026-10-05T14:02:10+00:00", ["laptop", "desk"])] };
    byId("content").value = "# My unsaved text\n";
    fire(byId("content"), "input");
    await fireTimers(30000);
    out.updated = { ...notice(), content: byId("content").value };
    putStatus = 409;
    fire(byId("save"), "click");
    await sleep(60);
    out.save_conflict = { notice: byId("notice").textContent, content: byId("content").value, side: byId("side-content").value,
                          side_label: byId("side-label").textContent, sync_notice: notice() };
    putStatus = 200;
    revisions["user:doc"] = "rev3";
    savedText["user:doc"] = "# Doc again\n";
    syncState = { ...syncState, activity: [...syncState.activity, receive("user:doc", "2026-10-05T14:03:00+00:00")] };
    await fireTimers(30000);
    out.again = notice();
    fire(byId("sync-reload"), "click");
    await sleep(40);
    out.reloaded = { ...notice(), content: byId("content").value };
    // A cycle deleted the open flow: said so, without Reload, and a save names the sync.
    deletedFlows.add("user:doc");
    syncState = { ...syncState, activity: [...syncState.activity, receive("user:doc", "2026-10-05T14:04:00+00:00", ["desk"])] };
    byId("content").value = "# Kept text\n";
    fire(byId("content"), "input");
    await fireTimers(30000);
    out.deleted = { ...notice(), reload_hidden: byId("sync-reload").classes.has("hidden") };
    putStatus = 409;
    fire(byId("save"), "click");
    await sleep(60);
    out.deleted_save = { notice: byId("notice").textContent, content: byId("content").value };
    await finish(out);
  }
}

if (isAutodev) {
  // The Autodev page as the user sees it, found by the controls' ids.
  const out = {};
  const textOf = (node) => (node.nodeType === 3 ? node.textContent : (node.textContent || "") + node.children.map(textOf).join(""));
  const root = () => byId("autodev-body");
  const field = (id) => findAll(root(), (n) => n.attrs.id === id)[0];
  const value = (id) => (field(id) ? field(id).value : null);
  const setValue = async (id, text) => {
    const control = field(id);
    control.value = text;
    fire(control, control.tag === "select" ? "change" : "input");
    await sleep(0);
  };
  const click = async (id) => { fire(field(id), "click"); await sleep(40); };
  const alert = () => ({ text: byId("autodev-alert").textContent, className: byId("autodev-alert").className,
                         items: findAll(byId("autodev-alert"), (n) => n.tag === "li").map((n) => n.textContent) });
  const options = (id) => field(id).children.map((option) => [option.value, option.textContent, option.disabled]);
  const profile = (name) => {
    const found = { client: value(`ad-${name}-client`) };
    for (const client of ["claude", "codex"]) {
      found[client] = [value(`ad-${name}-${client}-model`), value(`ad-${name}-${client}-effort`)];
    }
    if (field(`ad-${name}-followup`)) found.followup = value(`ad-${name}-followup`);
    if (field(`ad-${name}-role`)) found.role = value(`ad-${name}-role`);
    return found;
  };
  const form = () => ({
    times: value("ad-times"), catch_up: value("ad-catch-up"), hours: value("ad-hours"), ac: field("ad-ac").checked,
    limits: ["max_reviews", "max_bot_rounds", "max_wait_minutes"].map((key) => value("ad-" + key)),
    profiles: Object.fromEntries(["select", "implement", "submit", "code-review-pass", "change-premortem"].map((name) => [name, profile(name)])),
  });
  const page = () => ({ shown: !byId("autodev-page").classes.has("hidden"), main_hidden: byId("main").classes.has("hidden"),
                        chip: byId("autodev-chip").attrs["aria-pressed"] });
  const status = () => ({ state: textOf(field("ad-state")), next: field("ad-next") && textOf(field("ad-next")),
                          runs: textOf(field("ad-runs")), switch: field("ad-switch") && field("ad-switch").textContent });
  const openChip = async () => { fire(byId("autodev-chip"), "click"); await sleep(40); };
  const gets = () => autodevCalls.filter(([method]) => method === "GET").length;
  const puts = () => autodevCalls.filter(([method]) => method === "PUT");
  const unloadBlocked = () => {
    let blocked = false;
    for (const handler of windowHandlers.beforeunload || []) handler({ preventDefault() { blocked = true; } });
    return blocked;
  };
  const done = () => finish(out);

  if (scenario === "autodev_link") {  // /ui#autodev opens the page and clears the hash
    await sleep(40);
    out.opened = { ...page(), gets: gets(), replaced: replacedUrls, title_focused: byId("autodev-title").focused };
    await done();
  }
  await openChip();
  if (scenario === "autodev_invalid") {  // the problems, and no form or switch
    const card = root().children.find((child) => child.children.some((k) => k.tag === "h3" && k.textContent === "autodev.json is invalid"));
    out.invalid = { ...page(), problems: card ? findAll(card, (n) => n.tag === "li").map((n) => n.textContent) : null,
                    state: textOf(field("ad-state")),
                    controls: ["ad-save", "ad-defaults", "ad-reload", "ad-switch", "ad-times", "ad-select-client"].filter((id) => field(id)) };
    await done();
  }
  out.opened = { ...page(), title_focused: byId("autodev-title").focused, tabs: byId("tabs").children.map((b) => b.attrs["aria-pressed"]),
                 gets: gets() };
  out.form = form();
  out.codex = { select: textOf(field("ad-select-codex")), implement: textOf(field("ad-implement-codex")),
                review: textOf(field("ad-code-review-pass-codex")), clients: options("ad-select-client").map((o) => o[1]),
                implement_clients: options("ad-implement-client").map((o) => o[1]) };
  out.mode = options("ad-mode");
  out.automatic = { text: textOf(field("ad-automatic")),
                    missing: field("ad-missing") ? field("ad-missing").children.map((n) => n.textContent) : null };
  out.status = status();
  out.efforts = { claude: options("ad-select-claude-effort").map((o) => o[0]),
                  followup: options("ad-code-review-pass-followup").map((o) => o[0]) };
  if (scenario === "autodev_codex") await done();

  if (scenario === "autodev_ready") {
    // Run is offered; turning it on asks for the acceptance first, and "no" sends nothing.
    await setValue("ad-mode", "run");
    confirmAnswer = false;
    await click("ad-switch");
    out.declined = { confirms: confirms.slice(), posts: autodevCalls.filter(([method]) => method === "POST").length };
    confirmAnswer = true;
    await click("ad-switch");
    out.enabled = { post: autodevCalls.at(-1), status: status(), alert: alert() };
    // The conditions lost since the page read them (the recorded `autodev ready` aged, a tripwire, the
    // turn-off's withdrawal of the acceptance): the daemon refuses and the alert lists what is missing.
    await click("ad-switch");
    autodevReady = false;
    await setValue("ad-mode", "run");
    await click("ad-switch");
    out.refused = { post: autodevCalls.at(-1)[2], alert: alert(), state: status().state };
    await done();
  }

  if (scenario === "autodev_stored") {
    // Stored values the lists lack stay, marked; the review's follow-up list follows its client.
    out.stored = { select_effort: value("ad-select-claude-effort"), select_options: options("ad-select-claude-effort"),
                   followup: value("ad-code-review-pass-followup"), status: textOf(field("ad-status")) };
    await setValue("ad-code-review-pass-client", "claude");
    out.client_changed = { followup: value("ad-code-review-pass-followup"), options: options("ad-code-review-pass-followup") };
    await click("ad-save");
    const put = puts().at(-1)[2].settings;
    out.saved = { select: put.stages.select.claude, review: put.delegates["code-review-pass"], alert: alert().text };
    await done();
  }

  if (scenario === "autodev_save_edits") {
    // A held PUT stores the submitted snapshot; text and select edits made while it answers stay unsaved.
    await setValue("ad-catch-up", "55");
    const releasePut = holdAutodev("PUT /ui/api/autodev");
    await click("ad-save");
    const savedAt = autodevState.revision;
    await setValue("ad-catch-up", "66");
    await setValue("ad-code-review-pass-followup", "low");
    // Reopening while the save is pending must not start a read that can overwrite its revision.
    await openChip();
    await openChip();
    out.pending = { gets: gets(), catch_up: value("ad-catch-up"), followup: value("ad-code-review-pass-followup"),
                    submitted: puts().at(-1)[2].settings.catch_up_minutes, stored: autodevState.settings.catch_up_minutes };
    releasePut();
    await sleep(40);
    out.saved = { catch_up: value("ad-catch-up"), followup: value("ad-code-review-pass-followup"),
                  alert: alert(), unload_blocked: unloadBlocked(), saved_at: savedAt };
    // Later reads keep the unsaved form; Reload still asks before discarding it.
    await openChip();
    await openChip();
    confirmAnswer = false;
    await click("ad-reload");
    out.kept = { catch_up: value("ad-catch-up"), followup: value("ad-code-review-pass-followup"),
                 gets: gets(), confirms: confirms.slice(), unload_blocked: unloadBlocked() };
    await click("ad-save");
    out.resaved = { put: puts().at(-1)[2], alert: alert().text, unload_blocked: unloadBlocked() };

    // A held refusal keeps later corrections and the revision of the last successful save.
    const readAt = autodevState.revision;
    await setValue("ad-times", "25:00");
    const releaseError = holdAutodev("PUT /ui/api/autodev");
    await click("ad-save");
    await setValue("ad-times", "04:00");
    await setValue("ad-catch-up", "77");
    await openChip();
    await openChip();
    releaseError();
    await sleep(40);
    out.refused = { times: value("ad-times"), catch_up: value("ad-catch-up"), alert: alert(),
                    gets: gets(), unload_blocked: unloadBlocked(), read_at: readAt, stored_at: autodevState.revision };
    await click("ad-save");
    out.corrected = { put: puts().at(-1)[2], alert: alert().text, unload_blocked: unloadBlocked() };
    await done();
  }

  if (scenario === "autodev_save_writer") {
    await setValue("ad-catch-up", "55");
    const releasePut = holdAutodev("PUT /ui/api/autodev");
    await click("ad-save");
    const savedAt = autodevState.revision;
    await setValue("ad-catch-up", "66");
    // Another writer changes an untouched field after the first write. The PUT response
    // describes that first write, so the retained form must keep its older revision.
    autodevState.settings = { ...autodevState.settings, allowed_hours: "07:00-20:00" };
    autodevState.revision = "external-writer";
    releasePut();
    await sleep(40);
    out.saved = { catch_up: value("ad-catch-up"), hours: value("ad-hours"), saved_at: savedAt,
                  unload_blocked: unloadBlocked(), alert: alert() };
    await click("ad-save");
    out.refused = { put: puts().at(-1)[2], catch_up: value("ad-catch-up"), hours: value("ad-hours"),
                    stored_catch_up: autodevState.settings.catch_up_minutes,
                    stored_hours: autodevState.settings.allowed_hours, stored_at: autodevState.revision,
                    unload_blocked: unloadBlocked(), alert: alert() };
    await done();
  }

  if (scenario === "autodev_races") {
    // Reload asks before it drops unsaved edits; "no" keeps them and sends nothing.
    await setValue("ad-catch-up", "55");
    confirmAnswer = false;
    await click("ad-reload");
    out.reload_no = { confirms: confirms.slice(), gets: gets(), catch_up: value("ad-catch-up") };
    confirmAnswer = true;
    await click("ad-reload");
    out.reload_yes = { confirms: confirms.length, gets: gets(), catch_up: value("ad-catch-up") };
    // One request at a time: Save pressed again while the first save is under way sends nothing.
    const releasePut = holdAutodev("PUT /ui/api/autodev");
    await setValue("ad-max_reviews", "5");
    await click("ad-save");
    await click("ad-save");
    releasePut();
    await sleep(40);
    out.busy = { puts: puts().length, alert: alert().text };
    // A saved form is clean: the next read shows a change made elsewhere.
    autodevState.settings = { ...autodevState.settings, catch_up_minutes: 45 };
    autodevState.revision = "x1";
    await openChip();
    await openChip();
    out.clean = { catch_up: value("ad-catch-up"), max_reviews: value("ad-max_reviews") };
    // Defaults put in a clean form are unsaved edits too: a read on coming back keeps them.
    await click("ad-defaults");
    await openChip();
    await openChip();
    out.defaults_kept = { catch_up: value("ad-catch-up"), max_reviews: value("ad-max_reviews"), gets: gets() };
    // A read sent before a save and answered after it is dropped: the form and the revision stay the saved ones.
    const releaseGet = holdAutodev("GET /ui/api/autodev");
    await click("ad-reload");
    await setValue("ad-catch-up", "30");
    await click("ad-save");
    const savedAt = autodevState.revision;
    releaseGet();
    await sleep(40);
    out.stale = { catch_up: value("ad-catch-up"), saved_at: savedAt };
    await click("ad-save");
    out.stale = { ...out.stale, put: puts().at(-1)[2].expected_revision, alert: alert().text };
    // The switch with unsaved edits keeps the revision the form was read at: a change made elsewhere
    // before it (which its write carries) still refuses the save.
    const readAt = autodevState.revision;
    await setValue("ad-times", "04:00");
    autodevState.settings = { ...autodevState.settings, catch_up_minutes: 50 };
    autodevState.revision = "x2";
    await click("ad-switch");
    out.switched = { state: status().state, times: value("ad-times") };
    await click("ad-save");
    out.switched = { ...out.switched, put: puts().at(-1)[2].expected_revision, read_at: readAt, alert: alert(),
                     times_after: value("ad-times") };
    await done();
  }

  // Edits survive leaving the page; a tab leaves it too.
  await setValue("ad-catch-up", "77");
  await openChip();
  out.left = page();
  await openChip();
  out.back = { ...page(), catch_up: value("ad-catch-up"), gets: gets() };
  fire(byId("tabs").children[1], "click");
  await sleep(40);
  out.tab = { ...page(), tabs: byId("tabs").children.map((b) => b.attrs["aria-pressed"]) };
  await openChip();

  // A valid save sends every setting with the revision read; a refused one keeps what was typed.
  await setValue("ad-max_reviews", "4");
  await setValue("ad-code-review-pass-followup", "low");
  await setValue("ad-change-premortem-role", "software_engineer");
  await setValue("ad-implement-codex-model", "gpt-6.1-sol");
  await setValue("ad-implement-codex-effort", "ultra");
  await click("ad-save");
  out.saved = { put: autodevCalls.filter(([method]) => method === "PUT").at(-1)[2], alert: alert(), form: form() };
  await setValue("ad-times", "25:00, 13:47");
  await click("ad-save");
  out.refused = { alert: alert(), times: value("ad-times"), puts: autodevCalls.filter(([method]) => method === "PUT").length };
  // The switch: on in dry-run (the form keeps what was typed and the revision it was read at), then off.
  await click("ad-switch");
  out.enabled = { post: autodevCalls.at(-1), status: status(), alert: alert(), times: value("ad-times") };
  await setValue("ad-times", "03:00");
  await click("ad-save");
  out.after_switch = { put: autodevCalls.at(-1)[2].expected_revision, alert: alert().text };
  await click("ad-switch");
  out.disabled = { post: autodevCalls.at(-1).slice(0, 2), status: status() };
  // Defaults fill the form; Save writes them.
  await setValue("ad-max_reviews", "9");
  await click("ad-defaults");
  out.defaults = { form: form(), alert: alert().text };
  // The defaults are unsaved edits: leaving the page and coming back keeps them in the form.
  await openChip();
  await openChip();
  out.defaults_kept = form();
  await done();
}

if (scenario === "history_adopt") {
  // #292: an entry of the fallback journal moves into the project its named folder belongs to.
  const out = {};
  const textOf = (node) => (node.nodeType === 3 ? node.textContent : (node.textContent || "") + node.children.map(textOf).join(""));
  const view = byId("h-view"), doc = () => findAll(view, hasClass("md"))[0];
  const rendered = () => findAll(doc(), hasClass("h-entry"));
  await openTab("history");
  out.list = shown();
  await open(0);
  out.entry = textOf(rendered()[0]);
  fire(findAll(rendered()[0], hasClass("h-adopt"))[0], "click");
  await sleep(80);
  out.prompt = prompts.at(-1);
  out.posted = historyAdopts;
  out.title = byId("h-title").textContent;
  await finish(out);
}
if (scenario === "history") {
  const out = {};
  const textOf = (node) => (node.nodeType === 3 ? node.textContent : (node.textContent || "") + node.children.map(textOf).join(""));
  const view = byId("h-view"), doc = () => findAll(view, hasClass("md"))[0];
  const rendered = () => findAll(doc(), hasClass("h-entry"));
  const more = () => findAll(view, hasClass("h-more"))[0];
  const reads = () => requests.filter((path) => path.startsWith("/ui/api/history?"));
  await openTab("history");
  out.list = shown();
  out.chips = buttons().map((button) => button.children.filter((child) => (child.className || "").startsWith("chip"))
    .map((child) => child.textContent));
  await open(0);
  out.pane = { hist: !byId("hist").classes.has("hidden"), welcome: !byId("welcome").classes.has("hidden"),
               h_actions: !byId("h-actions").classes.has("hidden"), group_pane: byId("item-actions").dataset.pane,
               title: byId("h-title").textContent, meta: byId("h-meta").textContent,
               seg: byId("h-seg").children.map((button) => button.attrs["aria-pressed"]) };
  out.files = byId("h-file").children.map((option) => [option.text, option.value]);
  out.rendered = rendered().length;
  out.first = rendered().slice(0, 2).map((entry) => [textOf(entry.children[0]), textOf(entry.children[1]), textOf(entry.children[2])]);
  out.toc = findAll(view, hasClass("md-toc-item")).slice(0, 6).map((item) => item.className + ":" + item.textContent);
  out.toc_entries = findAll(view, hasClass("md-l2")).length;
  out.more = more().textContent;
  Object.assign(doc(), { scrollTop: 900, clientHeight: 500, scrollHeight: 1600 });
  fire(doc(), "scroll");
  await sleep(30);
  out.after_scroll = { rendered: rendered().length, more: more().textContent };
  const items = findAll(view, hasClass("md-l2"));
  fire(items[items.length - 1], "click");
  await sleep(30);
  out.after_contents = { rendered: rendered().length, scrolled: rendered()[44].scrolled, more_hidden: more().classes.has("hidden") };
  fire(seg("h-seg", 1), "click");
  await sleep(30);
  out.source = { value: byId("h-text").value, label: byId("h-source-label").textContent,
                 shown: !byId("h-source").classes.has("hidden"), rendered_hidden: view.classes.has("hidden") };
  fire(seg("h-seg", 0), "click");
  await sleep(10);
  byId("h-file").value = "2026-09";
  fire(byId("h-file"), "change");
  await sleep(30);
  out.archive = { rendered: rendered().length, first: textOf(rendered()[0].children[0]), toc_entries: findAll(view, hasClass("md-l2")).length };
  await type("proj");
  out.search_name = shown();
  await type("needle");
  out.search_pending = shown();
  await sleep(350);
  out.search_text = shown();
  await type("");
  await open(2);
  out.gone = { title: byId("h-title").textContent, notice: byId("h-notice").textContent,
               notice_hidden: byId("h-notice").classes.has("hidden"), seg_hidden: byId("h-seg").classes.has("hidden"),
               file_hidden: byId("h-file").classes.has("hidden"), rendered_hidden: view.classes.has("hidden") };
  context.location.hash = "#history/w-1/" + HISTORY_ENTRIES[30].id;
  for (const handler of windowHandlers.hashchange || []) handler({});
  await sleep(60);
  out.link = { rendered: rendered().length, scrolled: rendered()[30].scrolled, title: byId("h-title").textContent };
  out.reads = reads();
  const repoReads = requests.filter((path) => path === "/ui/api/history/repos").length;
  fire(findAll(rendered()[0], hasClass("h-delete"))[0], "click");
  await sleep(80);
  out.delete = { confirm: confirms.at(-1), posted: historyPosts, rendered: rendered().length,
                 first: textOf(rendered()[0].children[0]), meta: byId("h-meta").textContent,
                 repos_read_again: requests.filter((path) => path === "/ui/api/history/repos").length - repoReads,
                 reread: requests.filter((path) => path.startsWith("/ui/api/history?")).at(-1) };
  await finish(out);
}

if (scenario === "memory") {
  const out = {};
  const textOf = (node) => (node.nodeType === 3 ? node.textContent : (node.textContent || "") + node.children.map(textOf).join(""));
  const hidden = (id) => byId(id).classes.has("hidden");
  const view = byId("m-view"), doc = () => findAll(view, hasClass("md"))[0];
  const items = () => findAll(doc(), hasClass("m-item"));
  const section = (title) => items().find((node) => textOf(node.children[0]) === title);
  const tagged = (node, tag) => findAll(node, (child) => child.tag === tag);
  const labels = (node) => tagged(node, "button").map((button) => button.textContent);
  const click = async (node, label) => {
    fire(tagged(node, "button").find((button) => button.textContent === label), "click");
    await sleep(80);
  };
  const area = (title) => tagged(section(title), "textarea")[0];
  const write = (title, text) => { const box = area(title); box.value = text; fire(box, "input"); };
  const notice = () => ({ text: byId("m-notice").textContent, kind: byId("m-notice").className,
                          buttons: byId("m-notice").children.map((button) => button.textContent) });
  await openTab("memory");
  out.list = shown();
  out.chips = buttons().map((button) => button.children.filter((child) => (child.className || "").startsWith("chip"))
    .map((child) => child.textContent));
  await open(0);
  out.pane = { mem: !hidden("mem"), welcome: !hidden("welcome"), actions: !hidden("m-actions"),
               group_pane: byId("item-actions").dataset.pane, title: byId("m-title").textContent, meta: byId("m-meta").textContent,
               notice: notice(), stores: byId("m-store").children.map((option) => [option.text, option.value]),
               store: byId("m-store").value, seg: byId("m-seg").children.map((button) => button.attrs["aria-pressed"]) };
  out.sections = items().map((node) => textOf(node.children[0]));
  out.toc = findAll(view, hasClass("md-toc-item")).map((item) => item.className + ":" + item.textContent);
  const workflow = section("workflow");
  out.workflow = { chips: findAll(workflow.children[1], hasClass("chip")).map(textOf), meta: textOf(workflow.children[1]),
                   description: textOf(workflow.children[2]), links: tagged(workflow, "a").map((link) => link.attrs.href),
                   text: textOf(workflow), buttons: labels(workflow) };
  // A name with spaces and brackets links too, escaped; an unknown one stays as written.
  const named = element();
  context.renderMarkdown(context.memoryLinks("See [[Issue (x) *workflow*]] and [[nobody]].",
    new Map([["Issue (x) *workflow*", "issue.md"]])), named);
  out.named_link = { anchors: findAll(named, (node) => node.tag === "a").map((a) => [a.attrs.href, textOf(a)]),
                     text: textOf(named) };
  out.loose_buttons = labels(section("loose.md"));
  out.index_buttons = labels(section("MEMORY.md"));
  fire(tagged(workflow, "a")[0], "click", { preventDefault() {} });
  out.link_scrolled = section("zsh").scrolled;
  fire(seg("m-seg", 1), "click");
  await sleep(20);
  out.source = tagged(section("zsh"), "pre").map(textOf);
  fire(seg("m-seg", 0), "click");
  await sleep(20);
  await click(section("zsh"), "Edit");
  out.editor = { value: area("zsh").value, buttons: labels(section("zsh")) };
  write("zsh", "CONFLICT");
  await click(section("zsh"), "Save");
  out.conflict = { notice: notice(), editing: !!area("zsh"), put: memoryCalls.at(-1) };
  write("zsh", "new zsh text\n");
  await click(section("zsh"), "Save");
  out.saved = { notice: notice(), editing: !!area("zsh"), put: memoryCalls.at(-1), text: textOf(section("zsh")) };
  await click(section("zsh"), "Edit");
  write("zsh", "CONFLICT");
  await click(section("zsh"), "Save");
  await click(byId("m-notice"), "Reload");
  out.reloaded = { editing: !!area("zsh"), notice: notice(), text: textOf(section("zsh")) };
  await click(section("zsh"), "Copy to ~/.claude-work");
  out.compare = { pres: tagged(section("zsh"), "pre").map(textOf), buttons: labels(section("zsh")), call: memoryCalls.at(-1) };
  await click(section("zsh"), "Replace in ~/.claude-work");
  out.replaced = { call: memoryCalls.at(-1), notice: notice(), buttons: labels(section("zsh")) };
  await click(section("zsh"), "Move to ~/.claude-work");
  out.moved = { confirm: confirms.at(-1), call: memoryCalls.at(-1), notice: notice(), sections: items().map((node) => textOf(node.children[0])) };
  await click(section("loose.md"), "Add to MEMORY.md");
  out.indexed = { call: memoryCalls.at(-1), notice: notice(), buttons: labels(section("loose.md")) };
  await click(section("loose.md"), "Delete");
  out.deleted = { confirm: confirms.at(-1), call: memoryCalls.at(-1), notice: notice() };
  fire(byId("m-new"), "click");
  await sleep(20);
  const draft = () => items()[0];
  out.draft = { first: textOf(draft().children[0]), file: tagged(draft(), "input")[0].value,
                type: tagged(draft(), "select")[0].value, types: tagged(draft(), "select")[0].children.map((option) => option.value),
                index: tagged(draft(), "input")[1].checked, buttons: labels(draft()) };
  const kind = tagged(draft(), "select")[0];
  kind.value = "project";
  fire(kind, "change");
  out.draft_type = tagged(draft(), "textarea")[0].value;
  const input = tagged(draft(), "input")[0];
  input.value = "bad name";
  fire(input, "input");
  await click(draft(), "Save");
  out.bad_name = { notice: notice(), calls: memoryCalls.length };
  input.value = "fresh.md";
  fire(input, "input");
  await click(draft(), "Save");
  out.created = { call: memoryCalls.at(-1), notice: notice(), first: textOf(items()[0].children[0]) };
  byId("m-store").value = MEM_WORK;
  fire(byId("m-store"), "change");
  await sleep(60);
  out.other_store = { read: requests.filter((path) => path.startsWith("/ui/api/memory?")).at(-1), sections: items().map((node) => textOf(node.children[0])),
                      buttons: labels(section("zsh")) };
  await type("needle");
  out.search_pending = shown();
  await sleep(350);
  out.search_text = shown();
  // Leaving the tab with unsaved text asks first; once discarded, closing the page no longer asks.
  const unload = () => {
    const event = { prevented: false, preventDefault() { this.prevented = true; } };
    for (const handler of windowHandlers.beforeunload || []) handler(event);
    return event.prevented;
  };
  await click(section("zsh"), "Edit");
  write("zsh", "unsaved");
  out.leave = { guarded: unload(), asks: confirms.length };
  await openTab("flows");
  out.leave.asked = confirms.at(-1);
  out.leave.asks = confirms.length - out.leave.asks;
  out.leave.guarded_after = unload();
  await finish(out);
}

if (scenario === "memory_codex") {
  const out = {};
  const textOf = (node) => (node.nodeType === 3 ? node.textContent : (node.textContent || "") + node.children.map(textOf).join(""));
  const view = byId("m-view"), doc = () => findAll(view, hasClass("md"))[0];
  const items = () => findAll(doc(), hasClass("m-item"));
  const tagged = (node, tag) => findAll(node, (child) => child.tag === tag);
  await openTab("memory");
  await open(0);
  out.sections = items().map((node) => textOf(node.children[0]));
  out.pre = tagged(items()[0], "pre").map(textOf);
  out.headings = findAll(items()[0], (node) => /^h[1-6]$/.test(node.tag)).length;
  out.toc = findAll(view, hasClass("md-toc-item")).map((item) => item.textContent);
  out.buttons = tagged(items()[0], "button").map((button) => button.textContent);
  fire(byId("m-new"), "click");
  await sleep(20);
  const draft = items()[0];
  out.draft = { first: textOf(draft.children[0]), selects: tagged(draft, "select").length, inputs: tagged(draft, "input").length,
                file: tagged(draft, "input")[0].value, text: tagged(draft, "textarea")[0].value };
  const input = tagged(draft, "input")[0];
  input.value = "facts.txt";
  fire(input, "input");
  fire(tagged(draft, "button").find((button) => button.textContent === "Save"), "click");
  await sleep(80);
  out.created = memoryCalls.at(-1);
  await finish(out);
}

if (scenario === "agents") {
  const out = {};
  const hidden = (id) => byId(id).classes.has("hidden");
  // The detail pane: its text, the facts as [term, detail] pairs and which parts are hidden.
  const pane = () => {
    const rows = byId("c-facts").children;
    return {
      title: byId("c-title").textContent, meta: byId("c-meta").textContent, description: byId("c-description").textContent,
      facts: rows.filter((_, k) => k % 2 === 0).map((term, k) => [term.textContent, rows[2 * k + 1].textContent]),
      tags: [...new Set(rows.map((row) => row.tag))],
      hidden: { toggle: hidden("c-toggle"), notice: hidden("c-notice"), warning: hidden("c-warning"), facts: hidden("c-facts") },
    };
  };
  await openTab("agents");
  out.list = { ...shown(), roles: buttons().map((b) => b.children.find((c) => c.tag === "small").textContent),
               new_hidden: hidden("new"), header: panes() };
  await type("wireframe");  // a routing keyword of one agent, in no agent's name
  out.keyword = shown();
  await openTab("rules");
  out.rules_query = shown().query;
  await openTab("agents");
  out.query_kept = shown();
  await type("gamma_agent");
  out.by_name = shown();
  await type("");
  await open(0);
  out.alpha = { ...pane(), view: snap("c-view"), body: byId("c-body").value, source_hidden: hidden("c-source"),
                header: { ...panes(), seg: byId("c-seg").children.map((b) => b.textContent), toc: !hidden("c-toc") } };
  fire(byId("c-toggle"), "click");  // the switch is hidden; a click that still reaches it changes nothing
  await sleep(20);
  fire(seg("c-seg", 1), "click");
  out.alpha_source = { view_hidden: hidden("c-view"), source_hidden: hidden("c-source") };
  await open(2);
  out.gamma = { ...pane(), view: snap("c-view") };
  await openTab("skills");
  await open(0);
  out.skill = pane();
  await openTab("flows");
  await open(0);  // opening a flow fills the Persona picker
  out.persona_options = byId("persona-agent").children.map((option) => option.text + " = " + option.value);
  out.agent_requests = requests.filter((path) => path.startsWith("/ui/api/agents"));
  out.component_writes = requests.filter((path) => path === "/ui/api/component");
  await finish(out);
}

if (scenario === "persona_race") {
  const panel = () => ({ title: byId("title").textContent, save_disabled: byId("persona-save").disabled,
                         reset_disabled: byId("persona-reset").disabled });
  const steps = {};
  await context.openFlow("user:a");
  await sleep(20);  // the persona catalog
  steps.opened = panel();
  fire(byId("persona-save"), "click");  // the PUT now waits
  await sleep(5);
  steps.saving = panel();
  const navigation = context.openFlow("user:b");  // B's GET takes 100 ms
  await sleep(5);
  releasePersonaPut();  // the stale save answers before B arrives
  await sleep(20);
  steps.stale_save_answered = panel();
  await navigation;
  await sleep(20);
  steps.after_navigation = panel();
  await finish(steps);
}

if (scenario === "place") {
  // itemPlace for a group that ends at 800 px, a pane whose straight top edge starts at 339 px and a
  // version that ends, with the gap, at 230 px.
  const at = (groupWidth, narrow = false) =>
    context.itemPlace({ narrow, groupWidth, groupEnd: 800, versionEnd: 230, straightStart: 339 });
  await finish({ on_pane: at(461), past_corner: at(462), after_version: at(570),
                               too_wide: at(571), narrow: at(100, true) });
}

// --- workmap_routes: the layout and the edges' routes, checked by geometry ---------------------------
// The shape of a real release map (ids, kinds and edges of the reviewers' copy at revision 80): six
// stages first, two verifier trays, the long edges the old drawing crossed and ran through a tray.
const RELEASE_SHAPE = {
  nodes: [["goal", "goal"], ["triage", "stage"], ["readiness", "stage"], ["fix-701", "stage"], ["workmap-ui", "stage"],
    ["v701-contract", "stage"], ["v701-failure", "stage"], ["vui-security", "stage"], ["vui-correctness", "stage"],
    ["vui-ux", "stage"], ["design-meaning", "stage"], ["design-visual", "stage"], ["decide-fixes", "decision"],
    ["review-701", "stage"], ["apply-design", "stage"], ["fixes", "stage"], ["review-workmap-ui", "stage"],
    ["docs-refresh", "stage"], ["promotion", "stage"]].map(([id, kind]) => ({ id, kind, title: id })),
  edges: [["fixes", "triage"], ["fixes", "decide-fixes"], ["decide-fixes", "triage"], ["docs-refresh", "fixes"],
    ["docs-refresh", "readiness"], ["promotion", "docs-refresh"], ["review-701", "fix-701"], ["docs-refresh", "review-701"],
    ["review-workmap-ui", "workmap-ui"], ["docs-refresh", "review-workmap-ui"], ["v701-contract", "fix-701", "verifies"],
    ["v701-failure", "fix-701", "verifies"], ["review-701", "v701-contract"], ["review-701", "v701-failure"],
    ["review-workmap-ui", "vui-security"], ["review-workmap-ui", "vui-correctness"], ["review-workmap-ui", "vui-ux"],
    ["apply-design", "design-meaning"], ["apply-design", "design-visual"], ["vui-security", "apply-design", "verifies"],
    ["vui-correctness", "apply-design", "verifies"], ["vui-ux", "apply-design", "verifies"],
    ["review-workmap-ui", "review-701"]].map(([from, to, type = "depends_on"]) => ({ from, to, type })),
};
// A work map of n nodes for the layout checks, the same for a seed: stages in rounds, each following one
// to three earlier stages (now and then one two or three rounds up, so long edges cross layers), about
// one in seven a verifier of a stage in the round above (docked when it follows nothing else), a
// decision now and then, two supersedes edges between stages of one round and one between rounds, and
// the goal; titles of two lengths.
function workmapSynth(n, seed) {
  let state = seed >>> 0;
  const random = () => {  // mulberry32
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const pick = (list) => list[Math.floor(random() * list.length)];
  const words = ["Audit", "Fix", "Review", "Plan", "Ship", "Verify", "Draft", "Merge", "Measure", "Refresh"];
  const nodes = [{ id: "goal", kind: "goal", title: "Ship the release" }], edges = [], rounds = [];
  const perRound = Math.max(2, Math.round(Math.sqrt(n) * 0.9));
  for (let i = 1; i < n; i++) {
    const round = Math.floor((i - 1) / perRound);
    (rounds[round] = rounds[round] || []).push(`s${i}`);
    const verifier = round > 0 && random() < 0.15;
    const kind = !verifier && random() < 0.08 ? "decision" : "stage";
    nodes.push({ id: `s${i}`, kind, title: `${pick(words)} ${pick(words).toLowerCase()} ${i}${random() < 0.4 ? " and the follow-up checks" : ""}` });
    if (!round) continue;
    if (verifier) {
      const target = pick(rounds[round - 1]);
      edges.push({ from: `s${i}`, to: target, type: "verifies" });
      if (random() < 0.3 && round > 1) edges.push({ from: `s${i}`, to: pick(rounds[round - 2]), type: "depends_on" });
      continue;
    }
    const count = 1 + Math.floor(random() * 3), seen = new Set();
    for (let j = 0; j < count; j++) {
      const back = random() < 0.2 && round > 1 ? 1 + Math.floor(random() * Math.min(3, round)) : 1;
      const target = pick(rounds[round - back]);
      if (seen.has(target)) continue;
      seen.add(target);
      edges.push({ from: `s${i}`, to: target, type: "depends_on" });
    }
  }
  const last = rounds.length - 1;
  for (const round of [Math.floor(last / 2), last]) {
    const ids = rounds[round].filter((id) => !edges.some((edge) => edge.type === "verifies" && edge.from === id));
    if (ids.length > 1) edges.push({ from: ids[ids.length - 1], to: ids[0], type: "supersedes" });
  }
  if (rounds.length > 2) edges.push({ from: pick(rounds[last]), to: pick(rounds[0]), type: "supersedes" });
  return { nodes, edges };
}
// Boxes as the stylesheet draws a layout at `scale` (section 6): rows stacked with the layout's gaps,
// those within the height budget (`tight`), those its edges want, or (`tight` "least") every gap at its
// least: 14 px of each, 26 where it holds a second straight end, fixed, 4 more for the least bend
// (scaled above 100 %), the rest scaled. Each entry at its x, a card heightOf(id) px tall at 100 % and,
// under a card with docked verifiers, a tray: 7 px, then a 24-px pill every 28 px, and room over the
// first layer and under the last for the edges over and under them. Returns workmapRoutes' geo, the
// boxes it was made of and the height drawn.
function workmapGeometry(layout, scale, heightOf, tight = true) {
  const rows = [], cards = new Map(), groups = new Map(), lanes = new Map(), trays = new Set(), next = new Map();
  let y = 12 + (layout.head ? 15 + (6 + 5 * (layout.head - 1)) * scale : 0);
  layout.layers.forEach((entries, k) => {
    const gap = (tight === "least" ? layout.least : tight ? layout.tight : layout.gaps)[k], two = layout.heads[k] === 2;
    if (k) y += (two ? 26 : 14) + Math.max(4, 4 * scale) + (gap - (two ? 30 : 18)) * scale;
    const top = y;
    let bottom = top;
    for (const [i, entry] of entries.entries()) {
      const left = 16 + entry.x * scale;
      if (i + 1 < entries.length) next.set(entry.id, 16 + entries[i + 1].x * scale);
      if (entry.lane) { lanes.set(entry.id, left); continue; }
      const card = { l: left, t: top, r: left + entry.w * scale, b: top + heightOf(entry.id) * scale };
      cards.set(entry.id, card);
      let foot = card.b;
      entry.verifiers.forEach((id, i) => {
        const pillTop = card.b + (7 + 28 * i) * scale;
        cards.set(id, { l: left + 36 * scale, t: pillTop, r: card.r - 6 * scale, b: pillTop + 24 * scale });
        foot = pillTop + 31 * scale;
      });
      if (entry.verifiers.length) trays.add(entry.id);
      groups.set(entry.id, { l: left, t: top, r: card.r, b: foot });
      bottom = Math.max(bottom, foot);
    }
    rows.push({ t: top, b: bottom });
    y = bottom;
  });
  if (layout.foot) y += 15 + (6 + 5 * (layout.foot - 1)) * scale;
  const host = (id) => layout.docked[id] || id;
  return { geo: { scale, rows, card: (id) => cards.get(id), group: (id) => groups.get(host(id)), lane: (id) => lanes.get(id),
                  next: (id) => (next.has(host(id)) ? next.get(host(id)) : null) }, cards, groups, trays, height: y + 12 };
}
// Points along a route's steps about every px, for the checks below.
function workmapSample(steps) {
  const points = [];
  let x = 0, y = 0;
  for (const [op, ...v] of steps) {
    if (op === "M") { [x, y] = v; points.push([x, y]); continue; }
    if (op === "V" || op === "H") {
      const [tx, ty] = op === "V" ? [x, v[0]] : [v[0], y];
      const count = Math.max(1, Math.ceil(Math.hypot(tx - x, ty - y)));
      for (let i = 1; i <= count; i++) points.push([x + (tx - x) * i / count, y + (ty - y) * i / count]);
      [x, y] = [tx, ty];
      continue;
    }
    const [x1, y1, x2, y2, x3, y3] = v;
    const count = Math.max(8, Math.ceil(Math.hypot(x1 - x, y1 - y) + Math.hypot(x2 - x1, y2 - y1) + Math.hypot(x3 - x2, y3 - y2)));
    for (let i = 1; i <= count; i++) {
      const t = i / count, u = 1 - t;
      points.push([u * u * u * x + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
                   u * u * u * y + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3]);
    }
    [x, y] = [x3, y3];
  }
  return points;
}
// Crossings of two polylines, pruned by their bounding boxes.
function workmapCrossings(a, b) {
  const bounds = (points) => points.reduce((r, [x, y]) => [Math.min(r[0], x), Math.min(r[1], y), Math.max(r[2], x), Math.max(r[3], y)],
    [Infinity, Infinity, -Infinity, -Infinity]);
  const [ax0, ay0, ax1, ay1] = bounds(a), [bx0, by0, bx1, by1] = bounds(b);
  if (ax1 < bx0 || bx1 < ax0 || ay1 < by0 || by1 < ay0) return 0;
  const side = (p, q, r) => (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]);
  let count = 0;
  for (let i = 0; i + 1 < a.length; i++) {
    const [p1, p2] = [a[i], a[i + 1]];
    if (Math.max(p1[0], p2[0]) < bx0 || Math.min(p1[0], p2[0]) > bx1 || Math.max(p1[1], p2[1]) < by0 || Math.min(p1[1], p2[1]) > by1) continue;
    for (let j = 0; j + 1 < b.length; j++) {
      const [q1, q2] = [b[j], b[j + 1]];
      if (Math.max(p1[0], p2[0]) < Math.min(q1[0], q2[0]) || Math.max(q1[0], q2[0]) < Math.min(p1[0], p2[0])) continue;
      if (Math.max(p1[1], p2[1]) < Math.min(q1[1], q2[1]) || Math.max(q1[1], q2[1]) < Math.min(p1[1], p2[1])) continue;
      const d1 = side(q1, q2, p1), d2 = side(q1, q2, p2), d3 = side(p1, p2, q1), d4 = side(p1, p2, q2);
      if ((d1 > 0) !== (d2 > 0) && (d3 > 0) !== (d4 > 0)) count += 1;  // half-open: through a sample point counts once
    }
  }
  return count;
}
// Lays a map out and routes its edges over synthetic boxes, then measures what the criteria name: the
// layout's crossings and the drawing's (every pair of edges, `dips` those that involve an edge below its
// layer), points of any edge inside a card, a pill or a tray (a route inside one tray is left out of its
// own), each end's straight piece, its angle and the angle where the curve before it meets it, the
// angle at which any curve leaves or meets the steps beside it (`smooth`), the order of the ends on
// every side against the order of their other ends, the foot an edge from a docked verifier leaves,
// the spacing in each layer, the width, the gaps and the height drawn, and the curves that run under
// WM_GEOM.jog px across (`jogs`) or bend at all (`bent`) between two given nodes (`between`).
function workmapRouteChecks(data, scale = 1.25, heightOf = (id) => 52 + (id.charCodeAt(id.length - 1) % 3) * 20,
                            { tight = true, between = null } = {}) {
  const nodes = data.nodes.map((item) => ({ status: "planned", level: "V0", needs: "V2", risk: "routine", settled: false,
    ready: false, attention: false, ...item }));
  const map = { nodes, edges: data.edges, closed: false, verdicts: [] };
  const t0 = performance.now();
  const layout = context.workmapLayers(nodes, data.edges);
  const ms = performance.now() - t0;
  const { geo, cards, groups, trays, height } = workmapGeometry(layout, scale, heightOf, tight);
  const edges = context.workmapEdgeList(context.workmapView(map, layout));
  const routes = context.workmapRoutes(edges, layout, geo);
  const points = routes.map((route) => workmapSample(route.steps));
  const out = { crossings: layout.crossings, drawn: 0, dips: 0, inside: [], shortest: Infinity, steepest: 0, joint: 0, smooth: 0, misordered: [],
                closest: Infinity, foot: [], spacing: Infinity, width: layout.width, packed: 0, gaps: layout.gaps.slice(1),
                tight: layout.tight.slice(1), least: layout.least.slice(1), heads: layout.heads.slice(1),
                foot_room: layout.foot, head_room: layout.head, height: Math.round(height),
                ms: Math.round(ms), kinds: {}, routes: routes.length, edges: edges.length, jogs: 0, bent: 0, level_step: Infinity,
                turn: null, above: 0, tray_run: 0 };
  const jog = vm.runInContext("WM_GEOM.jog", context) * scale;
  routes.forEach((route, i) => {
    out.kinds[route.kind] = (out.kinds[route.kind] || 0) + 1;
    const boxes = [...[...cards].map(([id, box]) => [id, box]), ...[...trays].map((id) => [id + "#tray", groups.get(id)])];
    for (const [id, box] of boxes) {
      if (route.kind === "tray" && (id === route.start + "#tray" || layout.docked[id] === route.start || id === route.start)) continue;
      // An edge side to side from a pill leaves through its own tray's edge.
      if (route.kind === "side" && route.pills.some((pill, end) => pill && id === (end ? route.end : route.start) + "#tray")) continue;
      if (points[i].some(([x, y]) => x > box.l + 0.5 && x < box.r - 0.5 && y > box.t + 0.5 && y < box.b - 0.5)) out.inside.push(`${route.edge.key} in ${id}`);
    }
    // The straight end: the last step, from where the step before it ended, and its angle.
    const last = route.steps.at(-1), before = route.steps.at(-2);
    const from = before[0] === "C" ? [before[5], before[6]] : before[0] === "M" ? [before[1], before[2]] : null;
    const end = points[i].at(-1), back = points[i].at(-4);
    const vertical = route.kind !== "tray" && route.kind !== "side";
    for (const curve of route.curves || []) if (curve.dx > 0.05 && curve.dx < jog) out.jogs += 1;
    if (between && between.includes(route.start) && between.includes(route.end) && route.steps.some(([op]) => op === "C")) out.bent += 1;
    const length = from ? (vertical ? Math.abs(last[1] - from[1]) : Math.abs(last[1] - from[0])) : Math.abs(last[1] - (before[0] === "V" ? before[1] : 0));
    out.shortest = Math.min(out.shortest, length);
    const angle = Math.atan2(Math.abs(vertical ? end[0] - back[0] : end[1] - back[1]), Math.abs(vertical ? end[1] - back[1] : end[0] - back[0])) * 180 / Math.PI;
    out.steepest = Math.max(out.steepest, angle);
    if (before[0] === "C") {  // where the curve meets the straight end: its last control point lies on that line
      const [, , , x2, y2, x3, y3] = before;
      out.joint = Math.max(out.joint, Math.atan2(Math.abs(vertical ? x3 - x2 : y3 - y2), Math.abs(vertical ? y3 - y2 : x3 - x2)) * 180 / Math.PI);
    }
    // One smooth curve: each cubic leaves the point before it and meets the step after it along the
    // route's line, vertical (level inside a tray), so no piece kinks where it joins the next.
    let at = [0, 0];
    for (const [op, ...v] of route.steps) {
      if (op === "C") {
        const [x1, y1, x2, y2, x3, y3] = v;
        const off = (dx, dy) => Math.atan2(Math.abs(vertical ? dx : dy), Math.abs(vertical ? dy : dx)) * 180 / Math.PI;
        out.smooth = Math.max(out.smooth, off(x1 - at[0], y1 - at[1]), off(x3 - x2, y3 - y2));
        at = [x3, y3];
      } else at = op === "M" ? v : op === "V" ? [at[0], v[0]] : [v[0], at[1]];
    }
    if (layout.docked[route.edge.start] && route.kind === "down") {
      const foot = groups.get(layout.docked[route.edge.start]).b + 1;
      if (Math.abs(route.steps[0][2] - foot) > 0.01 || route.steps[1][0] === "H") out.foot.push(route.edge.key);
    }
  });
  // Every side's ends in the order of their other ends, and no two closer than a px.
  const sides = new Map();
  for (const route of routes) {
    if (route.kind !== "down" && route.kind !== "up") continue;
    const lanesAt = (i) => (route.via.length ? geo.lane(route.via.at(i)) : null);
    const mid = (id) => (geo.group(id).l + geo.group(id).r) / 2;
    const ends = [[route.start, route.kind === "down" ? "foot" : "top", route.ports[0][0], lanesAt(0) ?? mid(route.end)],
                  [route.end, route.kind === "down" ? "top" : "foot", route.ports[1][0], lanesAt(-1) ?? mid(route.start)]];
    for (const [id, where, x, other] of ends) {
      const key = id + " " + where;
      if (!sides.has(key)) sides.set(key, []);
      sides.get(key).push([x, other]);
    }
  }
  for (const [key, list] of sides) {
    list.sort((p, q) => p[0] - q[0]);
    for (let i = 0; i + 1 < list.length; i++) {
      out.closest = Math.min(out.closest, list[i + 1][0] - list[i][0]);
      if (list[i + 1][1] < list[i][1] - 0.01) out.misordered.push(key);
    }
  }
  for (let i = 0; i < routes.length; i++) {
    for (let j = i + 1; j < routes.length; j++) {
      const count = workmapCrossings(points[i], points[j]);
      out.drawn += count;
      if (routes[i].kind === "dip" || routes[j].kind === "dip") out.dips += count;
    }
  }
  // Curves of one gap whose spans overlap, that run the same way and keep their order, on one level:
  // their flat middles would lie on one line. And how far any edge runs below the last layer's room.
  out.flat = 0;
  const byGap = new Map();
  for (const route of routes) for (const curve of route.curves || []) byGap.set(curve.k, [...(byGap.get(curve.k) || []), curve]);
  for (const curves of byGap.values()) {
    for (let a = 0; a < curves.length; a++) {
      for (let b = a + 1; b < curves.length; b++) {
        const p = curves[a], q = curves[b], way = Math.sign(p.bottom - p.top);
        if (Math.abs(p.bottom - p.top) <= 0.5 || Math.abs(q.bottom - q.top) <= 0.5 || Math.sign(q.bottom - q.top) !== way) continue;
        if (p.lo > q.hi + 2 || q.lo > p.hi + 2 || (p.top - q.top) * (p.bottom - q.bottom) < 0) continue;
        if (p.level === q.level) out.flat += 1;
      }
    }
  }
  // The height between two levels of a zone, where curves that run the same way are staggered.
  const zones = new Map();
  for (const route of routes) for (const curve of route.curves || []) zones.set(curve.zone, [...(zones.get(curve.zone) || []), curve]);
  for (const curves of zones.values()) {
    const levels = [...new Set(curves.map((curve) => curve.ya))].sort((a, b) => a - b);
    for (let i = 1; i < levels.length; i++) out.level_step = Math.min(out.level_step, levels[i] - levels[i - 1]);
  }
  out.level_step = out.level_step === Infinity ? null : Math.round(out.level_step * 100) / 100;
  out.below = Math.max(0, Math.round(Math.max(...points.flat().map(([, y]) => y)) - (height - 12)));
  out.above = Math.max(0, Math.round(12 - Math.min(...points.flat().map(([, y]) => y))));
  // How far an edge side to side runs inside its own pill's tray (px at 100 %): the tray's right padding,
  // never its left side, where the comb is.
  routes.forEach((route, i) => {
    if (route.kind !== "side") return;
    route.pills.forEach((pill, end) => {
      if (!pill) return;
      const box = groups.get(end ? route.end : route.start), inside = points[i].filter(([x, y]) => x > box.l && x < box.r && y > box.t && y < box.b);
      if (inside.length) out.tray_run = Math.max(out.tray_run, Math.round((Math.max(...inside.map(([x]) => x)) - Math.min(...inside.map(([x]) => x))) / scale * 10) / 10);
    });
  });
  // The room an edge side to side from a pill turns in: the width of its curve, from the level piece
  // it leaves.
  for (const route of routes) {
    route.steps.forEach(([op, ...v], i) => {
      if (route.kind !== "side" || op !== "C") return;
      const width = Math.abs(v[4] - route.steps[i - 1][1]);
      out.turn = out.turn === null ? width : Math.min(out.turn, width);
    });
  }
  // How far right of the middle the cards lie on average (px at 100 %): the side with more comes first.
  const boxes = layout.layers.flat().filter((entry) => !entry.lane);
  out.lean = Math.round(boxes.reduce((sum, entry) => sum + entry.x + entry.w / 2 - layout.width / 2, 0) / (boxes.length || 1) * 10) / 10;
  // The room between two entries as the layout keeps it: more between neighbours joined side to side
  // and right of a group whose tray holds an edge of its own.
  // and between neighbours joined from a pill, where the edge turns; right of a group whose tray holds
  // an edge of its own.
  const host = (id) => layout.docked[id] || id, joined = new Map(), trayed = new Set();
  for (const route of routes) {
    const room = route.pills && (route.pills[0] || route.pills[1]) ? 48 : 28;
    for (const pair of [route.start + "\n" + route.end, route.end + "\n" + route.start]) {
      if (route.kind === "side") joined.set(pair, Math.max(joined.get(pair) || 0, room));
    }
    if (route.kind === "tray") trayed.add(host(route.start));
  }
  const sep = (a, b) => Math.max(a.lane && b.lane ? 12 : a.lane || b.lane ? 16 : 20, joined.get(a.id + "\n" + b.id) || 0, trayed.has(a.id) ? 36 : 0);
  for (const entries of layout.layers) {
    if (entries.length) out.packed = Math.max(out.packed, entries.reduce((sum, entry, i) => sum + entry.w + (i ? sep(entries[i - 1], entry) : 0), 0));
    entries.forEach((entry, i) => {
      out.spacing = Math.min(out.spacing, i ? entry.x - entries[i - 1].x - entries[i - 1].w - sep(entries[i - 1], entry) : entry.x);
      if (entry.x + entry.w > layout.width) out.spacing = -1;
    });
  }
  out.shortest = Math.round(out.shortest * 100) / 100;
  out.steepest = Math.round(out.steepest * 100) / 100;
  out.joint = Math.round(out.joint * 100) / 100;
  out.smooth = Math.round(out.smooth * 100) / 100;
  if (out.closest === Infinity) out.closest = null;
  return out;
}

// A map written compactly: nodes "id:kind" (g goal, s stage, d decision, q question) and edges "a>b"
// (a depends_on b), "a~b" (verifies), "a^b" (supersedes) and "a!b" (contradicts), in the payload's order.
function workmapShape(nodes, edges) {
  const kinds = { g: "goal", s: "stage", d: "decision", q: "question" }, types = { ">": "depends_on", "~": "verifies", "^": "supersedes", "!": "contradicts" };
  return { nodes: nodes.split(" ").map((item) => { const [id, kind] = item.split(":"); return { id, kind: kinds[kind], title: id }; }),
           edges: edges.split(" ").map((item) => { const [, from, type, to] = /^([^>~^!]+)([>~^!])(.+)$/.exec(item); return { from, to, type: types[type] }; }) };
}
// The structures of three of the verification's maps (ids replaced, order kept): a wide fan-in and
// fan-out, a plan as sessions write them, and seven verifier trays. Their fewest crossings: 17 and 2
// (exact, by dynamic programming over the permutations of neighbouring layers) and 6 (the best of 300
// random restarts of barycentre sweeps and sifting).
const WORKMAP_ORDERS = {
  fan: [workmapShape("f0:g f1:s f2:s f3:s f4:s f5:s f6:s f7:s f8:s f9:s f10:s f11:s f12:s f13:s f14:s f15:s f16:s f17:s f18:s f19:s f20:s f21:s f22:d",
    "f3>f1 f4>f1 f5>f1 f6>f1 f7>f1 f8>f1 f9>f1 f10>f1 f11>f1 f3>f2 f7>f2 f11>f2 f12>f3 f12>f4 f12>f5 f12>f6 f12>f7 f12>f8 f12>f9 f12>f10 "
    + "f12>f11 f13>f3 f13>f4 f13>f7 f14>f6 f14>f11 f15>f12 f16>f12 f17>f12 f18>f12 f19>f12 f20>f12 f15>f13 f18>f13 f18>f14 f19>f14 "
    + "f21>f15 f21>f16 f21>f17 f21>f18 f21>f19 f21>f20 f22>f21"), 17],
  mixed: [workmapShape("m0:s m1:s m2:s m3:s m4:q m5:s m6:s m7:d m8:s m9:s m10:s m11:s m12:s m13:s m14:s m15:s m16:s m17:s m18:g m19:s",
    "m4>m0 m7>m4 m5>m0 m6>m0 m10>m7 m10^m6 m1~m10 m2~m10 m11>m5 m11>m7 m12>m7 m3~m12 m13>m12 m13>m10 m14>m10 m15>m11 m15>m10 m15>m14 "
    + "m8~m15 m8>m5 m9>m5 m16>m15 m16>m13 m16>m9 m16>m0 m17>m16 m18>m16 m18>m17 m19>m17"), 2],
  trays: [workmapShape("t0:g t1:s t2:s t3:s t4:s t5:s t6:s t7:s t8:s t9:s t10:s t11:s t12:s t13:s t14:s t15:s t16:s t17:s t18:s t19:s t20:s t21:s "
    + "t22:s t23:s t24:s t25:s t26:s t27:s",
    "t3~t1 t4~t1 t5~t2 t18>t1 t6~t18 t7~t18 t8~t18 t19>t1 t19>t2 t9~t19 t20>t2 t22>t18 t22>t19 t10~t22 t11~t22 t12~t22 t13~t22 t23>t19 "
    + "t23>t20 t14~t23 t15~t23 t21~t18 t21>t2 t16~t19 t16~t20 t24~t22 t24>t23 t25>t22 t25>t10 t26>t22 t26>t23 t26>t14 t27>t25 t27>t26 t17~t27"), 6],
};
// Alternatives as a session records them: three options after one brief, the second supersedes the
// first, the third contradicts the second, one decision reads all three and a cost model the second.
const WORKMAP_ALT = workmapShape("goal:g brief:s a:s b:s c:s compare:d cost:s build:s",
  "a>brief b>brief c>brief b^a c!b compare>a compare>b compare>c cost>b build>compare build>cost goal>build");
// Edges between nodes of one layer beside edges down: three of x, y, z joined in a ring, so one of
// them cannot be between neighbours and runs below the layer, past the edges down from the one between.
const WORKMAP_BESIDE = workmapShape("goal:g r:s x:s y:s z:s p:s q:s",
  "x>r y>r z>r x^y y!z z^x p>x p>y q>y q>z goal>p goal>q");
// A tray of four verifiers whose node and verifiers one card follows: the five edges between the two
// run straight down side by side.
const WORKMAP_COMB = workmapShape("goal:g h:s v1:s v2:s v3:s v4:s t:s u:s",
  "v1~h v2~h v3~h v4~h t>h t>v1 t>v2 t>v3 t>v4 u>t goal>u");
// A tray whose verifiers hold edges of their own (one supersedes another, one contradicts the node they
// check) between two neighbours: the edges round the tray's right edge keep clear of them at every zoom.
const WORKMAP_TRAY = workmapShape("goal:g r:s m:s a:s n:s v1:s v2:s v3:s",
  "m>r a>r n>r v1~a v2~a v3~a v2^v1 v3!a goal>m goal>a goal>n");
// Three goals joined in a ring by edges between them: one runs below the last layer, in the room the
// layout keeps under it.
const WORKMAP_GOALS = workmapShape("g1:g g2:g g3:g s:s t:s", "g1>s g2>s g3>t g1^g2 g2!g3 g3^g1");
// The release map as it stood at revision 217 (ids, kinds and edges): the shape the owner watches, which
// the stability checks grow by one node at a time.
const WORKMAP_LIVE = workmapShape("goal:g triage:s readiness:s decide-fixes:d fixes:s docs-refresh:s promotion:s fix-701:s review-701:s "
  + "workmap-ui:s review-workmap-ui:s v701-contract:s v701-failure:s vui-security:s vui-correctness:s vui-ux:s design-meaning:s "
  + "design-visual:s apply-design:s edge-routing:s v701-4-redaction:s v701-4-regression:s v-edge-routing:s fix-701-5:s edge-routing-2:s "
  + "v701-5-redaction:s v701-5-regression:s v-edge-routing-2:s fix-628:s fix-722:s fix-456:s fix-754:s fix-592-440:s fix-scan-test:s "
  + "docs-delta:s fix-701-6:s sync-setup:s v-fix-456:s edge-routing-3:s v-docs-delta:s v-fix-592-440:s v-fix-628:s v-fix-722:s",
  "fixes>triage fixes>decide-fixes decide-fixes>triage docs-refresh>fixes docs-refresh>readiness promotion>docs-refresh review-701>fix-701 "
  + "docs-refresh>review-701 review-workmap-ui>workmap-ui docs-refresh>review-workmap-ui v701-contract~fix-701 v701-failure~fix-701 "
  + "review-701>v701-contract review-701>v701-failure review-workmap-ui>vui-security review-workmap-ui>vui-correctness review-workmap-ui>vui-ux "
  + "apply-design>design-meaning apply-design>design-visual vui-security~apply-design vui-correctness~apply-design vui-ux~apply-design "
  + "review-workmap-ui>review-701 review-workmap-ui>edge-routing v701-4-redaction~fix-701 v701-4-regression~fix-701 review-701>v701-4-redaction "
  + "review-701>v701-4-regression v-edge-routing~edge-routing review-workmap-ui>v-edge-routing fix-701-5^fix-701 review-701>fix-701-5 "
  + "edge-routing-2^edge-routing review-workmap-ui>edge-routing-2 v701-5-redaction~fix-701-5 v701-5-regression~fix-701-5 "
  + "review-701>v701-5-redaction review-701>v701-5-regression v-edge-routing-2~edge-routing-2 review-workmap-ui>v-edge-routing-2 "
  + "fix-628>decide-fixes fix-722>decide-fixes fix-456>decide-fixes fix-754>decide-fixes fix-592-440>decide-fixes fix-scan-test>decide-fixes "
  + "fixes>fix-628 fixes>fix-722 fixes>fix-456 fixes>fix-754 fixes>fix-592-440 fixes>fix-scan-test docs-refresh>docs-delta "
  + "fix-701-6^fix-701-5 review-701>fix-701-6 v-fix-456~fix-456 fixes>v-fix-456 edge-routing-3^edge-routing-2 review-workmap-ui>edge-routing-3 "
  + "v-docs-delta~docs-delta docs-refresh>v-docs-delta v-fix-592-440~fix-592-440 fixes>v-fix-592-440 v-fix-628~fix-628 fixes>v-fix-628 "
  + "v-fix-722~fix-722 fixes>v-fix-722");
// The second verification's map of attempts (its ids): two chains of three attempts in one layer, an
// edge between two neighbours each way, a docked verifier's edge to the card beside its group, a
// contradiction across the layer, and two nodes set aside (copy-v1 superseded, subject-a refuted).
const WORKMAP_ATTEMPTS = workmapShape("goal:g brief:s audience:s copy-v1:s copy-v2:s copy-v3:s subject-a:s subject-b:s timing:s legal-note:s "
  + "tone-check:s legal-check:s review-copy:s ab-subject:s send-plan:s launch:s launch-rv:s launch-2:s launch-3:s readout:d",
  "copy-v1>brief copy-v2>brief copy-v3>brief copy-v2^copy-v1 copy-v3^copy-v2 subject-a>audience subject-b>audience subject-b!subject-a "
  + "timing>brief timing>audience legal-note>audience legal-note!copy-v1 tone-check~copy-v2 tone-check!copy-v3 legal-check~legal-note "
  + "review-copy>copy-v3 review-copy>tone-check ab-subject>subject-a ab-subject>subject-b send-plan>timing send-plan>copy-v3 launch>review-copy "
  + "launch>ab-subject launch>send-plan launch>legal-note launch>legal-check launch-rv~launch launch-2>ab-subject launch-2>send-plan "
  + "launch-2^launch launch-3>send-plan launch-3>review-copy launch-3^launch-2 readout>launch-3 readout>launch-rv");
WORKMAP_ATTEMPTS.nodes.forEach((item) => { item.status = { "copy-v1": "superseded", "subject-a": "refuted" }[item.id] || "planned"; });
// Docked verifiers whose edges go to the cards beside their group, one on the left and two on the right:
// they leave the pill's side, past the tray's comb on the left, and the two into one card do not cross.
const WORKMAP_PILL = workmapShape("goal:g p:s a:s b:s c:s v1:s v2:s v3:s",
  "a>p b>p c>p v1~b v2~b v3~b v1!a v2^c v3!c goal>a goal>b goal>c");
// Three nodes of the top layer in a ring of edges: the one between two that are not neighbours runs
// over the layer, where nothing enters from above, in the room kept over it.
const WORKMAP_OVER = workmapShape("goal:g x:s y:s z:s p:s q:s", "x^y y!z z^x p>x p>y q>y q>z goal>p goal>q");
// Three goals in a ring, the middle one with nothing entering it: the edge between the other two runs
// over the last layer, past the curves into their tops, which end their bends above it.
const WORKMAP_GOALS_OVER = workmapShape("g1:g g2:g g3:g s:s t:s", "g1>s g3>t g1^g2 g2!g3 g3^g1");
// A review card that takes five edges from a tray (a node and its four verifiers) that is not above it:
// one trunk, one arrowhead.
const WORKMAP_FAR = workmapShape("goal:g a:s b:s c:s d:s h:s v1:s v2:s v3:s v4:s x:s y:s z:s r:s s:s",
  "h>a v1~h v2~h v3~h v4~h x>b y>c z>d r>h r>v1 r>v2 r>v3 r>v4 r>x r>y r>z s>a goal>r goal>s");
// A tall map: twelve rounds of a fan out and back in, every gap wanting to bend far.
const WORKMAP_TALL = (() => {
  const nodes = ["goal:g"], edges = [];
  for (let k = 0; k < 12; k++) {
    for (let i = 0; i < 4; i++) {
      nodes.push(`n${k}x${i}:s`);
      if (k) for (const j of [i, (i + 1) % 4, (i + 3) % 4]) edges.push(`n${k}x${i}>n${k - 1}x${j}`);
    }
  }
  for (let i = 0; i < 4; i++) edges.push(`goal>n11x${i}`);
  return workmapShape(nodes.join(" "), edges.join(" "));
})();

if (scenario === "workmap_routes") {
  const out = { release: workmapRouteChecks(RELEASE_SHAPE), release_100: workmapRouteChecks(RELEASE_SHAPE, 1, () => 72),
                release_wide: workmapRouteChecks(RELEASE_SHAPE, 1.25, undefined, { tight: false }),
                fixture: workmapRouteChecks({ nodes: workmapNodes(), edges: WORKMAP_EDGES }) };
  for (const n of [20, 40, 60]) out[`synth_${n}`] = workmapRouteChecks(workmapSynth(n, n));
  // Same-layer edges at every zoom from 50 to 160 %, and a tray of four verifiers feeding one card.
  for (const scale of [0.5, 0.8, 1, 1.25, 1.6]) {
    out[`alt_${scale * 100}`] = workmapRouteChecks(WORKMAP_ALT, scale);
    out[`beside_${scale * 100}`] = workmapRouteChecks(WORKMAP_BESIDE, scale);
    out[`tray_${scale * 100}`] = workmapRouteChecks(WORKMAP_TRAY, scale, () => 72);
    out[`goals_${scale * 100}`] = workmapRouteChecks(WORKMAP_GOALS, scale, () => 72);
  }
  // A dip from a to b past m beside an edge from a that runs on past b, laid out by hand: the dip leaves
  // a's foot at the end facing b, past that edge, and the curves of the gap bend below the dip, so the
  // edge from a runs under it; the two crossings are the dip's over the edge down from m and that edge's
  // with the one from a, as the order counts them.
  const card = (id, x) => ({ id, lane: false, verifiers: [], x, w: 232 });
  const dipLayout = { layers: [[card("a", 0), card("m", 252), card("b", 504)], [card("d", 252), card("c", 760)]],
    layer: { a: 0, m: 0, b: 0, d: 1, c: 1 }, docked: {}, via: {}, derived: [], crossings: 2, gaps: [0, 56], tight: [0, 56], heads: [0, 2],
    width: 992, foot: 0, beside: { "a|b|supersedes": "dip" } };
  const dipEdges = [{ key: "a|b|supersedes", type: "supersedes", start: "a", end: "b", via: [] },
                    { key: "c|a|depends_on", type: "depends_on", start: "a", end: "c", via: [] },
                    { key: "d|m|depends_on", type: "depends_on", start: "m", end: "d", via: [] }];
  out.dip_under = [0.5, 1, 1.6].map((scale) => {
    const routes = context.workmapRoutes(dipEdges, dipLayout, workmapGeometry(dipLayout, scale, () => 72).geo);
    const points = routes.map((route) => workmapSample(route.steps));
    let drawn = 0;
    for (let i = 0; i < routes.length; i++) for (let j = i + 1; j < routes.length; j++) drawn += workmapCrossings(points[i], points[j]);
    return drawn;
  });
  out.comb = workmapRouteChecks(WORKMAP_COMB, 1.25, () => 72, { between: ["h", "t"] });
  out.comb_wide = workmapRouteChecks(WORKMAP_COMB, 1, () => 72, { tight: false, between: ["h", "t"] });
  for (const scale of [0.5, 0.8, 1, 1.25, 1.6]) {
    out[`attempts_${scale * 100}`] = workmapRouteChecks(WORKMAP_ATTEMPTS, scale);
    out[`pill_${scale * 100}`] = workmapRouteChecks(WORKMAP_PILL, scale, () => 72);
    out[`over_${scale * 100}`] = workmapRouteChecks(WORKMAP_OVER, scale, () => 72);
    out[`far_${scale * 100}`] = workmapRouteChecks(WORKMAP_FAR, scale, () => 72, { between: ["h", "r"] });
    out[`goals_over_${scale * 100}`] = workmapRouteChecks(WORKMAP_GOALS_OVER, scale, () => 72);
  }
  // Every map at 50 % with every gap at its least: the straight ends and the least bend still hold.
  for (const [name, shape, height] of [["release", RELEASE_SHAPE], ["fixture", { nodes: workmapNodes(), edges: WORKMAP_EDGES }],
    ["synth_40", workmapSynth(40, 40)], ["synth_60", workmapSynth(60, 60)], ["beside", WORKMAP_BESIDE], ["tray", WORKMAP_TRAY, () => 72],
    ["goals", WORKMAP_GOALS, () => 72], ["attempts", WORKMAP_ATTEMPTS], ["pill", WORKMAP_PILL, () => 72], ["over", WORKMAP_OVER, () => 72],
    ["live", WORKMAP_LIVE], ["tall", WORKMAP_TALL, () => 72], ["goals_over", WORKMAP_GOALS_OVER, () => 72]]) {
    out[`${name}_least`] = workmapRouteChecks(shape, 0.5, height, { tight: "least" });
  }
  // The trunks into the review card: h is settled and its verifiers are not, so the head of the trunk
  // from its tray is hollow; with the first layer settled too, the critical path runs through a
  // verifier's edge, not the trunk's first, and the trunk is drawn on it.
  const farNodes = WORKMAP_FAR.nodes.map((item) => ({ status: "planned", level: "V0", needs: "V2", risk: "routine", ready: false,
    attention: false, settled: false, ...item,
    ...(["h", "a", "b", "c", "d"].includes(item.id) ? { status: "validated", level: "V2", settled: true } : {}) }));
  const far = context.workmapLayers(farNodes, WORKMAP_FAR.edges);
  const farList = context.workmapEdgeList(context.workmapView({ nodes: farNodes, edges: WORKMAP_FAR.edges, closed: false, verdicts: [] }, far));
  out.trunks = farList.filter((edge) => edge.end === "r").map((edge) => ({ start: edge.start, starts: edge.starts, keys: edge.keys.length,
    marker: edge.marker, path: edge.path }));
  // One node added to the live structure, laid out from the layout before it: the other rows keep their
  // order, no card moves more than one place, and it crosses no more than the same map opened afresh.
  const grow = (added, edges) => {
    const base = WORKMAP_LIVE.nodes.map((item) => ({ status: "planned", ...item })), before = context.workmapLayers(base, WORKMAP_LIVE.edges);
    const nodes = [...base, ...added.map((id) => ({ id, kind: "stage", title: id, status: "planned" }))];
    const after = context.workmapLayers(nodes, [...WORKMAP_LIVE.edges, ...edges], before), fresh = context.workmapLayers(nodes, [...WORKMAP_LIVE.edges, ...edges]);
    const rows = (layout) => layout.layers.map((entries) => entries.filter((entry) => !entry.lane).map((entry) => entry.id));
    const at = new Map();
    rows(after).forEach((row, k) => row.forEach((id, i) => at.set(id, [k, i])));
    let reordered = 0, slot = 0, lean = 0;
    rows(before).forEach((row, k) => {
      const kept = row.filter((id) => at.has(id) && at.get(id)[0] === k), now = rows(after)[k].filter((id) => kept.includes(id));
      if (now.join() !== kept.join() && !added.some((id) => rows(after)[k].includes(id))) reordered += 1;
      row.forEach((id, i) => { if (at.has(id) && at.get(id)[0] === k) slot = Math.max(slot, Math.abs(at.get(id)[1] - i)); });
    });
    const mid = (layout) => new Map(layout.layers.flat().filter((entry) => !entry.lane).map((entry) => [entry.id, entry.x + entry.w / 2]));
    const [x0, x1] = [mid(before), mid(after)];
    for (const [id, x] of x0) lean += Math.sign(x1.get(id) - x1.get(rows(after)[0][0])) === Math.sign(x - x0.get(rows(before)[0][0])) ? 0 : 1;
    return { reordered, slot, crossings: after.crossings, fresh: fresh.crossings, turned: lean };
  };
  out.grow = {
    attempt: grow(["fix-701-7"], [{ from: "fix-701-7", to: "fix-701-6", type: "supersedes" }, { from: "review-701", to: "fix-701-7", type: "depends_on" }]),
    verified: grow(["fix-701-7", "v701-7-a", "v701-7-b"], [{ from: "fix-701-7", to: "fix-701-6", type: "supersedes" },
      { from: "review-701", to: "fix-701-7", type: "depends_on" }, { from: "v701-7-a", to: "fix-701-7", type: "verifies" },
      { from: "review-701", to: "v701-7-a", type: "depends_on" }, { from: "v701-7-b", to: "fix-701-7", type: "verifies" },
      { from: "review-701", to: "v701-7-b", type: "depends_on" }]),
    verifier: grow(["v701-6-a"], [{ from: "v701-6-a", to: "fix-701-6", type: "verifies" }, { from: "review-701", to: "v701-6-a", type: "depends_on" }]),
    routing: grow(["edge-routing-4"], [{ from: "edge-routing-4", to: "edge-routing-3", type: "supersedes" },
      { from: "review-workmap-ui", to: "edge-routing-4", type: "depends_on" }]),
    stage: grow(["release-notes"], [{ from: "release-notes", to: "docs-refresh", type: "depends_on" }]),
  };
  // The order on the verification's maps, and the height budget on a tall one: the gaps within it, and
  // the height drawn at 50, 100 and 160 % against the layout before (28 px a gap, scaled).
  out.orders = Object.fromEntries(Object.entries(WORKMAP_ORDERS).map(([name, [shape, fewest]]) => {
    const laid = context.workmapLayers(shape.nodes, shape.edges), boxes = laid.layers.flat().filter((entry) => !entry.lane);
    return [name, { crossings: laid.crossings, fewest,
                    lean: Math.round(boxes.reduce((sum, entry) => sum + entry.x + entry.w / 2 - laid.width / 2, 0) / boxes.length * 10) / 10 }];
  }));
  const tall = context.workmapLayers(WORKMAP_TALL.nodes, WORKMAP_TALL.edges);
  out.tall = { layers: tall.layers.length, tight: tall.tight.slice(1).reduce((sum, gap) => sum + gap, 0),
               least: tall.least.slice(1).reduce((sum, gap) => sum + gap, 0),
               wanted: tall.gaps.slice(1).reduce((sum, gap) => sum + gap, 0),
               budget: vm.runInContext("WM_GEOM.gap.budget", context) * (tall.layers.length - 1),
               drawn: [0.5, 1, 1.6].map((scale) => [workmapGeometry(tall, scale, () => 72).height,
                                                   12 + 12 + tall.layers.length * 72 * scale + (tall.layers.length - 1) * 28 * scale]) };
  // The same structure, any statuses but set aside: the layout reads ids, kinds, which nodes are set aside
  // and edges only. A node set aside sends no derived connector to the goal, and changes the structure.
  const aside = (item) => ["refuted", "superseded"].includes(item.status);
  const restless = workmapNodes().map((item, i) => (aside(item) ? item
    : { ...item, status: ["prepared", "validated", "needs_attention"][i % 3], settled: i % 2 === 0 }));
  out.stable = JSON.stringify(context.workmapLayers(workmapNodes(), WORKMAP_EDGES)) === JSON.stringify(context.workmapLayers(restless, WORKMAP_EDGES));
  const awake = workmapNodes().map((item) => (aside(item) ? { ...item, status: "planned" } : item));
  out.aside = { derived: context.workmapLayers(workmapNodes(), WORKMAP_EDGES).derived.map(([id]) => id),
                awake: context.workmapLayers(awake, WORKMAP_EDGES).derived.map(([id]) => id),
                structure: vm.runInContext("workmapStructure", context)({ nodes: workmapNodes(), edges: WORKMAP_EDGES })
                  !== vm.runInContext("workmapStructure", context)({ nodes: awake, edges: WORKMAP_EDGES }) };
  // A verifier with an input besides the node it checks is a card of its own; one with none docks.
  const extra = context.workmapLayers(["a", "b", "v", "w"].map((id) => ({ id, kind: "stage" })),
    [{ from: "b", to: "a", type: "depends_on" }, { from: "v", to: "b", type: "verifies" }, { from: "v", to: "a", type: "depends_on" },
     { from: "w", to: "b", type: "verifies" }]);
  out.docking = { docked: extra.docked, layers: extra.layers.map((entries) => entries.filter((entry) => !entry.lane).map((entry) => entry.id)) };
  // Edges that climb, that join two nodes of one layer, and that join two pills of one tray.
  const odd = { nodes: ["a", "b", "c", "d", "e", "v1", "v2"].map((id) => ({ id, kind: "stage", title: id })), edges: [
    { from: "b", to: "a", type: "depends_on" }, { from: "c", to: "b", type: "depends_on" }, { from: "d", to: "a", type: "depends_on" },
    { from: "e", to: "a", type: "depends_on" }, { from: "a", to: "c", type: "contradicts" }, { from: "e", to: "d", type: "supersedes" },
    { from: "v1", to: "b", type: "verifies" }, { from: "v2", to: "b", type: "verifies" }, { from: "v2", to: "v1", type: "supersedes" }] };
  out.odd = workmapRouteChecks(odd);
  // The largest map the page draws, for the time its layout takes.
  const large = workmapSynth(400, 400), started = performance.now();
  const laid = context.workmapLayers(large.nodes, large.edges);
  out.large = { ms: Math.round(performance.now() - started), drawn: !laid.omitted, nodes: large.nodes.length };
  await finish(out);
}

if (scenario === "workmap_layout") {
  const nodes = workmapNodes(), out = {};
  const layout = context.workmapLayers(nodes, WORKMAP_EDGES);
  const ids = (entries) => entries.filter((entry) => !entry.lane).map((entry) => entry.id).sort();
  out.layers = layout.layers.map(ids);
  out.lanes = layout.layers.map((entries) => entries.filter((entry) => entry.lane).length);
  out.lane_ids = [...new Set(layout.layers.flat().filter((entry) => entry.lane).map((entry) => entry.id.replace(/\d+$/, "")))];
  out.trays = Object.fromEntries(layout.layers.flat().filter((entry) => !entry.lane && entry.verifiers.length)
    .map((entry) => [entry.id, entry.verifiers]));
  out.docked = layout.docked;
  out.via = Object.fromEntries(Object.entries(layout.via).map(([key, lanes]) => [key, lanes.length]));
  out.derived = layout.derived;
  out.crossings = layout.crossings;
  // A verifier that also follows a node in its target's layer stays a card of its own, a layer lower.
  const own = context.workmapLayers([{ id: "a", kind: "stage" }, { id: "b", kind: "stage" }, { id: "v", kind: "stage" }],
    [{ from: "v", to: "a", type: "verifies" }, { from: "v", to: "b", type: "depends_on" }]);
  out.own = { layers: own.layers.map(ids), docked: own.docked };
  const cycle = context.workmapLayers([{ id: "p", kind: "stage" }, { id: "r", kind: "stage" }],
    [{ from: "p", to: "r", type: "depends_on" }, { from: "r", to: "p", type: "depends_on" }]);
  out.cycle = cycle.layers.map(ids);
  out.empty = context.workmapLayers([], []);
  // A node whose id looks like a lane is a node: lanes have ids no node can have.
  const lookalike = context.workmapLayers(["wm-lane-0", "a", "b", "c"].map((id) => ({ id, kind: "stage" })),
    [{ from: "c", to: "b", type: "depends_on" }, { from: "b", to: "a", type: "depends_on" }, { from: "c", to: "wm-lane-0", type: "depends_on" }]);
  out.lookalike = { layers: lookalike.layers.map(ids), lanes: lookalike.layers.flat().filter((entry) => entry.lane).map((entry) => entry.id) };
  // A map too large to draw is not laid out; the page says so and keeps the summary.
  const many = Array.from({ length: 401 }, (_, index) => ({ id: `n${index}`, kind: "stage" }));
  out.omitted = context.workmapLayers(many, []).omitted;
  const data = { nodes, edges: WORKMAP_EDGES, closed: false, verdicts: WORKMAP_VERDICTS };
  const view = context.workmapView(data, layout), index = view.index;
  out.categories = Object.fromEntries(view.categories);
  out.counts = view.counts;
  out.who = Object.fromEntries(view.who);
  out.reasons = Object.fromEntries(["decide", "fix", "audit"].map((id) => [id, context.workmapReason(index.byId.get(id), view)]));
  out.brief = context.workmapReason(index.byId.get("fix"), view, true);
  out.chain = { length: view.chain.length, band: [...view.chain.band], frontier: view.chain.frontier,
                edges: [...view.chain.edges].sort() };
  // Verdicts: the store's own, on the result the target holds now; nothing derived from findings.
  const fix = index.byId.get("fix");
  const verifier = (status, findings, pin) => ({ id: "v", status, findings, inputs: pin ? { fix: pin } : {} });
  const recorded = (verdict, sha) => new Map([["v\nfix", { verifier: "v", target: "fix", verdict, result_sha256: sha }]]);
  out.verdicts = [
    [verifier("prepared", null, WORKMAP_FIX)], [verifier("planned", null)],
    [verifier("validated", {}, WORKMAP_FIX), recorded("clean", WORKMAP_FIX)],
    [verifier("validated", { high: 1 }, WORKMAP_FIX), recorded("found", WORKMAP_FIX)],
    [verifier("validated", { high: 1 }, "0".repeat(64)), recorded("found", "0".repeat(64))],  // judged an older result
    [verifier("validated", {}, "0".repeat(64))],  // given an older result, no verdict
    [verifier("validated", {}, WORKMAP_FIX)],  // the store recorded none (the target was not a verdict target)
    [verifier("validated", { critical: 2 })],  // no pin: no verdict, whatever its findings
    [verifier("refuted", {})],
    [verifier("prepared", null, "0".repeat(64))],  // running on an older result: no verdict on this one is coming
  ].map(([item, map]) => context.workmapVerdict(item, fix, map || new Map()));
  // Who acts while a verifier runs on an older result, is only ready, or checks a disputed node.
  const scene = (target, verifier) => {
    const pair = { nodes: [target, verifier], edges: [{ from: verifier.id, to: target.id, type: "verifies" }], closed: false };
    const sceneView = context.workmapView(pair, context.workmapLayers(pair.nodes, pair.edges));
    return [sceneView.who.get(target.id), context.workmapReason(target, sceneView)];
  };
  const t = { ...nodes[3], id: "t", ready: false }, running = { ...nodes[4], id: "w", inputs: { t: WORKMAP_FIX } };
  out.scenes = {
    checking: scene(t, running),
    older_pin: scene(t, { ...running, inputs: { t: "0".repeat(64) } }),
    ready: scene(t, { ...running, status: "planned", ready: true, inputs: {} }),
    disputed: scene({ ...t, status: "disputed", level: "V2" }, running),
    closed: context.workmapView({ ...data, closed: true }, layout).who.size,
  };
  out.words = ["goal", "build", "audit", "old"].map((id) => context.workmapStatusWord(index.byId.get(id), data));
  out.closed_goal = context.workmapStatusWord(index.byId.get("goal"), { closed: true });
  out.closed_prepared = context.workmapStatusWord(index.byId.get("build"), { closed: true });
  out.badges = ["triage", "fix", "build", "audit", "old", "goal"].map((id) => context.workmapLevelBadge(index.byId.get(id)));
  out.level_sentences = [index.byId.get("audit"), { ...index.byId.get("audit"), result_status: "failed" },
    { ...index.byId.get("audit"), result_status: "input_required" }, { ...index.byId.get("audit"), result_status: "completed" },
    index.byId.get("docs"), index.byId.get("fix")].map((item) => context.workmapLevelSentence(item));
  out.invalid = [2, 5, 7].map((count) => context.wmInvalid(count));
  out.ages = [0, 13, 125, 3 * 1440].map((minutes) => context.workmapAge(workmapAgo(minutes)));
  // A running card's age, every minute of two days and every hour up to 999 days: its longest text,
  // which the card's box for it (.wm-age, in ch of a monospaced face) must hold.
  const started = Date.parse("2026-11-27T12:00:00Z"), since = new Date(started).toISOString();
  let longest = "";
  for (let minute = 0; minute <= 999 * 1440; minute += minute < 2 * 1440 ? 1 : 60) {
    const text = context.workmapCardAge(since, started + minute * 60000);
    if (text.length > longest.length) longest = text;
  }
  out.card_ages = { longest, samples: [0, 59, 60, 119, 615, 1439, 1440, 999 * 1440]
    .map((minutes) => context.workmapCardAge(since, started + minutes * 60000)) };
  const noon = Date.parse("2026-11-27T12:00:00Z");
  out.when = ["2026-11-27T09:05:00Z", "2026-11-18T20:40:00Z", "2025-11-18T20:40:00Z"].map((iso) => context.workmapWhen(iso, noon));
  out.links = ["#workmap", "#workmap/task%3A1", "#workmap/ws-1/task%3A1", "#workmap/ws/%E0%A4%A", "#history/x", "#workmapx"]
    .map((hash) => context.workmapLink(hash));
  out.hashes = [context.workmapHash("ws-1", "task:1"), context.workmapHash("", "a/b")];
  out.latest = context.workmapLatest(workmapData("/ui/api/workmap/maps")[1].workspaces);
  await finish(out);
}

if (scenario === "workmap" || scenario === "workmap_signout") {
  const texts = (element) => [element.textContent, ...(element.children || []).flatMap((child) => (child.nodeType === 3
    ? [child.textContent] : texts(child)))].filter(Boolean);
  const walk = (element, found = [], test = (child) => String(child.className || "").split(" ").includes("wm-node")) => {
    for (const child of element.children || []) {
      if (child.nodeType !== 3 && test(child)) found.push(child);
      if (child.nodeType !== 3) walk(child, found, test);
    }
    return found;
  };
  const cards = () => Object.fromEntries(walk(byId("wm-layers")).map((card) => [card.dataset.id, card]));
  const cardOf = (id) => cards()[id];
  const classed = (element, name) => walk(element, [], (child) => String(child.className || "").split(" ").includes(name));
  const describe = (card) => ({ status: card.dataset.status, cat: card.dataset.cat, pill: card.className.includes("wm-pill"),
    text: texts(card), pressed: card.attrs["aria-pressed"], path: card.dataset.path || null, changed: "changed" in card.dataset,
    who: card.dataset.who || null, verdict: card.dataset.verdict || null,
    discs: classed(card, "wm-disc").map((disc) => disc.dataset.verdict || "status"),
    levels: classed(card, "wm-lv").map((box) => [box.dataset.lv, box.textContent]) });
  const mapRequests = () => requests.filter((path) => path.startsWith("/ui/api/workmap"));
  const slot = (row) => ({ list: row.dataset.list, label: texts(classed(row, "wm-list-label")[0]).join(""),
    chips: classed(row, "wm-jump").map((chip) => texts(chip).join(" ")), titles: classed(row, "wm-jump").map((chip) => chip.title),
    none: classed(row, "wm-list-none").map((none) => none.textContent),
    more: classed(row, "wm-list-more").map((more) => [more.textContent, more.hidden]) });
  // Every fit the page runs: an automatic fit only on a new map or a change of nodes or edges.
  const fits = [];
  const fitWorkmap = context.fitWorkmap;
  context.fitWorkmap = () => { fits.push(vm.runInContext("workmapFitFloor()", context)); return fitWorkmap(); };
  const out = {};
  if (scenario === "workmap_signout") {
    out.before = { hidden: byId("workmap-page").classes.has("hidden"), cards: Object.keys(cards()).length,
                   polls: pendingTimers(5000).length };
    byId("wm-result-text").textContent = "a result shown before";  // as the dialog holds one it showed
    refuseSignIn = true;  // the session is revoked with automatic sign-in off
    signedIn = false;
    await fireTimers(5000);
    await sleep(50);
    out.after = { hidden: byId("workmap-page").classes.has("hidden"), cards: walk(byId("wm-layers")).length,
                  polls: pendingTimers(5000).length, signin: !byId("signin").classList.contains("hidden"),
                  requests: mapRequests().length, result_text: byId("wm-result-text").textContent };
    await fireTimers(5000);
    out.after.requests_later = mapRequests().length;
    await finish(out);
  }
  const layers = byId("wm-layers").children;
  await sleep(40);  // the frame that fits and draws
  out.opened = {
    tabs: byId("tabs").children.map((b) => b.attrs["aria-pressed"]), main_hidden: byId("main").classes.has("hidden"),
    page_hidden: byId("workmap-page").classes.has("hidden"), landing_hidden: byId("landing").classes.has("hidden"),
    state: byId("workmap-page").dataset.state, requests: mapRequests(), url: replacedUrls.at(-1)[2],
    goal: byId("wm-goal").textContent, flag: byId("wm-flag").textContent, ids: texts(byId("wm-ids")).join(""),
    progress: texts(byId("wm-progress-text")).join(""), path: texts(byId("wm-path")).join(""), path_title: byId("wm-path").title,
    bar: byId("wm-progress").attrs["aria-label"],
    tiles: byId("wm-tiles").children.map((tile) => [tile.dataset.cat, texts(tile).join("")]),
    slots: byId("wm-lists").children.map(slot),
    pick: [byId("wm-pick-note").textContent, byId("wm-pick-note").hidden, "ring" in byId("wm-map").dataset],
    picker: byId("wm-map").children.map((group) => [group.label, group.children.map((option) => [option.textContent, option.selected])]),
    captions: layers.map((row) => row.attrs["aria-label"]),
    layer_ids: layers.map((row) => row.children.filter((child) => child.dataset && child.dataset.id).map((child) => child.dataset.id).sort()),
    cards: Object.fromEntries(Object.entries(cards()).map(([id, card]) => [id, describe(card)])),
    polls: pendingTimers(5000).length, fits: [...fits],
  };
  // Pointing at a chip outlines its card; leaving it takes the outline off.
  const chip = classed(byId("wm-lists"), "wm-jump").find((button) => button.dataset.go === "decide");
  fire(chip, "pointerenter");
  out.hover = ["hover" in cardOf("decide").dataset];
  fire(chip, "pointerleave");
  out.hover.push("hover" in cardOf("decide").dataset);
  // The panel puts the ladder first; the full result opens in a dialog, as text.
  fire(cardOf("fix"), "click");
  await sleep(50);
  const sheet = byId("wm-d");
  out.node = { panel: byId("wm-body").dataset.panel, hidden: sheet.hidden, text: texts(sheet),
               sections: classed(sheet, "wm-sec").map((section) => section.children[0].textContent),
               pressed: cardOf("fix").attrs["aria-pressed"], result_requested: mapRequests().some((p) => p.includes("/result?")),
               requests: mapRequests().slice(-1), fits: fits.length };
  const show = walk(sheet, [], (child) => child.tag === "button" && String(child.textContent).startsWith("Show full result"))[0];
  fire(show, "click");
  await sleep(50);
  const dialog = byId("wm-result-dialog"), pre = byId("wm-result-text");
  out.result = { open: dialog.open, text: pre.textContent, html_children: pre.children.length, requested: mapRequests().at(-1),
                 title: byId("wm-result-title").textContent, note: byId("wm-result-note").textContent };
  pre.scrollTop = 900;  // the reader scrolled the result
  // A category tile dims every other category; the same tile again shows all.
  const you = byId("wm-tiles").children.find((tile) => tile.dataset.cat === "you");
  fire(you, "click");
  out.filter = [byId("wm-stage").dataset.filter, you.attrs["aria-pressed"]];
  fire(you, "click");
  out.filter.push(byId("wm-stage").dataset.filter || null);
  // The theme: the header's switch overrides the system setting and is remembered in this browser.
  const [auto, , dark] = byId("theme").children;
  fire(dark, "click");
  out.theme = [context.document.documentElement.attrs["data-theme"] || null, storage.get("agents-core.theme"), dark.attrs["aria-pressed"]];
  fire(auto, "click");
  out.theme.push(context.document.documentElement.attrs["data-theme"] || null);
  // Present hides the header and the slots and refits; Fit may go below the automatic floor.
  fits.length = 0;
  fire(byId("wm-present"), "click");
  await sleep(40);
  out.present = { page: "present" in byId("workmap-page").dataset, root: "data-wm-present" in context.document.documentElement.attrs,
                  pressed: byId("wm-present").attrs["aria-pressed"], fits: [...fits] };
  fire(byId("wm-present"), "click");
  await sleep(40);
  out.present.off = ["present" in byId("workmap-page").dataset, "data-wm-present" in context.document.documentElement.attrs];
  fits.length = 0;
  fire(byId("wm-fit"), "click");
  await sleep(40);
  out.fit = [...fits];
  // A status change repaints in place: no fit, a ring on the changed card, the open result untouched.
  fits.length = 0;
  workmapRevision = 10;
  const before = mapRequests().length;
  await fireTimers(5000);
  await sleep(50);
  out.polled = { requests: mapRequests().slice(before), build: describe(cardOf("build")), fits: [...fits],
                 dialog: [dialog.open, pre.textContent, pre.scrollTop], polls: pendingTimers(5000).length,
                 changed_cleared: pendingTimers(3000).length, ids: texts(byId("wm-ids")).join("") };
  // A node added is a change of structure: the graph is refit and the new card ringed.
  workmapExtra = true;
  await fireTimers(5000);
  await sleep(50);
  out.grown = { fits: [...fits], extra: describe(cardOf("extra")) };
  // A window resize refits as well.
  fits.length = 0;
  for (const handler of windowHandlers.resize || []) handler({});
  await sleep(40);
  out.resized = [...fits];
  fire(byId("wm-result-close"), "click");  // Escape closes a modal dialog by itself, before the panel
  press("Escape");
  out.escaped = { panel: byId("wm-body").dataset.panel, hidden: byId("wm-d").hidden, pressed: cardOf("fix").attrs["aria-pressed"],
                  dialog: dialog.open };
  fire(byId("tabs").children[0], "click");
  await sleep(50);
  out.left = { page_hidden: byId("workmap-page").classes.has("hidden"), main_hidden: byId("main").classes.has("hidden"),
               polls: pendingTimers(5000).length, url: replacedUrls.at(-1)[2] };
  await finish(out);
}

// workmap_picker: the link opens a map older than the maps its repository lists, with a select that
// behaves as a browser's (browserSelect). The select must still name the open map.
if (scenario === "workmap_picker") {
  await sleep(40);
  const select = byId("wm-map");
  const options = select.children.flatMap((child) => (child.tag === "optgroup"
    ? child.children.map((option) => [child.label, option.textContent, option.selected]) : [[null, child.textContent, child.selected]]));
  await finish({ options, value: select.value, url: replacedUrls.at(-1)[2] });
}

// workmap_present: this tab was presenting before the reload, so the page opens presenting; Escape
// leaves Present and the tab forgets it.
if (scenario === "workmap_present") {
  await sleep(40);
  const state = () => ({ present: "present" in byId("workmap-page").dataset, pressed: byId("wm-present").attrs["aria-pressed"],
    root: "data-wm-present" in context.document.documentElement.attrs, floor: vm.runInContext("workmapFitFloor()", context),
    stored: session.get("agents-core.workmap-present") || null });
  const opened = state();
  press("Escape");
  await sleep(40);
  await finish({ opened, escaped: state() });
}

// workmap_refit: when a repaint refits, read from canvas and layer sizes the scenario sets, as a
// browser's layout would give them: a status change refits only a fitted map it left larger than the
// canvas (a card took a second state row), never with the panel open or after a manual zoom; the
// observer that watches the canvas and every card draws the edges again and refits the same way;
// a refresh rewrites only a running card's age, in its own box; Present's floor is the readable zoom
// for the viewport's width, and its own theme switch changes colours without a refit.
if (scenario === "workmap_refit") {
  await sleep(40);
  const canvas = byId("wm-canvas"), layers = byId("wm-layers");
  Object.assign(canvas, { clientWidth: 1600, clientHeight: 800 });
  Object.assign(layers, { offsetWidth: 1000, offsetHeight: 600 });
  const nodes = (element, found = []) => {
    for (const child of element.children || []) {
      if (child.nodeType === 3) continue;
      if (String(child.className || "").split(" ").includes("wm-node")) found.push(child);
      nodes(child, found);
    }
    return found;
  };
  const cardOf = (id) => nodes(layers).find((card) => card.dataset.id === id);
  const texts = (element) => [element.textContent, ...(element.children || []).flatMap((child) => (child.nodeType === 3
    ? [child.textContent] : texts(child)))].filter(Boolean);
  const fits = [], draws = [];
  const fitWorkmap = context.fitWorkmap, drawWorkmapEdges = context.drawWorkmapEdges;
  context.fitWorkmap = () => { fits.push(vm.runInContext("workmapFitFloor()", context)); return fitWorkmap(); };
  context.drawWorkmapEdges = () => { draws.push(1); return drawWorkmapEdges(); };
  const out = {};
  // The running card's age has a box of its own; a refresh three hours on rewrites it and nothing else.
  const meta = (id) => texts(cardOf(id)).slice(1, 4);
  out.age = { before: meta("build") };
  vm.runInContext("{ const real = Date.now; Date.now = () => real() + 3 * 3600000; }", context);
  context.refreshWorkmapAges();
  out.age.after = meta("build");
  const poll = async (revision, height) => {
    layers.offsetHeight = height;
    workmapRevision = revision;
    fits.length = 0;
    await fireTimers(5000);
    await sleep(50);
    return [...fits];
  };
  out.fits = await poll(10, 600);  // a status change whose repaint still fits
  out.outgrown = await poll(11, 900);  // one that left the map taller than its canvas
  fire(cardOf("fix"), "click");  // the panel keeps the zoom while it is open
  await sleep(50);
  out.panel = await poll(12, 900);
  fits.length = 0;
  press("Escape");
  await sleep(40);
  out.panel_closed = [...fits];
  fire(byId("wm-zoom-in"), "click");  // the viewer's own zoom stands
  await sleep(40);
  out.manual = await poll(13, 1200);
  fire(byId("wm-fit"), "click");
  await sleep(40);
  const observer = resizeObservers[0];
  out.observed = { canvas: observer.observed.has(canvas), cards: [...observer.observed].filter((element) => element !== canvas).length,
                   drawn: nodes(layers).length };
  for (const [name, height] of [["resized", 600], ["resized_outgrown", 900]]) {
    layers.offsetHeight = height;
    fits.length = 0;
    draws.length = 0;
    observer.callback([]);
    await sleep(40);
    out[name] = { draws: draws.length, fits: [...fits] };
  }
  layers.offsetHeight = 600;
  fits.length = 0;
  fire(byId("wm-present"), "click");
  await sleep(40);
  out.present = { fits: [...fits], stored: session.get("agents-core.workmap-present") || null };
  viewportWidth = 1280;
  fits.length = 0;
  for (const handler of windowHandlers.resize || []) handler({});
  await sleep(40);
  out.present_1280 = [...fits];
  viewportWidth = 1920;
  fits.length = 0;
  const root = context.document.documentElement;
  fire(byId("wm-theme"), "click");
  out.theme = [root.attrs["data-theme"], byId("wm-theme").attrs["aria-pressed"], storage.get("agents-core.theme")];
  fire(byId("wm-theme"), "click");
  out.theme.push(root.attrs["data-theme"], byId("wm-theme").attrs["aria-pressed"]);
  await sleep(40);
  out.theme_fits = [...fits];
  fire(byId("wm-present"), "click");
  await sleep(40);
  out.present_off = session.has("agents-core.workmap-present");
  // Fit takes the largest step at which the map shows whole with the gaps within the height budget
  // (the stage's data-tight), then the gaps the edges want only where the map's height still fits with
  // them at that step: here 600 px at 100 % with the budget's gaps, 700 or 620 with the wanted ones; and
  // a map wider than the canvas at the floor takes them too while its height fits.
  const stage = byId("wm-stage"), zoom = () => vm.runInContext("wm.zoom", context);
  out.gaps = {};
  for (const [name, wanted] of [["tight", 700], ["wanted", 620]]) {
    Object.defineProperty(layers, "offsetHeight", { configurable: true, get: () => Math.round(("tight" in stage.dataset ? 600 : wanted) * zoom() / 100) });
    Object.defineProperty(layers, "offsetWidth", { configurable: true, get: () => Math.round(1000 * zoom() / 100) });
    context.fitWorkmap();
    out.gaps[name] = [zoom(), "tight" in stage.dataset];
  }
  Object.defineProperty(layers, "offsetWidth", { configurable: true, get: () => Math.round(4000 * zoom() / 100) });
  context.fitWorkmap();
  out.gaps.wide = [zoom(), "tight" in stage.dataset];
  // From the smallest zoom, with 200 px of the height not scaling: the estimate lands nine steps low,
  // and Fit climbs on until the next step no longer fits (140 %: 200 + 400 x 1.4 = 760 of 776 px).
  Object.defineProperty(layers, "offsetHeight", { configurable: true, get: () => Math.round(200 + 400 * zoom() / 100) });
  Object.defineProperty(layers, "offsetWidth", { configurable: true, get: () => Math.round(1000 * zoom() / 100) });
  context.setWorkmapZoom(50);
  context.fitWorkmap();
  out.climb = zoom();
  await finish(out);
}

const result = { startup_posts: posts.length };
if (scenario === "concurrent") {
  signedIn = false;  // the session expires while the page is open
  posts.length = 0;
  const answers = await Promise.all(["/ui/api/flows", "/ui/api/workspaces", "/ui/api/components?kind=rules"]
    .map((path) => context.api(path).then(() => "ok", (error) => error.message)));
  Object.assign(result, { answers, concurrent_posts: posts.length });
}
Object.assign(result, {
  posts, signin_shown: !byId("signin").classList.contains("hidden"),
  main_hidden: byId("main").classList.contains("hidden"),
});
console.log(JSON.stringify(result));
