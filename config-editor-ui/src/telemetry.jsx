import { useState } from "preact/hooks";
import { commit, deletePath, getPath, isObject, issueAt, setPath, store, valueOrDefault } from "./store.js";
import { Chips, Disclosure, FieldIssue, Icon, Select, Toggle } from "./ui.jsx";

const linkBtn = "focus-ring min-h-8 cursor-pointer rounded px-1.5 text-[13px] font-medium text-brand transition-colors duration-150 hover:underline";
const note = "mt-2 max-w-prose text-[13px] leading-5 text-muted";

const put = (path, value) => commit((s) => {
  if (value === undefined || value === "") deletePath(s.config, path);
  else setPath(s.config, path, value);
});

// ------------------------------------------------------------------ form controls

function Labeled({ id, label, path, help, children }) {
  return (
    <div class="pt-4">
      <div class="flex items-baseline gap-2.5">
        <label for={id} class="text-sm font-semibold text-ink">{label}</label>
        {path ? <code class="hidden font-mono text-[11px] text-faint sm:inline">{path}</code> : null}
      </div>
      <div class="mt-2 max-w-[520px]">{children}</div>
      {help ? <p class={note}>{help}</p> : null}
      <FieldIssue issue={issueAt(path)} />
    </div>
  );
}

function TextField({ path, label, help, placeholder }) {
  const key = path.join(".");
  const id = `f-${path.join("-")}`;
  const value = getPath(store.config, path);
  return (
    <Labeled id={id} label={label} path={key} help={help}>
      <input
        id={id} type="text" value={value ?? ""} placeholder={placeholder} autocomplete="off" spellcheck={false}
        class={`input font-mono ${issueAt(key)?.level === "error" ? "input-invalid" : ""}`}
        onInput={(e) => put(path, e.currentTarget.value)}
      />
    </Labeled>
  );
}

function NumberField({ path, label, help }) {
  const key = path.join(".");
  const id = `f-${path.join("-")}`;
  const value = getPath(store.config, path);
  return (
    <Labeled id={id} label={label} path={key} help={help}>
      <input
        id={id} type="number" inputmode="numeric" min="1" step="1" value={value ?? ""}
        class={`input max-w-40 font-mono tabular-nums ${issueAt(key)?.level === "error" ? "input-invalid" : ""}`}
        onInput={(e) => { const raw = e.currentTarget.value; put(path, raw === "" ? undefined : Number(raw)); }}
      />
    </Labeled>
  );
}

function SelectField({ path, label, help, options, value, onChange }) {
  const id = `f-${path.join("-")}`;
  return (
    <Labeled id={id} label={label} path={path.join(".")} help={help}>
      <Select id={id} plain options={options} value={value} onChange={onChange} />
    </Labeled>
  );
}

function CheckField({ path, label, checked }) {
  const id = `f-${path.join("-")}`;
  return (
    <label class="mt-3 inline-flex min-h-11 cursor-pointer items-center gap-2.5 text-sm font-semibold text-ink">
      <input id={id} type="checkbox" class="size-[18px] accent-brand-solid" checked={checked} onChange={(e) => put(path, e.currentTarget.checked)} />
      {label}
    </label>
  );
}

// ------------------------------------------------------------------ AI tracing

const DESTINATIONS = [
  { key: "logfire", name: "Logfire", label: "Logfire", text: "Pydantic Logfire.", secret: <>Token: set <code class="font-mono text-xs">LOGFIRE_TOKEN</code> in .env.</> },
  { key: "langsmith", name: "LangSmith", label: "LangSmith", text: "LangChain's tracing service.", secret: <>API key: set <code class="font-mono text-xs">LANGSMITH_API_KEY</code> in .env.</> },
  { key: "phoenix", name: "Phoenix", label: "Phoenix", text: "Arize Phoenix, local or hosted.", secret: <>API key: set <code class="font-mono text-xs">PHOENIX_API_KEY</code> in .env. A local Phoenix does not need one.</> },
  { key: "otlp", name: "OTLP endpoint", label: "OTLP endpoint", text: "Any OpenTelemetry endpoint: a collector, Tempo, Jaeger.", secret: <>Auth headers: set <code class="font-mono text-xs">OTEL_EXPORTER_OTLP_HEADERS</code> in .env.</> },
];
const LEVELS = [["standard", "Standard"], ["spans", "Spans only"], ["full", "Full"]];
const LEVEL_HELP = {
  standard: "Adds model name, token counts and request parameters.",
  spans: "Timing and structure only. No inputs or outputs.",
  full: "Records prompts and tool I/O. May contain PII.",
};
const USER_IDS = [["raw", "Raw"], ["hash", "Hashed"], ["mask", "Masked"]];
const USER_ID_HELP = {
  raw: "The user id is sent as is.",
  hash: "A SHA-256 of the user id, keyed when TENXGRAPH_TRACE_USER_ID_KEY is set.",
  mask: "The middle of the user id is hidden.",
};

