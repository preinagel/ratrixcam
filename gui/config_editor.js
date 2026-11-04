"use strict";

// ratrixcam camera config editor. Every value is picked from a drop-down menu fed by the
// machine's list of legal values (local_options.json, via config_editor.py); a new value
// enters the list only through the "Add new..." dialog, which checks it first.

const POSITIONS = ["A", "B", "C", "D", "E", "F", "G", "H"];
const ADD = "__add__";

// kinds: name (used in file and folder names; maxLen per field), int, float, path,
// resolution ("640x480"), machine (a free name for this computer)
const RULES = {
  name: { re: (f) => new RegExp(`^[A-Za-z0-9-]{1,${f.maxLen}}$`), help: (f) => `Letters, digits and - only (case matters); no spaces, dots or _; up to ${f.maxLen} characters.` },
  machine: { re: () => /^[A-Za-z0-9][A-Za-z0-9 -]{0,31}$/, help: () => "Letters, digits, spaces and -; up to 32 characters." },
  path: { re: () => /^\/[^\s]*\/$/, help: () => "An absolute path starting and ending with /, no spaces (the camera program does not expand ~)." },
  resolution: { re: () => /^\d{2,5}x\d{2,5}$/, help: () => "Width x height in pixels, e.g. 640x480; the camera must support it." },
};

// run-level fields
// labels are the words users see; the json keys are the camera code's. No explanations
// here: the documentation has them.
const RUN_FIELDS = [
  { key: "study_label", label: "Study label", kind: "name", maxLen: 12 },
  { key: "subject_prefix", label: "Subject prefix", kind: "name", maxLen: 12 },
  { key: "time_slice", label: "Clip length (s)", kind: "int", min: 10, max: 3600 },
  { key: "save_path", label: "Save drive folder", kind: "path" },
  { key: "temp_path", label: "Temp folder", kind: "path" },
];
// advanced: defaults for every camera, at the bottom of the form and of the file
const ADVANCED_FIELDS = [
  { key: "default_crf", label: "Default crf", kind: "int", min: 0, max: 51 },
  { key: "default_fps", label: "Default fps", kind: "int", min: 1, max: 240 },
  { key: "default_resolution", label: "Default resolution", kind: "resolution" },
  { key: "default_cam_exposure", label: "Default LUT code", kind: "float", min: -20, max: 20 },
  { key: "preview_interval", label: "Still image interval (s)", kind: "int", min: 1, max: 3600 },
];
// per camera: the required name fields, the position, then optional overrides of the
// defaults, each drawing on its own (initially empty) list of legal values
const CAMERA_FIELDS = [
  { key: "subjID", label: "Subject", kind: "name", maxLen: 24, options: "subjID" },
  { key: "view", label: "View", kind: "name", maxLen: 24, options: "view" },
  { key: "position", label: "Position", kind: "position" },
  { key: "crf", label: "crf", kind: "int", min: 0, max: 51, options: "camera_crf", optional: true },
  { key: "fps", label: "fps", kind: "int", min: 1, max: 240, options: "camera_fps", optional: true },
  { key: "resolution", label: "Resolution", kind: "resolution", options: "camera_resolution", optional: true },
  { key: "exposure", label: "LUT", kind: "float", min: -20, max: 20, options: "camera_exposure", optional: true },
];
// the file's key order (the server writes the same): run-level, cameras, then the defaults
const FILE_KEY_ORDER = ["save_path", "temp_path", "study_label", "subject_prefix", "time_slice", "cameras",
  "default_crf", "default_fps", "default_width", "default_height", "default_cam_exposure", "preview_interval"];
const CAMERA_KEY_ORDER = ["subjID", "view", "position", "crf", "fps", "width", "height", "exposure"];

const state = {
  options: null, // the options file as served: {machine_id, machine_name, options: {list: [values]}, presets, ...}
  config: null, // the form's values; resolutions kept as "WxH" until written
};

