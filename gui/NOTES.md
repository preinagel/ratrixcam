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

## Next

1. `gui/index.html`, `gui/style.css`, `gui/viewer.js`: the page described above.
2. `gui/STATUS_FILE_WISHLIST.md`: the status-file schema the viewer expects.
3. `gui/README.md`: how to start the server and open the page in Safari.
4. A sample status file under `gui/sample_data/` (outside the temp folder) and a `?status=`
   URL override so the accounting and warning rendering can be tested before python writes it.
5. Test in Safari against the sample folder; then commit in small steps.

## Needed from the user

1. Git cannot run inside the sandbox: the worktree's metadata lives under
   `/Users/rat_hub_01/Desktop/ratrixcam/.git/worktrees/ratrixcam-gui`, and the sandbox denies
   reading `/Users/rat_hub_01/Desktop/ratrixcam`. Every `git` command exits with
   "not a git repository". Until the read deny is adjusted no commits are possible; I keep
   working and will commit everything in small steps once git works.
2. Memory directory is write-denied in this session, so no memories are saved from here.
3. The viewer cannot be opened by me (no `open` in the sandbox); after each step I will give
   the exact command and URL to open, and verify what I can with curl.
