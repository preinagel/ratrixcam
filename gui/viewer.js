"use strict";

// ratrixcam passive viewer. Reads the temp folder that serve.py exposes at /temp/ and shows
// the camera stills and the run accounting. Never writes anything.

const TEMP = "/temp/";
const METADATA_DIR = "ratrixcam_run_metadata";
const STILLS_DIR = "status";
const STATUS_FILENAME = "ratrixcam_status.json"; // written by python (see STATUS_FILE_WISHLIST.md)
const OFFLINE_IMAGE = "offline_status.png";

// camera positions on the display: A B | E F over C D | G H; grid column 2 is the gap
const POSITIONS = { A: [0, 0], B: [0, 1], E: [0, 3], F: [0, 4], C: [1, 0], D: [1, 1], G: [1, 3], H: [1, 4] };
const GAP_COL = 2;

// sizing, as fractions of the tile width
const TILE_GAP = 0.025;
const BLOCK_GAP = 0.09;
const TITLE_FONT = 0.05; // clamped to 9..18 px
const ACC_ROWS = 4; // rows in the accounting table (two columns)
const ACC_MIN_H = 80; // the accounting table never gets less than this (px)
const ACC_FONT_MIN = 11, ACC_FONT_MAX = 26;

// save-drive warning thresholds, on the expected recording time left
const FYI_S = 24 * 3600; // orange: space for less than a day
const NEARLY_FULL_S = 3600; // red

// "no space on any drive": the status file has not been rewritten for this long, and its last
// contents showed the save drive full and the temp drive without room for even the status
// file itself (python keeps writing it as long as the temp drive can hold it)
const STATUS_STALE_S = 60;
const STATUS_FILE_ROOM = 1000000; // bytes

const LOG_CHECK_S = 10; // how often the log tail is read while every camera is offline
const LOG_CHECK_SHUTDOWN_S = 1; // ... and once a shutdown is under way, to catch its last lines
const LOG_TAIL_LINES = 60;
const LAST = "/last/"; // serve.py keeps the last status file and log it served, for after cleanup

const state = {
  config: null,
  configName: null,
  configCandidates: [],
  cameras: [], // {name, subjID, view, position, tile, stillMtime, lastSeen, offline}
  status: null, // last good status file contents
  statusWritten: null, // Date from its "written" field
  runStarted: null, // Date
  runId: null,
  logName: null,
  layout: "strict",
  tempSeen: false, // the temp folder has answered at least once
  tempGone: null, // Date the temp folder disappeared, or null
  shutdown: null, // {kind: "graceful" | "aborted" | "crash", since: Date} while all cameras are offline
  lastLogCheck: 0,
  logTail: [], // last lines of the terminal log, from the most recent read
  lastChecked: false, // /last/ was consulted because the temp folder was never seen
};

// ---------- fetch helpers ----------

async function fetchJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}

async function fetchText(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.text();
}

async function listDir(rel) {
  const data = await fetchJSON(TEMP + rel + (rel === "" || rel.endsWith("/") ? "" : "/"));
  return data.entries || [];
}

// ---------- formatting ----------

