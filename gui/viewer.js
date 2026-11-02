"use strict";

// ratrixcam passive viewer. Reads the temp folder that serve.py exposes at /temp/ and shows
// the camera stills and the run accounting. Never writes anything.

const TEMP = "/temp/";
const METADATA_DIR = "ratrixcam_run_metadata";
const MANIFESTS_DIR = "manifests";
const STILLS_DIR = "status";
const STATUS_FILENAME = "ratrixcam_status.json"; // written by python (see STATUS_FILE_WISHLIST.md)

// camera positions on the display: A B | E F over C D | G H; grid column 2 is the gap
const POSITIONS = { A: [0, 0], B: [0, 1], E: [0, 3], F: [0, 4], C: [1, 0], D: [1, 1], G: [1, 3], H: [1, 4] };
const GAP_COL = 2;

const MANIFEST_POLL_S = 15;
const STATUS_STALE_FACTOR = 5; // status file older than this many update intervals is reported stale

const state = {
  config: null,
  configName: null,
  configCandidates: [],
  cameras: [], // {name, subjID, view, position, tile, stillMtime, offlineSince, clips, lastClip}
  status: null, // last good status file contents
  statusError: null, // "missing" | "unreadable" | null
  runStarted: null, // Date
  runStartedSource: null, // "status file" | "log filename"
  runId: null,
  layout: "strict",
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
  const data = await fetchJSON(TEMP + rel + (rel.endsWith("/") || rel === "" ? "" : "/"));
  return data.entries || [];
}

// ---------- formatting ----------

const pad2 = (n) => String(n).padStart(2, "0");

function fmtDateTime(d) {
  const days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  const months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  return `${days[d.getDay()]}, ${months[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}, ${fmtTime(d)}`;
}

function fmtTime(d) {
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

function fmtDHM(seconds) {
  if (seconds == null || !isFinite(seconds) || seconds < 0) return null;
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  return `${days} d ${hours} h ${mins} min`;
}

function fmtBytes(bytes) {
  if (bytes == null || !isFinite(bytes)) return null;
  const units = ["bytes", "kB", "MB", "GB", "TB"];
  let v = bytes, i = 0;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  const approx = i === 0 ? `${bytes} bytes` : `${v.toFixed(v < 10 ? 2 : 1)} ${units[i]}`;
  return i === 0 ? approx : `${approx} (${bytes.toLocaleString()} bytes)`;
}

// "2026-11-02 09:41:39.803" or ISO "2026-11-02T09:41:39" (local time) -> Date
function parseLocalTime(text) {
  if (!text) return null;
  const m = String(text).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?/);
  if (!m) return null;
  return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6], m[7] ? +m[7].slice(0, 3).padEnd(3, "0") : 0);
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
  if (candidates.length === 0) { state.config = null; state.configName = null; return; }
  state.config = candidates[0].config;
  state.configName = candidates[0].name;
}

function cameraName(cfg, cam) {
  return `${cfg.subject_prefix}${cam.subjID}_${cam.view}`;
}

// ---------- run start fallback: from the terminal log's filename ----------

async function findRunIdFromLog() {
  const entries = await listDir("");
  for (const e of entries) {
    const m = e.name.match(/^terminal_(ratrixcam_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_[0-9a-f]+)\.log$/);
    if (m) return { runId: m[1], started: new Date(+m[2], +m[3] - 1, +m[4], +m[5], +m[6], +m[7]) };
  }
  return null;
}

// ---------- camera grid ----------

function buildTiles() {
  const grid = document.getElementById("grid");
  grid.textContent = "";
  state.cameras = [];
  if (!state.config) return;
  for (const cam of state.config.cameras) {
    const tile = document.createElement("div");
    tile.className = "tile waiting";
    tile.innerHTML = `
      <div class="title"><span class="subj"></span><span class="view"></span><span class="pos"></span></div>
      <div class="still"><img alt=""><div class="overlay"></div></div>
      <div class="clips">&mdash;</div>`;
    tile.querySelector(".subj").textContent = `${state.config.subject_prefix}${cam.subjID}`;
    tile.querySelector(".view").textContent = cam.view;
    tile.querySelector(".pos").textContent = cam.position;
    tile.querySelector("img").src = "offline_status.png";
    tile.querySelector(".overlay").textContent = "waiting for first still";
    grid.appendChild(tile);
    state.cameras.push({
      name: cameraName(state.config, cam), subjID: cam.subjID, view: cam.view, position: cam.position,
      tile, stillMtime: null, offlineSince: null, clips: null, lastClip: null,
    });
  }
  applyLayout();
}

