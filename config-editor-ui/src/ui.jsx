import { useRef } from "preact/hooks";
import { ICONS } from "./icons.js";

export function Icon({ name, class: cls = "size-4" }) {
  if (!/(^|\s)size-/.test(cls)) cls = `size-4 ${cls}`;
  return (
    <span class={`inline-flex shrink-0 ${cls}`} aria-hidden="true">
      <svg
        xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" class="size-full"
        dangerouslySetInnerHTML={{ __html: ICONS[name] }}
      />
    </span>
  );
}

export function Pill({ class: cls, children }) {
  return (
    <span class={`inline-flex h-5 shrink-0 items-center rounded-full px-2 text-[11px] font-semibold ${cls}`}>
      {children}
    </span>
  );
}

export function Toggle({ id, on, label, onChange }) {
  return (
    <button
      id={id} type="button" role="switch" aria-checked={on ? "true" : "false"} aria-label={label}
      class={`focus-ring relative inline-flex h-6 w-10 shrink-0 cursor-pointer items-center rounded-full transition-colors duration-200 before:absolute before:-inset-2.5 before:content-[''] ${on ? "bg-brand-solid" : "bg-line-strong"}`}
      onClick={() => onChange(!on)}
    >
      <span
        class={`pointer-events-none inline-block size-5 rounded-full bg-white shadow-sm ring-1 ring-black/5 transition-transform duration-200 ease-out ${on ? "translate-x-[18px]" : "translate-x-0.5"}`}
      />
    </button>
  );
}

// Pick one of a few options; "Use defaults / Customize" and Default / On / Off.
export function Segmented({ id, label, options, value, onPick }) {
  return (
    <div id={id} role="radiogroup" aria-label={label} class="inline-flex max-w-full flex-wrap gap-0.5 rounded-[10px] border border-line-strong bg-raised p-0.5">
      {options.map(([v, text]) => (
        <button
          key={v} type="button" role="radio" aria-checked={v === value ? "true" : "false"}
          class={`focus-ring min-h-10 cursor-pointer rounded-lg px-3.5 text-sm font-medium transition-colors duration-150 ${v === value ? "bg-surface text-ink shadow-card" : "text-muted hover:text-ink"}`}
          onClick={() => { if (v !== value) onPick(v); }}
        >
          {text}
        </button>
      ))}
    </div>
  );
}

