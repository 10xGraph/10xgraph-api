import { go, setQuery } from "./actions.js";
import { SECTION_ICONS } from "./icons.js";
import { navItem, section, sectionChanged, store, worstIssue } from "./store.js";
import { Icon } from "./ui.jsx";

function Markers({ id }) {
  const changed = !!section(id) && sectionChanged(section(id));
  const issue = worstIssue(id);
  if (!changed && !issue) return null;
  return (
    <span class="flex shrink-0 gap-1">
      {changed ? <span class="size-2 rounded-full bg-brand" title="Changed from default" /> : null}
      {issue ? (
        <span
          class={`size-2 rounded-full ${issue.level === "error" ? "bg-danger" : "bg-warn"}`}
          title={issue.level === "error" ? "Needs attention" : "Has a warning"}
        />
      ) : null}
    </span>
  );
}

function NavLink({ id }) {
  const item = navItem(id);
  if (!item) return null;
  const current = !store.query && store.page === id;
  return (
    <a
      href={`#${id}`} aria-current={current ? "page" : undefined}
      onClick={(e) => { e.preventDefault(); go(id); }}
      ref={(el) => { if (el && current && window.innerWidth < 1024) el.scrollIntoView({ block: "nearest", inline: "nearest" }); }}
      class={`focus-ring flex min-h-10 items-center gap-2.5 rounded-lg px-3 font-medium transition-colors duration-150 max-lg:min-h-11 max-lg:shrink-0 max-lg:whitespace-nowrap ${current
        ? "bg-brand-soft text-ink" : "text-muted hover:bg-raised hover:text-ink"}`}
    >
      <Icon name={SECTION_ICONS[id] || "box"} class={`size-[18px] ${current ? "text-brand" : ""}`} />
      <span class="min-w-0 flex-1 truncate">{item.title}</span>
      <Markers id={id} />
    </a>
  );
}

// Sticky column from lg up; a horizontal tab row above the page below that.
export function Sidebar() {
  if (!store.sideRender) return null;
  return (
    <aside
      id="side" aria-label="Settings navigation"
      class={`contents lg:sticky lg:top-14 lg:block lg:h-[calc(100dvh-3.5rem)] lg:overflow-y-auto lg:border-r lg:border-line lg:bg-surface lg:px-3 lg:pt-4 lg:pb-16 lg:transition-[transform,opacity] lg:duration-200 lg:ease-out ${store.sideShown
        ? "lg:translate-x-0 lg:opacity-100" : "lg:-translate-x-6 lg:opacity-0"}`}
      inert={!store.sideOpen}
    >
      <div role="search" class="relative mx-1 mb-3 max-lg:mx-4 max-lg:mt-3">
        <Icon name="search" class="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-faint" />
        <label for="q" class="sr-only">Search settings</label>
        <input
          id="q" type="search" value={store.query} autocomplete="off" spellcheck={false}
          placeholder="Search settings  ( / )" class="input pl-10"
          onInput={(e) => setQuery(e.currentTarget.value)}
        />
      </div>
      <nav
        aria-label="Pages"
        class="max-lg:sticky max-lg:top-14 max-lg:z-20 max-lg:mt-3 max-lg:flex max-lg:gap-0.5 max-lg:overflow-x-auto max-lg:border-b max-lg:border-line max-lg:bg-surface max-lg:px-3 max-lg:py-1.5"
      >
        {store.nav.map((g) => (
          <div key={g.label || "top"} class="max-lg:contents">
            {g.label ? <div class="mt-4 mb-1 px-3 text-[11px] font-semibold tracking-wider text-faint uppercase max-lg:hidden">{g.label}</div> : null}
            {g.items.map((id) => <NavLink key={id} id={id} />)}
          </div>
        ))}
      </nav>
    </aside>
  );
}