function emptyConfig() {
  const presets = (state.options && state.options.presets) || {};
  const c = {};
  for (const f of RUN_FIELDS) c[f.key] = null;
  for (const f of ADVANCED_FIELDS) c[f.key] = presets[f.key] != null ? presets[f.key] : null;
  c.cameras = [emptyCamera()];
  return c;
}

function emptyCamera() {
  const cam = {};
  for (const f of CAMERA_FIELDS) cam[f.key] = null;
  return cam;
}

function el(id) { return document.getElementById(id); }

function resolutionOf(width, height) {
  return width != null && height != null ? `${width}x${height}` : null;
}

function splitResolution(text) {
  const m = /^(\d+)x(\d+)$/.exec(String(text || ""));
  return m ? [Number(m[1]), Number(m[2])] : [null, null];
}

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
  if (!rule.re(field).test(text)) return { error: rule.help(field) };
  if (field.kind === "path" && text.split("/").includes("..")) return { error: "No .. in a path." };
  return { value: text };
}

function helpFor(field) {
  if (field.kind === "int" || field.kind === "float") return `A number between ${field.min} and ${field.max}.`;
  return RULES[field.kind].help(field);
}

function legalValues(optionsKey) {
  return (state.options && state.options.options && state.options.options[optionsKey]) || [];
}

function isLegal(optionsKey, value) {
  return legalValues(optionsKey).some((v) => String(v) === String(value));
}

// ---------- drop-down menus ----------