export function Select({ id, options, value, invalid, onChange }) {
  return (
    <div class="relative">
      <select
        id={id} value={value}
        class={`input cursor-pointer appearance-none pr-9 font-mono ${invalid ? "input-invalid" : ""}`}
        onChange={(e) => onChange(e.currentTarget.value)}
      >
        {options.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
      </select>
      <Icon name="chevronDown" class="pointer-events-none absolute top-1/2 right-3 size-4 -translate-y-1/2 text-faint" />
    </div>
  );
}

export function Chips({ id, values, placeholder, suggestions, onChange }) {
  const input = useRef(null);
  const add = (raw) => {
    const v = raw.trim();
    if (input.current) input.current.value = "";
    if (v && !values.includes(v)) onChange([...values, v]);
  };
  const listId = suggestions ? `${id}-options` : undefined;
  return (
    <div
      class="flex min-h-11 cursor-text flex-wrap items-center gap-1.5 rounded-lg border border-line-strong bg-sunken px-1.5 py-1.5 transition-[border-color,box-shadow] duration-150 focus-within:border-brand focus-within:ring-4 focus-within:ring-brand-ring sm:py-1"
      onClick={(e) => { if (e.target === e.currentTarget) input.current?.focus(); }}
    >
      {values.map((v, i) => (
        <span key={v} class="inline-flex h-7 items-center gap-0.5 rounded-md border border-line bg-raised pr-0.5 pl-2 font-mono text-xs text-body sm:h-6">
          {v}
          <button
            type="button" aria-label={`Remove ${v}`}
            class="inline-flex size-6 cursor-pointer items-center justify-center rounded text-faint transition-colors hover:bg-hover hover:text-ink sm:size-5"
            onClick={() => onChange(values.filter((_, j) => j !== i))}
          >
            <Icon name="x" class="size-3" />
          </button>
        </span>
      ))}
      <input
        ref={input} id={id} type="text" list={listId} placeholder={values.length ? "Add..." : placeholder}
        autocomplete="off" spellcheck={false} autocapitalize="off"
        class="h-7 min-w-[7rem] flex-1 bg-transparent px-1 font-mono text-base text-ink outline-none placeholder:text-faint sm:text-sm"
        onKeyDown={(e) => {
          const el = e.currentTarget;
          if ((e.key === "Enter" || e.key === ",") && el.value.trim()) { e.preventDefault(); add(el.value); }
          else if (e.key === "Backspace" && !el.value && values.length) onChange(values.slice(0, -1));
        }}
        onChange={(e) => { if (suggestions?.includes(e.currentTarget.value.trim())) add(e.currentTarget.value); }}
        onBlur={(e) => add(e.currentTarget.value)}
      />
      {suggestions ? (
        <datalist id={listId}>
          {suggestions.filter((s) => !values.includes(s)).map((s) => <option key={s} value={s} />)}
        </datalist>
      ) : null}
    </div>
  );
}

export function FieldMeta({ label, path, help, id, required }) {
  return (
    <div class="min-w-0">
      <label for={id} class="block text-sm font-medium text-ink">
        {label}{required ? <span class="text-danger"> *</span> : null}
      </label>
      <div class="mt-0.5 font-mono text-xs text-faint">{path}</div>
      {help ? <p class="mt-1.5 text-[13px] leading-5 text-muted">{help}</p> : null}
    </div>
  );
}

export function FieldIssue({ issue }) {
  if (!issue) return null;
  const error = issue.level === "error";
  return (
    <p class={`mt-2 flex gap-1.5 text-[13px] leading-5 ${error ? "text-danger" : "text-warn"}`} role={error ? "alert" : undefined}>
      <Icon name={error ? "xCircle" : "alert"} class="mt-0.5 size-3.5" />
      <span>{issue.message}</span>
    </p>
  );
}

// Label and help on the left, control on the right (stacked on phones).
export function Row({ children }) {
  return <div class="grid gap-3 px-5 py-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)] sm:gap-8">{children}</div>;
}

export function TextRow({ id, label, path, value, placeholder, onInput, issue, help }) {
  return (
    <Row>
      <FieldMeta label={label} path={path} help={help} id={id} />
      <div class="min-w-0 sm:pt-0.5">
        <input
          id={id} type="text" value={value ?? ""} placeholder={placeholder}
          autocomplete="off" spellcheck={false} autocapitalize="off"
          class={`input font-mono ${issue?.level === "error" ? "input-invalid" : ""}`}
          onInput={(e) => onInput(e.currentTarget.value)}
        />
        <FieldIssue issue={issue} />
      </div>
    </Row>
  );
}

export function ChoiceGroup({ name, label, options, value, onPick }) {
  return (
    <div role="radiogroup" aria-label={label} class="grid gap-3 sm:grid-cols-2">
      {options.map(([v, title, detail]) => {
        const selected = v === value;
        return (
          <button
            key={v} type="button" role="radio" aria-checked={selected ? "true" : "false"} id={`${name}-${v}`}
            class={`focus-ring flex cursor-pointer items-start gap-3 rounded-xl border p-4 text-left transition-colors duration-150 ${selected
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

export function Group({ title, children }) {
  return (
    <section>
      {title ? <h4 class="eyebrow mb-2.5">{title}</h4> : null}
      {children}
    </section>
  );
}

const CALLOUT = {
  info: ["info", "border-brand/20 bg-brand-soft", "text-brand"],
  warn: ["alert", "border-warn/25 bg-warn-soft", "text-warn"],
  error: ["xCircle", "border-danger/25 bg-danger-soft", "text-danger"],
};

export function Callout({ kind, title, lines }) {
  const [icon, box, tint] = CALLOUT[kind];
  return (
    <div class={`flex gap-3 rounded-xl border px-4 py-3 ${box}`} role={kind === "error" ? "alert" : undefined}>
      <Icon name={icon} class={`mt-0.5 size-4 ${tint}`} />
      <div class="min-w-0 space-y-1 text-[13px] leading-5 text-body">
        {title ? <p class="font-medium text-ink">{title}</p> : null}
        {lines.map((line, i) => <p key={i}>{line}</p>)}
      </div>
    </div>
  );
}

export function IssueCallout({ issue }) {
  return (
    <Callout
      kind={issue.level === "error" ? "error" : "warn"}
      lines={[<span>{issue.path ? <code class="mr-1.5 font-mono text-xs text-muted">{issue.path}</code> : null}{issue.message}</span>]}
    />
  );
}

export function EmptyPanel({ icon, tint, title, text, children }) {
  return (
    <div class="px-6 py-14 text-center">
      <div class={`mx-auto flex size-11 items-center justify-center rounded-full bg-raised ${tint}`}>
        <Icon name={icon} class="size-5" />
      </div>
      <p class="mt-4 text-sm font-semibold text-ink">{title}</p>
      <p class="mx-auto mt-1 max-w-xs text-sm leading-6 text-muted">{text}</p>
      {children}
    </div>
  );
}
