# Status file: what the viewer needs python to write

File: `<temp_path>/ratrixcam_status.json`, one per run, rewritten every `preview_interval`
seconds (same cadence as the stills). Write to a temporary name in the same folder and rename
over the old file, so the viewer never reads a half-written file. Timestamps are local time
in the same form the manifests use (`YYYY-MM-DD HH:MM:SS.mmm`); an ISO `T` separator is also
accepted.

Every field below is always present. Where python has no value it writes the default given
in the table; it never omits a field. Extra fields may be added later and are ignored.

Where in the loop the file is written matters: it must keep being rewritten for as long as
the temp drive has room for it, even after the cameras no longer have room to write frames.
The viewer reads "no space on any drive" from the file going quiet while its last contents
showed both drives full.

```json
{
  "schema": 1,
  "written": "2026-11-02 09:44:12.345",
  "run": {
    "run_id": "ratrixcam_20261102_094137_7e5a797f",
    "started": "2026-11-02 09:41:37.000",
    "machine_name": "Mac 4"
  },
  "save_drive": {
    "path": "/Volumes/data/",
    "found": true,
    "total_bytes": 2000000000000,
    "used_bytes": 1500000000000,
    "free_bytes": 500000000000,
    "seconds_until_full": 30000
  },
  "temp_drive": {
    "path": "/Users/rat_hub_01/Desktop/temp/",
    "found": true,
    "total_bytes": 1000000000000,
    "used_bytes": 400000000000,
    "free_bytes": 600000000000,
    "seconds_until_full": 36000
  }
}
```

## Fields

| field | used for | default when unknown |
|---|---|---|
| `schema` | future format changes | `1` |
| `written` | out-of-space detection: a file older than 60 s whose last contents showed both drives full triggers the red NO SPACE banner | always known |
| `run.run_id` | "run ended" screen: where the metadata went | always known |
| `run.started` | "Started", "Running for" | always known |
| `run.machine_name` | top-right strip, before the date: a user-friendly name of the camera computer ("Mac 4", "Stations 9-10") | the hostname |
| `save_drive.path` | shown with the accounting | `save_path` from the config |
| `save_drive.found` | NOT FOUND banner | `false` when `shutil.disk_usage(save_path)` raises; the byte fields are then `null` |
| `save_drive.total_bytes`, `used_bytes`, `free_bytes` | used bar, "Save drive free" (0 shown in red) | `null` |
| `save_drive.seconds_until_full` | "Recording time left" and the warning level | python's `seconds_until_full(free)`; the viewer does not know the assumed write rate; `null` when not found |
| `temp_drive.*` | same fields for the drive holding `temp_path`; "Temp drive free", temp cache capacity, NO SPACE detection | same defaults |

## Warning levels (decided in the viewer)

| condition | banner |
|---|---|
| save drive not found | red: SAVE DRIVE NOT FOUND |
| save full (free 0, or less than one clip left) | red: SAVE DRIVE FULL. Temp drive cache capacity: d h min |
| save full, status file older than 60 s, its last temp free under 1 MB | red: NO SPACE ON ANY DRIVE: cameras are not being recorded |
| less than 1 hour of recording left | red: SAVE DRIVE NEARLY FULL: d h min of recording left |
| less than 24 hours left | orange: SAVE DRIVE ALMOST FULL: d h min of recording left |

These thresholds replace the ones in `drive_warning()` in ratrixcam_IO.py (one clip, one hour),
which the viewer no longer reads; python only needs to supply the numbers.

Not needed from python: clip length (from the config), still freshness (from the stills' mtimes),
clean stop vs crash (from the terminal log's tail), run ended (the temp folder disappears; the
viewer's server remembers the last status file and log it served).

Open (TBD): whether python should also write a final status file to a persistent place after
the temp folder is removed, so the viewer can describe the last run even after a restart.
