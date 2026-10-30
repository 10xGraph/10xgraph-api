import {
  NAV, PAGES, TOKEN, clone, commit, counts, currentRouteId, fieldIdFor, isDirty, navItem, plural,
  routeHash, section, snapshot, store,
} from "./store.js";

// ------------------------------------------------------------------ network

async function api(method, url, body) {
  const headers = { "X-10xGraph-Token": TOKEN };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  return { ok: res.ok, status: res.status, data };
}

const fatal = (title, text) => commit((s) => { s.fatal = { title, text }; });

export async function load() {
  if (!TOKEN) return fatal("Open the link printed by 10xgraph config", "This page needs the session token included in that link.");
  let res;
  try {
    res = await api("GET", "/api/state");
  } catch {
    return fatal("Cannot reach the config server", "Check that 10xgraph config is still running in your terminal, then reload.");
  }
  if (!res.ok) return fatal("This link has expired", res.data.error || "Restart 10xgraph config and open the new link it prints.");

  const data = res.data;
  commit((s) => {
    s.schema = data.schema;
    s.sections = data.schema.sections;
    const listed = new Set(NAV.flatMap((g) => g.items));
    const extra = s.sections.map((sec) => sec.id).filter((id) => id !== "core" && !listed.has(id));
    s.nav = NAV.map((g, i) => ({
      label: g.label,
      // Sections the nav does not name join the last group, ahead of the environment page.
      items: i < NAV.length - 1 ? g.items.filter((id) => PAGES[id] || section(id)) : [...extra, ...g.items],
    }));
    for (const sec of s.sections) for (const f of sec.fields) s.fields.set(f.path.join("."), f);
    s.config = data.config;
    s.version = data.version;
    s.exists = data.exists;
    s.path = data.path;
    s.parseError = data.parse_error;
    s.issues = data.issues;
    const clean = data.exists && !data.parse_error;
    s.saved = clean ? snapshot() : "";
    s.validated = clean ? snapshot() : null;
  });
  applyRoute({ focus: false });
  // Swap #token=... for a clean page route (the token stays in memory and sessionStorage).
  history.replaceState(null, "", routeHash(store.page));
}

// Runs automatically a moment after edits (see main.jsx). Quiet: results show inline.
let lastTried = null;
export async function validate() {
  const snap = snapshot();
  if (!store.schema || store.validated === snap || lastTried === snap) return;
  if (Object.values(store.toolParams).some((t) => t.error)) return;
  lastTried = snap;
  try {
    const res = await api("POST", "/api/validate", { config: JSON.parse(snap) });
    if (!res.ok) return;
    commit((s) => {
      s.issues = res.data.issues;
      s.validated = snap;
    });
  } catch {
    // Offline or stopped: the next edit tries again.
  }
}

export async function save() {
  if (store.busy || !store.schema) return;
  if (store.exists && !isDirty() && !store.parseError) return toast("success", "No changes to save.");
  if (!guardJson()) return;
  commit((s) => { s.busy = "save"; });
  try {
    const res = await api("POST", "/api/save", { config: store.config, version: store.version });
    if (res.data.issues) {
      store.issues = res.data.issues;
      store.validated = snapshot();
    }
    if (res.ok) {
      store.version = res.data.version;
      store.config = res.data.config;
      store.toolParams = {};
      store.validated = snapshot();
      store.saved = snapshot();
      store.exists = true;
      store.parseError = null;
      const name = res.data.path.split(/[\\/]/).pop();
      toast("success", res.data.backup ? `Saved ${name}. The previous version is in ${name}.bak.` : `Created ${name}.`);
    } else if (res.status === 409) {
      toast("error", res.data.error, { label: "Reload", run: () => location.reload() });
    } else if (res.status === 422) {
      const { errors } = counts();
      toast("error", `Not saved: ${plural(errors, "error")} to fix first.`, { label: "Show", run: showFirstError });
    } else {
      toast("error", res.data.error || "Save failed.");
    }
  } catch {
    toast("error", "Cannot reach the config server.");
  } finally {
    commit((s) => { s.busy = null; });
  }
}

