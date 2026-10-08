
## Overview
videoproc is the companion to ratrixCam for what happens *after* a recording session: compressing the week's videos down to an archivable size, verifying that every compressed copy is sound, and only then deleting the bulky originals. It consists of two command-line tools in the `videoproc/` folder of this repository:

- `compress_drive.py` compresses every video in a source folder into a standard archive layout, optionally choosing how hard to compress each clip from a quick motion analysis, and verifies each output as it goes.
- `confirm_and_delete.py` re-checks, file by file, that a valid compressed copy exists at the archive for every original, and deletes the originals only when asked, only after showing you the tally, and only after you type `DELETE`.

Compression is CPU-intensive — it can take longer than the video's own duration — so it is not run on the recording Mac during a session. We run it on a separate computer against folders copied off the recording drives. Both tools write a complete record of what they did, so you can later reconstruct how any archived clip was produced.

### System Operation in Brief
- Run `compress_drive.py` on a session folder; it works unattended for hours.
- Read the summary it prints at the end: every file should be `compressed`, `copied`, or `already_done`, and nothing flagged for review.
- Run `confirm_and_delete.py --delete` on the same folder; it checks every file, decodes every archived copy, prints a summary, and asks you to type `DELETE`.
- Compare the summary with what you expected before typing `DELETE`.
- Deal with whatever is left in the source folder (notes, listings, broken clips); the tool leaves those for you.

## Operation Instructions in Detail

### Preparing
Both tools are run from a terminal, from the `videoproc/` folder of your clone of this repository. They need `ffmpeg` on the PATH and Python 3 with `opencv-python` and `numpy`. The source folder and the destination (archive) folder must both exist. If your archive contains files named by an older convention, see *Legacy filenames* under Technical Details before you start.

Every command begins by asking you to press Enter after it prints its configuration, so you can catch a wrong path or flag before anything happens.

### Compressing a session folder
A typical command looks like

```
cd <path-to-clone>/videoproc
python3 -u compress_drive.py <source folder> <archive root> --output_studyname <NAME> --pattern '<glob>' --motion_detection --motion_threshold 0.001
```

`<source folder>` is searched with `--pattern` (a glob relative to the source; the default `**/LS*/*.mp4` finds `.mp4` files inside folders whose names start with `LS`). Every clip found gets a destination path built from its own filename:

```
<archive root>/<subjectID>/<NAME>_<subjectID>_<view>_<YYYYMMDD>/<subjectID>_<view>_<YYYYMMDD>_<HH-MM-SS>.mp4
```

so the archive is organized by subject, then by subject-view-day, regardless of how the source folders were arranged.

As it runs, the terminal shows one block per clip: the filename, whether motion was found (if motion detection is on), which compression setting was chosen, and any problem. On an 8-core Apple-silicon Mac a 10-minute 640×480 clip takes roughly 15–25 seconds, i.e. 150–250 clips per hour. If the source or archive is on a network volume that drops out, the tool pauses and retries for up to ten hours before giving up.

At the end it prints a summary: how many files matched the pattern, how many rows were logged (these should be equal), a count per outcome, whether motion detection was on, and how many rows were flagged for human review, with the path of the file listing them. Files that were already correctly archived are reported as `already_done` and not touched, so re-running a folder is cheap and safe.

What the tool will **not** do: it never deletes or modifies anything in the source folder; it never copies a file it cannot open; and it never silently overwrites a different video that happens to sit at the destination path — such collisions are flagged for you.

### Verifying and deleting the originals
When a folder has been compressed and the archive has been backed up, run

```
cd <path-to-clone>/videoproc
python3 -u confirm_and_delete.py <source folder> <archive root> --output_studyname <NAME> --pattern '<glob>' --delete
```

The tool works in two passes. First the cheap checks on every file: the filename parses, a copy exists at the destination, it opens, it is compressed (not raw), its frame count equals the original's, and it is at least `--min_age_hours` old (default 24 hours, so that a nightly backup has had time to run). If anything fails these checks and you did not pass `--delete_partial`, the run stops right there with `REFUSING to delete` and tells you why, before spending time on the second pass. Otherwise every surviving candidate is fully decoded with ffmpeg (about 1–3 seconds per clip) to catch corruption that a frame count cannot. Then the summary is printed and you are asked to type `DELETE`. Anything else aborts with nothing deleted.

Deletion removes the verified originals one at a time, recording each in `deletion_log.csv`. Folders left empty are removed; folders that still contain something — notes, listings, a clip that could not be verified — are kept and their contents listed, so what remains is exactly what needs a human decision.

To see what the tool would decide without deleting anything, run the same command without `--delete` (add `--decode` to include the decode pass).

