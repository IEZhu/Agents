// Runs the flow editor page script (src/daemon/flows_ui.html) against a stub DOM and
// fetch, for tests/test_flows_ui_page.py. Usage: node flows_ui_page_harness.mjs PAGE SCENARIO
// Prints JSON: the sign-in requests made and whether the sign-in section is shown, or, for the
// "search" scenario, what the list shows after each step of the search sequence.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const [, , pagePath, scenario] = process.argv;
const html = readFileSync(pagePath, "utf8");
const script = html.match(/<script nonce="\{\{NONCE\}\}">([\s\S]*?)<\/script>/)[1];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function element(id = "", tag = "") {
  const classes = new Set(["signin"].includes(id) ? ["hidden"] : []);
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
    removeAttribute() {}, querySelector: () => element(), querySelectorAll: () => [], showModal() {}, close() {},
    focus() { self.focused += 1; },
  };
  return self;
}
const elements = new Map();
const byId = (id) => { if (!elements.has(id)) elements.set(id, element(id)); return elements.get(id); };

const posts = [];
const isUi = scenario.startsWith("ui");
let signedIn = scenario === "search" || scenario === "render" || isUi;
const puts = [];
let putStatus = 200;
const savedText = {};  // what a PUT stored, served back by the next GET
const respond = (status, body) => ({ status, ok: status < 400, statusText: "", json: async () => body });

async function fetchStub(path, init = {}) {
  if (path === "/ui/api/session") {
    const body = JSON.parse(init.body || "{}");
    posts.push(body);
    if (posts.length > 5) {  // a sign-in loop: report it instead of spinning forever
      console.log(JSON.stringify({ looped: true, posts }));
      process.exit(0);
    }
    await sleep(10);  // keep the attempt pending while other 401s arrive
    if ("code" in body) return respond(401, { error: "code_invalid" });
    if (scenario === "refused") return respond(401, { error: "sign_in_required" });
    if (scenario !== "cookie_lost") signedIn = true;
    return respond(200, { status: "ok" });
  }
  if (!signedIn) return respond(401, { error: "session_required" });
  if (isUi) return respond(...uiData(path, init));
  if (scenario === "search") {
    if (path.includes("kind=skills")) await sleep(80);  // a slow tab, to type while it loads
    return respond(200, searchData(path));
  }
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
});
for (const [index, tab] of ["flows", "rules", "skills", "implants"].entries()) {
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

if (scenario === "render") {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const host = element("", "div");
  const rendered = context.renderMarkdown(Buffer.concat(chunks).toString("utf8"), host);
  console.log(JSON.stringify({
    html: host.children.map(ser).join(""), meta: rendered.meta,
    headings: rendered.headings.map((h) => ({ level: h.level, text: h.text, id: h.id })),
  }));
  process.exit(0);
}

if (isUi) {
  const out = {};
  const snap = (id) => {
    const root = byId(id), doc = findAll(root, hasClass("md"))[0];
    return {
      hidden: root.classes.has("hidden"), toc_hidden: root.classes.has("toc-hidden"), no_toc: root.classes.has("no-toc"),
      toc: findAll(root, hasClass("md-toc-item")).map((n) => n.className + ":" + n.textContent),
      hide_label: findAll(root, hasClass("md-toc-hide"))[0].textContent, html: doc.children.map(ser).join(""),
    };
  };
  const open = async (index) => { fire(buttons()[index], "click"); await sleep(40); };
  const seg = (id, index) => byId(id).children[index];
  out.stored_at_start = [...storage.entries()];
  out.initial_toc_hidden = byId("e-view").classes.has("toc-hidden");
  if (scenario === "ui_nostorage" || scenario.startsWith("ui_narrow")) {
    await open(0);
    out.opened = snap("e-view");
    fire(byId("e-toc"), "click");
    out.after_toolbar = snap("e-view");
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
  out.hidden = { ...snap("e-view"), stored: [...storage.entries()] };
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
  fire(seg("c-seg", 1), "click");
  out.skill_source = { view_hidden: byId("c-view").classes.has("hidden"), source_hidden: byId("c-source").classes.has("hidden") };
  console.log(JSON.stringify(out));
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