const pad2 = (n) => String(n).padStart(2, "0");
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function fmtDate(d) {
  return `${DAYS[d.getDay()]}, ${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
}

function fmtHM(d) {
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

function fmtHMS(d) {
  return `${fmtHM(d)}:${pad2(d.getSeconds())}`;
}

function fmtDHM(seconds) {
  if (seconds == null || !isFinite(seconds)) return "";
  seconds = Math.max(0, seconds);
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  return `${days} d ${hours} h ${mins} min`;
}

// at most three digits, in the unit that makes that possible
function fmtBytes(bytes) {
  if (bytes == null || !isFinite(bytes)) return "";
  const units = ["bytes", "kB", "MB", "GB", "TB", "PB"];
  let v = bytes, i = 0;
  while (v >= 999.5 && i < units.length - 1) { v /= 1000; i++; }
  if (i === 0) return `${Math.round(v)} bytes`;
  return `${v >= 99.5 ? v.toFixed(0) : v >= 9.95 ? v.toFixed(1) : v.toFixed(2)} ${units[i]}`;
}

// "2026-11-02 09:41:39.803" or ISO "2026-11-02T09:41:39" (local time) -> Date
function parseLocalTime(text) {
  if (!text) return null;
  const m = String(text).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?/);
  if (!m) return null;
  return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6], m[7] ? +m[7].slice(0, 3).padEnd(3, "0") : 0);
}

// a path for reading at a glance: the home folder prefix dropped, long paths cut to their tail
function shortPath(path) {
  if (!path) return "";
  let s = String(path).replace(/\/+$/, "").replace(/^\/Users\/[^/]+\//, "");
  const parts = s.split("/").filter(Boolean);
  if (parts.length > 3) s = "…/" + parts.slice(-2).join("/");
  return s || path;
}

// ---------- config ----------

function looksLikeConfig(obj) {
  return obj && typeof obj === "object" && Array.isArray(obj.cameras)
    && obj.cameras.every((c) => c && c.subjID != null && c.view && c.position)
    && typeof obj.study_label === "string" && typeof obj.subject_prefix === "string"
    && typeof obj.time_slice === "number";
}

async function loadConfig() {
  const entries = await listDir(METADATA_DIR);
  const candidates = [];
  for (const e of entries) {
    if (e.is_dir || !e.name.toLowerCase().endsWith(".json")) continue;
    try {
      const obj = await fetchJSON(`${TEMP}${METADATA_DIR}/${encodeURIComponent(e.name)}`);
      if (looksLikeConfig(obj)) candidates.push({ name: e.name, mtime: e.mtime, config: obj });
    } catch (err) { /* unreadable or not JSON: not a config */ }
  }
  candidates.sort((a, b) => b.mtime - a.mtime);
  state.configCandidates = candidates.map((c) => c.name);
  state.config = candidates.length ? candidates[0].config : null;
  state.configName = candidates.length ? candidates[0].name : null;
}

function cameraName(cfg, cam) {
  return `${cfg.subject_prefix}${cam.subjID}_${cam.view}`;
}

function previewInterval() {
  return state.config && state.config.preview_interval > 0 ? state.config.preview_interval : 1;
}

function offlineAfterSeconds() {
  return Math.max(10, 10 * previewInterval());
}

// ---------- camera grid ----------

function el(id) { return document.getElementById(id); }

function buildTiles() {
  const grid = el("grid");
  grid.textContent = "";
  state.cameras = [];
  if (!state.config) return;
  for (const cam of state.config.cameras) {
    const tile = document.createElement("div");
    tile.className = "tile";
    tile.innerHTML = `
      <div class="title"><span class="subj"></span><span class="view"></span><span class="seen"></span><span class="pos"></span></div>
      <div class="still"><img alt=""></div>`;
    tile.querySelector(".subj").textContent = `${state.config.subject_prefix}${cam.subjID}`;
    tile.querySelector(".view").textContent = cam.view;
    tile.querySelector(".pos").textContent = cam.position;
    tile.querySelector("img").src = OFFLINE_IMAGE;
    grid.appendChild(tile);
    state.cameras.push({
      name: cameraName(state.config, cam), subjID: cam.subjID, view: cam.view, position: cam.position,
      tile, stillMtime: null, lastSeen: null, offline: false,
    });
  }
  applyLayout();
}

// strict: all eight positions keep their place; compact: rows and columns without a camera
// are dropped, relative order preserved, the block gap kept only when both blocks are used
function applyLayout() {
  const grid = el("grid");
  let rows = [0, 1], cols = [0, 1, GAP_COL, 3, 4];
  if (state.layout === "compact") {
    const used = state.cameras.map((c) => POSITIONS[c.position]).filter(Boolean);
    rows = [0, 1].filter((r) => used.some((p) => p[0] === r));
    const usedCols = [0, 1, 3, 4].filter((c) => used.some((p) => p[1] === c));
    const left = usedCols.some((c) => c < GAP_COL), right = usedCols.some((c) => c > GAP_COL);
    cols = usedCols.slice();
    if (left && right) cols.splice(cols.findIndex((c) => c > GAP_COL), 0, GAP_COL);
  }
  grid.style.gridTemplateColumns = cols.map((c) => (c === GAP_COL ? "var(--block-gap)" : "var(--tile-w)")).join(" ");
  grid.style.gridTemplateRows = rows.map(() => "auto").join(" ");
  for (const cam of state.cameras) {
    const p = POSITIONS[cam.position];
    const r = p ? rows.indexOf(p[0]) : -1, c = p ? cols.indexOf(p[1]) : -1;
    cam.tile.style.display = r < 0 || c < 0 ? "none" : "";
    cam.tile.style.gridRow = String(r + 1);
    cam.tile.style.gridColumn = String(c + 1);
  }
  grid.dataset.rows = rows.length;
  grid.dataset.cols = cols.filter((c) => c !== GAP_COL).length;
  grid.dataset.gap = cols.includes(GAP_COL) ? "1" : "0";
  fitTiles();
}

function titleFont(w) {
  return Math.min(18, Math.max(9, w * TITLE_FONT));
}

// choose the tile width so the grid fills the window's width, or its height after the top
// strip, the banners and a minimum accounting table; gaps and the title font follow the
// tile width, so they shrink with the window
function fitTiles() {
  const grid = el("grid");
  const nRows = +grid.dataset.rows || 0, nCols = +grid.dataset.cols || 0;
  if (!nRows || !nCols) return;
  const hasGap = grid.dataset.gap === "1";
  const pad = 2 * (parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--pad")) || 8);
  const availW = window.innerWidth - pad - 4;
  const availH = window.innerHeight - el("topbar").offsetHeight - el("banners").offsetHeight - ACC_MIN_H - pad - 8;
  const wByWidth = availW / (nCols + TILE_GAP * (nCols - 1) + (hasGap ? BLOCK_GAP : 0));
  let w = wByWidth;
  for (let i = 0; i < 4; i++) {
    const chrome = titleFont(w) * 1.25 * 1.2 + 2; // title line, its padding, borders
    const height = nRows * (w * 0.75 + chrome) + (nRows - 1) * TILE_GAP * w;
    if (height <= availH) break;
    w = (availH - nRows * chrome) / (nRows * 0.75 + (nRows - 1) * TILE_GAP);
  }
  w = Math.max(100, Math.floor(Math.min(w, wByWidth)));
  const root = document.documentElement.style;
  root.setProperty("--tile-w", `${w}px`);
  root.setProperty("--tile-gap", `${Math.max(2, Math.round(TILE_GAP * w))}px`);
  root.setProperty("--block-gap", `${Math.max(6, Math.round(BLOCK_GAP * w))}px`);
  root.setProperty("--title-font", `${titleFont(w).toFixed(1)}px`);
  fitAccounting();
}

// the accounting table takes whatever height is left; its font grows to use it, as far as
// the widest cell still fits its column
function fitAccounting() {
  const box = el("accounting");
  if (box.hidden) return;
  const root = document.documentElement.style;
  let size = (box.clientHeight - 8) / (ACC_ROWS * 1.5);
  const probe = 10; // measure every cell's text at this size, then scale
  root.setProperty("--acc-font", `${probe}px`);
  for (const cell of box.querySelectorAll(".cell")) {
    const inner = cell.firstElementChild; // the text, measured on its own so it can exceed the cell
    const textW = inner ? inner.getBoundingClientRect().width : 0;
    if (textW > 0) size = Math.min(size, probe * cell.clientWidth / textW);
  }
  size = Math.min(ACC_FONT_MAX, Math.max(ACC_FONT_MIN, size));
  root.setProperty("--acc-font", `${size.toFixed(1)}px`);
}

function setLayout(mode) {
  state.layout = mode;
  el("layout-btn").textContent = `Layout: ${mode}`;
  try { localStorage.setItem("ratrixcam-viewer-layout", mode); } catch (e) { /* private window etc. */ }
  applyLayout();
}

// ---------- stills ----------

// a still may be half-written: decode it off-screen and only then swap it in
function loadStill(cam, mtime) {
  const img = new Image();
  const url = `${TEMP}${STILLS_DIR}/cam_${encodeURIComponent(cam.name)}_status.png?t=${mtime}`;
  img.onload = () => { if (!cam.offline) cam.tile.querySelector("img").src = url; };
  img.onerror = () => { cam.stillMtime = null; }; // retry on the next poll
  cam.stillMtime = mtime;
  img.src = url;
}

function updateStills(entries) {
  const byName = new Map(entries.map((e) => [e.name, e]));
  const now = Date.now() / 1000;
  for (const cam of state.cameras) {
    const entry = byName.get(`cam_${cam.name}_status.png`);
    if (entry) cam.lastSeen = new Date(entry.mtime * 1000);
    const offline = !entry || now - entry.mtime > offlineAfterSeconds();
    if (offline && !cam.offline) cam.tile.querySelector("img").src = OFFLINE_IMAGE;
    cam.offline = offline;
    if (entry && entry.mtime !== cam.stillMtime && !offline) loadStill(cam, entry.mtime);
    cam.tile.querySelector(".seen").textContent = cam.lastSeen ? `still ${fmtHMS(cam.lastSeen)}` : "no still yet";
  }
}

// ---------- temp folder, log and shutdown detection ----------

function noteLogEntry(entries) {
  for (const e of entries) {
    const m = e.name.match(/^terminal_(ratrixcam_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_[0-9a-f]+)\.log$/);
    if (!m) continue;
    state.logName = e.name;
    // until the status file provides them, run id and start time come from the log's name
    if (!state.runId) state.runId = m[1];
    if (!state.runStarted) state.runStarted = new Date(+m[2], +m[3] - 1, +m[4], +m[5], +m[6], +m[7]);
    return;
  }
}

// while every camera is offline: did the program shut down on purpose, or just stop?
async function checkShutdown() {
  const all = state.cameras.length > 0 && state.cameras.every((c) => c.offline);
  if (!all) { state.shutdown = null; return; }
  const now = Date.now();
  const every = state.shutdown && state.shutdown.kind !== "crash" ? LOG_CHECK_SHUTDOWN_S : LOG_CHECK_S;
  if (now - state.lastLogCheck < every * 1000) return;
  state.lastLogCheck = now;
  const since = state.cameras.reduce((d, c) => (c.lastSeen && (!d || c.lastSeen > d) ? c.lastSeen : d), null);
  if (state.logName) {
    try { noteLogText(await fetchText(TEMP + encodeURIComponent(state.logName))); } catch (e) { /* unreadable: keep the last tail */ }
  }
  state.shutdown = { kind: shutdownKind(state.logTail), since };
}

function noteLogText(text) {
  state.logTail = text.split(/\r?\n/).filter((l) => l !== "").slice(-LOG_TAIL_LINES);
}

function shutdownKind(lines) {
  const tail = lines.join("\n");
  if (tail.includes("attempting graceful shutdown")) return "graceful";
  if (tail.includes("Run aborted")) return "aborted";
  return "crash";
}

// the temp folder is already gone when the page opens: what serve.py last saw of the run
async function loadLast() {
  state.lastChecked = true;
  let found = false;
  try {
    const st = await fetchJSON(LAST + "status.json");
    state.status = st;
    state.statusWritten = parseLocalTime(st.written);
    if (st.run) {
      const d = parseLocalTime(st.run.started);
      if (d) state.runStarted = d;
      if (st.run.run_id) state.runId = st.run.run_id;
    }
    found = true;
  } catch (e) { /* nothing cached */ }
  try { noteLogText(await fetchText(LAST + "log.txt")); found = true; } catch (e) { /* nothing cached */ }
  if (found) state.tempGone = state.statusWritten || new Date();
}

async function pollTemp() {
  let root;
  try { root = await listDir(""); } catch (e) { root = null; }
  if (root === null) {
    if (state.tempSeen && !state.tempGone) state.tempGone = new Date();
    else if (!state.tempSeen && !state.lastChecked) await loadLast();
    return;
  }
  if (state.tempGone) { location.reload(); return; } // a new run in the same temp folder
  state.tempSeen = true;
  noteLogEntry(root);
  let stills = [];
  try { stills = await listDir(STILLS_DIR); } catch (e) { /* no stills folder yet */ }
  updateStills(stills);
  await checkShutdown();
}

// ---------- status file (run accounting) ----------

function statusUrl() {
  const override = new URLSearchParams(location.search).get("status"); // testing aid
  return override || TEMP + STATUS_FILENAME;
}

async function pollStatus() {
  let r;
  try { r = await fetch(statusUrl(), { cache: "no-store" }); } catch (e) { return; }
  if (!r.ok) return;
  try { state.status = await r.json(); } catch (e) { return; } // half-written: keep the previous contents
  state.statusWritten = parseLocalTime(state.status && state.status.written);
  const run = state.status && state.status.run;
  if (run) {
    const d = parseLocalTime(run.started);
    if (d) state.runStarted = d;
    if (run.run_id) state.runId = run.run_id;
  }
}

// {cls, head, body} for the save-drive banner, or null
function driveWarning() {
  const st = state.status;
  if (!st) return null;
  const save = st.save_drive || {}, temp = st.temp_drive || {};
  const tempLeft = temp.seconds_until_full;
  if (save.found === false) {
    return { cls: "alarm", head: "SAVE DRIVE NOT FOUND", body: save.path || (state.config && state.config.save_path) || "" };
  }
  const left = save.seconds_until_full;
  if (left == null) return null;
  const clip = state.config ? state.config.time_slice : 0;
  const saveFull = save.free_bytes === 0 || left < clip;
  // the status file stopped coming after reporting the save drive full and the temp drive
  // without room for the file itself: the cameras are dropping frames (a still may still
  // appear now and then; it is shown while fresh)
  const stale = state.statusWritten && Date.now() - state.statusWritten > STATUS_STALE_S * 1000;
  if (stale && saveFull && temp.free_bytes != null && temp.free_bytes < STATUS_FILE_ROOM) {
    return { cls: "alarm", head: "NO SPACE ON ANY DRIVE: cameras are not being recorded",
      body: `Status last written ${fmtHM(state.statusWritten)} with ${fmtBytes(temp.free_bytes)} left on the temp drive` };
  }
  if (saveFull) {
    return { cls: "alarm", head: "SAVE DRIVE FULL", body: `Temp drive cache capacity: ${tempLeft != null ? fmtDHM(tempLeft) : "unknown"}` };
  }
  if (left < NEARLY_FULL_S) return { cls: "alarm", head: "SAVE DRIVE NEARLY FULL", body: `${fmtDHM(left)} of recording left` };
  if (left < FYI_S) return { cls: "warn", head: `Save drive has space for another ${fmtDHM(left)}`, body: "" };
  return null;
}

// ---------- rendering ----------

function cellHTML(label, value, cls = "") {
  const span = document.createElement("span");
  span.textContent = value;
  const v = cls ? `<span class="${cls}">${span.innerHTML}</span>` : span.innerHTML;
  const l = document.createElement("span");
  l.textContent = label;
  return `<div class="cell"><span class="inner"><span class="label">${l.innerHTML}</span>${v}</span></div>`;
}

function renderAccounting() {
  const cfg = state.config, st = state.status;
  const now = new Date();
  el("machine").textContent = st && st.run && st.run.machine_name ? st.run.machine_name : "";
  el("clock").textContent = `${fmtDate(now)}  ${fmtHM(now)}`;

  const save = (st && st.save_drive) || {}, temp = (st && st.temp_drive) || {};
  const warning = driveWarning();
  const level = warning ? warning.cls : "";
  // left column: the run in time; right column: the drives. Filled row by row.
  const left = [], right = [];
  left.push(cellHTML("Started", state.runStarted ? `${fmtDate(state.runStarted)} ${fmtHM(state.runStarted)}` : ""));
  left.push(cellHTML("Running for", state.runStarted ? fmtDHM((now - state.runStarted) / 1000) : ""));
  const leftKnown = save.found !== false && save.seconds_until_full != null;
  const leftCls = !leftKnown ? "" : save.seconds_until_full <= 0 ? "zero" : save.seconds_until_full < FYI_S ? "warn" : "";
  left.push(cellHTML("Recording time left", leftKnown ? fmtDHM(save.seconds_until_full) : "", leftCls));
  left.push(cellHTML("Clip length", cfg ? `${Number.isInteger(cfg.time_slice / 60) ? cfg.time_slice / 60 : (cfg.time_slice / 60).toFixed(1)} min` : ""));

  right.push(cellHTML("Save drive", cfg ? shortPath(cfg.save_path) : ""));
  const freeKnown = save.found !== false && save.free_bytes != null;
  if (save.total_bytes > 0 && save.used_bytes != null && freeKnown) {
    const pct = Math.min(100, 100 * save.used_bytes / save.total_bytes);
    const free = document.createElement("span");
    free.textContent = `${pct.toFixed(0)}% used, ${fmtBytes(save.free_bytes)} free`;
    const freeCls = save.free_bytes === 0 ? "zero" : "";
    right.push(`<div class="cell"><span class="inner"><span class="bar"><span class="bar-fill ${level}" style="width:${pct.toFixed(1)}%"></span></span><span class="${freeCls}">${free.innerHTML}</span></span></div>`);
  } else {
    right.push(cellHTML("Save drive free", freeKnown ? fmtBytes(save.free_bytes) : "", freeKnown && save.free_bytes === 0 ? "zero" : ""));
  }
  right.push(cellHTML("Temp folder", cfg ? shortPath(cfg.temp_path) : ""));
  right.push(cellHTML("Temp drive free", temp.free_bytes != null ? fmtBytes(temp.free_bytes) : "", temp.free_bytes === 0 ? "zero" : ""));

  const cells = [];
  for (let i = 0; i < ACC_ROWS; i++) cells.push(left[i] || "", right[i] || "");
  const html = cells.join("");
  const box = el("accounting");
  if (box.dataset.html !== html) { box.innerHTML = html; box.dataset.html = html; fitAccounting(); }
}

function renderBanners() {
  const banners = [];
  if (state.shutdown) {
    // the program is not running: its drive warnings no longer apply
    const when = state.shutdown.since ? fmtHM(state.shutdown.since) : "start";
    if (state.shutdown.kind === "graceful") banners.push({ cls: "info", head: `Recording stopped at ${when}`, body: "Shutting down" });
    else if (state.shutdown.kind === "aborted") banners.push({ cls: "info", head: `Run aborted at ${when}`, body: "Everything left in the temp folder" });
    else banners.push({ cls: "alarm", head: "CAMERA PROGRAM STOPPED WITHOUT SHUTTING DOWN", body: `No still from any camera since ${when}` });
  } else {
    const drive = driveWarning();
    if (drive) banners.push(drive);
  }
  if (!state.tempSeen) {
    banners.push({ cls: "info", head: "Temp folder not found", body: "No run in progress?" });
  } else if (!state.config) {
    banners.push({ cls: "alarm", head: "No camera config found in the temp folder", body: `Looked in ${METADATA_DIR}/` });
  } else if (state.configCandidates.length > 1) {
    banners.push({ cls: "warn", head: `Several config files in ${METADATA_DIR}/`, body: `Showing the newest (${state.configName}); others: ${state.configCandidates.slice(1).join(", ")}` });
  }
  const box = el("banners");
  const sig = JSON.stringify(banners);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.textContent = "";
  for (const b of banners) {
    const node = document.createElement("div");
    node.className = `banner ${b.cls}`;
    const head = document.createElement("div");
    head.className = "head";
    head.textContent = b.head;
    node.appendChild(head);
    if (b.body) {
      const body = document.createElement("div");
      body.className = "body";
      body.textContent = b.body;
      node.appendChild(body);
    }
    box.appendChild(node);
  }
  fitTiles();
}

function renderEnded() {
  const ended = !!state.tempGone;
  el("ended").hidden = !ended;
  el("grid-wrap").hidden = ended;
  el("accounting").hidden = ended;
  el("banners").hidden = ended;
  if (!ended) return;
  el("ended-text").textContent = state.runStarted
    ? `Last run started ${fmtDate(state.runStarted)} ${fmtHM(state.runStarted)} and ran for ${fmtDHM((state.tempGone - state.runStarted) / 1000)}`
    : "";
}

// ---------- main ----------

async function tick() {
  await pollStatus();
  await pollTemp();
  renderEnded();
  if (!state.tempGone) { renderAccounting(); renderBanners(); }
}

async function main() {
  try { state.layout = localStorage.getItem("ratrixcam-viewer-layout") || "strict"; } catch (e) { /* ignore */ }
  el("layout-btn").addEventListener("click", () => setLayout(state.layout === "strict" ? "compact" : "strict"));
  el("layout-btn").textContent = `Layout: ${state.layout}`;
  window.addEventListener("resize", fitTiles);

  try { await loadConfig(); } catch (e) { state.config = null; }
  buildTiles();
  await tick();
  fitTiles();
  setInterval(tick, previewInterval() * 1000);
  // the config is re-read occasionally in case the viewer was opened before the run started
  setInterval(async () => {
    if (state.config || state.tempGone) return;
    try { await loadConfig(); } catch (e) { return; }
    if (state.config) buildTiles();
  }, 5000);
}

main();
