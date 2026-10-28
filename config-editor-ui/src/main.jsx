import { render } from "preact";
import { useEffect, useReducer } from "preact/hooks";
import {
  applyRoute, dismissToast, focusSearch, load, save, setQuery, setSidebar, validate,
} from "./actions.js";
import { Drawer } from "./inspector.jsx";
import { Header, Page } from "./page.jsx";
import { Sidebar } from "./sidebar.jsx";
import { isDirty, snapshot, store, subscribe } from "./store.js";
import { Icon } from "./ui.jsx";

const TOAST_LOOK = {
  success: ["checkCircle", "text-ok"],
  warning: ["alert", "text-warn"],
  error: ["xCircle", "text-danger"],
};

function Toasts() {
  return (
    <div class="pointer-events-none fixed right-4 bottom-4 left-4 z-50 flex flex-col items-end gap-2 sm:left-auto" aria-live="polite">
      {store.toasts.map((t) => {
        const [icon, tint] = TOAST_LOOK[t.kind];
        return (
          <div
            key={t.id} role={t.kind === "error" ? "alert" : "status"}
            class="pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-xl border border-line bg-surface px-4 py-3 text-sm shadow-pop"
          >
            <Icon name={icon} class={`mt-0.5 size-4 ${tint}`} />
            <p class="min-w-0 flex-1 leading-5 text-body">{t.message}</p>
            {t.action ? (
              <button type="button" class="shrink-0 font-medium text-brand hover:underline" onClick={() => { dismissToast(t.id); t.action.run(); }}>
                {t.action.label}
              </button>
            ) : null}
            <button type="button" class="-mr-1 shrink-0 cursor-pointer rounded text-faint hover:text-ink" aria-label="Dismiss" onClick={() => dismissToast(t.id)}>
              <Icon name="x" />
            </button>
          </div>
        );
      })}
    </div>
  );
}

const typing = () => /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName) || document.activeElement?.isContentEditable;

function App() {
  const [, rerender] = useReducer((n) => n + 1, 0);

  useEffect(() => {
    subscribe(rerender);
    try { if (localStorage.getItem("tg-sidebar") === "hidden") setSidebar(false, { instant: true }); } catch { /* storage blocked */ }
    const onKey = (e) => {
      const mod = e.ctrlKey || e.metaKey;
      const key = e.key.toLowerCase();
      if (mod && key === "s") { e.preventDefault(); save(); }
      else if (mod && key === "b") { e.preventDefault(); setSidebar(!store.sideOpen); }
      else if (mod && key === "k") { e.preventDefault(); focusSearch(); }
      else if (e.key === "/" && !mod && !e.altKey && !typing() && !store.drawerOpen) { e.preventDefault(); focusSearch(); }
      else if (e.key === "Escape" && store.query && !store.drawerOpen) setQuery("");
    };
    const onUnload = (e) => { if (store.schema && isDirty()) e.preventDefault(); };
    const onHash = () => applyRoute();
    document.addEventListener("keydown", onKey);
    window.addEventListener("beforeunload", onUnload);
    window.addEventListener("hashchange", onHash);
    load();
    return () => {
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("beforeunload", onUnload);
      window.removeEventListener("hashchange", onHash);
    };
  }, []);

  // Validate ~400 ms after the last edit.
  useEffect(() => {
    if (!store.schema || store.validated === snapshot()) return undefined;
    const timer = setTimeout(validate, 400);
    return () => clearTimeout(timer);
  });

  const withSide = store.sideRender;
  return (
    <>
      <a href="#main" class="sr-only z-50 rounded-md bg-brand-solid px-3 py-2 text-white focus:not-sr-only focus:fixed focus:top-3 focus:left-3">
        Skip to content
      </a>
      <Header />
      <div class={withSide ? "lg:grid lg:grid-cols-[260px_minmax(0,1fr)] lg:items-start" : ""}>
        <Sidebar />
        <main id="main" tabindex={-1} class="min-w-0 px-4 pt-6 pb-24 focus:outline-none sm:px-8 sm:pt-8">
          <div class="mx-auto w-full max-w-[760px]">
            <Page />
          </div>
        </main>
      </div>
      <Drawer />
      <div class="sr-only" role="status" aria-live="polite">{store.copied ? "Copied" : ""}</div>
      <Toasts />
    </>
  );
}

render(<App />, document.getElementById("app"));