// strict: all eight positions keep their place; compact: rows and columns without a camera
// are dropped, relative order preserved, the block gap kept only when both blocks are used
function applyLayout() {
  const grid = document.getElementById("grid");
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
    if (!p) { cam.tile.style.display = "none"; continue; }
    const r = rows.indexOf(p[0]), c = cols.indexOf(p[1]);
    cam.tile.style.display = r < 0 || c < 0 ? "none" : "";
    cam.tile.style.gridRow = String(r + 1);
    cam.tile.style.gridColumn = String(c + 1);
  }
  grid.dataset.rows = rows.length;
  grid.dataset.cols = cols.filter((c) => c !== GAP_COL).length;
  grid.dataset.gap = cols.includes(GAP_COL) ? "1" : "0";
  fitTiles();
}

// choose --tile-w so the whole grid fits the space under the header
function fitTiles() {
  const wrap = document.getElementById("grid-wrap"), grid = document.getElementById("grid");
  const nRows = +grid.dataset.rows || 0, nCols = +grid.dataset.cols || 0;
  if (!nRows || !nCols) return;
  const css = getComputedStyle(document.documentElement);
  const tileGap = parseFloat(css.getPropertyValue("--tile-gap")) || 12;
  const blockGap = grid.dataset.gap === "1" ? (parseFloat(css.getPropertyValue("--block-gap")) || 36) + tileGap : 0;
  const availW = wrap.clientWidth - 24, availH = wrap.clientHeight - 24;
  const chrome = 34 + 22 + 2; // title + clips line + borders, per tile
  const byWidth = (availW - blockGap - tileGap * (nCols - 1)) / nCols;
  const byHeight = ((availH - tileGap * (nRows - 1)) / nRows - chrome) * 4 / 3;
  const w = Math.max(160, Math.floor(Math.min(byWidth, byHeight)));
  document.documentElement.style.setProperty("--tile-w", `${w}px`);
}

function setLayout(mode) {
  state.layout = mode;
  document.getElementById("layout-btn").textContent = `Layout: ${mode}`;
  try { localStorage.setItem("ratrixcam-viewer-layout", mode); } catch (e) { /* private window etc. */ }
  applyLayout();
}

// ---------- stills ----------

function offlineAfterSeconds() {
  const interval = state.config && state.config.preview_interval ? state.config.preview_interval : 1;
  return Math.max(10, 10 * interval);
}

async function pollStills() {
  let entries;
  try { entries = await listDir(STILLS_DIR); } catch (e) { entries = []; }
  const byName = new Map(entries.map((e) => [e.name, e]));
  const now = Date.now() / 1000;
  for (const cam of state.cameras) {
    const entry = byName.get(`cam_${cam.name}_status.png`);
    const age = entry ? now - entry.mtime : Infinity;
    if (entry && entry.mtime !== cam.stillMtime) loadStill(cam, entry.mtime);
    setOffline(cam, age > offlineAfterSeconds() ? (entry ? entry.mtime : null) : null, !entry);
  }
}

// a still may be half-written: decode it off-screen and only then swap it in
function loadStill(cam, mtime) {
  const img = new Image();
  const url = `${TEMP}${STILLS_DIR}/cam_${encodeURIComponent(cam.name)}_status.png?t=${mtime}`;
  img.onload = () => {
    cam.tile.querySelector("img").src = url;
    cam.tile.classList.remove("waiting");
  };
  img.onerror = () => { cam.stillMtime = null; }; // retry on the next poll
  cam.stillMtime = mtime;
  img.src = url;
}

function setOffline(cam, sinceEpoch, missing) {
  const overlay = cam.tile.querySelector(".overlay");
  if (sinceEpoch == null && !missing) {
    if (cam.offlineSince != null) { cam.offlineSince = null; cam.tile.classList.remove("offline"); }
    return;
  }
  const key = sinceEpoch == null ? -1 : sinceEpoch;
  if (cam.offlineSince !== key) {
    cam.offlineSince = key;
    cam.tile.classList.add("offline");
    cam.tile.classList.remove("waiting");
    cam.tile.querySelector("img").src = "offline_status.png";
    overlay.textContent = missing
      ? "OFFLINE: no still image"
      : `OFFLINE: no still since ${fmtTime(new Date(sinceEpoch * 1000))}`;
  }
}

