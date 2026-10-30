import { copyCommand, go, setSection } from "./actions.js";
import { Field, fieldGroups } from "./fields.jsx";
import { SECTION_ICONS } from "./icons.js";
import {
  PAGES, WIDGET_FIELD_IDS, getPath, isAgentReady, isOn, issueAt, issuesFor, navItem, section, store,
  valueOrDefault,
} from "./store.js";
import { Callout, Icon, IssueCallout, Segmented } from "./ui.jsx";
import { AuthWidget, AuthorizationWidget, RemoteToolsWidget } from "./widgets.jsx";

const Heading = ({ children, code }) => (
  <h1 id="page-title" tabindex={-1} class="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-2xl font-semibold tracking-tight text-ink focus:outline-none">
    {children}
    {code ? <code class="rounded border border-line bg-raised px-1.5 font-mono text-xs font-normal text-faint">{code}</code> : null}
  </h1>
);

const Lead = ({ children }) => <p class="mt-1.5 max-w-[62ch] text-[15px] leading-6 text-muted">{children}</p>;

const Eyebrow = ({ children }) => <h2 class="mt-10 mb-2 eyebrow">{children}</h2>;

// Link rows: icon, title, one line of text.
function PageLink({ id, text, warn, compact }) {
  const item = navItem(id);
  if (!item) return null;
  return (
    <a
      href={`#${id}`} onClick={(e) => { e.preventDefault(); go(id); }}
      class={`focus-ring group flex items-center gap-3.5 border-t border-line py-2 last:border-b ${compact ? "min-h-12" : "min-h-14"}`}
    >
      <Icon name={SECTION_ICONS[id] || "box"} class="size-[18px] text-faint" />
      <span class={`min-w-0 flex-1 ${compact ? "flex flex-wrap items-baseline gap-x-4" : ""}`}>
        <span class={`block text-sm text-ink group-hover:text-brand ${compact ? "min-w-40 font-medium" : "font-semibold"}`}>{item.title}</span>
        <span class={`block text-[13px] leading-5 ${warn ? "text-warn" : "text-muted"}`}>{text ?? item.description}</span>
      </span>
      <Icon name="chevronRight" class="size-4 text-faint" />
    </a>
  );
}

// ------------------------------------------------------------------ get running

function offText(id) {
  return `Not set. ${section(id)?.off_summary || ""}`.trim();
}

function authSummary() {
  const a = store.config.auth;
  if (a === undefined || a === null) return { text: offText("auth") };
  if (a === "jwt") return { text: "JWT bearer tokens." };
  if (a && typeof a === "object") return { text: `Custom backend${a.path ? `: ${a.path}` : "."}` };
  return { text: String(a) };
}

function rateSummary() {
  const r = store.config.rate_limit;
  if (r === undefined || r === null) return { text: offText("rate_limit") };
  const v = (k) => valueOrDefault(["rate_limit", k]);
  if (v("enabled") === false) return { text: "Settings kept, not enforced." };
  const by = { ip: "per IP", user: "per user", global: "across all clients" }[v("by")] || v("by");
  const warn = issueAt("rate_limit.backend")?.level === "warning";
  const custom = ["requests", "window", "by"].some((k) => getPath(store.config, ["rate_limit", k]) !== undefined
    && getPath(store.config, ["rate_limit", k]) !== store.fields.get(`rate_limit.${k}`)?.default);
  return {
    text: `${v("requests")} requests per ${v("window")} s, ${by}${custom ? "" : " (default)"}.${warn ? " The memory backend counts per process." : ""}`,
    warn,
  };
}

function traceSummary() {
  const o = store.config.observability;
  if (o === undefined || o === null) return { text: offText("observability") };
  const on = [];
  if (valueOrDefault(["observability", "logfire", "enabled"])) on.push("Logfire");
  if (valueOrDefault(["observability", "langsmith", "enabled"])) on.push("LangSmith");
  const level = valueOrDefault(["observability", "level"]);
  return { text: `Level ${level}${on.length ? `, sending to ${on.join(" and ")}` : ", no destination chosen yet"}.` };
}

