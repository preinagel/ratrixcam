"use strict";

// ratrixcam camera config editor. Every value is picked from a drop-down menu fed by the
// machine's list of legal values (local_options.json, via config_editor.py); a new value
// enters the list only through the "Add new..." dialog, which checks it first.

const POSITIONS = ["A", "B", "C", "D", "E", "F", "G", "H"];
const ADD = "__add__";

// kinds: name (used in file and folder names), int, float, path
const RULES = {
  name: { re: /^[A-Za-z0-9-]{1,24}$/, help: "Letters, digits and - only; no spaces, dots or _; up to 24 characters." },
  machine: { re: /^[A-Za-z0-9][A-Za-z0-9 -]{0,31}$/, help: "Letters, digits, spaces and -; up to 32 characters." },
  path: { re: /^(\/|~\/)[^\s]*\/$/, help: "An absolute path (or ~/...) ending in /; no spaces." },
};

// the run-level and default fields, in the order they are written to the file
const RUN_FIELDS = [
  { key: "study_label", label: "Study label", kind: "name", hint: "first part of each clip folder's name" },
  { key: "subject_prefix", label: "Subject prefix", kind: "name", hint: "goes before every subject ID, e.g. rat" },
  { key: "time_slice", label: "Clip length (seconds)", kind: "int", min: 1, max: 86400 },
  { key: "preview_interval", label: "Still image every (seconds)", kind: "int", min: 1, max: 3600 },
  { key: "save_path", label: "Save drive folder", kind: "path" },
  { key: "temp_path", label: "Temp folder", kind: "path", hint: "on the local disk; emptied at the end of a run" },
];
const DEFAULT_FIELDS = [
  { key: "default_crf", label: "crf (0 best quality, 51 smallest files)", kind: "int", min: 0, max: 51 },
  { key: "default_fps", label: "Frames per second", kind: "int", min: 1, max: 240 },
  { key: "default_width", label: "Width (pixels)", kind: "int", min: 16, max: 8192 },
  { key: "default_height", label: "Height (pixels)", kind: "int", min: 16, max: 8192 },
  { key: "default_cam_exposure", label: "Exposure (camera LUT code)", kind: "float", min: -20, max: 20 },
];
// per-camera: the required name fields, the position, then optional overrides of the defaults
const CAMERA_FIELDS = [
  { key: "subjID", label: "Subject ID", kind: "name", options: "subjID" },
  { key: "view", label: "View", kind: "name", options: "view" },
  { key: "position", label: "Position", kind: "position" },
  { key: "crf", kind: "int", min: 0, max: 51, options: "default_crf", optional: true },
  { key: "fps", kind: "int", min: 1, max: 240, options: "default_fps", optional: true },
  { key: "width", kind: "int", min: 16, max: 8192, options: "default_width", optional: true },
  { key: "height", kind: "int", min: 16, max: 8192, options: "default_height", optional: true },
  { key: "exposure", kind: "float", min: -20, max: 20, options: "default_cam_exposure", optional: true },
];
const FILE_KEY_ORDER = ["save_path", "temp_path", "study_label", "subject_prefix", "default_crf", "time_slice",
  "default_fps", "default_width", "default_height", "default_cam_exposure", "preview_interval", "cameras"];
const CAMERA_KEY_ORDER = ["subjID", "view", "position", "crf", "fps", "width", "height", "exposure"];

const state = {
  options: null, // the options file as served: {machine_id, machine_name, options: {field: [values]}, ...}
  config: emptyConfig(),
};

function emptyConfig() {
  const c = {};
  for (const f of [...RUN_FIELDS, ...DEFAULT_FIELDS]) c[f.key] = null;
  c.cameras = [emptyCamera()];
  return c;
}

function emptyCamera() {
  const cam = {};
  for (const f of CAMERA_FIELDS) cam[f.key] = null;
  return cam;
}

function el(id) { return document.getElementById(id); }

// ---------- server ----------

async function api(method, path, body) {
  const r = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store" });
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, data };
}

async function loadOptions() {
  const { data } = await api("GET", "/api/options");
  state.options = data;
  el("machine-name").textContent = data.machine_name || "(unnamed)";
  const warn = el("machine-warning");
  warn.hidden = !data.machine_mismatch;
  if (data.machine_mismatch) {
    warn.textContent = `The legal-values file was made on another machine ("${data.machine_name}", id ${data.machine_id}); this machine is ${data.machine_id_now}. Saving any value rebinds the file to this machine.`;
  }
}

