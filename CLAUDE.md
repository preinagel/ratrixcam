# ratrixcam GUI worktree

You are building the passive viewer for ratrixcam, the lab's 24/7 multi-camera recording
program. The viewer reads what the running cameras write into the temp folder and shows it.
It never controls cameras and never writes into the temp folder.

## Scope
- All your files live under `gui/`. Everything else in this repository is the camera and
  post-processing code; read it when useful, never edit it.
- GitHub issues #36, #37, #38, #39 are earlier ideas, not decisions. Take them or leave them.
- Plain HTML, CSS and JavaScript, no frameworks, no installs; the lab machines are offline.
  If something seems to need a tool, stop and ask.

## Data you read
`gui/sample_data/temp/` is a real temp folder from a short camera run, with the layout the
viewer will see live:
- `status/cam_<camera name>_status.png`: the latest still per camera, rewritten every
  preview interval; a still may be half-written when you read it, so swap a new image in
  only after it loads.
- `ratrixcam_run_metadata/<config file>.json`: the run's camera config (study label,
  subject prefix, per-camera subjID, view, position A-H, clip length `time_slice` in
  seconds, `temp_path`, `save_path`). Note that positions are defined as:
     A B   E F
     C D   G H 
- `ratrixcam_run_metadata/manifests/<camera name>_manifest.csv`: one row per closed clip. Note that  in the runtime environment the manifest's lines will end with CRLF but repo converts to LF so code must tolerate both.
- `terminal_<run id>.log`: the run's terminal output.
- `<study>_<camera name>/*.mp4`: clips not yet transferred (normally only the currently open stream)
A camera name is `<subject_prefix><subjID>_<view>`. A status file with run accounting data
does not exist yet: design the display for it, show placeholders, and keep a list in
`gui/STATUS_FILE_WISHLIST.md` of every field you need python to write.

## Functional Specification
Currently the camera code is displaying stills, accounting metadata and warnings in a tkinter window; that functionality will be replaced by this GUI. Python code will retain the active components (start and stop controls).
Success definition GUI should
- Display the camera stills, updated as often as cameras are saving them (1/sec?)
- Diplay camera-offline image if stills cease to update, per camera
- Show titles above camera stills with subjID and view
- Display runtime accounting (all of this is already computed in the camera code, and can be written to the status file if you request it)
  - Date and time run started
  - duration of run so far (days, hours, min)
  - Clip length in minutes
  - save_path space still available (in bytes)
  - save_path recording time left before full (days,hours,min)
- Display salient warnings when save drive is getting full
  read the camera code for warning thresholds and messages it is currently using

Good to know: the system normally operates unattended; operator checks the display only when they want to. Runs are normally ~1 week long; Sound alerts are prohibited (would interfere with experiment).

## Working rules
- Stop and ask at scope boundaries: Anything outside gui/, anything that seems to need an install or a tool, and any question about what the viewer should show that the spec does not answer.
- Commit on this branch as you go with one-line messages. Small frequent commits. 
- Never push; never touch other branches or worktrees.
- Do not write user documentation; a short `gui/README.md` on how to open the viewer is the
  one exception.
- A browser can test the page from `file://` for images; reading text files needs a local
  server. `python3 -m http.server` from `gui/` is available and needs no install.
