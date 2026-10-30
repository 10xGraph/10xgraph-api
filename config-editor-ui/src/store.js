// One mutable store. Components read it directly; every change goes through
// commit(), which re-renders the app. The tree is small, so a full render is cheap
// and Preact's diff keeps focus, selection, and scroll position intact.

// Sidebar structure. Section ids not listed here land in the last group (before the
// environment page), so new schema entries still render. "get-running" and "environment"
// are pages of their own rather than schema sections.
export const NAV = [
  { label: null, items: ["get-running"] },
  { label: "Before production", items: ["auth", "authorization", "rate_limit", "observability", "token_revocation"] },
  { label: "Runs and streaming", items: ["runs", "websocket", "ag_ui", "remote_tools", "media"] },
  { label: "Project wiring", items: ["plugins"] },
  { label: "Developer tools", items: ["test", "evaluation"] },
  { label: "Reference", items: ["environment"] },
];

export const PAGES = {
  "get-running": {
    title: "Get running", icon: "flag",
    description: "Only the agent graph is required. Everything else is optional.",
  },
  environment: {
    title: "Server environment (.env)", icon: "terminal",
    description: "Variables the server reads from .env. The editor never reads that file, so values are not shown.",
  },
};

// Issues a widget renders beside its own input rather than at the top of the section.
export const WIDGET_FIELD_IDS = { "auth.path": "auth-path", authorization: "authz-path" };

// The page token arrives in the URL hash (#token=...). Read it once, keep it in memory, and
// mirror it to sessionStorage so a reload still works after the hash switches to page routing.
const TOKEN_KEY = "tg-config-token";
let tokenSaved = false;
function readToken() {
  const fromHash = new URLSearchParams(location.hash.slice(1)).get("token") || "";
  try {
    if (fromHash) sessionStorage.setItem(TOKEN_KEY, fromHash);
    const stored = sessionStorage.getItem(TOKEN_KEY) || "";
    tokenSaved = stored !== "" && stored === (fromHash || stored);
    return fromHash || stored;
  } catch {
    return fromHash;
  }
}
export const TOKEN = readToken();

// "#id" normally. If sessionStorage is unavailable the token has to stay in the hash.
export const routeHash = (id) => (tokenSaved || !TOKEN ? `#${id}` : `#token=${encodeURIComponent(TOKEN)}&page=${id}`);

export function currentRouteId() {
  const raw = location.hash.slice(1);
  if (raw.includes("=")) {
    const params = new URLSearchParams(raw);
    return params.get("page") || params.get("section") || "";
  }
  try { return decodeURIComponent(raw); } catch { return raw; }
}

export const store = {
  schema: null,
  sections: [],            // in page order
  nav: [],                 // NAV with unlisted sections appended: [{label, items: [id]}]
  fields: new Map(),       // "a.b.c" -> field definition
  config: {},
  version: null,
  exists: false,
  path: "",
  parseError: null,
  saved: "",               // snapshot of the last loaded/saved config
  validated: null,         // snapshot the current issues belong to
  issues: [],
  page: "get-running",     // id of the current page (a nav item)
  query: "",               // search text; non-empty replaces the page with results
  sideOpen: true,          // sidebar wanted
  sideRender: true,        // sidebar in the layout (false only after the slide-out finished)
  sideShown: true,         // slide state, flips a frame after sideRender
  drawerOpen: false,       // 10xgraph.json drawer
  tab: "json",
  copied: false,           // "10xgraph dev" copy feedback
  busy: null,              // "save" | null
  toolParams: {},          // remote tool index -> {text, error} while JSON is being edited
  toasts: [],
  fatal: null,             // {title, text} when the page cannot load
};

let rerender = () => {};
export const subscribe = (fn) => { rerender = fn; };
export function commit(mutate) {
  if (mutate) mutate(store);
  rerender();
}

// ------------------------------------------------------------------ helpers

export const clone = (v) => JSON.parse(JSON.stringify(v));
export const snapshot = () => JSON.stringify(store.config);
export const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
export const getPath = (obj, path) => path.reduce((acc, k) => (isObject(acc) ? acc[k] : undefined), obj);
export const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

export function setPath(obj, path, value) {
  let target = obj;
  for (const k of path.slice(0, -1)) {
    if (!isObject(target[k])) target[k] = {};
    target = target[k];
  }
  target[path[path.length - 1]] = value;
}

export function deletePath(obj, path) {
  const parents = [];
  let target = obj;
  for (const k of path.slice(0, -1)) {
    if (!isObject(target)) return;
    parents.push([target, k]);
    target = target[k];
  }
  if (isObject(target)) delete target[path[path.length - 1]];
  // Drop nested objects left empty, but never the section key itself.
  for (let i = parents.length - 1; i >= 1; i--) {
    const [parent, k] = parents[i];
    if (isObject(parent[k]) && Object.keys(parent[k]).length === 0) delete parent[k];
    else break;
  }
}

export function valueOrDefault(path) {
  const v = getPath(store.config, path);
  return v !== undefined ? v : store.fields.get(path.join("."))?.default;
}

export const deepEqual = (a, b) => JSON.stringify(a) === JSON.stringify(b);
export const section = (id) => store.sections.find((s) => s.id === id);
export const isDirty = () => snapshot() !== store.saved;
export const isOn = (sec) => !sec.optional || (sec.key in store.config && store.config[sec.key] !== null);
export const issuesFor = (id) => store.issues.filter((i) => i.section === id);
export const issueAt = (path) => store.issues.find((i) => i.path === path);
export const isStale = () => store.validated !== null && store.validated !== snapshot();
export function counts(list = store.issues) {
  const errors = list.filter((i) => i.level === "error").length;
  return { errors, warnings: list.length - errors };
}
export const fieldIdFor = (path) => WIDGET_FIELD_IDS[path] || `f-${path.replaceAll(".", "-")}`;

// A field whose value is set and differs from its default.
export const fieldChanged = (f) => {
  const v = getPath(store.config, f.path);
  return v !== undefined && !deepEqual(v, f.default);
};
export const sectionChanged = (sec) => {
  if (sec.id === "core") return false;
  return sec.optional ? isOn(sec) : sec.fields.some(fieldChanged);
};
// Highest-severity issue of a section, or null.
export function worstIssue(id) {
  const list = issuesFor(id);
  return list.find((i) => i.level === "error") || list[0] || null;
}
// show_if that does not match the current value: the field is dimmed with a hint.
export const fieldUnmet = (f) => !!f.show_if && valueOrDefault(f.show_if.path) !== f.show_if.equals;
export function unmetHint(f) {
  const dep = store.fields.get(f.show_if.path.join("."));
  const name = dep?.label || f.show_if.path.join(".");
  const eq = f.show_if.equals;
  return `Used when ${name} is ${eq === true ? "on" : eq === false ? "off" : String(eq)}.`;
}
export const navItem = (id) => {
  const sec = section(id);
  return PAGES[id] ? { id, ...PAGES[id] } : sec ? { id, title: sec.title, description: sec.description, icon: null } : null;
};
export const isAgentReady = () => /^[\w.]+:[\w.]+$/.test(String(store.config.agent || ""));
