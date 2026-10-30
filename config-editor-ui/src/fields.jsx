import { useEffect, useState } from "preact/hooks";
import {
  commit, deletePath, fieldChanged, fieldUnmet, getPath, issueAt, setPath, store, unmetHint,
} from "./store.js";
import { Chips, FieldIssue, Segmented, Select } from "./ui.jsx";

// Fields grouped by their `group` label. Fields with an unmet show_if stay in the list;
// the Field itself renders dimmed with a "Used when ..." hint.
export function fieldGroups(fields) {
  const groups = [];
  for (const f of fields) {
    const name = f.group || "";
    let group = groups.find((g) => g.name === name);
    if (!group) groups.push(group = { name, fields: [] });
    group.fields.push(f);
  }
  return groups;
}

// Editing a field of an optional section that is still on defaults switches the section on first.
const put = (path, value) => commit((s) => {
  if (value === undefined) {
    deletePath(s.config, path);
    return;
  }
  const sec = path.length > 1 ? s.sections.find((x) => x.optional && x.key === path[0]) : null;
  if (sec && (s.config[sec.key] === undefined || s.config[sec.key] === null)) {
    s.config[sec.key] = JSON.parse(JSON.stringify(sec.default));
  }
  setPath(s.config, path, value);
});

export const fieldDomId = (f) => `f-${f.path.join("-")}`;

// Keeps the raw text while typing so "0." or "1e" do not get rewritten mid-edit.
function NumberInput({ id, value, field, invalid, disabled }) {
  const [text, setText] = useState(String(value ?? ""));
  useEffect(() => {
    if (text === "" ? value !== undefined : Number(text) !== value) setText(String(value ?? ""));
  }, [value]);
  return (
    <input
      id={id} type="number" inputmode="decimal" value={text} disabled={disabled}
      min={field.min} step={field.step ?? "any"} placeholder={field.placeholder ?? (field.default ?? "")}
      class={`input max-w-40 font-mono tabular-nums ${invalid ? "input-invalid" : ""}`}
      onInput={(e) => {
        const raw = e.currentTarget.value;
        setText(raw);
        if (raw === "") put(field.path, undefined);
        else if (!Number.isNaN(Number(raw))) put(field.path, Number(raw));
      }}
    />
  );
}

// exclusive_min is only a hint; the server decides what is valid.
function RangeHint({ field, value }) {
  if (field.exclusive_min === undefined || typeof value !== "number" || value > field.exclusive_min) return null;
  return <p class="mt-1.5 text-[13px] text-warn">Must be greater than {field.exclusive_min}.</p>;
}

function NumberControl({ id, field: f, value, invalid }) {
  const unlimited = f.unlimited_value !== undefined && value === f.unlimited_value;
  const input = <NumberInput id={id} value={unlimited ? undefined : value} field={f} invalid={invalid} disabled={unlimited} />;

  if (f.null_label !== undefined) {
    const mode = value === null ? "n" : value === undefined ? "d" : "s";
    const seconds = typeof f.default === "number" ? f.default : 300;
    return (
      <div>
        <Segmented
          id={id} label={f.label} value={mode}
          options={[["d", "Default"], ["s", "Seconds"], ["n", f.null_label]]}
          onPick={(m) => put(f.path, m === "d" ? undefined : m === "n" ? null : seconds)}
        />
        {mode === "s" ? <div class="mt-2.5">{input}<RangeHint field={f} value={value} /></div> : null}
      </div>
    );
  }
  if (f.unlimited_value !== undefined) {
    return (
      <div>
        <div class="flex flex-wrap items-center gap-x-4">
          {input}
          <label class="inline-flex min-h-11 cursor-pointer items-center gap-2.5 text-sm text-body">
            <input
              type="checkbox" class="size-[18px] accent-brand-solid" checked={unlimited}
              onChange={(e) => put(f.path, e.currentTarget.checked ? f.unlimited_value : undefined)}
            />
            Unlimited
          </label>
        </div>
        <RangeHint field={f} value={value} />
      </div>
    );
  }
  return <div>{input}<RangeHint field={f} value={value} /></div>;
}