const obs = () => (isObject(store.config.observability) ? store.config.observability : {});
const isPicked = (key) => isObject(obs()[key]) && obs()[key].enabled === true;

function addDestination(key) {
  commit((s) => {
    if (!isObject(s.config.observability)) s.config.observability = { level: "standard", user_id_mode: "raw" };
    s.config.observability[key] = { enabled: true };
  });
}

function removeDestination(key) {
  commit((s) => {
    delete s.config.observability[key];
    if (!DESTINATIONS.some((d) => isPicked(d.key))) delete s.config.observability;
  });
}

// Base URL of the metrics OTLP endpoint when traces can share it, else null.
function sharedEndpoint() {
  const m = store.config.metrics;
  const url = isObject(m) && isObject(m.otlp) ? m.otlp.endpoint : null;
  if (!isObject(m) || m.enabled !== true || m.export !== "otlp" || !url) return null;
  return url.replace(/\/+$/, "").replace(/\/v1\/metrics$/, "");
}

function OtlpFields() {
  const flag = ["observability", "otlp", "use_metrics_endpoint"];
  const on = getPath(store.config, flag) === true;
  const base = sharedEndpoint();
  const id = "f-observability-otlp-use_metrics_endpoint";
  return (
    <>
      {base ? (
        <div class="pt-4">
          <div class="flex items-center gap-3">
            <Toggle id={id} on={on} label="Use the metrics endpoint" onChange={(v) => put(flag, v ? true : undefined)} />
            <label for={id} class="cursor-pointer text-sm font-semibold text-ink">Use the metrics endpoint</label>
          </div>
          <p class={note}>
            {on ? <>Traces go to <code class="font-mono text-xs text-body">{base}/v1/traces</code></> : "Send traces to the same endpoint as the metrics."}
          </p>
          <FieldIssue issue={issueAt(flag.join("."))} />
        </div>
      ) : null}
      {on && base ? null : (
        <TextField
          path={["observability", "otlp", "endpoint"]} label="Endpoint" placeholder="http://collector:4318"
          help="Base URL of an OTLP HTTP receiver. /v1/traces is appended."
        />
      )}
      {!base ? (
        <p class={note}>
          Sharing the metrics endpoint needs Metrics set to OTLP push with an endpoint.
          {on ? <> It is on in the file. <button type="button" class={linkBtn} onClick={() => put(flag, undefined)}>Stop sharing</button></> : null}
        </p>
      ) : null}
      <FieldIssue issue={on && !base ? issueAt(flag.join(".")) : null} />
      <p class={note}>
        Prometheus stores metrics only. To send traces and metrics to one place, use a collector
        (OTel Collector, Grafana Alloy, Grafana Cloud).
      </p>
    </>
  );
}