function GetRunning() {
  const core = section("core");
  const ready = isAgentReady();
  const general = core ? issuesFor("core").filter((i) => !core.fields.some((f) => f.path.join(".") === i.path)) : [];
  const rows = [["auth", authSummary()], ["rate_limit", rateSummary()], ["observability", traceSummary()]];
  const all = store.nav.flatMap((g) => g.items).filter((id) => id !== "get-running");
  return (
    <>
      <Heading>{ready ? "You're ready to run" : "One step to run your agent"}</Heading>
      <Lead>{PAGES["get-running"].description}</Lead>
      <div class={`mt-4 flex items-center gap-2 font-medium ${ready ? "text-ok" : "text-muted"}`}>
        <Icon name={ready ? "checkCircle" : "circle"} class="size-[18px]" />
        Required: {ready ? "1" : "0"} of 1 done
      </div>
      {general.length ? <div class="mt-4 space-y-2">{general.map((i, n) => <IssueCallout key={n} issue={i} />)}</div> : null}
      <div class="mt-2">
        {core ? fieldGroups(core.fields).flatMap((g) => g.fields).map((f) => <Field key={f.path.join(".")} field={f} />) : null}
      </div>
      {ready ? (
        <div class="mt-7 border-t border-line pt-5">
          <div class="flex items-center gap-2.5 font-medium text-ok">
            <Icon name="checkCircle" class="size-[18px]" />Ready for 10xgraph dev
          </div>
          <div class="mt-3 flex max-w-[420px] items-center gap-2 rounded-lg border border-line-strong bg-sunken py-0 pr-1 pl-3.5">
            <code class="flex-1 font-mono text-[13px] font-medium text-ink">10xgraph dev</code>
            {store.copied ? <span class="text-xs text-ok">Copied</span> : null}
            <button
              type="button" class="icon-btn" onClick={copyCommand}
              aria-label={store.copied ? "Copied 10xgraph dev" : "Copy command 10xgraph dev"}
            >
              <Icon name="copy" class="size-[18px]" />
            </button>
          </div>
          <p class="mt-3 text-muted">Everything else already has working defaults.</p>
        </div>
      ) : (
        <p class="mt-4 text-xs text-faint">Add the agent graph above, then run 10xgraph dev.</p>
      )}
      <Eyebrow>Before production (optional)</Eyebrow>
      <div>{rows.map(([id, s]) => <PageLink key={id} id={id} text={s.text} warn={s.warn} />)}</div>
      <Eyebrow>What you can configure</Eyebrow>
      <div>{all.map((id) => <PageLink key={id} id={id} compact />)}</div>
    </>
  );
}

// ------------------------------------------------------------------ section pages

export function EnvList({ vars }) {
  return (
    <dl class="mb-3 grid gap-x-4 gap-y-1 sm:grid-cols-[minmax(0,260px)_minmax(0,1fr)]">
      {vars.map((v) => [
        <dt key={`${v.name}-n`} class="py-0.5 font-mono text-xs font-medium break-all text-ink">{v.name}</dt>,
        <dd key={`${v.name}-p`} class="m-0 pb-2 text-[13px] text-muted sm:py-0.5">{v.purpose}</dd>,
      ])}
    </dl>
  );
}

function EnvDisclosure({ vars, open }) {
  if (!vars?.length) return null;
  return (
    <details class="group mt-9 border-t border-line pt-1" open={open}>
      <summary class="focus-ring flex min-h-11 cursor-pointer list-none items-center gap-2 font-medium text-muted [&::-webkit-details-marker]:hidden">
        <Icon name="chevronRight" class="size-4 transition-transform group-open:rotate-90" />Set in .env
      </summary>
      <p class="mb-2 text-xs text-faint">Read only. The editor never reads .env, so only names are shown.</p>
      <EnvList vars={vars} />
    </details>
  );
}

function SectionWidget({ sec }) {
  if (sec.widget === "auth") return <AuthWidget />;
  if (sec.widget === "authorization") return <AuthorizationWidget sec={sec} />;
  if (sec.widget === "remote_tools") return <RemoteToolsWidget />;
  return null;
}

// Issues that no rendered field or widget shows inline.
export function generalIssues(sec) {
  const inline = new Set([...sec.fields.map((f) => f.path.join(".")), ...Object.keys(WIDGET_FIELD_IDS)]);
  return issuesFor(sec.id).filter((i) => !inline.has(i.path));
}

// Fields and widget of a section, no heading. Shared by the page and search results.
export function SectionFields({ sec }) {
  const on = isOn(sec);
  const widget = sec.widget && (on || !sec.optional);
  return (
    <>
      {widget ? <div class="mt-6"><SectionWidget sec={sec} /></div> : null}
      <div class={sec.optional && !on ? "opacity-80" : ""}>
        {fieldGroups(sec.fields).map((g, i) => (
          <div key={g.name}>
            {g.name ? (
              <h3 class={`eyebrow ${i === 0 ? "mt-6" : "mt-8 border-t border-line pt-4"}`}>{g.name}</h3>
            ) : null}
            {g.fields.map((f) => <Field key={f.path.join(".")} field={f} />)}
          </div>
        ))}
      </div>
    </>
  );
}

function SectionPage({ sec }) {
  const on = isOn(sec);
  const general = generalIssues(sec);
  return (
    <>
      <Heading code={sec.key}>{sec.title}</Heading>
      <Lead>{sec.description}</Lead>
      {general.length ? <div class="mt-4 space-y-2">{general.map((i, n) => <IssueCallout key={n} issue={i} />)}</div> : null}
      {sec.optional ? (
        <div class="mt-5 flex flex-wrap items-center gap-4">
          <Segmented
            label={`Settings for ${sec.title}`} value={on ? "c" : "d"}
            options={[["d", "Use defaults"], ["c", "Customize"]]}
            onPick={(v) => setSection(sec, v === "c")}
          />
          <p class="m-0 min-w-56 flex-1 text-[13px] text-muted">
            {on ? `Saved in 10xgraph.json under ${sec.key}.` : sec.off_summary}
          </p>
        </div>
      ) : null}
      {sec.notes?.length ? <div class="mt-5"><Callout kind="info" lines={sec.notes} /></div> : null}
      <SectionFields sec={sec} />
      <EnvDisclosure vars={sec.env} />
    </>
  );
}