function BoolControl({ id, field: f, value }) {
  if (f.unset_label !== undefined) {
    const options = [["d", "Default"]];
    if (f.allow_true !== false) options.push(["t", "On"]);
    options.push(["f", "Off"]);
    return (
      <div>
        <Segmented
          id={id} label={f.label} value={value === undefined ? "d" : value ? "t" : "f"} options={options}
          onPick={(v) => put(f.path, v === "d" ? undefined : v === "t")}
        />
        <p class="mt-1.5 text-xs text-faint">Default: {f.unset_label}</p>
      </div>
    );
  }
  return (
    <label class="inline-flex min-h-11 cursor-pointer items-center gap-2.5 text-sm font-semibold text-ink">
      <input
        id={id} type="checkbox" class="size-[18px] accent-brand-solid"
        checked={(value ?? f.default ?? false) === true}
        onChange={(e) => put(f.path, e.currentTarget.checked)}
      />
      {f.label}
    </label>
  );
}

function Control({ id, field: f, value, invalid }) {
  if (f.type === "bool") return <BoolControl id={id} field={f} value={value} />;
  if (f.type === "number") return <NumberControl id={id} field={f} value={value} invalid={invalid} />;
  if (f.type === "list") {
    return (
      <Chips
        id={id} values={Array.isArray(value) ? value : []} placeholder={f.placeholder}
        onChange={(next) => put(f.path, next.length ? next : undefined)}
      />
    );
  }
  if (f.type === "select") {
    const options = f.options.map((o) => [o, o]);
    if (f.unset_label !== undefined) options.unshift(["", f.unset_label]);
    return (
      <Select
        id={id} options={options} invalid={invalid}
        value={value ?? (f.unset_label !== undefined ? "" : f.default ?? f.options[0])}
        onChange={(v) => put(f.path, v === "" ? undefined : v)}
      />
    );
  }
  return (
    <input
      id={id} type="text" value={value ?? ""} placeholder={f.placeholder}
      autocomplete="off" spellcheck={false} autocapitalize="off"
      class={`input font-mono ${invalid ? "input-invalid" : ""}`}
      onInput={(e) => put(f.path, e.currentTarget.value === "" ? undefined : e.currentTarget.value)}
    />
  );
}

export function Field({ field: f }) {
  const key = f.path.join(".");
  const id = fieldDomId(f);
  const value = getPath(store.config, f.path);
  const issue = issueAt(key);
  const changed = fieldChanged(f);
  const unmet = fieldUnmet(f);
  const checkbox = f.type === "bool" && f.unset_label === undefined;
  const hasDefault = f.default !== undefined && !changed;
  const defaultText = Array.isArray(f.default) ? f.default.join(", ") : String(f.default ?? "");

  return (
    <div id={`row-${id}`} class={`relative pt-5 pb-1 ${changed ? "before:absolute before:top-6 before:-left-2.5 before:bottom-2 before:w-[3px] before:rounded before:bg-brand before:content-[''] sm:before:-left-3.5" : ""}`}>
      <div class="flex min-h-6 items-center gap-2.5">
        {checkbox ? null : (
          <label for={id} class={`text-sm font-semibold text-ink ${unmet ? "opacity-50" : ""}`}>
            {f.label}{f.required ? <span class="text-danger"> *</span> : null}
          </label>
        )}
        {checkbox ? null : <code class="hidden font-mono text-[11px] text-faint sm:inline">{key}</code>}
        {hasDefault && !checkbox ? (
          <span class="rounded border border-line px-1.5 text-[11px] leading-4 font-medium text-faint">default</span>
        ) : null}
        <span class="flex-1" />
        {changed ? (
          <button
            type="button" class="focus-ring min-h-8 cursor-pointer rounded px-1 text-[13px] font-medium text-brand hover:underline"
            title={`Reset to default${f.default !== undefined ? ` (${defaultText})` : ""}`}
            aria-label={`Reset ${f.label} to default`}
            onClick={() => commit((s) => deletePath(s.config, f.path))}
          >
            Reset
          </button>
        ) : null}
      </div>
      {f.help ? <p class={`mt-0.5 max-w-prose text-[13px] leading-5 text-muted ${unmet ? "opacity-50" : ""}`}>{f.help}</p> : null}
      <fieldset disabled={unmet} class={`m-0 mt-2.5 min-w-0 max-w-[520px] border-0 p-0 ${unmet ? "opacity-50" : ""}`}>
        <Control id={id} field={f} value={value} invalid={issue?.level === "error"} />
      </fieldset>
      {unmet ? <p class="mt-1.5 text-xs text-faint">{unmetHint(f)}</p> : null}
      <FieldIssue issue={issue} />
    </div>
  );
}