// a <select> for a field; optionsKey names the legal-values list it draws from
function makeSelect(field, optionsKey, getCurrent, onChange) {
  const sel = document.createElement("select");
  sel.dataset.optionsKey = optionsKey || "";
  sel._refresh = () => fillSelect(sel, field, optionsKey, getCurrent());
  sel._refresh();
  sel.addEventListener("change", async () => {
    if (sel.value === ADD) {
      const added = await promptNewValue(field, optionsKey);
      if (added === undefined) { sel._refresh(); return; }
      onChange(added);
      refreshSelects(optionsKey);
      return;
    }
    onChange(sel.value === "" ? null : (field.kind === "int" || field.kind === "float" ? Number(sel.value) : sel.value));
    sel._refresh(); // drops a "(not in list)" entry once a legal value replaced it, and the red outline with it
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
  for (const sel of document.querySelectorAll(`select[data-options-key="${optionsKey}"]`)) sel._refresh();
}

// ---------- the "Add new..." dialog ----------

function promptNewValue(field, optionsKey, title) {
  return new Promise((resolve) => {
    const box = el("prompt"), input = el("prompt-input"), err = el("prompt-error");
    el("prompt-title").textContent = title || `New legal value for ${field.label || field.key}`;
    el("prompt-help").textContent = helpFor(field);
    input.value = "";
    err.textContent = "";
    box.hidden = false;
    input.focus();
    const done = (value) => { box.hidden = true; cleanup(); resolve(value); };
    const ok = async () => {
      const { value, error } = checkValue(field, input.value);
      if (error) { err.textContent = error; return; }
      if (optionsKey) {
        if (isLegal(optionsKey, value)) { err.textContent = "Already in the list."; return; }
        const list = legalValues(optionsKey).slice();
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

function buildField(field, container) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const label = document.createElement("label");
  label.textContent = field.label || field.key;
  const sel = makeSelect(field, field.key, () => state.config[field.key], (v) => { state.config[field.key] = v; render(); });
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
  for (const f of RUN_FIELDS) buildField(f, run);
  for (const f of ADVANCED_FIELDS) buildField(f, defaults);
  buildCameraRows();
}

function buildCameraRows() {
  const tbody = el("cameras").querySelector("tbody");
  tbody.textContent = "";
  state.config.cameras.forEach((cam, idx) => {
    const tr = document.createElement("tr");
    for (const f of CAMERA_FIELDS) {
      const td = document.createElement("td");
      const sel = makeSelect(f, f.options || "", () => cam[f.key], (v) => { cam[f.key] = v; render(); });
      sel.title = f.optional ? `${f.label}: (default) uses the value under Advanced` : f.label;
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
  for (const f of [...RUN_FIELDS, ...ADVANCED_FIELDS]) {
    if (c[f.key] == null) out.push(`${f.label} is not set`);
    else if (!isLegal(f.key, c[f.key])) out.push(`${f.label}: "${c[f.key]}" is not in the list of legal values`);
  }
  if (c.temp_path && c.save_path && c.temp_path === c.save_path) out.push("The temp folder and the save drive folder must differ");
  if (c.cameras.length === 0) out.push("At least one camera is needed");
  const names = new Map(), positions = new Map();
  c.cameras.forEach((cam, i) => {
    const n = i + 1;
    for (const f of CAMERA_FIELDS) {
      if (cam[f.key] == null) {
        if (!f.optional) out.push(`Camera ${n}: ${f.label.toLowerCase()} is not set`);
      } else if (f.options && !isLegal(f.options, cam[f.key])) {
        out.push(`Camera ${n}: ${f.label.toLowerCase()} "${cam[f.key]}" is not in the list of legal values`);
      }
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

// the config as it will be written: keys in the file's order, resolutions split into
// width and height, unset optionals omitted
function fileObject() {
  const c = state.config;
  const out = {};
  const [dw, dh] = splitResolution(c.default_resolution);
  const flat = { ...c, default_width: dw, default_height: dh };
  for (const k of FILE_KEY_ORDER) {
    if (k === "cameras") {
      out.cameras = c.cameras.map((cam) => {
        const [w, h] = splitResolution(cam.resolution);
        const src = { ...cam, width: w, height: h };
        const o = {};
        for (const key of CAMERA_KEY_ORDER) if (src[key] != null) o[key] = src[key];
        return o;
      });
    } else out[k] = flat[k];
  }
  return out;
}

// the same layout the server writes: one camera per line
function fileText() {
  const obj = fileObject();
  const lines = ["{"];
  const keys = Object.keys(obj);
  keys.forEach((key, i) => {
    const comma = i < keys.length - 1 ? "," : "";
    if (key === "cameras") {
      lines.push('  "cameras": [');
      lines.push(obj.cameras.map((cam) => "    " + JSON.stringify(cam).replace(/,"/g, ', "').replace(/":/g, '": ')).join(",\n"));
      lines.push("  ]" + comma);
    } else lines.push(`  ${JSON.stringify(key)}: ${JSON.stringify(obj[key])}${comma}`);
  });
  lines.push("}");
  return lines.join("\n") + "\n";
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
  const src = data.config;
  const c = emptyConfig();
  for (const f of [...RUN_FIELDS, ...ADVANCED_FIELDS]) if (f.key !== "default_resolution" && src[f.key] !== undefined) c[f.key] = src[f.key];
  c.default_resolution = resolutionOf(src.default_width, src.default_height);
  c.cameras = (Array.isArray(src.cameras) ? src.cameras : []).map((s) => {
    const cam = emptyCamera();
    for (const f of CAMERA_FIELDS) if (f.key !== "resolution" && s[f.key] !== undefined) cam[f.key] = s[f.key];
    cam.resolution = resolutionOf(s.width, s.height);
    return cam;
  });
  state.config = c;
  buildForm();
  render();
  const extra = Object.keys(src).filter((k) => !FILE_KEY_ORDER.includes(k));
  setStatus(`Loaded ${data.path}` + (extra.length ? ` (unknown fields dropped: ${extra.join(", ")})` : ""), data.problems.length ? "error" : "ok");
}

async function saveFile() {
  const path = el("file-path").value.trim();
  if (!path) { setStatus("Enter a file path first", "error"); return; }
  const found = problems();
  if (found.length) { setStatus(`Not saved: ${found.length} problem${found.length === 1 ? "" : "s"} listed below`, "error"); return; }
  const { data: probe } = await api("GET", `/api/config?path=${encodeURIComponent(path)}`);
  if (probe.exists && !confirm(`${probe.path} exists. Overwrite it?`)) { setStatus("Not saved", ""); return; }
  const { ok, data } = await api("PUT", `/api/config?path=${encodeURIComponent(path)}`, fileObject());
  if (ok) setStatus(`Saved ${data.path}`, "ok");
  else setStatus(`Not saved: ${(data.problems || ["unknown error"]).join("; ")}`, "error");
}

// ---------- the legal-values panel ----------

// every list the menus draw on, in the form's three sections, with which field(s) use it
function listSections() {
  const run = RUN_FIELDS.map((f) => ({ key: f.key, label: f.label, inUse: (v) => String(state.config[f.key]) === String(v) }));
  const cameras = CAMERA_FIELDS.filter((f) => f.options).map((f) => ({
    key: f.options, label: f.label, hasDefault: !!f.optional,
    inUse: (v) => state.config.cameras.some((cam) => String(cam[f.key]) === String(v)),
  }));
  const defaults = ADVANCED_FIELDS.map((f) => ({ key: f.key, label: f.label, inUse: (v) => String(state.config[f.key]) === String(v) }));
  return [{ title: "Run", lists: run }, { title: "Per-camera settings", lists: cameras }, { title: "Default settings", lists: defaults }];
}

function allLists() {
  return listSections().flatMap((s) => s.lists);
}

function renderManage() {
  const box = el("manage-lists");
  box.textContent = "";
  for (const section of listSections()) {
    const block = document.createElement("div");
    block.className = "vsection";
    const title = document.createElement("div");
    title.className = "vtitle";
    title.textContent = section.title;
    block.appendChild(title);
    const grid = document.createElement("div");
    grid.className = "vgrid";
    block.appendChild(grid);
    box.appendChild(block);
    for (const list of section.lists) renderList(list, grid);
  }
}

function renderList(list, box) {
  {
    const wrap = document.createElement("div");
    wrap.className = "vlist";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = list.label;
    wrap.appendChild(name);
    const values = legalValues(list.key);
    if (list.hasDefault) { // always on the menu, never removable
      const item = document.createElement("div");
      item.className = "item";
      const text = document.createElement("span");
      text.className = "v";
      text.textContent = "(default)";
      item.appendChild(text);
      wrap.appendChild(item);
    } else if (values.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "none";
      wrap.appendChild(empty);
    }
    for (const v of values) {
      const item = document.createElement("div");
      item.className = "item";
      const text = document.createElement("span");
      text.className = "v";
      text.textContent = String(v);
      item.appendChild(text);
      if (list.inUse(v)) {
        const tag = document.createElement("span");
        tag.className = "inuse";
        tag.textContent = "in use";
        item.appendChild(tag);
      } else {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "small";
        remove.textContent = "remove";
        remove.addEventListener("click", async () => {
          state.options.options[list.key] = legalValues(list.key).filter((x) => String(x) !== String(v));
          await saveOptions();
          refreshSelects(list.key);
          render();
          renderManage();
        });
        item.appendChild(remove);
      }
      wrap.appendChild(item);
    }
    box.appendChild(wrap);
  }
}

async function resetValues() {
  if (!confirm("Replace every list with the values shipped with the program? Values added on this machine are lost (the machine name is kept).")) return;
  const { ok, data } = await api("PUT", "/api/options?reset=1", {});
  if (!ok) { alert(`Could not reset: ${data.error || "unknown error"}`); return; }
  await loadOptions();
  for (const list of allLists()) refreshSelects(list.key);
  render();
  renderManage();
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
  state.config = emptyConfig();
  el("file-path").value = "~/Desktop/camera_config.json";
  buildForm();
  render();
  el("load").addEventListener("click", loadFile);
  el("save").addEventListener("click", saveFile);
  el("add-camera").addEventListener("click", () => { state.config.cameras.push(emptyCamera()); buildCameraRows(); render(); });
  el("rename-machine").addEventListener("click", renameMachine);
  el("manage-values").addEventListener("click", () => { renderManage(); el("manage").hidden = false; });
  el("manage-close").addEventListener("click", () => { el("manage").hidden = true; });
  el("manage-reset").addEventListener("click", resetValues);
  el("file-path").addEventListener("keydown", (e) => { if (e.key === "Enter") loadFile(); });
}

main();
