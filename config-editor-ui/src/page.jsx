import { openDrawer, save, setSidebar } from "./actions.js";
import { PaneContent } from "./pages.jsx";
import { isDirty, store } from "./store.js";
import { Callout, EmptyPanel, Icon, Pill } from "./ui.jsx";

// ------------------------------------------------------------------ header

function FileInfo() {
  const name = store.path.split(/[\\/]/).pop() || "10xgraph.json";
  const dir = store.path.slice(0, store.path.length - name.length);
  let badge = null;
  if (!store.schema) badge = null;
  else if (!store.exists) badge = <Pill class="bg-brand-soft text-brand">New</Pill>;
  else if (isDirty()) badge = <Pill class="bg-warn-soft text-warn">Unsaved</Pill>;
  else badge = <Pill class="bg-ok-soft text-ok">Saved</Pill>;
  return (
    <div class="flex min-w-0 flex-1 items-center gap-2" title={store.path || undefined}>
      <Icon name="file" class="size-4 text-faint" />
      <span class="truncate font-mono text-[13px] font-medium text-ink">{name}</span>
      {badge}
      {dir ? (
        <span class="hidden min-w-0 truncate font-mono text-xs text-faint lg:block" dir="rtl"><bdi>{dir}</bdi></span>
      ) : null}
    </div>
  );
}

export function Header() {
  const ready = !!store.schema && !store.fatal;
  const label = store.sideOpen ? "Hide sidebar" : "Show sidebar";
  return (
    <header class="sticky top-0 z-40 flex h-14 items-center gap-2 border-b border-line bg-surface px-3 sm:gap-3 sm:px-4">
      <button
        type="button" class="icon-btn" aria-controls="side" aria-expanded={store.sideOpen ? "true" : "false"}
        aria-label={label} title={`${label} (Ctrl/Cmd+B)`} onClick={() => setSidebar(!store.sideOpen)}
      >
        <Icon name="panel" class="size-[18px]" />
      </button>
      <div class="flex shrink-0 items-center gap-2.5">
        {/* The 10xGraph mark, same geometry as agentflow-docs/src/components/Logo.astro. */}
        <svg viewBox="0 0 48 48" class="size-7 shrink-0 text-ink" aria-hidden="true">
          <rect x="4" y="4" width="40" height="40" rx="11" fill="none" stroke="currentColor" stroke-width="4.4" />
          <path d="M13 33L21 23L34 15L34 31L21 23" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />
          <g fill="currentColor"><circle cx="21" cy="23" r="4" /><circle cx="34" cy="15" r="4" /><circle cx="34" cy="31" r="4" /></g>
          <circle cx="13" cy="33" r="4.6" class="fill-brand-amber" />
        </svg>
        <div class="hidden items-baseline gap-1.5 sm:flex">
          <span class="text-[15px] font-semibold tracking-tight text-ink">10xGraph</span>
          <span class="text-[15px] text-faint">Config</span>
        </div>
      </div>
      <span class="hidden h-6 w-px shrink-0 bg-line md:block" aria-hidden="true" />
      <FileInfo />
      <button type="button" class="btn btn-ghost" aria-label="View 10xgraph.json" aria-haspopup="dialog" disabled={!ready} onClick={() => openDrawer()}>
        <Icon name="code" class="size-[18px]" />
        <span class="hidden md:inline">View 10xgraph.json</span>
      </button>
      <button
        type="button" class="btn btn-primary" onClick={save}
        disabled={!ready || !!store.busy || (store.exists && !isDirty() && !store.parseError)}
      >
        {store.busy === "save" ? <Icon name="spinner" class="size-4 animate-spin" /> : null}
        {store.exists ? "Save" : "Create file"}
        <span class="kbd hidden lg:inline-flex">Ctrl S</span>
      </button>
    </header>
  );
}

// ------------------------------------------------------------------ page

export function Page() {
  if (store.fatal) {
    return (
      <div class="panel mt-10">
        <EmptyPanel icon="xCircle" tint="text-danger" title={store.fatal.title} text={store.fatal.text} />
      </div>
    );
  }
  if (!store.schema) {
    return (
      <div class="animate-pulse space-y-4 pt-2" aria-label="Loading">
        <div class="h-7 w-48 rounded-md bg-raised" />
        <div class="h-4 w-80 max-w-full rounded-md bg-raised" />
        {[0, 1, 2].map((i) => <div key={i} class="h-16 rounded-xl bg-raised" />)}
      </div>
    );
  }
  return (
    <>
      {store.parseError ? (
        <div class="mb-6">
          <Callout
            kind="warn" title="The existing file is not valid JSON"
            lines={[`${store.parseError}. The form starts from defaults; saving replaces the file and keeps the old one as 10xgraph.json.bak.`]}
          />
        </div>
      ) : null}
      <PaneContent />
    </>
  );
}