async function saveOptions() {
  const { ok, data } = await api("PUT", "/api/options", state.options);
  if (!ok) alert(`Could not save the legal values: ${data.error || "unknown error"}`);
  else await loadOptions();
}

// ---------- validation of a single value ----------

// {value, error} for raw text entered for a field of this kind
function checkValue(field, raw) {
  const text = String(raw).trim();
  if (field.kind === "int" || field.kind === "float") {
    const n = field.kind === "int" ? (/^-?\d+$/.test(text) ? parseInt(text, 10) : NaN) : Number(text);
    if (text === "" || !isFinite(n)) return { error: field.kind === "int" ? "Must be a whole number." : "Must be a number." };
    if (n < field.min || n > field.max) return { error: `Must be between ${field.min} and ${field.max}.` };
    return { value: n };
  }
  const rule = RULES[field.kind];
  if (!rule.re.test(text)) return { error: rule.help };
  if (field.kind === "path" && text.split("/").includes("..")) return { error: "No .. in a path." };
  return { value: text };
}

function legalValues(optionsKey) {
  return (state.options && state.options.options && state.options.options[optionsKey]) || [];
}

// ---------- drop-down menus ----------

// a <select> for a field; optionsKey names the legal-values list it draws from
function makeSelect(field, optionsKey, current, onChange) {
  const sel = document.createElement("select");
  sel.dataset.optionsKey = optionsKey || "";
  fillSelect(sel, field, optionsKey, current);
  sel.addEventListener("change", async () => {
    if (sel.value === ADD) {
      const added = await promptNewValue(field, optionsKey);
      if (added === undefined) { fillSelect(sel, field, optionsKey, current); return; }
      current = added;
      refreshSelects(optionsKey);
      onChange(added);
      return;
    }
    current = sel.value === "" ? null : (field.kind === "int" || field.kind === "float" ? Number(sel.value) : sel.value);
    onChange(current);
  });
  return sel;
}

function fillSelect(sel, field, optionsKey, current) {
  sel.textContent = "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = field.optional ? "(default)" : "— choose —";
  sel.appendChild(blank);
  const values = field.kind === "position" ? POSITIONS : legalValues(optionsKey);
  let found = current == null;
  for (const v of values) {
    const o = document.createElement("option");
    o.value = String(v);
    o.textContent = String(v);
    if (current != null && String(v) === String(current)) { o.selected = true; found = true; }
    sel.appendChild(o);
  }
  if (!found) { // a loaded file holds a value that is not in the legal list: show it, flagged
    const o = document.createElement("option");
    o.value = String(current);
    o.textContent = `${current} (not in list)`;
    o.selected = true;
    sel.appendChild(o);
  }
  if (field.kind !== "position") {
    const add = document.createElement("option");
    add.value = ADD;
    add.textContent = "Add new…";
    sel.appendChild(add);
  }
  sel.classList.toggle("invalid", !found);
}

// after a legal list changed: rebuild every menu that draws from it, keeping selections
function refreshSelects(optionsKey) {
  for (const sel of document.querySelectorAll(`select[data-options-key="${optionsKey}"]`)) {
    const field = sel._field;
    const current = sel._current();
    fillSelect(sel, field, optionsKey, current);
  }
}

// ---------- the "Add new..." dialog ----------

function promptNewValue(field, optionsKey, title) {
  return new Promise((resolve) => {
    const box = el("prompt"), input = el("prompt-input"), err = el("prompt-error");
    el("prompt-title").textContent = title || `New legal value for ${field.label || field.key}`;
    el("prompt-help").textContent = field.kind === "int" || field.kind === "float"
      ? `A number between ${field.min} and ${field.max}.` : RULES[field.kind].help;
    input.value = "";
    err.textContent = "";
    box.hidden = false;
    input.focus();
    const done = (value) => { box.hidden = true; cleanup(); resolve(value); };
    const ok = async () => {
      const { value, error } = checkValue(field, input.value);
      if (error) { err.textContent = error; return; }
      if (optionsKey) {
        const list = legalValues(optionsKey);
        if (list.some((v) => String(v) === String(value))) { err.textContent = "Already in the list."; return; }
        list.push(value);
        if (field.kind === "int" || field.kind === "float") list.sort((a, b) => a - b);
        state.options.options[optionsKey] = list;
        await saveOptions();
      }
      done(value);
    };
    const key = (e) => { if (e.key === "Enter") ok(); else if (e.key === "Escape") done(undefined); };
    const cancel = () => done(undefined);
    el("prompt-ok").addEventListener("click", ok);
    el("prompt-cancel").addEventListener("click", cancel);
    input.addEventListener("keydown", key);
    function cleanup() {
      el("prompt-ok").removeEventListener("click", ok);
      el("prompt-cancel").removeEventListener("click", cancel);
      input.removeEventListener("keydown", key);
    }
  });
}

