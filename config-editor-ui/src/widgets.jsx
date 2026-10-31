import { useEffect, useState } from "preact/hooks";
import { Field, fieldGroups } from "./fields.jsx";
import { commit, isObject, issueAt, setPath, store } from "./store.js";
import {
  Callout, ChoiceGroup, Chips, FieldIssue, FieldMeta, Group, Icon, Pill, Row, Segmented, Select, TextRow, Toggle,
} from "./ui.jsx";

// ------------------------------------------------------------------ auth

export function AuthWidget() {
  const value = store.config.auth;
  const mode = isObject(value) ? "custom" : "jwt";
  return (
    <div class="space-y-6">
      <Group title="Method">
        <ChoiceGroup
          name="auth" label="Authentication method" value={mode}
          options={[
            ["jwt", "JWT", "Verify bearer tokens signed with JWT_SECRET_KEY."],
            ["custom", "Custom backend", "Your own BaseAuth subclass decides who is calling."],
          ]}
          onPick={(m) => commit((s) => { s.config.auth = m === "jwt" ? "jwt" : { method: "custom", path: "" }; })}
        />
      </Group>
      {mode === "custom" ? (
        <Group title="Backend">
          <div class="panel">
            <TextRow
              id="auth-path" label="Auth class" path="auth.path" value={value.path} placeholder="auth.custom:MyAuth"
              help="Import path of a BaseAuth subclass, as module:attribute." issue={issueAt("auth.path")}
              onInput={(v) => commit((s) => { s.config.auth = { ...s.config.auth, method: "custom", path: v }; })}
            />
          </div>
        </Group>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------ cache store

const CACHE_CARDS = [
  ["memory", "Memory", "This process only"],
  ["redis", "Redis", "Shared. Redis, Valkey, Dragonfly, KeyDB"],
  ["custom", "Custom", "Your BaseCacheStore class"],
];
const DEFAULT_RATE_LIMIT = { requests: 100, window: 60, by: "ip" };

const cacheStore = () => (isObject(store.config.cache_store) ? store.config.cache_store : {});

// Edit cache_store in place; the key goes away when nothing is left in it.
function editCache(mutate) {
  commit((s) => {
    const next = isObject(s.config.cache_store) ? s.config.cache_store : {};
    mutate(next);
    if (Object.keys(next).length) s.config.cache_store = next;
    else delete s.config.cache_store;
  });
}

const rateLimitOn = () => {
  const r = cacheStore().rate_limit;
  return isObject(r) && r.enabled !== false;
};
const revocationOn = () => {
  const r = cacheStore().token_revocation;
  return r === true || (isObject(r) && r.enabled !== false);
};

function setCacheType(kind) {
  editCache((c) => {
    delete c.url;
    delete c.path;
    if (kind === "memory") delete c.type;
    else c.type = kind;
  });
}

function setRateLimit(on) {
  editCache((c) => {
    if (!on) delete c.rate_limit;
    else if (isObject(c.rate_limit)) delete c.rate_limit.enabled;
    else c.rate_limit = { ...DEFAULT_RATE_LIMIT };
  });
}

function setRevocation(on) {
  editCache((c) => {
    if (!on) delete c.token_revocation;
    else if (isObject(c.token_revocation)) delete c.token_revocation.enabled;
    else c.token_revocation = true;
  });
}

const putCache = (path, value) => commit((st) => {
  setPath(st.config, path, value);
});

function TypeCards({ value, onPick }) {
  const move = (e, i) => {
    const d = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    const next = CACHE_CARDS[(i + d + CACHE_CARDS.length) % CACHE_CARDS.length][0];
    onPick(next);
    requestAnimationFrame(() => document.getElementById(`cache-type-${next}`)?.focus());
  };
  return (
    <div role="radiogroup" aria-label="Cache store type" class="grid gap-3 sm:grid-cols-3">
      {CACHE_CARDS.map(([v, title, detail], i) => {
        const selected = v === value;
        return (
          <button
            key={v} type="button" role="radio" id={`cache-type-${v}`} aria-checked={selected ? "true" : "false"}
            tabIndex={selected ? 0 : -1} onKeyDown={(e) => move(e, i)}
            class={`focus-ring flex cursor-pointer items-start gap-3 rounded-xl border p-4 text-left transition-colors duration-150 motion-reduce:transition-none ${selected
              ? "border-brand bg-brand-soft"
              : "border-line bg-surface shadow-card hover:border-line-strong"}`}
            onClick={() => { if (!selected) onPick(v); }}
          >
            <span class={`mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full border ${selected ? "border-brand-solid bg-brand-solid" : "border-line-strong"}`}>
              {selected ? <span class="size-1.5 rounded-full bg-white" /> : null}
            </span>
            <span class="min-w-0">
              <span class="block text-sm font-medium text-ink">{title}</span>
              <span class="mt-0.5 block text-[13px] leading-5 text-muted">{detail}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function InlineNote({ warn, children }) {
  return (
    <p class={`mt-3 flex items-start gap-1.5 text-[13px] leading-5 ${warn ? "text-warn" : "text-muted"}`}>
      {warn ? <Icon name="alert" class="mt-0.5 size-3.5" /> : null}
      <span>{children}</span>
    </p>
  );
}

function FeatureHead({ id, title, text, on, onChange }) {
  return (
    <div class="flex items-center gap-4">
      <div class="min-w-0 flex-1">
        <h3 class="text-base font-semibold text-ink">{title}</h3>
        <p class="mt-0.5 text-[13px] leading-5 text-muted">{text}</p>
      </div>
      <Toggle id={id} on={on} label={title} onChange={onChange} />
    </div>
  );
}

const LIMIT_BY = [["ip", "IP"], ["user", "User"], ["global", "Global"]];

function RateLimitPanel({ field, memory }) {
  const [adv, setAdv] = useState(false);
  const r = cacheStore().rate_limit;
  const by = (isObject(r) && r.by) || "ip";
  const failOpen = !(isObject(r) && r.fail_open === false);
  const advanced = ["trusted_proxy_headers", "trusted_proxy_hops", "trusted_proxies", "exclude_paths"];
  return (
    <>
      {memory ? <InlineNote warn>Counts per worker with the Memory store.</InlineNote> : null}
      <div class="grid gap-x-6 sm:grid-cols-2">
        <Field field={field("cache_store.rate_limit.requests")} />
        <Field field={field("cache_store.rate_limit.window")} />
        <div class="pt-5">
          <span id="cache-by-label" class="block text-sm font-semibold text-ink">Limit by</span>
          <div class="mt-2.5">
            <Segmented
              id="f-cache_store-rate_limit-by" label="Limit by" options={LIMIT_BY} value={by}
              onPick={(v) => putCache(["cache_store", "rate_limit", "by"], v)}
            />
          </div>
          <FieldIssue issue={issueAt("cache_store.rate_limit.by")} />
        </div>
        <div class="pt-5">
          <span id="cache-fail-open-label" class="block text-sm font-semibold text-ink">Fail open</span>
          <div class="mt-2.5 flex items-center gap-3">
            <Toggle
              id="f-cache_store-rate_limit-fail_open" on={failOpen} label="Fail open"
              onChange={(v) => putCache(["cache_store", "rate_limit", "fail_open"], v)}
            />
            <p class="m-0 text-[13px] leading-5 text-muted">
              {failOpen ? "Allow requests if the store errors." : "Deny with 503 if the store errors."}
            </p>
          </div>
        </div>
      </div>
      <div class="mt-5 border-t border-line pt-1">
        <button
          type="button" aria-expanded={adv ? "true" : "false"} aria-controls="cache-advanced"
          class="focus-ring flex min-h-11 cursor-pointer items-center gap-2 rounded font-medium text-muted hover:text-ink"
          onClick={() => setAdv(!adv)}
        >
          <Icon name="chevronRight" class={`size-4 transition-transform duration-150 motion-reduce:transition-none ${adv ? "rotate-90" : ""}`} />
          Advanced
        </button>
        {adv ? (
          <div id="cache-advanced">
            {advanced.map((k) => <Field key={k} field={field(`cache_store.rate_limit.${k}`)} />)}
          </div>
        ) : null}
      </div>
    </>
  );
}

const TABS = [["rate", "Rate limiting"], ["revoke", "Token revocation"]];

export function CacheStoreWidget({ sec }) {
  const [tab, setTab] = useState("rate");
  const kind = cacheStore().type || "memory";
  const field = (path) => sec.fields.find((f) => f.path.join(".") === path);
  const on = { rate: rateLimitOn(), revoke: revocationOn() };
  const memory = kind === "memory";
  const warned = {
    rate: !!issueAt("cache_store.rate_limit"),
    revoke: !!issueAt("cache_store.token_revocation"),
  };
  const onKey = (e, i) => {
    const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    const to = e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : d ? (i + d + TABS.length) % TABS.length : -1;
    if (to < 0) return;
    e.preventDefault();
    setTab(TABS[to][0]);
    document.getElementById(`cache-tab-${TABS[to][0]}`)?.focus();
  };
  return (
    <div>
      <Group title="Store type">
        <TypeCards value={kind} onPick={setCacheType} />
        {kind === "redis" ? <Field field={field("cache_store.url")} /> : null}
        {kind === "custom" ? <Field field={field("cache_store.path")} /> : null}
        {memory ? (
          <p class="mt-3 mb-0 text-[13px] leading-5 text-muted">
            Not shared between workers: each worker keeps its own locks, limits and revocations.
          </p>
        ) : null}
      </Group>

      <div role="tablist" aria-label="Cache store features" class="mt-8 flex gap-1 border-b border-line">
        {TABS.map(([id, label], i) => {
          const sel = tab === id;
          return (
            <button
              key={id} type="button" role="tab" id={`cache-tab-${id}`} aria-selected={sel ? "true" : "false"}
              aria-controls={`cache-panel-${id}`} tabIndex={sel ? 0 : -1} onKeyDown={(e) => onKey(e, i)}
              class={`focus-ring -mb-px flex min-h-11 cursor-pointer items-center gap-2 border-b-2 px-3 text-sm font-medium transition-colors duration-150 motion-reduce:transition-none ${sel ? "border-brand-solid text-ink" : "border-transparent text-muted hover:text-ink"}`}
              onClick={() => setTab(id)}
            >
              {label}
              <Pill class={on[id] ? "bg-brand-soft text-brand" : "bg-raised text-muted"}>{on[id] ? "On" : "Off"}</Pill>
            </button>
          );
        })}
      </div>

      <div role="tabpanel" id={`cache-panel-${tab}`} aria-labelledby={`cache-tab-${tab}`} class="pt-5">
        {tab === "rate" ? (
          <>
            <FeatureHead
              id="cache-rate-limit" title="Rate limiting" on={on.rate} onChange={setRateLimit}
              text="Cap how many requests one caller can make."
            />
            {on.rate ? <RateLimitPanel field={field} memory={memory} />
              : warned.rate ? <InlineNote warn>Off: one caller can exhaust the service.</InlineNote> : null}
          </>
        ) : (
          <>
            <FeatureHead
              id="cache-token-revocation" title="Token revocation" on={on.revoke} onChange={setRevocation}
              text="Reject revoked tokens before they expire (10xgraph revoke)."
            />
            {on.revoke ? (
              <>
                {memory ? <InlineNote warn>10xgraph revoke cannot reach an in-memory store; pick Redis or Custom.</InlineNote> : null}
                <div class="grid gap-x-6 sm:grid-cols-2">
                  <Field field={field("cache_store.token_revocation.token_lifetime")} />
                  <Field field={field("cache_store.token_revocation.user_ttl")} />
                </div>
                <InlineNote>
                  Fails closed: while the cache store is unreachable, authenticated requests get 503 and the server will not start.
                </InlineNote>
              </>
            ) : warned.revoke ? <InlineNote warn>Off: a leaked token works until it expires.</InlineNote> : null}
          </>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ choice (id generator, thread naming)

// A dropdown of built-in names plus "custom" (a module:attr field). An unset key is the
// first option for id_generator ("Not set") and the default name for thread_name_generator.
export function ChoiceWidget({ sec }) {
  const key = sec.fields[0].path[0];
  const names = sec.choices.map((c) => c[0]);
  const value = store.config[key];
  const unsetValue = key === "thread_name_generator" ? "random" : "";
  const mode = value === undefined ? unsetValue : typeof value === "string" && names.includes(value) ? value : "custom";
  const options = [
    ...(key === "id_generator" ? [["", "Not set (the graph's own generator)"]] : []),
    ...sec.choices.map(([v, label]) => [v, key === "thread_name_generator" && v === "random" ? `${label} (default)` : label]),
    ["custom", "Custom (module:attr)"],
  ];
  const detail = mode === "" ? "The graph keeps the generator it was compiled with."
    : mode === "custom" ? "Your own class, as module:attribute."
    : sec.choices.find((c) => c[0] === mode)?.[2];
  const pick = (v) => commit((s) => {
    if (v === unsetValue) delete s.config[key];
    else s.config[key] = v === "custom" ? "" : v;
  });
  const id = `f-${key}`;
  return (
    <div>
      <Group title="Choice">
        <div class="max-w-[520px]">
          <label for={`${id}-select`} class="sr-only">{sec.title}</label>
          <Select id={`${id}-select`} options={options} value={mode} onChange={pick} />
          <p class="mt-1.5 text-[13px] text-muted">{detail}</p>
        </div>
      </Group>
      {mode === "custom" ? (
        <Group title="Class">
          <div class="panel">
            <TextRow
              id={id} label={sec.fields[0].label} path={key} value={typeof value === "string" ? value : ""}
              placeholder={sec.fields[0].placeholder} help={sec.fields[0].help} issue={issueAt(key)}
              onInput={(v) => commit((s) => { s.config[key] = v; })}
            />
          </div>
        </Group>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------ authorization

function authzModeOf(v) {
  if (isObject(v)) return "rbac";
  if (typeof v === "string") {
    const key = v.trim().toLowerCase();
    if (key === "ownership") return "ownership";
    if (["allow_all", "default", "none"].includes(key)) return "allow_all";
  }
  return v === undefined || v === null ? null : "custom";
}

const AUTHZ_PRESETS = {
  ownership: () => "ownership",
  allow_all: () => "allow_all",
  custom: () => "",
  rbac: () => ({
    backend: "rbac",
    roles: { admin: ["*"], member: ["graph:invoke", "graph:stream", "graph:read", "checkpointer:read"] },
    default_scopes: [],
    isolation: "owner",
  }),
};

export function AuthorizationWidget({ sec }) {
  const value = store.config.authorization;
  const mode = authzModeOf(value) ?? "ownership";
  return (
    <div class="space-y-6">
      <Group title="Policy">
        <ChoiceGroup
          name="authz" label="Authorization policy" value={mode}
          options={[
            ["ownership", "Ownership", "Users can only reach threads they created."],
            ["allow_all", "Allow all", "Any authenticated user can do anything."],
            ["rbac", "Roles and scopes", "Map roles to scopes, on top of owner isolation."],
            ["custom", "Custom backend", "Your own AuthorizationBackend class."],
          ]}
          onPick={(m) => commit((s) => { s.config.authorization = AUTHZ_PRESETS[m](); })}
        />
      </Group>
      {mode === "custom" ? (
        <Group title="Backend">
          <div class="panel">
            <TextRow
              id="authz-path" label="Backend class" path="authorization" placeholder="auth.permissions:MyBackend"
              value={typeof value === "string" ? value : ""} issue={issueAt("authorization")}
              help="Import path of an AuthorizationBackend, as module:attribute."
              onInput={(v) => commit((s) => { s.config.authorization = v; })}
            />
          </div>
        </Group>
      ) : null}
      {mode === "rbac" ? <Rbac sec={sec} value={value} /> : null}
    </div>
  );
}

// Renames apply on blur or Enter; the draft lives here so re-renders do not reset it.
function RoleName({ id, name, onRename }) {
  const [draft, setDraft] = useState(name);
  useEffect(() => setDraft(name), [name]);
  return (
    <div>
      <label for={id} class="sr-only">Role name</label>
      <input
        id={id} type="text" value={draft} class="input font-mono" autocomplete="off" spellcheck={false}
        onInput={(e) => setDraft(e.currentTarget.value)}
        onChange={(e) => { if (!onRename(e.currentTarget.value.trim())) setDraft(name); }}
      />
    </div>
  );
}

function Rbac({ sec, value }) {
  const roleKey = "roles" in value || !("role_scopes" in value) ? "roles" : "role_scopes";
  const roles = isObject(value[roleKey]) ? value[roleKey] : {};
  const scopeOptions = ["*", ...(sec.scopes || [])];
  const entries = Object.entries(roles);
  const setRoles = (next) => commit(() => { value[roleKey] = next; });
  const addRole = () => {
    let n = entries.length + 1;
    while (`role${n}` in roles) n++;
    setRoles({ ...roles, [`role${n}`]: [] });
  };

  return (
    <>
      <Group title="Roles">
        <div class="panel">
          <div class="hidden grid-cols-[10rem_minmax(0,1fr)_auto] gap-3 border-b border-line px-5 py-2.5 sm:grid">
            <span class="text-xs font-medium text-faint">Role</span>
            <span class="text-xs font-medium text-faint">Scopes</span>
            <span class="w-9" />
          </div>
          {entries.length ? (
            <div class="divide-y divide-line">
              {entries.map(([name, scopes], index) => (
                <div key={index} class="grid gap-3 px-5 py-4 sm:grid-cols-[10rem_minmax(0,1fr)_auto] sm:items-start">
                  <RoleName
                    id={`role-name-${index}`} name={name}
                    onRename={(renamed) => {
                      if (!renamed || (renamed !== name && renamed in roles)) return false;
                      setRoles(Object.fromEntries(Object.entries(roles).map(([k, v]) => [k === name ? renamed : k, v])));
                      return true;
                    }}
                  />
                  <Chips
                    id={`role-scopes-${index}`} values={Array.isArray(scopes) ? scopes : []}
                    placeholder="Add a scope" suggestions={scopeOptions}
                    onChange={(next) => setRoles({ ...roles, [name]: next })}
                  />
                  <button
                    type="button" class="icon-btn justify-self-end hover:text-danger" aria-label={`Remove role ${name}`}
                    onClick={() => setRoles(Object.fromEntries(Object.entries(roles).filter(([k]) => k !== name)))}
                  >
                    <Icon name="trash" />
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <p class="px-5 py-6 text-center text-sm text-muted">No roles yet. Users only get the default scopes.</p>
          )}
          <div class="flex items-center justify-between gap-4 border-t border-line px-5 py-3">
            <p class="text-[13px] text-muted">
              Read from the user's roles (or role) claim. <code class="font-mono">*</code> grants every scope.
            </p>
            <button type="button" class="btn btn-ghost" onClick={addRole}><Icon name="plus" />Add role</button>
          </div>
        </div>
      </Group>
      <Group title="Defaults">
        <div class="panel divide-y divide-line">
          <div class="px-5 py-4">
            <FieldMeta
              label="Default scopes" path="authorization.default_scopes" id="rbac-default-scopes"
              help="Granted to every authenticated user, even without a role."
            />
            <div class="mt-3">
              <Chips
                id="rbac-default-scopes" values={Array.isArray(value.default_scopes) ? value.default_scopes : []}
                placeholder="graph:read" suggestions={scopeOptions}
                onChange={(next) => commit(() => { value.default_scopes = next; })}
              />
            </div>
          </div>
          <Row>
            <FieldMeta
              label="Thread isolation" path="authorization.isolation" id="rbac-isolation"
              help="owner keeps each user's threads private; none shares them."
            />
            <div class="min-w-0 sm:pt-0.5">
              <Select
                id="rbac-isolation" options={[["owner", "owner"], ["none", "none"]]} value={value.isolation ?? "owner"}
                onChange={(v) => commit(() => { value.isolation = v; })}
              />
            </div>
          </Row>
        </div>
      </Group>
    </>
  );
}

// ------------------------------------------------------------------ remote tools

export function RemoteToolsWidget() {
  const tools = Array.isArray(store.config.remote_tools) ? store.config.remote_tools : [];
  const addTool = () => {
    commit((s) => {
      tools.push({ node: "", name: "", description: "", parameters: { type: "object", properties: {}, required: [] } });
      s.config.remote_tools = tools;
    });
    requestAnimationFrame(() => document.getElementById(`tool-${tools.length - 1}-name`)?.focus());
  };

  if (!tools.length) {
    return (
      <div class="rounded-xl border border-dashed border-line-strong bg-surface px-6 py-10 text-center">
        <div class="mx-auto flex size-11 items-center justify-center rounded-full bg-raised text-faint">
          <Icon name="wrench" class="size-5" />
        </div>
        <p class="mt-4 text-sm font-semibold text-ink">No remote tools yet</p>
        <p class="mx-auto mt-1 max-w-sm text-sm leading-6 text-muted">
          Declare a tool here and the client that calls the API runs it, for example to read the user's location.
        </p>
        <button type="button" class="btn btn-outline mt-5" onClick={addTool}><Icon name="plus" />Add remote tool</button>
      </div>
    );
  }

  return (
    <div class="space-y-4">
      {tools.map((tool, index) => <ToolCard key={index} tools={tools} tool={tool} index={index} />)}
      <button
        type="button" onClick={addTool}
        class="focus-ring flex h-12 w-full cursor-pointer items-center justify-center gap-2 rounded-xl border border-dashed border-line-strong text-sm font-medium text-muted transition-colors duration-150 hover:border-brand hover:text-brand"
      >
        <Icon name="plus" />Add remote tool
      </button>
    </div>
  );
}

function ToolCard({ tools, tool, index }) {
  const params = store.toolParams[index] ?? { text: JSON.stringify(tool.parameters ?? {}, null, 2), error: null };
  const update = (patch) => commit(() => { tools[index] = { ...tools[index], ...patch }; });
  const at = (key) => `remote_tools[${index}].${key}`;
  return (
    <section class="panel">
      <div class="flex items-center gap-3 border-b border-line px-5 py-3">
        <Icon name="wrench" class="size-4 text-faint" />
        <span class="min-w-0 flex-1 truncate font-mono text-sm font-medium text-ink">{tool.name || `tool_${index + 1}`}</span>
        {tool.node ? <span class="rounded-md border border-line bg-raised px-1.5 py-0.5 font-mono text-xs text-muted">{tool.node}</span> : null}
        <button
          type="button" class="icon-btn hover:text-danger" aria-label={`Remove ${tool.name || "tool"}`}
          onClick={() => commit((s) => { tools.splice(index, 1); s.toolParams = {}; })}
        >
          <Icon name="trash" />
        </button>
      </div>
      <div class="divide-y divide-line">
        <TextRow id={`tool-${index}-name`} label="Tool name" path={at("name")} value={tool.name} placeholder="get_location" onInput={(v) => update({ name: v })} />
        <TextRow id={`tool-${index}-node`} label="Graph node" path={at("node")} value={tool.node} placeholder="MAIN" help="The node whose model may call this tool." onInput={(v) => update({ node: v })} />
        <TextRow id={`tool-${index}-description`} label="Description" path={at("description")} value={tool.description} placeholder="Returns the user's current city" help="What the model reads when deciding to call it." onInput={(v) => update({ description: v })} />
        <div class="px-5 py-4">
          <FieldMeta label="Parameters" path={at("parameters")} help="JSON schema of the arguments. type must be object." id={`tool-${index}-params`} />
          <textarea
            id={`tool-${index}-params`} rows={7} spellcheck={false} value={params.text}
            class={`input mt-3 h-auto py-2.5 font-mono text-[13px] leading-5 ${params.error ? "input-invalid" : ""}`}
            onInput={(e) => {
              const text = e.currentTarget.value;
              commit((s) => {
                try {
                  tools[index] = { ...tools[index], parameters: JSON.parse(text) };
                  s.toolParams[index] = { text, error: null };
                } catch (err) {
                  s.toolParams[index] = { text, error: err.message };
                }
              });
            }}
          />
          <FieldIssue issue={params.error ? { level: "error", message: params.error } : null} />
        </div>
      </div>
    </section>
  );
}