// ---------- manifests ----------

async function pollManifests() {
  for (const cam of state.cameras) {
    let text;
    try {
      text = await fetchText(`${TEMP}${METADATA_DIR}/${MANIFESTS_DIR}/${encodeURIComponent(cam.name)}_manifest.csv`);
    } catch (e) {
      cam.tile.querySelector(".clips").textContent = "no manifest yet";
      continue;
    }
    const lines = text.split(/\r?\n/).filter((l) => l.trim() !== "");
    if (lines.length === 0) { cam.tile.querySelector(".clips").textContent = "manifest empty"; continue; }
    const header = lines[0].split(",");
    const col = header.indexOf("row_written");
    const rows = lines.slice(1);
    const last = rows.length && col >= 0 ? rows[rows.length - 1].split(",")[col] : null;
    const lastDate = parseLocalTime(last);
    cam.tile.querySelector(".clips").textContent =
      `${rows.length} clip${rows.length === 1 ? "" : "s"} closed` + (lastDate ? `, last at ${fmtTime(lastDate)}` : "");
  }
}

// ---------- status file (run accounting) ----------

function statusUrl() {
  const override = new URLSearchParams(location.search).get("status"); // testing aid
  return override || TEMP + STATUS_FILENAME;
}

async function pollStatus() {
  let r;
  try { r = await fetch(statusUrl(), { cache: "no-store" }); } catch (e) { state.statusError = "missing"; return; }
  if (!r.ok) { state.statusError = "missing"; return; }
  try {
    state.status = await r.json();
    state.statusError = null;
  } catch (e) {
    state.statusError = "unreadable"; // half-written: keep the previous contents
  }
  const run = state.status && state.status.run;
  if (run && run.started) {
    const d = parseLocalTime(run.started);
    if (d) { state.runStarted = d; state.runStartedSource = "status file"; }
    if (run.run_id) state.runId = run.run_id;
  }
}

function el(id) { return document.getElementById(id); }

function setValue(id, text) {
  const node = el(id);
  node.textContent = text == null ? "—" : text;
  node.classList.toggle("placeholder", text == null);
}

function renderAccounting() {
  const cfg = state.config, st = state.status;
  const now = new Date();
  el("clock").textContent = fmtTime(now);
  el("date").textContent = fmtDateTime(now).replace(/, \d\d:\d\d:\d\d$/, "");
  el("study-label").textContent = cfg ? cfg.study_label : "no config";
  el("run-id").textContent = state.runId || (state.configName ? state.configName : "");

  setValue("started", state.runStarted ? fmtDateTime(state.runStarted) + (state.runStartedSource === "log filename" ? " (from log name)" : "") : null);
  setValue("duration", state.runStarted ? fmtDHM((now - state.runStarted) / 1000) : null);
  if (cfg) {
    const mins = cfg.time_slice / 60;
    setValue("clip-length", `${Number.isInteger(mins) ? mins : mins.toFixed(1)} min`);
  } else setValue("clip-length", null);

  const drive = st && st.save_drive;
  setValue("free", drive && drive.found !== false ? fmtBytes(drive.free_bytes) : null);
  setValue("time-left", drive && drive.found !== false ? fmtDHM(drive.seconds_until_full) : null);
  const bar = el("used-bar");
  if (drive && drive.total_bytes > 0 && drive.used_bytes != null) {
    const pct = 100 * drive.used_bytes / drive.total_bytes;
    bar.style.width = `${Math.min(100, pct).toFixed(1)}%`;
    const level = drive.warning && drive.warning.level;
    bar.className = "bar-fill" + (level === "full" || level === "not_found" ? " alarm" : level === "almost_full" ? " warn" : "");
    el("used-text").textContent = `${pct.toFixed(1)}% used of ${fmtBytes(drive.total_bytes).split(" (")[0]}`;
  } else {
    bar.style.width = "0";
    el("used-text").textContent = drive && drive.found === false ? "drive not found" : "—";
  }
}

