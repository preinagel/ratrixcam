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
  "update_interval": 1,
  "run": {
    "run_id": "ratrixcam_20261102_094137_7e5a797f",
    "started": "2026-11-02 09:41:37.000",
    "state": "running",
    "stopped": null,
    "config_file": "camera_config261019_desktop_test.json",
    "code_version": "209e5cd"
  },
  "save_drive": {
    "path": "/Volumes/SSD/save/",
    "found": true,
    "total_bytes": 2000000000000,
    "used_bytes": 1500000000000,
    "free_bytes": 500000000000,
    "seconds_until_full": 30000,
    "warning": { "level": "none", "text": "" }
  },
  "cameras": {
    "rat999_face": { "online": true, "offline_since": null }
  }
}
```

## Fields, in order of need

| field | used for | notes |
|---|---|---|
| `written` | staleness check | viewer warns when older than 5 update intervals |
| `update_interval` | staleness check | seconds; defaults to the config's `preview_interval` |
| `run.started` | "Started" line, "Running for" | until present, the viewer derives it from the terminal log's filename |
| `run.run_id` | header | until present, taken from the terminal log's filename |
| `run.state` | banner when not `running` | `running`, `stopping`, `stopped`, `aborted` |
| `run.stopped` | banner | time the run stopped, null while running |
| `save_drive.found` | NOT FOUND handling | false when `shutil.disk_usage(save_path)` raises |
| `save_drive.free_bytes` | "Save drive free" | spec asks for bytes; the viewer also shows a rounded unit |
| `save_drive.total_bytes`, `used_bytes` | used bar | |
| `save_drive.seconds_until_full` | "Recording time left" | python's `seconds_until_full(free)`; the viewer does not know the assumed write rate |
| `save_drive.warning.level` | warning banner colour | `none`, `almost_full` (orange), `full` (red), `not_found` (red): the same thresholds as `drive_warning()` in ratrixcam_IO.py |
| `save_drive.warning.text` | warning banner text | the exact text python would show, e.g. "SSD ALMOST FULL\n~45 min of recording left"; newlines kept |
| `cameras.<name>.online` | optional | the viewer already detects a stale still by its mtime; python's own view would let the two agree |
| `run.config_file`, `run.code_version` | footer, optional | |

Not needed from python: clip length (from the config), clip counts and last clip time
(from the manifests), still freshness (from the stills' mtimes).
