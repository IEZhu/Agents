// Runs the flow editor page script (src/daemon/flows_ui.html) against a stub DOM and
// fetch, for tests/test_flows_ui_page.py. Usage: node flows_ui_page_harness.mjs PAGE SCENARIO
// Prints JSON: the sign-in requests made and whether the sign-in section is shown, or, for the
// "search" scenario, what the list shows after each step of the search sequence, or, for
// "persona_race", the Persona panel's state while a save and a navigation overlap, or, for
// "agents", what the Agents tab lists, finds and shows, and which listings the page requested, or,
// for "ui_panes" and "ui_signout", which pane and header controls show as items open and close, or,
// for "place", where itemPlace puts groups of given widths, or, for "ui_place", where the open
// item's group goes as the window is resized, or, for the "sync_*" scenarios (library sync, #170),
// what the header chip, the Sync page, its wizard and the editor's notice show and which
// /ui/api/sync requests the page sent, against a scripted API and timers the scenario fires.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const [, , pagePath, scenario] = process.argv;
const html = readFileSync(pagePath, "utf8");
const script = html.match(/<script nonce="\{\{NONCE\}\}">([\s\S]*?)<\/script>/)[1];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
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
let signedIn = ["search", "render", "persona_race", "agents"].includes(scenario) || isUi || isSync;
const revisions = {};  // sync_notice: a flow's revision after another machine changed it
const deletedFlows = new Set();  // sync_notice: flows a cycle deleted (GET answers 404)
const confirms = [];  // every question the page asked with confirm()
const puts = [];
let putStatus = 200;
const savedText = {};  // what a PUT stored, served back by the next GET
let releasePersonaPut = null;  // persona_race: the test decides when the PUT answers
const deletes = [];
let holdDelete = false, releaseDelete = null;  // ui_panes: a delete that answers late
let holdFlowGet = false, releaseFlowGet = null;  // ui_signout: a flow that loads past the sign-in screen
const respond = (status, body) => ({ status, ok: status < 400, statusText: "", json: async () => body });

async function fetchStub(path, init = {}) {
  requests.push(path);
  if (path === "/ui/api/session") {
    const body = JSON.parse(init.body || "{}");
    posts.push(body);
    if (posts.length > 5) {  // a sign-in loop: report it instead of spinning forever
      console.log(JSON.stringify({ looped: true, posts }));
      process.exit(0);
    }
    await sleep(10);  // keep the attempt pending while other 401s arrive
    if ("code" in body) return respond(401, { error: "code_invalid" });
    if (scenario === "refused" || scenario === "ui_signout") return respond(401, { error: "sign_in_required" });
    if (scenario !== "cookie_lost") signedIn = true;
    return respond(200, { status: "ok" });
  }
  if (!signedIn) return respond(401, { error: "session_required" });
  if (isSync && path.startsWith("/ui/api/sync")) {
    const method = init.method || "GET", hold = syncHolds[method + " " + path.split("?")[0]];
    const answer = syncAnswer(method, path, init.body ? JSON.parse(init.body) : null);
    if (hold) await hold.promise;  // the scenario decides when this request answers
    return respond(...answer);
  }
  if (isSync) return respond(...uiData(path, init));
  if (isUi && init.method === "DELETE") {
    deletes.push(JSON.parse(init.body));
    if (holdDelete) await new Promise((resolve) => { releaseDelete = resolve; });
    return respond(200, { status: "deleted" });
  }
  if (isUi && holdFlowGet && path.startsWith("/ui/api/flow?")) await new Promise((resolve) => { releaseFlowGet = resolve; });
  if (isUi) return respond(...uiData(path, init));
  if (scenario === "agents") return respond(...agentsData(path, init));
  if (scenario === "search") {
    if (path.includes("kind=skills")) await sleep(80);  // a slow tab, to type while it loads
    return respond(200, searchData(path));
  }
  if (scenario === "persona_race") return personaRace(path, init);
  if (path.startsWith("/ui/api/flows")) return respond(200, { flows: [], repositories: [] });
  return respond(200, { workspaces: [], items: [] });
}