function renderBanners() {
  const banners = [];
  const st = state.status;
  const drive = st && st.save_drive;
  const warning = drive && drive.warning;
  if (warning && warning.level && warning.level !== "none") {
    const cls = warning.level === "almost_full" ? "warn" : "alarm";
    banners.push({ cls, text: warning.text || warning.level });
  }
  if (st && st.run && st.run.state && st.run.state !== "running") {
    banners.push({ cls: "info", text: `Recording ${st.run.state}` + (st.run.stopped ? ` at ${st.run.stopped}` : "") });
  }
  if (!state.config) {
    banners.push({ cls: "alarm", text: `No camera config found in <temp>/${METADATA_DIR}/: nothing to display` });
  } else if (state.configCandidates.length > 1) {
    banners.push({ cls: "warn", text: `Several config files in <temp>/${METADATA_DIR}/; showing the newest (${state.configName}). Others: ${state.configCandidates.slice(1).join(", ")}` });
  }
  const offline = state.cameras.filter((c) => c.offlineSince != null);
  if (offline.length) {
    banners.push({ cls: "alarm", text: `${offline.length} camera${offline.length === 1 ? "" : "s"} offline: ${offline.map((c) => `${c.name} (${c.position})`).join(", ")}` });
  }
  if (state.statusError === "missing") {
    banners.push({ cls: "muted", text: `Status file ${STATUS_FILENAME} not found in the temp folder: run accounting shows placeholders` });
  } else if (st && st.written) {
    const written = parseLocalTime(st.written);
    const interval = st.update_interval || (state.config && state.config.preview_interval) || 1;
    if (written && (Date.now() - written) / 1000 > STATUS_STALE_FACTOR * interval) {
      banners.push({ cls: "warn", text: `Status file not updated since ${fmtTime(written)}: is the camera program still running?` });
    }
  }
  const box = el("banners");
  const html = banners.map((b) => `<div class="banner ${b.cls}"></div>`).join("");
  if (box.dataset.sig !== JSON.stringify(banners)) {
    box.innerHTML = html;
    [...box.children].forEach((node, i) => { node.textContent = banners[i].text; });
    box.dataset.sig = JSON.stringify(banners);
    fitTiles();
  }
}

function renderFooter() {
  const parts = [];
  if (state.configName) parts.push(`config: ${state.configName}`);
  if (state.config && state.config.temp_path) parts.push(`temp: ${state.config.temp_path}`);
  if (state.config && state.config.save_path) parts.push(`save: ${state.config.save_path}`);
  parts.push(`stills every ${state.config && state.config.preview_interval ? state.config.preview_interval : 1} s, offline after ${offlineAfterSeconds()} s without one`);
  el("footer").textContent = parts.join("   |   ");
}

// ---------- main ----------

async function main() {
  try { state.layout = localStorage.getItem("ratrixcam-viewer-layout") || "strict"; } catch (e) { /* ignore */ }
  el("layout-btn").addEventListener("click", () => setLayout(state.layout === "strict" ? "compact" : "strict"));
  el("layout-btn").textContent = `Layout: ${state.layout}`;
  window.addEventListener("resize", fitTiles);

  try { await loadConfig(); } catch (e) { state.config = null; }
  buildTiles();
  renderFooter();

  try {
    const fromLog = await findRunIdFromLog();
    if (fromLog && !state.runStarted) { state.runStarted = fromLog.started; state.runStartedSource = "log filename"; state.runId = fromLog.runId; }
  } catch (e) { /* no log: no fallback */ }

  const interval = state.config && state.config.preview_interval ? state.config.preview_interval : 1;
  await pollStatus();
  await pollStills();
  await pollManifests();
  renderAccounting();
  renderBanners();

  setInterval(async () => { await pollStatus(); await pollStills(); renderAccounting(); renderBanners(); }, interval * 1000);
  setInterval(pollManifests, MANIFEST_POLL_S * 1000);
  // the config is re-read occasionally in case the viewer was opened before the run started
  setInterval(async () => {
    if (state.config) return;
    try { await loadConfig(); } catch (e) { return; }
    if (state.config) { buildTiles(); renderFooter(); pollManifests(); }
  }, 5000);
}

main();
