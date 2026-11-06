# GUI developer notes

Current state of the viewer and the config editor; read first on every start.
Updated 2026-11-04 after the external code review.

## What exists

Two independent pages, each with its own stdlib-only server, both under `gui/`:

- **Viewer** (`index.html`, `viewer.js`, `style.css`; server `serve.py`): shows the camera
  stills and the run accounting from a temp folder. Read-only. `serve.py <temp folder>`
  serves the page at `/`, the temp folder at `/temp/` (JSON folder listings), and remembers
  the last status file at `/last/status.json` for the run-ended screen after cleanup.
- **Config editor** (`config_editor.html`, `config_editor.js`, `editor.css`; server
  `config_editor.py`): a form of drop-down menus that writes a camera config file. Legal
  values live in `local_options.json` next to the script (gitignored, per machine, bound to
  the machine's hardware address; `--options FILE` overrides it, the tests use that).
- Shared look: `theme.css` (tokens and base rules); `offline_status.png` (copy of
  `blanks/offline_status.png`).
- `STATUS_FILE_WISHLIST.md`: what python must write to `<temp>/ratrixcam_status.json`,
  including the shutdown milestones, plus the one new config field (`machine_name`).
- `simulate.py`: pretends a run from a sample folder (stills touched every second, status
  file written as the wishlist asks) and serves the viewer on it; `--scenario` picks the
  drive situation or a shutdown.
- `sample_data/temp` (4 cameras) and `sample_data/temp from 7cam run` (7 cameras): real
  temp folders, clips ignored via `sample_data/.gitignore`.
- Tests: `tests/test_gui_serve.py` (16) and `tests/test_gui_editor.py` (14), collected by
  `python3 -m pytest -q` from the repository root with the rest of the suite.

## How to run

```
cd /Users/rat_hub_01/Desktop/ratrixcam-gui/gui
python3 serve.py ~/Desktop/temp                              # viewer on a live run, port 8000
python3 simulate.py "sample_data/temp from 7cam run"         # viewer on a simulated run
python3 simulate.py sample_data/temp --scenario save_full    # normal|fyi|nearly_full|save_full|no_space|crash|shutdown
python3 config_editor.py                                     # editor, port 8001
```

Pre-push checks: `basedpyright gui/` must report 0 errors (warnings about `Any` from JSON
and argparse remain on purpose); `python3 -m pytest -q` from the repository root must pass.

## Design, as it stands

Viewer:
- Sources: the stills (freshness by mtime from the folder listing; offline after
  max(10 s, 10 x preview_interval)) and the status file. The terminal log is a human record
  and is never read.
- Facts come from status fields: `recording`, `save_drive.accepting_clips`,
  `temp_drive.clips_waiting`, `run.state`, `run.message`. The viewer's own decisions are
  the soft thresholds only: orange under 24 h and red under 1 h on `seconds_until_full`,
  and "no still from any camera" when every still is stale and python still says running.
- Layout: strict (all eight positions A B | E F over C D | G H) or compact (empty rows and
  columns dropped), remembered in localStorage. Tiles fill the width or the height; gaps
  and title font scale with the tile. Accounting table below at natural height, two
  columns, font from the column width. Banners under the top strip, head plus one line.
- Run ended: `run.state` "ended" or the temp folder gone; the page reloads itself when a
  new run appears in the same folder.

Config editor:
- Every field is a menu of legal values plus "Add new…" (checked: name fields
  `[A-Za-z0-9-]`, 12 chars for study label and subject prefix, 24 for subject and view, no
  `_`; numbers within ranges; absolute paths ending in `/`; resolution `WxH`). The
  "Edit legal values" panel removes values not in use, or resets to the shipped lists.
- Sections: Run (machine name, study label, subject prefix, clip length 10..3600 s, save
  and temp folders), Cameras (subject, view, position, optional crf/fps/resolution/LUT
  overrides from their own lists, "(default)" preselected), Advanced Settings (defaults and
  the still interval, preset from the options file).
- The file is written by the server: keys run-level first, cameras one per line, advanced
  defaults last; `~` expanded; validated with the camera code's Config class;
  refused when the name does not end in .json, the folder does not exist, or the location
  is inside the temp or save folder. Save and temp paths are not checked for existence: a
  config may be written for another machine.

## Open items

- Camera developer: implement the status file and the config field per the wishlist.
- Hardware tests (user): an unsupported resolution and an unsupported LUT code, to learn
  the failure modes before deciding whether the editor should constrain those lists.
- Linking the two pages (menu, tabs, or nothing) is undecided.
- A live camera run on this machine with the viewer attached has not been done yet.
- `local_options.json` on this machine carries test values ("tail" view, old time_slice
  3600); delete it for a clean start.

## Rules for this worktree

- Everything under `gui/`; nothing outside it is touched (the root CLAUDE.md and
  .claude/settings.json are sandbox files, dropped at merge).
- Write and edit under `gui/` without asking; commit as you go with a short subject line
  (an index entry, well under 72 characters) plus the Co-Authored-By line. Never push.
- Never request anything outside the sandbox; report a sandbox failure instead. Test
  servers only as a child of a python script that stops them.
- Use the Edit tool for file changes, not shell substitutions.
- Test files are `gui/tests/test_gui_*.py`; a basename must never duplicate one in
  `tests/` (pytest imports test modules by bare name).
- No explanations of settings on the editor's interface; the documentation has them.