// ------------------------------------------------------------------ environment

function EnvironmentPage() {
  const groups = store.schema.environment || [];
  return (
    <>
      <Heading>{PAGES.environment.title}</Heading>
      <Lead>
        These variables are set in .env or your deployment environment. The editor never reads that file, so only
        names and purposes are shown.
      </Lead>
      {groups.map((g) => (
        <section key={g.title}>
          <h2 class="mt-8 mb-2 text-sm font-semibold text-ink">{g.title}</h2>
          {g.note ? <p class="mb-2 text-xs text-faint">{g.note}</p> : null}
          <EnvList vars={g.vars} />
        </section>
      ))}
    </>
  );
}

// ------------------------------------------------------------------ search

const norm = (s) => String(s ?? "").toLowerCase();

function search(terms) {
  const hit = (text) => terms.every((w) => norm(text).includes(w));
  const results = [];
  for (const sec of store.sections) {
    const secHit = hit(`${sec.title} ${sec.description} ${sec.key || ""}`);
    const fields = sec.fields.filter((f) => secHit || hit(`${f.label} ${f.help || ""} ${f.path.join(".")}`));
    const env = (sec.env || []).filter((v) => hit(`${v.name} ${v.purpose}`));
    const widget = !!sec.widget && secHit;
    if (fields.length || env.length || widget) results.push({ id: sec.id, sec, fields, env, widget });
  }
  const envGroups = (store.schema.environment || [])
    .map((g) => ({ title: g.title, vars: g.vars.filter((v) => hit(`${v.name} ${v.purpose} ${g.title}`)) }))
    .filter((g) => g.vars.length);
  return { results, envGroups };
}

function SearchResults() {
  const terms = norm(store.query).split(/\s+/).filter(Boolean);
  const { results, envGroups } = search(terms);
  const count = results.reduce((n, r) => n + r.fields.length + r.env.length + (r.widget ? 1 : 0), 0)
    + envGroups.reduce((n, g) => n + g.vars.length, 0);
  if (!count) {
    return (
      <>
        <Heading>No matches</Heading>
        <p class="mt-8 text-muted">Nothing matches "{store.query}". Try a shorter word, or press Esc to go back.</p>
      </>
    );
  }
  return (
    <>
      <Heading code={store.query}>{count} {count === 1 ? "setting matches" : "settings match"}</Heading>
      {results.map(({ id, sec, fields, env, widget }) => (
        <section key={id} class="mt-8">
          <h2 class="eyebrow m-0 flex items-center gap-2">
            <Icon name={SECTION_ICONS[id] || "box"} class="size-4" />{sec.title}
            <a
              href={`#${id}`} onClick={(e) => { e.preventDefault(); go(id); }}
              class="focus-ring ml-auto text-xs font-medium tracking-normal text-brand normal-case hover:underline"
            >
              Open page
            </a>
          </h2>
          {widget ? <div class="mt-4"><SectionWidget sec={sec} /></div> : null}
          {fields.map((f) => <Field key={f.path.join(".")} field={f} />)}
          {env.length ? (
            <details class="group mt-5" open>
              <summary class="focus-ring flex min-h-11 cursor-pointer list-none items-center gap-2 font-medium text-muted [&::-webkit-details-marker]:hidden">
                <Icon name="chevronRight" class="size-4 transition-transform group-open:rotate-90" />Set in .env
              </summary>
              <EnvList vars={env} />
            </details>
          ) : null}
        </section>
      ))}
      {envGroups.length ? (
        <section class="mt-8">
          <h2 class="eyebrow m-0 flex items-center gap-2">
            <Icon name="terminal" class="size-4" />{PAGES.environment.title}
            <a
              href="#environment" onClick={(e) => { e.preventDefault(); go("environment"); }}
              class="focus-ring ml-auto text-xs font-medium tracking-normal text-brand normal-case hover:underline"
            >
              Open page
            </a>
          </h2>
          {envGroups.map((g) => (
            <div key={g.title}>
              <h3 class="mt-4 mb-1.5 text-sm font-semibold text-ink">{g.title}</h3>
              {g.note ? <p class="mb-1.5 text-xs text-faint">{g.note}</p> : null}
              <EnvList vars={g.vars} />
            </div>
          ))}
        </section>
      ) : null}
    </>
  );
}

// ------------------------------------------------------------------ router

export function PaneContent() {
  if (store.query.trim()) return <SearchResults />;
  if (store.page === "get-running") return <GetRunning />;
  if (store.page === "environment") return <EnvironmentPage />;
  const sec = section(store.page);
  return sec ? <SectionPage sec={sec} /> : <GetRunning />;
}
