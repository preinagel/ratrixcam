# GUI developer notes (state and plan)

Updated 2026-11-02 (end of day). Working notes for the viewer session; not user documentation.

## RESUME HERE

- Iteration 3 committed 2026-11-03 (see below); the user's Safari test of it and a test
  with a LIVE camera run on this machine are next.
- Simulator for testing without cameras:
  `cd /Users/rat_hub_01/Desktop/ratrixcam-gui/gui && python3 simulate.py "sample_data/temp from 7cam run"`
  then http://127.0.0.1:8000/ ; `--scenario normal|fyi|nearly_full|save_full|no_space|crash|shutdown`.
  (The old `?status=` URL override still works with `serve.py` but the simulator is simpler.)
- Session rules confirmed by the user: write and edit under gui/ without asking, commit as
  you go (one subject line + the Co-Authored-By line); never request anything outside the
  sandbox, report sandbox failures instead; use the Edit tool for file changes, not shell
  substitutions; test servers only as a child of a python script that stops them (a server
  left running cannot be killed from the sandbox; the user has to).
- Open for the user: wishlist item C (persistent final status file after cleanup) is TBD;
  the wishlist itself still has to be sent to the camera developer by the user.

## Iteration 3 (2026-11-03, after the user's second Safari feedback)

- Machine name centred in the top strip (grid 1fr auto 1fr), date and time at the right.
- Banners half the height: head line 17 px bold (caps for real warnings), body 13 px.
- Crash/stop banner replaces the drive warnings instead of stacking with them.
- Orange banner is an FYI: "Save drive has space for another d h min"; the time-left cell
  in the accounting table is orange under 24 h too.
- No dead space: the grid sits right under the strip/banners; the accounting table takes
  the remaining height in its own frame and its font grows to use it (11..26 px).
- Tile gaps and the title font scale with the tile width (2.5 %, 9 %, 5 % of it).
- Run-ended screen is just "Recording run ended" + "Last run started ... and ran for ...".
- Second sample folder `sample_data/temp from 7cam run` (7 cameras, positions A-F,H), with
  `sample_data/.gitignore` keeping the stills and dropping the clips (the repository-wide
  rules ignore folders named temp/status). `simulate.py` copies a sample to a scratch
  folder, touches the stills every second, writes a status file, and runs serve.py on it.

## What exists

- `gui/serve.py`: stdlib-only read-only server. `python3 gui/serve.py <temp folder> [--port N]`
  serves the viewer folder at `/` and the temp folder at `/temp/`. A folder under `/temp/`
  returns a JSON listing (name, is_dir, size, mtime). `..` is stripped, so neither root can
  be escaped. `Cache-Control: no-store` on everything. Tested on the sample folder: listings,
  PNG, manifest CSV, and traversal attempts (404) all behave. Not yet committed (see below).
- `gui/offline_status.png`: copy of `blanks/offline_status.png` (640x480), shown for a camera
  whose still has stopped updating.
- `gui/sample_data/temp/`: the sample temp folder, committed earlier by the user.

## Iteration 2 (2026-11-02, after the user's first Safari feedback)

Feedback applied:
- Header gone. Thin top strip: layout toggle at the left, gray date and time (no seconds) at
  the right. Study label and run id no longer shown.
