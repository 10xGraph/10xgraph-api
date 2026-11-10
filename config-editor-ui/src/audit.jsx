import { commit, isObject, issueAt, store } from "./store.js";
import { Chips, FieldIssue, Toggle } from "./ui.jsx";

const note = "mt-2 max-w-prose text-[13px] leading-5 text-muted";
const audit = () => (isObject(store.config.audit) ? store.config.audit : {});
const isOn = () => audit().enabled === true;
const sinks = () => (Array.isArray(audit().sinks) ? audit().sinks : ["log"]);
const custom = () => sinks().filter((s) => s !== "log" && s !== "publisher");

const edit = (fn) => commit((s) => {
  const a = isObject(s.config.audit) ? s.config.audit : {};
  fn(a);
  s.config.audit = a;
});

function setOn(on) {
  if (!on) { commit((s) => { delete s.config.audit; }); return; }
  edit((a) => {
    a.enabled = true;
    a.sinks = ["log"];
    a.include_reads = true;
  });
}

// "log" is always kept, so the file never ends up with an audit log that goes nowhere.
function setSinks({ publisher = sinks().includes("publisher"), extra = custom() }) {
  edit((a) => { a.sinks = ["log", ...(publisher ? ["publisher"] : []), ...extra]; });
}

export function AuditWidget() {
  const on = isOn();
  const reads = audit().include_reads !== false;
  const publisher = sinks().includes("publisher");
  return (
    <div>
      <div class="flex items-center gap-3">
        <Toggle id="f-audit-enabled" on={on} label="Record an audit log" onChange={setOn} />
        <label for="f-audit-enabled" class="cursor-pointer text-sm font-semibold text-ink">Record an audit log</label>
      </div>
      <p class={note}>{on ? "Events are written as JSON lines on stdout." : "No audit events are recorded."}</p>
      <FieldIssue issue={issueAt("audit.enabled")} />
      {on ? (
        <div class="mt-5 rounded-xl border border-line bg-surface px-5 pt-1 pb-5">
          <div class="pt-4">
            <h3 class="text-sm font-semibold text-ink">Sinks</h3>
            <p class={note}>
              <code class="font-mono text-xs text-body">log</code> is always on: one JSON line per event on
              stdout, for your log pipeline to collect.
            </p>
            <div class="mt-3 flex items-center gap-3">
              <Toggle id="f-audit-publisher" on={publisher} label="Publisher" onChange={(v) => setSinks({ publisher: v })} />
              <label for="f-audit-publisher" class="cursor-pointer text-sm font-semibold text-ink">Publisher</label>
            </div>
            <p class={note}>Also send events through the configured publisher. Best effort.</p>
            <label for="f-audit-sinks" class="mt-4 block text-sm font-semibold text-ink">Custom sinks</label>
            <div class="mt-2 max-w-[520px]">
              <Chips
                id="f-audit-sinks" values={custom()} placeholder="my_pkg.audit:MySink"
                onChange={(next) => setSinks({ extra: next })}
              />
            </div>
            <p class={note}>A <code class="font-mono text-xs text-body">module:Class</code> that extends BaseAuditSink.</p>
            <FieldIssue issue={issueAt("audit.sinks")} />
          </div>
          <div class="mt-5 border-t border-line pt-4">
            <div class="flex items-center gap-3">
              <Toggle id="f-audit-include_reads" on={reads} label="Include allowed reads" onChange={(v) => edit((a) => { a.include_reads = v; })} />
              <label for="f-audit-include_reads" class="cursor-pointer text-sm font-semibold text-ink">Include allowed reads</label>
            </div>
            <p class={note}>Off keeps denials, auth failures and changes, and drops allowed reads.</p>
            <FieldIssue issue={issueAt("audit.include_reads")} />
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function auditSummary(offText) {
  if (!isOn()) return { text: offText("audit") };
  return { text: audit().include_reads === false ? "On, denials and changes only." : "On, all access and changes." };
}