// ---------- form ----------

function buildField(field, container, getValue, setValue) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const label = document.createElement("label");
  label.textContent = field.label || field.key;
  const sel = makeSelect(field, field.key, getValue(), (v) => { setValue(v); render(); });
  sel._field = field;
  sel._current = getValue;
  wrap.appendChild(label);
  wrap.appendChild(sel);
  if (field.hint) {
    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = field.hint;
    wrap.appendChild(hint);
  }
  container.appendChild(wrap);
}

function buildForm() {
  const run = el("run-fields"), defaults = el("default-fields");
  run.textContent = "";
  defaults.textContent = "";
  for (const f of RUN_FIELDS) buildField(f, run, () => state.config[f.key], (v) => { state.config[f.key] = v; });
  for (const f of DEFAULT_FIELDS) buildField(f, defaults, () => state.config[f.key], (v) => { state.config[f.key] = v; });
  buildCameraRows();
}

function buildCameraRows() {
  const tbody = el("cameras").querySelector("tbody");
  tbody.textContent = "";
  state.config.cameras.forEach((cam, idx) => {
    const tr = document.createElement("tr");
    for (const f of CAMERA_FIELDS) {
      const td = document.createElement("td");
      const sel = makeSelect(f, f.options || "", cam[f.key], (v) => { cam[f.key] = v; render(); });
      sel._field = f;
      sel._current = () => cam[f.key];
      sel.title = f.label || `${f.key} for this camera (blank: the default above)`;
      td.appendChild(sel);
      tr.appendChild(td);
    }
    const td = document.createElement("td");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "small";
    remove.textContent = "remove";
    remove.addEventListener("click", () => { state.config.cameras.splice(idx, 1); buildCameraRows(); render(); });
    td.appendChild(remove);
    tr.appendChild(td);
    tbody.appendChild(tr);
  });
}

// ---------- checking the whole config ----------

function problems() {
  const c = state.config, out = [];
  for (const f of [...RUN_FIELDS, ...DEFAULT_FIELDS]) {
    if (c[f.key] == null) out.push(`${f.label} is not set`);
    else if (!legalValues(f.key).some((v) => String(v) === String(c[f.key]))) out.push(`${f.label}: "${c[f.key]}" is not in the list of legal values`);
  }
  if (c.temp_path && c.save_path && c.temp_path === c.save_path) out.push("The temp folder and the save drive folder must differ");
  if (c.cameras.length === 0) out.push("At least one camera is needed");
  const names = new Map(), positions = new Map();
  c.cameras.forEach((cam, i) => {
    const n = i + 1;
    for (const f of CAMERA_FIELDS) {
      if (f.optional) {
        if (cam[f.key] != null && !legalValues(f.options).some((v) => String(v) === String(cam[f.key]))) out.push(`Camera ${n}: ${f.key} "${cam[f.key]}" is not in the list of legal values`);
      } else if (cam[f.key] == null) out.push(`Camera ${n}: ${f.label.toLowerCase()} is not set`);
      else if (f.options && !legalValues(f.options).some((v) => String(v) === String(cam[f.key]))) out.push(`Camera ${n}: ${f.label.toLowerCase()} "${cam[f.key]}" is not in the list of legal values`);
    }
    if (cam.subjID != null && cam.view != null) {
      const key = `${cam.subjID}_${cam.view}`;
      if (names.has(key)) out.push(`Cameras ${names.get(key)} and ${n} would both be named ${c.subject_prefix || ""}${key}: same subject ID and view`);
      else names.set(key, n);
    }
    if (cam.position != null) {
      if (positions.has(cam.position)) out.push(`Cameras ${positions.get(cam.position)} and ${n} both have position ${cam.position}`);
      else positions.set(cam.position, n);
    }
  });
  return out;
}

// the config as it will be written: keys in the camera code's order, unset optionals omitted
function fileObject() {
  const out = {};
  for (const k of FILE_KEY_ORDER) {
    if (k === "cameras") {
      out.cameras = state.config.cameras.map((cam) => {
        const o = {};
        for (const key of CAMERA_KEY_ORDER) if (cam[key] != null) o[key] = cam[key];
        return o;
      });
    } else out[k] = state.config[k];
  }
  return out;
}

