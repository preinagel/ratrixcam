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

// save-drive warning thresholds, on the expected recording time left
const ALMOST_FULL_S = 24 * 3600; // orange
const NEARLY_FULL_S = 3600; // red

// a status file this old with the temp drive nearly full means the cameras most likely
// stopped for lack of space (python's own staleness is not otherwise reported)
const STATUS_STALE_S = 60;

const LOG_CHECK_S = 10; // how often the log tail is read while every camera is offline
const LOG_TAIL_LINES = 60;

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

// choose --tile-w so the whole grid fits the space left between the banners and the accounting
function fitTiles() {
  const wrap = el("grid-wrap"), grid = el("grid");
  const nRows = +grid.dataset.rows || 0, nCols = +grid.dataset.cols || 0;
  if (!nRows || !nCols) return;
  const css = getComputedStyle(document.documentElement);
  const tileGap = parseFloat(css.getPropertyValue("--tile-gap")) || 12;
  const blockGap = grid.dataset.gap === "1" ? (parseFloat(css.getPropertyValue("--block-gap")) || 36) + tileGap : 0;
  const availW = wrap.clientWidth - 24, availH = wrap.clientHeight - 16;
  const chrome = 32; // title bar and borders, per tile
  const byWidth = (availW - blockGap - tileGap * (nCols - 1)) / nCols;
  const byHeight = ((availH - tileGap * (nRows - 1)) / nRows - chrome) * 4 / 3;
  const w = Math.max(120, Math.floor(Math.min(byWidth, byHeight)));
  document.documentElement.style.setProperty("--tile-w", `${w}px`);
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
  if (now - state.lastLogCheck < LOG_CHECK_S * 1000) return;
  state.lastLogCheck = now;
  const since = state.cameras.reduce((d, c) => (c.lastSeen && (!d || c.lastSeen > d) ? c.lastSeen : d), null);
  let kind = "crash";
  if (state.logName) {
    try {
      const text = await fetchText(TEMP + encodeURIComponent(state.logName));
      const tail = text.split(/\r?\n/).slice(-LOG_TAIL_LINES).join("\n");
      if (tail.includes("attempting graceful shutdown")) kind = "graceful";
      else if (tail.includes("Run aborted")) kind = "aborted";
    } catch (e) { /* log unreadable: keep "crash" */ }
  }
  state.shutdown = { kind, since };
}