### Working with several computers
Several compressions may run at once against the same archive, from one or several computers, as long as each works on a different source folder; the shared run index is locked against simultaneous writes. Never run two jobs over the same source folder at the same time. A single computer is CPU-bound by one compression; a verification can overlap with it.

## Settings

### `compress_drive.py`
| Argument | Default | Description |
| ----------- | ----------- | ----------- |
| `input` | required | source folder, searched with `--pattern` |
| `output` | required | archive root; compressed files and run records are written under it |
| `--output_studyname` | required | study-name prefix for archive folders (e.g. `LS`), independent of whatever prefix the source files carry |
| `--pattern` | `**/LS*/*.mp4` | glob selecting which source files are in scope |
| `--motion_detection` | off | analyse each raw clip for motion and compress motionless clips much more strongly; also saves per-frame motion values. Off: every clip is encoded at `--taskcam_crf`. Never applied to already-compressed sources |
| `--motion_percentile` | `99.9` | with motion detection: percentile of the per-frame motion values used as a clip's motion score |
| `--motion_threshold` | `0` | with motion detection: a clip whose score is at or above this counts as having motion. 0 treats every clip as motion while still recording the motion values; we use `0.001` for our rats, calibrated on our own raw video |
| `--n_threads` | `4` | threads given to ffmpeg |
| `--taskcam_crf` | `25` | ffmpeg quality (CRF) for task-chamber views with motion, and for every clip when motion detection is off; lower is better quality and larger files |
| `--compress_spd` | `veryfast` | ffmpeg preset; slower presets give smaller files for the same quality |
| `--recompress` | off | re-encode source files that are already compressed instead of copying them as-is (a second lossy generation — for when the raw originals are gone and the first compression was too weak) |
| `--overwrite_raw` | off | replace a raw (uncompressed) video found at the destination with the processed source |
| `--overwrite_compressed` | off | replace a compressed video found at the destination with a fresh processing of the source |

### `confirm_and_delete.py`
| Argument | Default | Description |
| ----------- | ----------- | ----------- |
| `source` | required | folder holding the originals you are considering deleting |
| `destination` | required | archive root (the folder you gave `compress_drive.py` as `output`) |
| `--output_studyname` | required | study-name prefix used in the archive |
| `--pattern` | `**/LS*/*.mp4` | glob selecting which source files are in scope |
| `--min_age_hours` | `24` | an archived copy modified more recently than this is not yet deletable — a proxy for "a backup has run since it was written"; backups themselves are not checked |
| `--decode` / `--no-decode` | on with `--delete`, else off | fully decode every archived copy with ffmpeg |
| `--delete` | off | after the checks, delete the verified originals (asks you to type `DELETE`) |
| `--delete_partial` | off | with `--delete`: proceed even if some files could not be verified (they are left in place). Without it, any unverified file blocks all deletion |

### About motion detection
With `--motion_detection`, the tool samples every tenth frame of a clip at reduced resolution, subtracts a running background, and records the fraction of pixels that changed. The clip's motion score is a high percentile of those values; if it reaches `--motion_threshold`, the clip is encoded at a visually lossless setting, otherwise at a very strong compression that is adequate for an empty or sleeping-animal view. Our threshold was chosen empirically so that any clip in which an awake rat is present, however briefly, is kept at full quality.

The threshold and percentile are specific to our cameras, lighting and animals; another setup will need its own calibration, which is why the tool's default is to compress everything at full quality and record the motion values (`motion_timeseries.csv`) for later analysis rather than act on them. Already-compressed sources are never motion-analysed: compression artifacts register as motion and would defeat the purpose.

## Technical Details
Information below here is not necessary to use the tools, but may be useful when something unexpected turns up or when reconstructing how an archived clip was made.

### Where metadata is saved
Every run — compression or verification — creates its own folder under the archive root:

```
<archive root>/videoproc_run_metadata/<run_id>/
```

`run_id` is `videoproc_<date>_<time>_<random>` for `compress_drive.py` and `confirm_<date>_<time>_<random>` for `confirm_and_delete.py`. The folder is created at the start, so even an interrupted run leaves a record.