function DestinationFields({ dest }) {
  const f = (name) => ["observability", dest.key, name];
  if (dest.key === "logfire") {
    const sendPath = f("send_to_logfire");
    const consolePath = f("console");
    return (
      <>
        <TextField path={f("service_name")} label="Service name" placeholder="my-agent" />
        <CheckField path={sendPath} label="Send to Logfire" checked={getPath(store.config, sendPath) !== false} />
        <SelectField
          path={consolePath} label="Print spans to console"
          help="Logfire also takes console options that JSON cannot express, so only its own behavior or Off is offered."
          options={[["", "Logfire's own behavior"], ["off", "Off"]]}
          value={getPath(store.config, consolePath) === false ? "off" : ""}
          onChange={(v) => put(consolePath, v === "off" ? false : undefined)}
        />
      </>
    );
  }
  if (dest.key === "otlp") return <OtlpFields />;
  const phoenix = dest.key === "phoenix";
  return (
    <div class="grid gap-x-6 sm:grid-cols-2">
      <TextField path={f("project")} label="Project" placeholder="my-agent" />
      <TextField
        path={f("endpoint")} label="Endpoint"
        placeholder={phoenix ? "http://localhost:6006" : "https://api.smith.langchain.com/otel"}
        help={phoenix ? "Base collector URL. /v1/traces is appended." : undefined}
      />
    </div>
  );
}

function DestinationCard({ dest }) {
  return (
    <div id={`dest-${dest.key}`} class="mt-4 rounded-xl border border-line bg-surface p-5">
      <div class="flex items-start gap-3">
        <div class="min-w-0 flex-1">
          <h3 class="text-sm font-semibold text-ink">{dest.name}</h3>
          <p class="mt-0.5 text-[13px] text-muted">{dest.text}</p>
        </div>
        <button type="button" class={linkBtn} aria-label={`Remove ${dest.name}`} onClick={() => removeDestination(dest.key)}>Remove</button>
      </div>
      <DestinationFields dest={dest} />
      <p class="mt-5 border-t border-line pt-3 text-[13px] text-muted">{dest.secret}</p>
    </div>
  );
}