function guardJson() {
  const broken = Object.values(store.toolParams).some((t) => t.error);
  if (broken) {
    go("remote_tools");
    toast("error", "A remote tool has invalid parameters JSON.");
  }
  return !broken;
}

// ------------------------------------------------------------------ sections

export function setSection(sec, on) {
  commit((s) => {
    if (on) s.config[sec.key] = clone(sec.default);
    else delete s.config[sec.key];
    if (sec.key === "remote_tools") s.toolParams = {};
  });
}

function showFirstError() {
  const issue = store.issues.find((i) => i.level === "error") || store.issues[0];
  if (issue) go(issue.section, fieldIdFor(issue.path));
}

// ------------------------------------------------------------------ routing

const smooth = () => (window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");

function resolvePage(id) {
  if (id === "core") return "get-running";
  return PAGES[id] || section(id) ? id : "get-running";
}

// Read the hash into store.page. A route change leaves search and moves focus to the heading.
export function applyRoute({ focus = true } = {}) {
  commit((s) => {
    s.page = resolvePage(currentRouteId());
    s.query = "";
    s.drawerOpen = false;
  });
  if (!focus) return;
  requestAnimationFrame(() => {
    window.scrollTo(0, 0);
    document.getElementById("page-title")?.focus({ preventScroll: true });
  });
}

// Open a page, and optionally bring one of its fields into view.
export function go(id, fieldId) {
  const hash = routeHash(id);
  if (location.hash !== hash) history.pushState(null, "", hash);
  applyRoute();
  if (!fieldId) return;
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const field = document.getElementById(fieldId);
    if (!field) return;
    field.scrollIntoView({ block: "center", behavior: smooth() });
    field.focus({ preventScroll: true });
  }));
}

export const hasPage = (id) => !!navItem(id);

export function setQuery(query) {
  commit((s) => { s.query = query; });
}

// ------------------------------------------------------------------ sidebar

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
let sideTimer;

export function setSidebar(open, { instant = false } = {}) {
  try { localStorage.setItem("tg-sidebar", open ? "shown" : "hidden"); } catch { /* private mode */ }
  clearTimeout(sideTimer);
  const still = instant || reducedMotion();
  commit((s) => {
    s.sideOpen = open;
    if (open) { s.sideRender = true; s.sideShown = still; } else s.sideShown = false;
    if (!open && still) s.sideRender = false;
  });
  if (open && !still) requestAnimationFrame(() => requestAnimationFrame(() => commit((s) => { s.sideShown = true; })));
  if (!open && !still) sideTimer = setTimeout(() => commit((s) => { if (!s.sideOpen) s.sideRender = false; }), 200);
}

export function focusSearch() {
  if (!store.sideOpen) setSidebar(true, { instant: true });
  requestAnimationFrame(() => {
    const input = document.getElementById("q");
    input?.focus();
    input?.select();
  });
}

// ------------------------------------------------------------------ drawer

export function openDrawer(tab) {
  commit((s) => {
    if (tab) s.tab = tab;
    s.drawerOpen = true;
  });
}

export const closeDrawer = () => commit((s) => { s.drawerOpen = false; });

export async function copyCommand() {
  const text = "10xgraph dev";
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.append(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    } catch { /* nothing more to try */ }
  }
  commit((s) => { s.copied = true; });
  setTimeout(() => commit((s) => { s.copied = false; }), 2000);
}

export async function copyJson() {
  try {
    await navigator.clipboard.writeText(JSON.stringify(store.config, null, 2) + "\n");
    toast("success", "Copied 10xgraph.json to the clipboard.");
  } catch {
    toast("error", "The clipboard is not available here.");
  }
}

// ------------------------------------------------------------------ toasts

let toastId = 0;

export function toast(kind, message, action) {
  const id = ++toastId;
  commit((s) => { s.toasts = [...s.toasts, { id, kind, message, action }]; });
  setTimeout(() => dismissToast(id), action ? 8000 : 4000);
}

export function dismissToast(id) {
  commit((s) => { s.toasts = s.toasts.filter((t) => t.id !== id); });
}
