// Runs the flow editor page script (src/daemon/flows_ui.html) against a stub DOM and
// fetch, for tests/test_flows_ui_page.py. Usage: node flows_ui_page_harness.mjs PAGE SCENARIO
// Prints JSON: the sign-in requests made and whether the sign-in section is shown, or, for the
// "search" scenario, what the list shows after each step of the search sequence, or, for
// "persona_race", the Persona panel's state while a save and a navigation overlap, or, for
// "agents", what the Agents tab lists, finds and shows, and which listings the page requested, or,
// for "ui_panes" and "ui_signout", which pane and header controls show as items open and close, or,
// for "place", where itemPlace puts groups of given widths.
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
    removeAttribute() {}, querySelector: () => element(), querySelectorAll: () => [], showModal() {}, close() {},
    focus() { self.focused += 1; },
  };
  return self;
}
const elements = new Map();
const byId = (id) => { if (!elements.has(id)) elements.set(id, element(id)); return elements.get(id); };

const posts = [];
const requests = [];  // every path the page fetched, in order
const isUi = scenario.startsWith("ui");
let signedIn = ["search", "render", "persona_race", "agents"].includes(scenario) || isUi;
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
    return [200, { flow: { id, source: "user", title: id.slice(5), revision: "rev1", source_path: "p" },
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

const docHandlers = {};
const storage = new Map(scenario === "ui_narrow_stored" ? [["agents-ui-toc-hidden", "0"]] : []);
const context = vm.createContext({
  document: {
    getElementById: byId, createElement: (tag) => element("", tag),
    createTextNode: (text) => ({ nodeType: 3, textContent: text }),
    addEventListener(type, handler) { (docHandlers[type] = docHandlers[type] || []).push(handler); },
  },
  window: { addEventListener() {}, matchMedia: () => ({ matches: scenario.includes("narrow") }) },
  localStorage: {
    getItem: (key) => { if (scenario === "ui_nostorage") throw new Error("denied"); return storage.has(key) ? storage.get(key) : null; },
    setItem: (key, value) => { if (scenario === "ui_nostorage") throw new Error("denied"); storage.set(key, value); },
  },
  location: { hash: scenario === "used_code" ? "#code=used-code" : "", pathname: "/ui" },
  history: { replaceState() {} },
  fetch: fetchStub, alert() {}, confirm: () => true, setTimeout, clearTimeout, console, URLSearchParams,
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
