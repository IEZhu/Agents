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

function element(id = "") {
  const classes = new Set(["signin"].includes(id) ? ["hidden"] : []);
  const handlers = {};
  const self = {
    id, children: [], dataset: {}, style: {}, value: "", textContent: "", className: "", disabled: false,
    scrollTop: 0, handlers, focused: 0,
    classList: {
      add: (name) => classes.add(name), remove: (name) => classes.delete(name), contains: (name) => classes.has(name),
      toggle: (name, on) => ((on ?? !classes.has(name)) ? classes.add(name) : classes.delete(name)),
    },
    addEventListener(type, handler) { (handlers[type] = handlers[type] || []).push(handler); },
    appendChild(child) { self.children.push(child); },
    append() {},
    replaceChildren() { self.children = []; self.scrollTop = 0; },  // a browser resets scrolling here
    setAttribute() {},
    removeAttribute() {}, querySelector: () => element(), querySelectorAll: () => [], showModal() {}, close() {},
    focus() { self.focused += 1; },
  };
  return self;
}
const elements = new Map();
const byId = (id) => { if (!elements.has(id)) elements.set(id, element(id)); return elements.get(id); };

const posts = [];
let signedIn = scenario === "search";
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
  if (scenario === "search") return respond(200, searchData(path));
  if (path.startsWith("/ui/api/flows")) return respond(200, { flows: [], repositories: [] });
  return respond(200, { workspaces: [], items: [] });
}

const flow = (id, source, title, content, extra = {}) => ({ id, source, title, content, ...extra });
const rule = (id, short_name, description, body, enabled = true) =>
  ({ id, short_name, description, body, enabled, declared_by: [] });
function searchData(path) {
  if (path.startsWith("/ui/api/flows")) {
    if (!path.includes("with_content=1")) throw new Error("the page must ask for flow content");
    return {
      flows: [flow("user:alpha", "user", "Alpha plan", "deploy steps"),
              flow("user:beta", "user", "Beta", "a mention of ALPHA here"),
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
const context = vm.createContext({
  document: {
    getElementById: byId, createElement: () => element(),
    addEventListener(type, handler) { (docHandlers[type] = docHandlers[type] || []).push(handler); },
  },
  window: { addEventListener() {} },
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