- Accounting moved below the cameras into a 3-column table of single strings ("Started
  Mon, Nov 2, 2026 09:41"), font scales with the window (`clamp(11px, 1.2vw, 18px)`), cells
  clip with an ellipsis so nothing overlaps. Rows: started / running for / clip length;
  save drive free / recording time left / used bar; temp folder / save drive / temp drive free.
- Missing data is blank, never a dash. A real zero (free bytes, time left) is red.
- Bytes shown with at most three digits (455 GB, 2.00 TB).
- Paths shortened: home prefix dropped ("Desktop/.../temp"), more than three parts cut to
  "…/last/two".
- Removed: "N cameras offline" banner, status-file-missing note, stale-status-file banner,
  footer, per-camera clip counts, "(from log name)" tag, red title on offline tiles.
- Tile title: subject and view in white, "still HH:MM:SS" in gray, position letter at the right.
  Offline tile shows offline_status.png (the same image python copies into the stills folder).
- Drive warnings now decided in the viewer from `seconds_until_full`: orange under 24 h,
  red under 1 h, red "SAVE DRIVE FULL. Temp drive cache capacity: ..." when the save drive
  cannot take another clip, red "NO SPACE ON ANY DRIVE: cameras are not being recorded" when
  the temp drive is full too. Wishlist updated: python supplies numbers for both drives, no
  warning text. (Reverses decision 4 below.)
- Crash vs clean stop: while every camera is offline, the log tail is read every 10 s.
  "attempting graceful shutdown" -> blue "Recording stopped at HH:MM: shutting down";
  "Run aborted" -> blue "Run aborted ..."; neither -> red "CAMERA PROGRAM STOPPED WITHOUT
  SHUTTING DOWN: no still from any camera since HH:MM". With the static sample folder this
  red banner appears after 10 s (expected: the folder was copied mid-run).
- Run ended: when the temp folder listing stops answering after having answered, the page
  shows "Recording run ended" with the run's start, duration and where the metadata moved
  (`<save>/ratrixcam_run_metadata/<run id>`). It reloads itself when the folder reappears.
  Reading the moved metadata on the save drive is NOT done (the server only exposes temp).

Test URLs (server on sample_data/temp):
- `/` placeholders; `/?status=/sample_data/ratrixcam_status_almost_full.json` orange;
  `.../ratrixcam_status_nearly_full.json` red under an hour; `.../ratrixcam_status_save_full.json`
  save full with temp cache capacity.

Answered 2026-11-02:
1. Stills may keep updating, perhaps intermittently, when the disks are full (camera-side
   issue, not fixed yet). Viewer: the NO SPACE banner is driven by the status file alone;
   fresh stills are shown while fresh, the offline image when stale. Added: a status file
   older than 60 s whose last contents showed save full and the temp drive under an hour
   also triggers the red NO SPACE banner.
2. Station name requested (`run.station`); shown in the top strip before the date when present.
3. The viewer's server cannot see the save drive. Proposal pending (see below).

Decided 2026-11-02 (second round):
- NO SPACE banner only when the cameras are really dropping frames: status file older than
  60 s AND its last contents showed the save drive full AND temp free under 1 MB (my choice
  for "no room for the status file itself"). No live-status NO SPACE rule any more; while
  the file is fresh the SAVE DRIVE FULL banner shows the temp cache capacity counting down.
- Wishlist: all fields mandatory with defaults (my call); `run.machine_name` (default
  hostname) replaces station; note on writing the file late in the loop so it keeps
  updating while the temp drive has room.
- After cleanup, A + B built, C left TBD in the wishlist:
  A. once a shutdown is seen, the log tail is read every second; the ended screen lists the
     outcome lines (metadata moved to ..., WARNING ..., Temp folder removed ...).
  B. serve.py remembers the last valid status file and last log it served, at /last/status.json
     and /last/log.txt; a page opened after the temp folder is gone loads them and shows the
     ended screen. Lost on server restart.
- With no temp folder and nothing remembered, the page shows a blue "Temp folder not found:
  no run in progress?" banner.

## Decisions taken in iteration 1 (open to iteration)

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

1. Safari test of iteration 2 by the user; fix what it shows.
2. Answers to the open questions above.

## Working notes

- No node or browser inside the sandbox: JavaScript is self-reviewed only; the user tests
  in Safari. Test servers are started as a child of a short python script that stops them
  (a background server cannot be killed from the sandbox).
- Memory directory is write-denied in this session, so no memories are saved from here.
- Writes under gui/ are auto-allowed by the user's settings; commit as you go.
