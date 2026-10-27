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
// for the "landing_updates*" scenarios (#381), what the overview's Updates section shows and saves.
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
    querySelector: () => element(), querySelectorAll: () => [], showModal() {}, close() {},
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
let signedIn = ["search", "render", "persona_race", "agents", "history", "history_adopt"].includes(scenario) || isUi || isSync
  || isAutodev;
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
    if (scenario === "refused" || scenario === "ui_signout") return respond(401, { error: "sign_in_required" });
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
  if (path.startsWith("/ui/api/flows")) return respond(200, { flows: [], repositories: [] });
  return respond(200, { workspaces: [], items: [] });
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
const documentStub = {
  getElementById: byId, createElement: (tag) => element("", tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: text }),
  addEventListener(type, handler) { (docHandlers[type] = docHandlers[type] || []).push(handler); },
  visibilityState: "visible",
};
const context = vm.createContext({
  document: documentStub,
  window: {
    addEventListener(type, handler) { (windowHandlers[type] = windowHandlers[type] || []).push(handler); },
    matchMedia: (query) => ({ matches: query.includes("reduced-motion") ? scenario.includes("reduced")
                                                                    : scenario.includes("narrow") || narrowNow }),
  },
  // ui_place measures stub boxes; the page reads the header gap, the pane's corner and the joint.
  getComputedStyle: () => ({ columnGap: "8px", borderTopLeftRadius: "22px", width: "13px" }),
  localStorage: {
    getItem: (key) => { if (scenario.endsWith("nostorage")) throw new Error("denied"); return storage.has(key) ? storage.get(key) : null; },
    setItem: (key, value) => { if (scenario.endsWith("nostorage")) throw new Error("denied"); storage.set(key, value); },
  },
  location: { hash: scenario === "used_code" ? "#code=used-code" : scenario === "autodev_link" ? "#autodev" : "",
              pathname: "/ui" },
  history: { replaceState(...args) { replacedUrls.push(args); } },
  fetch: fetchStub, alert(message) { alerts.push(message); }, confirm: (question) => { confirms.push(question); return confirmAnswer; },
  prompt: (question, suggested) => { prompts.push([question, suggested]); return suggested; },
  setTimeout: pageSetTimeout, clearTimeout: pageClearTimeout,
  console, URLSearchParams,
  Option: function Option(text, value) { return Object.assign(element(), { text, value }); },
});
for (const [index, tab] of ["flows", "agents", "rules", "skills", "implants", "history"].entries()) {
  const button = element();
  button.dataset.tab = tab;
  if (tab === "flows") button.classes.add("active");  // as the markup has it
  byId("tabs").children[index] = button;
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
  for (const tab of ["flows", "agents", "rules", "skills", "implants", "history"]) {
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