function fileText() {
  // one camera per line, like the hand-written configs
  const obj = fileObject();
  const cameras = obj.cameras.map((c) => "    " + JSON.stringify(c).replace(/,"/g, ', "').replace(/":/g, '": ')).join(",\n");
  const head = JSON.stringify({ ...obj, cameras: [] }, null, 2).replace(/\n  "cameras": \[\]\n\}$/, "");
  return `${head}\n  "cameras": [\n${cameras}\n  ]\n}\n`;
}

function render() {
  const list = el("problems");
  list.textContent = "";
  for (const p of problems()) {
    const li = document.createElement("li");
    li.textContent = p;
    list.appendChild(li);
  }
  el("preview").textContent = fileText();
  const used = new Map(state.config.cameras.map((cam, i) => [cam.position, i + 1]));
  const cell = (p) => used.has(p) ? `<span class="used">${p}</span>` : p;
  el("position-map").innerHTML = `${cell("A")} ${cell("B")}   ${cell("E")} ${cell("F")}\n${cell("C")} ${cell("D")}   ${cell("G")} ${cell("H")}`;
  document.querySelectorAll("#cameras tbody tr").forEach((tr, i) => {
    const cam = state.config.cameras[i];
    const dupName = state.config.cameras.some((o, j) => j !== i && o.subjID === cam.subjID && o.view === cam.view && cam.subjID != null && cam.view != null);
    const dupPos = state.config.cameras.some((o, j) => j !== i && o.position === cam.position && cam.position != null);
    tr.classList.toggle("invalid", dupName || dupPos);
  });
}

// ---------- load and save ----------

function setStatus(text, cls) {
  const s = el("file-status");
  s.textContent = text;
  s.className = cls || "muted";
}

async function loadFile() {
  const path = el("file-path").value.trim();
  if (!path) { setStatus("Enter a file path first", "error"); return; }
  const { data } = await api("GET", `/api/config?path=${encodeURIComponent(path)}`);
  if (!data.exists) { setStatus(`${data.path} does not exist; Save will create it`, ""); return; }
  if (data.error) { setStatus(`Could not read ${data.path}: ${data.error}`, "error"); return; }
  const c = emptyConfig();
  for (const f of [...RUN_FIELDS, ...DEFAULT_FIELDS]) if (data.config[f.key] !== undefined) c[f.key] = data.config[f.key];
  c.cameras = (Array.isArray(data.config.cameras) ? data.config.cameras : []).map((src) => {
    const cam = emptyCamera();
    for (const f of CAMERA_FIELDS) if (src[f.key] !== undefined) cam[f.key] = src[f.key];
    return cam;
  });
  state.config = c;
  buildForm();
  render();
  const extra = Object.keys(data.config).filter((k) => !FILE_KEY_ORDER.includes(k));
  setStatus(`Loaded ${data.path}` + (extra.length ? ` (unknown fields dropped: ${extra.join(", ")})` : ""), data.problems.length ? "error" : "ok");
}

async function saveFile() {
  const path = el("file-path").value.trim();
  if (!path) { setStatus("Enter a file path first", "error"); return; }
  const found = problems();
  if (found.length) { setStatus(`Not saved: ${found.length} problem${found.length === 1 ? "" : "s"} listed on the right`, "error"); return; }
  const { data: probe } = await api("GET", `/api/config?path=${encodeURIComponent(path)}`);
  if (probe.exists && !confirm(`${probe.path} exists. Overwrite it?`)) { setStatus("Not saved", ""); return; }
  const { ok, data } = await api("PUT", `/api/config?path=${encodeURIComponent(path)}`, fileObject());
  if (ok) setStatus(`Saved ${data.path}`, "ok");
  else setStatus(`Not saved: ${(data.problems || ["unknown error"]).join("; ")}`, "error");
}

async function renameMachine() {
  const name = await promptNewValue({ key: "machine_name", label: "machine name", kind: "machine" }, null, "Name of this machine");
  if (name === undefined) return;
  state.options.machine_name = name;
  await saveOptions();
}

// ---------- main ----------

async function main() {
  await loadOptions();
  el("file-path").value = "~/Desktop/camera_config.json";
  buildForm();
  render();
  el("load").addEventListener("click", loadFile);
  el("save").addEventListener("click", saveFile);
  el("add-camera").addEventListener("click", () => { state.config.cameras.push(emptyCamera()); buildCameraRows(); render(); });
  el("rename-machine").addEventListener("click", renameMachine);
  el("file-path").addEventListener("keydown", (e) => { if (e.key === "Enter") loadFile(); });
}

main();
