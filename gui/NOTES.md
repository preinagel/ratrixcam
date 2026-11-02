# GUI developer notes (state and plan)

Updated 2026-11-02. Working notes for the viewer session; not user documentation.

## What exists

- `gui/serve.py`: stdlib-only read-only server. `python3 gui/serve.py <temp folder> [--port N]`
  serves the viewer folder at `/` and the temp folder at `/temp/`. A folder under `/temp/`
  returns a JSON listing (name, is_dir, size, mtime). `..` is stripped, so neither root can
  be escaped. `Cache-Control: no-store` on everything. Tested on the sample folder: listings,
  PNG, manifest CSV, and traversal attempts (404) all behave. Not yet committed (see below).
- `gui/offline_status.png`: copy of `blanks/offline_status.png` (640x480), shown for a camera
  whose still has stopped updating.
- `gui/sample_data/temp/`: the sample temp folder, committed earlier by the user.

## Decisions taken (report back to the user, open to iteration)

1. Launch: the start command takes the temp folder path as a mandatory argument, mirroring
   how the camera program takes its config path. The viewer never sees `temp_path` from the
   config before it has the folder, so the argument is the only way in.
2. Config discovery: list `ratrixcam_run_metadata/`, fetch every `*.json`, keep those that
   look like a camera config (cameras list with subjID/view/position, study_label,
   subject_prefix, time_slice). One match: use it. None: banner "no config found". Several:
   use the most recently modified and show a banner naming all of them.
3. Camera-offline rule: a camera is offline when its still's mtime (from the folder listing)
   is older than max(10 s, 10 x preview_interval), or the still is missing. The offline image
   plus "no still since HH:MM:SS" replaces the tile. Stills are reloaded only when the mtime
   advances, swapped in only after the browser has decoded them; a decode failure (half-written
   file) is retried on the next poll.
4. Drive warnings: python computes level and text (same thresholds as the tkinter window:
   FULL when expected to fill within one clip, ALMOST FULL within an hour, NOT FOUND when the
   save path cannot be read) and writes them to the status file; the viewer only renders them.
   The viewer does not duplicate the thresholds or the assumed write rate.
5. Status file: one JSON per run, `<temp>/ratrixcam_status.json`, rewritten every preview
   interval; schema in `gui/STATUS_FILE_WISHLIST.md` (to be written). Until it exists the
   accounting fields show placeholders and a muted note "status file not found".
   Fallback while no status file: run start derived from the terminal log's filename
   (`terminal_ratrixcam_YYYYMMDD_HHMMSS_*.log`) and duration from that; marked as derived.
6. Layout: strict mode keeps all eight positions (A B | E F over C D | G H, gap between the
   blocks); compact mode drops rows and columns with no camera while preserving relative
   order. Toggle button in the header; choice remembered in the browser's localStorage.
7. Per-camera line under each still from its manifest: number of closed clips and the time
   the last row was written. Polled every 15 s. Manifests parsed tolerating CRLF and LF.
8. Not displayed: terminal log, notes.txt (per the user).

- `gui/index.html`, `gui/style.css`, `gui/viewer.js`: the page. Header with accounting,
  banner strip for warnings, camera grid, footer with paths. Written, served, NOT yet seen in
  a browser (no node or browser inside the sandbox; the user tests in Safari).
- `gui/STATUS_FILE_WISHLIST.md`: schema of `<temp>/ratrixcam_status.json`.
- `gui/README.md`: how to start.
- `gui/sample_data/ratrixcam_status_almost_full.json`: fake status file; open the page with
  `?status=/sample_data/ratrixcam_status_almost_full.json` to see the accounting and the
  orange warning. (The sample temp folder has no status file, so without it placeholders show.)

Testing note: the sample stills are static, so every camera shows OFFLINE within 10 s of
opening. `touch sample_data/temp/status/*.png` in another terminal brings them back online
(mtimes are not tracked by git).

## Next

1. First Safari test by the user; fix what it shows.
2. Review sizing on the lab display (tkinter window was 1424x1200).
3. Possibly: a status-file field for per-camera online state, once python writes it.

## Needed from the user

1. Open the page in Safari and report what is wrong (I cannot open a browser from the sandbox).
2. Memory directory is write-denied in this session, so no memories are saved from here.
