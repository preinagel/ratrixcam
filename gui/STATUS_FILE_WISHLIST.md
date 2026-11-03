# Status file: what the viewer needs python to write

File: `<temp_path>/ratrixcam_status.json`, one per run, rewritten every `preview_interval`
seconds (same cadence as the stills). Write to a temporary name in the same folder and rename
over the old file, so the viewer never reads a half-written file. Timestamps are local time
in the same form the manifests use (`YYYY-MM-DD HH:MM:SS.mmm`); an ISO `T` separator is also
accepted. Unknown extra fields are ignored, so python may add more later.

```json
{
  "schema": 1,
  "written": "2026-11-02 09:44:12.345",
  "run": {
    "run_id": "ratrixcam_20261102_094137_7e5a797f",
    "started": "2026-11-02 09:41:37.000",
    "station": "Mac 4"
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

| field | used for | notes |
|---|---|---|
| `run.started` | "Started", "Running for" | until present, the viewer takes it from the terminal log's filename |
| `run.run_id` | "run ended" screen (where the metadata went) | until present, from the terminal log's filename |
| `run.station` | top-right strip, before the date | a human-friendly machine name ("Mac 4", "Stations 9-10"); omit if none exists |
| `save_drive.found` | NOT FOUND banner | false when `shutil.disk_usage(save_path)` raises |
| `save_drive.free_bytes` | "Save drive free"; 0 shown in red | |
| `save_drive.total_bytes`, `used_bytes` | used bar | |
| `save_drive.seconds_until_full` | "Recording time left" and the warning level | python's `seconds_until_full(free)`; the viewer does not know the assumed write rate |
| `temp_drive.free_bytes` | "Temp drive free" | the drive holding `temp_path` |
| `temp_drive.seconds_until_full` | "SAVE DRIVE FULL. Temp drive cache capacity: ..." | same write-rate estimate applied to the temp drive's free space |
| `written` | out-of-space detection | a file older than 60 s whose last contents showed the temp drive nearly full triggers the red NO SPACE banner |

## Warning levels (decided in the viewer, from `seconds_until_full`)

| condition | banner |
|---|---|
| save drive not found | red: SAVE DRIVE NOT FOUND |
| save full (free 0, or less than one clip left) and temp full | red: NO SPACE ON ANY DRIVE: cameras are not being recorded |
| save full, status file stale, last temp figure under 1 hour | red: NO SPACE ON ANY DRIVE (status last written HH:MM ...) |
| save full | red: SAVE DRIVE FULL. Temp drive cache capacity: d h min |
| less than 1 hour left | red: SAVE DRIVE NEARLY FULL: d h min of recording left |
| less than 24 hours left | orange: SAVE DRIVE ALMOST FULL: d h min of recording left |

These thresholds replace the ones in `drive_warning()` in ratrixcam_IO.py (one clip, one hour),
which the viewer no longer reads; python only needs to supply the numbers.

Not needed from python: clip length (from the config), still freshness (from the stills' mtimes),
clean stop vs crash (from the terminal log's tail), run ended (the temp folder disappears).