export function TracingWidget() {
  const [adding, setAdding] = useState(false);
  const chosen = DESTINATIONS.filter((d) => isPicked(d.key));
  const free = DESTINATIONS.filter((d) => !isPicked(d.key));
  const o = obs();
  return (
    <div>
      {chosen.length === 0 ? (
        <div class="max-w-[520px]">
          <label for="trace-dest" class="mb-2 block text-sm font-semibold text-ink">Send AI traces to</label>
          <Select
            id="trace-dest" value="" plain
            options={[["", "None"], ...free.map((d) => [d.key, d.label])]}
            onChange={(v) => { if (v) addDestination(v); }}
          />
          <p class={note}>Graph runs are not traced.</p>
        </div>
      ) : (
        <>
          <h2 class="eyebrow">Send AI traces to</h2>
          {chosen.map((d) => <DestinationCard key={d.key} dest={d} />)}
          {free.length ? (
            adding ? (
              <div class="mt-4 max-w-[320px]">
                <label for="trace-add" class="sr-only">Add another destination</label>
                <Select
                  id="trace-add" value="" plain
                  options={[["", "Choose a destination"], ...free.map((d) => [d.key, d.label])]}
                  onChange={(v) => { setAdding(false); if (v) addDestination(v); }}
                />
              </div>
            ) : (
              <button type="button" class={`${linkBtn} mt-3 inline-flex min-h-10 items-center gap-1.5`} onClick={() => setAdding(true)}>
                <Icon name="plus" class="size-4" />Add another destination
              </button>
            )
          ) : null}
          <h2 class="eyebrow mt-8">Trace settings</h2>
          <div class="grid gap-x-6 sm:grid-cols-2">
            <SelectField
              path={["observability", "level"]} label="Level" options={LEVELS}
              value={o.level ?? "standard"} onChange={(v) => put(["observability", "level"], v)}
              help={LEVEL_HELP[o.level ?? "standard"]}
            />
            <SelectField
              path={["observability", "user_id_mode"]} label="User id in traces" options={USER_IDS}
              value={o.user_id_mode ?? "raw"} onChange={(v) => put(["observability", "user_id_mode"], v)}
              help={USER_ID_HELP[o.user_id_mode ?? "raw"]}
            />
          </div>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ Metrics

const EXPORTS = [["", "None"], ["otlp", "OTLP push"], ["prometheus", "Prometheus scrape"]];
const EXPORT_HELP = {
  "": "Metrics are not exported.",
  otlp: "Recommended for production. Works with several worker processes.",
  prometheus: "One process only. The API serves the scrape path, the worker its own port.",
};
const BUCKETS = [50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000, 120000];
const metrics = () => (isObject(store.config.metrics) ? store.config.metrics : {});
const metricsMode = () => (metrics().enabled === true && ["otlp", "prometheus"].includes(metrics().export) ? metrics().export : "");

function pickMode(mode) {
  commit((s) => {
    if (!mode) { delete s.config.metrics; return; }
    const m = isObject(s.config.metrics) ? s.config.metrics : {};
    m.enabled = true;
    m.export = mode;
    if (mode === "otlp" && !isObject(m.otlp)) m.otlp = { endpoint: null };
    if (mode === "prometheus" && !isObject(m.prometheus)) m.prometheus = { path: "/metrics", worker_port: 9464 };
    s.config.metrics = m;
  });
}

export function MetricsWidget() {
  const mode = metricsMode();
  const buckets = metrics().latency_buckets_ms;
  const bucketIssue = issueAt("metrics.latency_buckets_ms");
  return (
    <div>
      <div class="max-w-[520px]">
        <label for="f-metrics-export" class="mb-2 block text-sm font-semibold text-ink">Export metrics</label>
        <Select id="f-metrics-export" plain options={EXPORTS} value={mode} invalid={issueAt("metrics.export")?.level === "error"} onChange={pickMode} />
        <p class={note}>{EXPORT_HELP[mode]}</p>
        <FieldIssue issue={issueAt("metrics.export")} />
      </div>
      {mode === "" ? null : (
        <div class="mt-5 rounded-xl border border-line bg-surface px-5 pt-1 pb-5">
          {mode === "otlp" ? (
            <TextField
              path={["metrics", "otlp", "endpoint"]} label="Endpoint" placeholder="http://collector:4318"
              help={<>/v1/metrics is appended.<br />Prometheus: <code class="font-mono text-xs">http://prometheus:9090/api/v1/otlp</code> (start it with <code class="font-mono text-xs">--web.enable-otlp-receiver</code>).</>}
            />
          ) : (
            <>
              <div class="grid gap-x-6 sm:grid-cols-2">
                <TextField path={["metrics", "prometheus", "path"]} label="Scrape path" placeholder="/metrics" help="Route on the API server that Prometheus scrapes." />
                <NumberField path={["metrics", "prometheus", "worker_port"]} label="Worker port" help="Each worker serves its own metrics on this port." />
              </div>
            </>
          )}
          {mode === "otlp" ? (
            <p class="mt-5 border-t border-line pt-3 text-[13px] text-muted">Auth headers: set <code class="font-mono text-xs">OTEL_EXPORTER_OTLP_HEADERS</code> in .env.</p>
          ) : null}
        </div>
      )}
      {mode !== "" ? (
        <Disclosure title="Advanced" class="mt-4" open={!!bucketIssue || buckets !== undefined}>
          <div class="pt-4">
            <label for="f-metrics-latency_buckets_ms" class="text-sm font-semibold text-ink">Latency buckets (ms)</label>
            <div class="mt-2 max-w-[520px]">
              <Chips
                id="f-metrics-latency_buckets_ms" values={(buckets ?? BUCKETS).map(String)} placeholder="250"
                onChange={(next) => put(["metrics", "latency_buckets_ms"], next.length ? next.map(Number).filter(Number.isFinite) : undefined)}
              />
            </div>
            <p class={note}>Upper bounds of the latency histogram in milliseconds, ascending.</p>
            <FieldIssue issue={bucketIssue} />
          </div>
        </Disclosure>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------ summaries

const list = (names) => (names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}` : names[0]);

export function traceSummary(offText) {
  const on = DESTINATIONS.filter((d) => isPicked(d.key)).map((d) => d.name);
  if (!on.length) return { text: offText("observability") };
  return { text: `Level ${valueOrDefault(["observability", "level"])}, sending to ${list(on)}.` };
}

export function metricsSummary(offText) {
  const how = { otlp: "OTLP push", prometheus: "Prometheus scrape" }[metricsMode()];
  return how ? { text: `${how}.` } : { text: offText("metrics") };
}