| File | Written by | Contents |
| ----------- | ----------- | ----------- |
| `config.json` | both | every argument the run was given; `code_version` (the git commit of the code, `+dirty` if it had uncommitted edits); for compression runs also `encoding_policy` (codec, pixel format, preset, view lists, CRF per branch, no-motion GOP) and `motion_detection` (enabled, percentile, threshold, detector parameters) |
| `manifest.csv` | both | per (subject, view) stream: date range covered and file count seen in the source scan |
| `log.csv` | both | one row per file considered: `input_path`, `output_path`, `start_time`, `status`, `action_item`, `skipped_reason`, `error`, then the compression fields (`motion_perc`, `found_motion`, `motion_detection_time`, `fract_frames_exceeding`, `compression_ratio`, `compression_success`, `compression_time`, `valid_output`); verification runs add `source_codec`, `destination_codec`, `destination_mtime`, `age_hours`, `size_ratio`, `decode_ok`, `deletable` |
| `action_items.csv` | both | the subset of `log.csv` rows flagged for human review (header only if none) |
| `terminal.log` | both | a copy of the terminal output including the end-of-run summary (messages printed directly by OpenCV or ffmpeg are not captured) |
| `motion_timeseries.csv` | compression, only with `--motion_detection` | one row per compressed clip: `clip_filename`, `motion_values` (space-separated, one value per sampled frame, 4 significant digits) |
| `deletion_log.csv` | verification, only when files were deleted | one row per deleted original: `input_path`, `output_path`, `input_size_bytes`, `deleted_at` |

In addition `<archive root>/videoproc_run_metadata/index.csv` gains one line per run (run id, subjects, date range, stream and file counts). Appends to it are locked so runs from several computers can share one archive.

The footprint is about 9 KB per compressed clip (of which ~8 KB is the motion timeseries) and about 0.6 KB per verified clip — well under 0.1 % of the video.

### What happens to each file
For every file matching `--pattern`, `compress_drive.py` parses the filename, computes the destination path, probes both files (codec and frame count), and decides:

| Source | Destination | Result | Status | Flagged |
| ----------- | ----------- | ----------- | ----------- | ----------- |
| cannot be opened | nothing there | nothing written | `invalid_source` | no |
| cannot be opened | cannot be opened | nothing written | `invalid_source` | no |
| cannot be opened | valid, compressed | nothing written | `invalid_source` | no |
| cannot be opened | valid, **raw** | nothing written | `invalid_source` | **yes** |
| valid | nothing there | compress or copy | — | — |
| valid | cannot be opened | compress or copy; the broken file is replaced | — | — |
| valid | valid, frame count **differs** | nothing written | `collision` | **yes** |
| valid | valid, compressed, frame count matches | skip unless `--overwrite_compressed` | `already_done` | no |
| valid | valid, raw, frame count matches | skip unless `--overwrite_raw` | `raw_at_destination` | **yes** |

A valid source that is itself already compressed is copied byte-for-byte unless `--recompress` is given. Otherwise it is encoded and then verified: the output must exist, be non-empty, and have the same frame count as the source — else `output_invalid`, flagged. Unparseable filenames give `unparseable` (flagged); `.mp4` files outside the pattern give `pattern_mismatch` rows (flagged; listed individually only when there are ten or fewer); unexpected exceptions give `error` (flagged); files never reached because connectivity was lost give `not_attempted` (flagged).

`confirm_and_delete.py` applies the same table in report-only form and adds `not_at_destination`, `destination_invalid`, `too_recent` and `decode_failed`, all flagged. A file is deletable only if its status is `already_done` after the age gate and the decode check.

### Compression settings in force
Outputs are H.264 (`libx264`, `yuv420p`). With motion detection off, or for an already-compressed source, every clip is encoded at `--taskcam_crf`. With motion detection on: clips with motion from the task-chamber views (`lid`, `face`) use `--taskcam_crf`; clips with motion from the cage views (`buddy`, `home`) use CRF 30; clips with motion from any other view name use `--taskcam_crf` and produce a warning, since an unknown view name usually means a misnamed file; clips without motion use CRF 40 with a 1800-frame keyframe interval. The view names are currently fixed in the code; the complete table in force is written to each run's `config.json`.

### Legacy filenames and `custom_filename_parser.py`
The tools recognize one filename shape, `<subjectID>_<view>_<YYYYMMDD>_<HH-MM-SS>.mp4`. For archives with older conventions, copy `videoproc/custom_filename_parser_example.py` to `videoproc/custom_filename_parser.py` and implement `parse(stem)`, which receives the filename without extension and returns `(subjectID, view, YYYYMMDD, HH-MM-SS)` or `None`. The tools call it for any name the standard rule does not recognize. The file is excluded from version control because it describes your data, not the tool.

### Timing and capacity
On an 8-core Apple-silicon Mac with 10-minute 640×480 clips: compression about 12–15 s per clip plus about 11 s of motion detection when enabled; verification probe about one minute per thousand clips; decode about 1–3 s per clip. Raw clips of 200–770 MB typically compress to 1–60 MB depending on view and motion, an aggregate ratio of roughly 25–30 to one. The source and destination may both be on a network volume; a dropped mount pauses the run (fast retries for a minute, then every ten minutes, for up to ten hours) rather than failing it.
