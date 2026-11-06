# Status file: what the viewer needs python to write

The viewer reads two things from the temp folder: the stills and this file. The terminal
log is a human record and is not a source for the viewer. Every statement of fact the viewer
shows ("save drive full", "clips waiting in temp", "cameras are not being recorded",
"stopping", "aborted", "ended") comes from a field below; the viewer's own decisions are
limited to soft thresholds on python's numbers (see "Viewer-side decisions").

File: `<temp_path>/ratrixcam_status.json`, one per run. Write it through a temporary name in
the same folder and rename over the old file, so the viewer never reads a half-written file.
Timestamps are local time in the manifests' form (`YYYY-MM-DD HH:MM:SS.mmm`); an ISO `T`
separator is also accepted. Every field is always present; where python has no value it
writes the default in the table. Extra fields may be added later and are ignored.

## When it is written

- Every `preview_interval` seconds while the run is in progress, from a point in the loop
  that keeps running after the cameras can no longer write frames, as long as the temp
  drive has room for the file. The viewer reads "cameras not recording" from `recording`,
  so the file must keep coming after the launcher's hard stop.
- Explicitly, independent of the poll loop, at each shutdown milestone: at the stop click
  (`run.state` = `stopping`), when Abort is chosen (`aborted`), and after the collection has
  finished (`ended`, with `run.message`).
- The shutdown sequence pauses for 2 to 3 poll intervals after writing the `ended` file
  before deleting the status file and the temp folder, so the viewer has time to load it.

```json
{
  "schema": 1,
  "written": "2026-11-02 09:44:12.345",
  "recording": true,
  "run": {
    "run_id": "ratrixcam_20261102_094137_7e5a797f",
    "started": "2026-11-02 09:41:37.000",
    "state": "running",
    "message": "",
    "machine_name": "Mac 4"
  },
  "save_drive": {
    "path": "/Volumes/data/",
    "found": true,
    "accepting_clips": true,
    "total_bytes": 2000000000000,
    "used_bytes": 1500000000000,
    "free_bytes": 500000000000,
    "seconds_until_full": 30000
  },
  "temp_drive": {
    "path": "/Users/rat_hub_01/Desktop/temp/",
    "found": true,
    "clips_waiting": 0,
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
| `written` | time of the last write; dates the run-ended screen | always known |
| `recording` | red "CAMERAS ARE NOT BEING RECORDED": `false` once the launcher's hard stop fires because the temp drive is nearly full | `true` while frames are being written |
| `run.run_id` | identifies the run | always known |
| `run.started` | "Started", "Running for" | always known |
| `run.state` | shutdown banners and the run-ended screen: `running`, `stopping` (stop clicked), `aborted` (Abort chosen), `ended` (collection finished) | `running` |
| `run.message` | one line on the run-ended screen, whatever python wants the operator to read about the cleanup (where the metadata went, what was left behind) | `""` |
| `run.machine_name` | top strip of the viewer | copied from the camera config's `machine_name` (see below); the hostname when the config has none |
| `save_drive.path` | shown with the accounting | `save_path` from the config |
| `save_drive.found` | red "SAVE DRIVE NOT FOUND" | `false` when `shutil.disk_usage(save_path)` raises; the byte fields are then `null` |
| `save_drive.accepting_clips` | red "SAVE DRIVE FULL": python's own transfer rule says a clip would (not) be accepted now | `true` |
| `save_drive.total_bytes`, `used_bytes`, `free_bytes` | used bar, "Save drive free" (0 shown in red) | `null` when not found |
| `save_drive.seconds_until_full` | "Recording time left" and the time-based colours | python's `seconds_until_full(free)`; the viewer does not know the assumed write rate; `null` when not found |
| `temp_drive.path`, `found`, byte fields, `seconds_until_full` | "Temp drive free"; temp cache capacity in the SAVE DRIVE FULL banner | same defaults as the save drive |
| `temp_drive.clips_waiting` | "N clips waiting in the temp folder" in the drive banners: closed clips sitting in temp beyond the one each camera has open | `0` |

## Camera config: one new optional field

`machine_name` (string, no default) as a run-level field of the camera code's `Config`
class: a user-friendly name of the camera computer ("Mac 4", "Stations 9-10"). Python copies
it into `run.machine_name`; when the config has none, the hostname. The config editor writes
it when the user sets one.

## Viewer-side decisions (soft thresholds on python's numbers)

| condition | banner |
|---|---|
| `save_drive.seconds_until_full` under 1 hour | red: SAVE DRIVE NEARLY FULL: d h min of recording left |
| `save_drive.seconds_until_full` under 24 hours | orange: Save drive has space for another d h min |
| a still older than max(10 s, 10 x `preview_interval`) | that camera shows the offline image |
| every camera offline and `run.state` still `running` | red: NO STILL FROM ANY CAMERA SINCE HH:MM, the camera program has not reported a shutdown |

These replace the thresholds in `drive_warning()` in ratrixcam_IO.py, which the viewer does
not read.

## Placeholders in the viewer until these fields exist

Marked in the code with "PLACEHOLDER until status file supplies <field>":
- `run.run_id`, `run.started`: taken from the terminal log's file name.
- `run.state`: the log's last lines are searched for the shutdown messages.
- `save_drive.accepting_clips`: "full" taken as free space under one clip.
- `recording`: taken as both drives without room for a clip.