async function pollTemp() {
  let root;
  try { root = await listDir(""); } catch (e) { root = null; }
  if (root === null) {
    if (state.tempSeen && !state.tempGone) state.tempGone = new Date();
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

// {cls, text} for the save-drive banner, or null
function driveWarning() {
  const st = state.status;
  if (!st) return null;
  const save = st.save_drive || {}, temp = st.temp_drive || {};
  const tempLeft = temp.seconds_until_full;
  if (save.found === false) {
    return { cls: "alarm", text: `SAVE DRIVE NOT FOUND: ${save.path || (state.config && state.config.save_path) || ""}` };
  }
  const left = save.seconds_until_full;
  if (left == null) return null;
  const clip = state.config ? state.config.time_slice : 0;
  const saveFull = save.free_bytes === 0 || left < clip;
  if (saveFull && (temp.free_bytes === 0 || (tempLeft != null && tempLeft < clip))) {
    return { cls: "alarm", text: "NO SPACE ON ANY DRIVE: cameras are not being recorded" };
  }
  // the status file stopped coming while the temp drive was nearly full: the cameras most
  // likely ran out of space (stills may still appear now and then; they are shown while fresh)
  const stale = state.statusWritten && Date.now() - state.statusWritten > STATUS_STALE_S * 1000;
  if (stale && saveFull && tempLeft != null && tempLeft < NEARLY_FULL_S) {
    return { cls: "alarm", text: `NO SPACE ON ANY DRIVE: cameras are not being recorded\n(status last written ${fmtHM(state.statusWritten)} with ${fmtDHM(tempLeft)} left on the temp drive)` };
  }
  if (saveFull) {
    return { cls: "alarm", text: `SAVE DRIVE FULL. Temp drive cache capacity: ${tempLeft != null ? fmtDHM(tempLeft) : "unknown"}` };
  }
  if (left < NEARLY_FULL_S) return { cls: "alarm", text: `SAVE DRIVE NEARLY FULL: ${fmtDHM(left)} of recording left` };
  if (left < ALMOST_FULL_S) return { cls: "warn", text: `SAVE DRIVE ALMOST FULL: ${fmtDHM(left)} of recording left` };
  return null;
}

// ---------- rendering ----------

function cellHTML(label, value, opts = {}) {
  const span = document.createElement("span");
  span.textContent = value;
  const v = opts.zero ? `<span class="zero">${span.innerHTML}</span>` : span.innerHTML;
  const l = document.createElement("span");
  l.textContent = label;
  return `<div class="cell"><span class="label">${l.innerHTML}</span>${v}</div>`;
}

function renderAccounting() {
  const cfg = state.config, st = state.status;
  const now = new Date();
  const station = st && st.run && st.run.station ? `${st.run.station}   ` : "";
  el("clock").textContent = `${station}${fmtDate(now)}  ${fmtHM(now)}`;

  const save = (st && st.save_drive) || {}, temp = (st && st.temp_drive) || {};
  const warning = driveWarning();
  const level = warning ? warning.cls : "";
  const cells = [];
  cells.push(cellHTML("Started", state.runStarted ? `${fmtDate(state.runStarted)} ${fmtHM(state.runStarted)}` : ""));
  cells.push(cellHTML("Running for", state.runStarted ? fmtDHM((now - state.runStarted) / 1000) : ""));
  cells.push(cellHTML("Clip length", cfg ? `${Number.isInteger(cfg.time_slice / 60) ? cfg.time_slice / 60 : (cfg.time_slice / 60).toFixed(1)} min` : ""));

  const freeKnown = save.found !== false && save.free_bytes != null;
  cells.push(cellHTML("Save drive free", freeKnown ? fmtBytes(save.free_bytes) : "", { zero: freeKnown && save.free_bytes === 0 }));
  const leftKnown = save.found !== false && save.seconds_until_full != null;
  cells.push(cellHTML("Recording time left", leftKnown ? fmtDHM(save.seconds_until_full) : "", { zero: leftKnown && save.seconds_until_full <= 0 }));
  if (save.total_bytes > 0 && save.used_bytes != null) {
    const pct = Math.min(100, 100 * save.used_bytes / save.total_bytes);
    cells.push(`<div class="cell"><span class="bar"><span class="bar-fill ${level}" style="width:${pct.toFixed(1)}%"></span></span>${pct.toFixed(0)}% of ${fmtBytes(save.total_bytes)} used</div>`);
  } else {
    cells.push(cellHTML("Save drive used", ""));
  }

  cells.push(cellHTML("Temp folder", cfg ? shortPath(cfg.temp_path) : ""));
  cells.push(cellHTML("Save drive", cfg ? shortPath(cfg.save_path) : ""));
  cells.push(cellHTML("Temp drive free", temp.free_bytes != null ? fmtBytes(temp.free_bytes) : "", { zero: temp.free_bytes === 0 }));

  const html = cells.join("");
  const box = el("accounting");
  if (box.dataset.html !== html) { box.innerHTML = html; box.dataset.html = html; }
}

function renderBanners() {
  const banners = [];
  const drive = driveWarning();
  if (drive) banners.push(drive);
  if (state.shutdown) {
    const when = state.shutdown.since ? fmtHM(state.shutdown.since) : "start";
    if (state.shutdown.kind === "graceful") banners.push({ cls: "info", text: `Recording stopped at ${when}: shutting down` });
    else if (state.shutdown.kind === "aborted") banners.push({ cls: "info", text: `Run aborted at ${when}: everything left in the temp folder` });
    else banners.push({ cls: "alarm", text: `CAMERA PROGRAM STOPPED WITHOUT SHUTTING DOWN: no still from any camera since ${when}` });
  }
  if (state.tempSeen && !state.config) {
    banners.push({ cls: "alarm", text: `No camera config found in the temp folder (${METADATA_DIR}/)` });
  } else if (state.configCandidates.length > 1) {
    banners.push({ cls: "warn", text: `Several config files in ${METADATA_DIR}/; showing the newest (${state.configName}). Others: ${state.configCandidates.slice(1).join(", ")}` });
  }
  const box = el("banners");
  const sig = JSON.stringify(banners);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.textContent = "";
  for (const b of banners) {
    const node = document.createElement("div");
    node.className = `banner ${b.cls}`;
    node.textContent = b.text;
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
  const lines = [`The temp folder is gone (noticed ${fmtDate(state.tempGone)} ${fmtHM(state.tempGone)}).`];
  if (state.runStarted) lines.push(`Run started ${fmtDate(state.runStarted)} ${fmtHM(state.runStarted)}, ran for ${fmtDHM((state.tempGone - state.runStarted) / 1000)}.`);
  if (state.config && state.runId) lines.push(`Run metadata moved to ${shortPath(state.config.save_path)}/${METADATA_DIR}/${state.runId}`);
  lines.push("This page reloads by itself when a new run starts in the same temp folder.");
  el("ended-text").textContent = lines.join("\n");
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
  setInterval(tick, previewInterval() * 1000);
  // the config is re-read occasionally in case the viewer was opened before the run started
  setInterval(async () => {
    if (state.config || state.tempGone) return;
    try { await loadConfig(); } catch (e) { return; }
    if (state.config) buildTiles();
  }, 5000);
}

main();