const DOCS = {
  "user:doc": "---\npersona: tester\n---\n# Doc\n\n## One\n\ntext **bold**\n\n## Two\n\n- item\n",
  "user:plain": "just text, no headings\n",
};
function uiData(path, init) {
  if (path.startsWith("/ui/api/flows")) {
    return [200, { flows: Object.keys(DOCS).map((id) => ({ id, source: "user", title: id.slice(5), content: DOCS[id] })), repositories: [] }];
  }
  if (path.startsWith("/ui/api/flow?")) {
    const id = new URLSearchParams(path.split("?")[1]).get("id");
    if (deletedFlows.has(id)) return [404, { status: "error", error: "flow_not_found: use list_flows to discover available flows" }];
    return [200, { flow: { id, source: "user", title: id.slice(5), revision: revisions[id] || "rev1", source_path: "p" },
                   content: savedText[id] ?? DOCS[id], history: [] }];
  }
  if (path === "/ui/api/flow" && init.method === "PUT") {
    const body = JSON.parse(init.body);
    puts.push(body);
    if (putStatus !== 200) return [putStatus, { error: "conflict" }];
    savedText[body.id] = body.content;
    return [200, { status: "saved", flow: { id: body.id } }];
  }
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
// last one repeats); `syncCalls` records [method, path with query, body] of every sync request.
const syncCalls = [];
const syncReplies = {};
const PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaFakeKeyForTests agents-core-sync:mac-1a2b";
const SYNC_OFF = { state: "off", reason: null, message: "sync is not set up", conflicts: 0, started: null, github: null,
                   conflict_list: [], suggested_label: "mac-1a2b", repository: null, github_repository: null,
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
                        deleted_on: null, mine: "version" };
const PERSONA_CONFLICT = { id: "20261005T120100000000Z-bbbbbbbbbb", path: "personas/common/doc.json", flow: "user:doc",
                           repo_key: null, kind: "both_changed", kept: "remote", machine: "laptop",
                           time: "2026-10-05T12:01:00+00:00", deleted_on: null, mine: "content" };
const OLD_ENTRY = { time: "2026-10-05T09:00:00+00:00", result: "ok", machine: "mac-1a2b", sent: ["user:old"], received: [],
                    received_from: [], scripts: [], conflicts: 0 };
const started = () => ({
  state: "synced", reason: null, message: null, started: "2026-10-01T10:00:00+00:00", last_success: minutesAgo(2),
  last_attempt: minutesAgo(2), retry_at: null, remote: "github.com/octocat/agents-library",
  repository: "octocat/agents-library", github_repository: "octocat/agents-library", ssh: true, branch: "main",
  label: "mac-1a2b", identity: { name: "Owner", email: "owner@example.com" }, pending: 0, stale: false,
  conflicts: 2, conflict_list: [FLOW_CONFLICT, PERSONA_CONFLICT], fetch_minutes: 5, ask_new_repositories: false,
  activity: [OLD_ENTRY, { time: "2026-10-05T11:00:00+00:00", result: "ok", machine: "mac-1a2b", sent: [],
                          received: ["user:tool"], received_from: ["laptop"], scripts: ["common/tool.py"], conflicts: 0 }],
  announced_groups: [{ group: "repos/abc", origin: "github.com/o/r", time: "2026-10-05T11:00:00+00:00" }],
  pending_groups: ["repos/def"], pending_files: [], blocked: [], public_key: PUBLIC_KEY,
  github: { connected: true, login: "octocat", host: "github.com", reconnect_needed: false, warning: null },
  loop: { state: "running", active: true, syncing: false, held: null, next_fetch_seconds: 240, last_cycle: null },
});
if (["sync_dashboard", "sync_notice", "sync_more"].includes(scenario)) syncState = started();
// Set up over SSH, stopped at the host's fingerprints, then the page was reloaded.
if (scenario === "sync_hostkey") {
  syncState = { ...SYNC_OFF, state: "waiting_for_access", started: null, suggested_label: undefined,
                repository: "git.example.com/me/library", ssh: true, host_key_unconfirmed: true, label: "laptop",
                identity: { name: "Owner", email: "owner@example.com" }, public_key: PUBLIC_KEY };
}

const docHandlers = {};
const windowHandlers = {};
let narrowNow = false;  // ui_place: the stylesheet's narrow layout applies
const storage = new Map(scenario === "ui_narrow_stored" ? [["agents-ui-toc-hidden", "0"]] : []);
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
    matchMedia: () => ({ matches: scenario.includes("narrow") || narrowNow }),
  },
  // ui_place measures stub boxes; the page reads the header gap, the pane's corner and the joint.
  getComputedStyle: () => ({ columnGap: "8px", borderTopLeftRadius: "22px", width: "13px" }),
  localStorage: {
    getItem: (key) => { if (scenario === "ui_nostorage") throw new Error("denied"); return storage.has(key) ? storage.get(key) : null; },
    setItem: (key, value) => { if (scenario === "ui_nostorage") throw new Error("denied"); storage.set(key, value); },
  },
  location: { hash: scenario === "used_code" ? "#code=used-code" : "", pathname: "/ui" },
  history: { replaceState() {} },
  fetch: fetchStub, alert() {}, confirm: (question) => { confirms.push(question); return true; },
  setTimeout: pageSetTimeout, clearTimeout: pageClearTimeout,
  console, URLSearchParams,
  Option: function Option(text, value) { return Object.assign(element(), { text, value }); },
});
for (const [index, tab] of ["flows", "agents", "rules", "skills", "implants"].entries()) {
  const button = element();
  button.dataset.tab = tab;
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
  console.log(JSON.stringify(steps));
  process.exit(0);
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
    console.log(JSON.stringify(out));
    process.exit(0);
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
    console.log(JSON.stringify(steps));
    process.exit(0);
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
    console.log(JSON.stringify({ signed_out: signedOut, late, after: panes() }));
    process.exit(0);
  }
  out.stored_at_start = [...storage.entries()];
  out.initial_toc_hidden = byId("e-view").classes.has("toc-hidden");
  if (scenario === "ui_nostorage" || scenario.startsWith("ui_narrow")) {
    await open(0);
    out.opened = snap("e-view");
    fire(byId("e-toc"), "click");
    out.after_header_button = snap("e-view");
    out.stored_after = [...storage.entries()];
    console.log(JSON.stringify(out));
    process.exit(0);
  }
  await open(0);
  out.opened = { ...snap("e-view"), panes_hidden: byId("panes").classes.has("hidden"),
                 toc_button_hidden: byId("e-toc").classes.has("hidden"), seg: byId("e-seg").children.map((b) => b.classes.has("active")) };
  fire(findAll(byId("e-view"), hasClass("md-toc-item"))[1], "click");
  out.heading_scrolled = findAll(byId("e-view"), (n) => n.tag === "h2").map((n) => n.scrolled);
  fire(findAll(byId("e-view"), hasClass("md-toc-hide"))[0], "click");
  out.hidden = { ...snap("e-view"), stored: [...storage.entries()], focus_moved: byId("e-toc").focused };
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
  out.revealed = { ...snap("e-view"), stored: [...storage.entries()] };
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
  console.log(JSON.stringify(out));
  process.exit(0);
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
    console.log(JSON.stringify(out));
    process.exit(0);
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
    console.log(JSON.stringify(out));
    process.exit(0);
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
          [200, { status: "confirmation_needed", hash: "inc1", upload: [".history/common/a/20261005T1-x.md"] }],
          [200, { status: "saved", groups: [{ group: "common", syncs: true }, { group: "history", syncs: true }] }]);
    await toggle("Saved versions (history)", false);
    out.excluded = { alert: alertText(), boxes: boxes() };
    await toggle("Saved versions (history)", true);
    out.include_asks = snapshot();
    await click("Upload them");
    out.included = { alert: alertText(), boxes: boxes() };
    reply("PUT /ui/api/sync/settings", [200, { state: "waiting_for_access" }]);
    await toggle("Ask before uploading the flows of a repository that is new to the library", true);
    reply("GET /ui/api/sync/preview", [200, {
      kind: "push", hash: "h123", privacy: "private", private_confirmed: false, upload_bytes: 120,
      upload: [{ path: "common/a.md", flow: "user:a", size: 120 }], download: [], delete_local: [],
      remove_from_remote: [], conflicts: [], held: [], excluded: [], pending_groups: [], pending_files: [],
      blocked: [{ path: "common/secret.md", pattern: "github_token", sha256: "x" }],
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
    console.log(JSON.stringify(out));
    process.exit(0);
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
    console.log(JSON.stringify(out));
    process.exit(0);
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
    console.log(JSON.stringify(out));
    process.exit(0);
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
    console.log(JSON.stringify(out));
    process.exit(0);
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
    console.log(JSON.stringify(out));
    process.exit(0);
  }

  if (scenario === "sync_more") {
    syncState = { ...started(), fetch_minutes: 3 };  // set from the command line, not in the list
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
    console.log(JSON.stringify(out));
    process.exit(0);
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
    console.log(JSON.stringify(out));
    process.exit(0);
  }
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
  console.log(JSON.stringify(out));
  process.exit(0);
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
  console.log(JSON.stringify(steps));
  process.exit(0);
}

if (scenario === "place") {
  // itemPlace for a group that ends at 800 px, a pane whose straight top edge starts at 339 px and a
  // version that ends, with the gap, at 230 px.
  const at = (groupWidth, narrow = false) =>
    context.itemPlace({ narrow, groupWidth, groupEnd: 800, versionEnd: 230, straightStart: 339 });
  console.log(JSON.stringify({ on_pane: at(461), past_corner: at(462), after_version: at(570),
                               too_wide: at(571), narrow: at(100, true) }));
  process.exit(0);
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
