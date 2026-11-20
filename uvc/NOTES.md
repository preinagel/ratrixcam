# Session handoff notes (written 2026-11-19, before a context compaction)

Read this first after a compaction. The brief is `CLAUDE.md` at the clone root (authoritative;
last changed at c4942b0). The results are in `uvc/REPORT.md`, the frame-drop findings for the
camera developer in `uvc/FRAME_DROPS.md`, and the tooling in `uvc/probe.py` and
`uvc/frames_analysis.py`. The memory folder (`~/.claude/projects/.../memory`) is not writable
in this session's permissions, so these notes are the persistent record.

## How the user wants to work
- **"Write to memory" / "update your memory" means: update this file, `uvc/NOTES.md`**, and
  commit it. It does not mean the Claude memory folder (that one is not writable here anyway).
- **I never run anything that opens or talks to a camera.** I write the command as a
  "RUN THIS:" fenced block (from `~/Desktop/ratrixcam-uvc`), with `2>&1 | tee
  uvc/logs/<sub-command>-<kind>-runN.log`. The user runs it and tells me; I read the log
  myself. Use a new log name per run: reruns must not overwrite.
- No viewer runs during probes: all visual evidence is printed as numbers (brightness, fps).
- Do not read files outside the clone (e.g. Desktop configs) without naming them and asking.
  Files under `uvc/` are mine to change without asking. Ask when unsure of the user's
  *intent* or of scope.
- Keep the shell's working directory at the clone root. Claude Code creates stray
  `.claude/.cc-writes` folders in whatever directory is current (it happened in
  `uvc/logs/` and `for_uvc_developer/`). Use absolute paths, not `cd` into subfolders.
- The user is not a USB specialist: give a plain-language bottom line first, details after.
- Report per kind, never pooled. Every streaming run must identify which kind is behind its
  index ("index N is kind K at port P"). Restore every control written, read it back, and
  record it in REPORT.md's Restored section.
- Commit after each change, one subject line plus the attribution line; never push. Do not
  commit the user's untracked files at the root: `camera_config_5.json`,
  `for_uvc_developer/`, `uvc_developer_frame_drop_investigation.md`.

## Rig (8 cameras, two hubs; all Arducam OV2710, Sonix vendor 0x0c45)
Port N = cv2 index N-1 = config entry N-1 (the recorder indexes in config list order; port
order confirmed by the user and by marker tests for ports 1, 2 and 4).

| Port | Location | Kind | Product | Name string |
|---|---|---|---|---|
| 1 | 0x01141000 | A | 0x6369 | USB 2.0 Camera (no serial) |
| 2 | 0x01142000 | B | 0x6366 | USB 2.0 Camera (SN0001) |
| 3 | 0x01143000 | A | 0x6369 | USB 2.0 Camera |
| 4 | 0x01144000 | C | 0x6366 | USB Camera (SN0001) |
| 5 | 0x00141000 | A | 0x6369 | USB 2.0 Camera |
| 6 | 0x00142000 | B | 0x6366 | USB 2.0 Camera |
| 7 | 0x00143000 | C | 0x6366 | USB Camera |
| 8 | 0x00144000 | B | 0x6366 | USB 2.0 Camera |

B and C differ only by name string. B has an extra H.264 stream and no 800x600. C's YUY2
640x480 is 15 fps, but it is always streamed as MJPEG.

## Technical facts established
- pyusb's `ctrl_transfer` silently claims the interface; Apple's driver refuses ("exclusive
  access"). The probe sends via `dev._ctx.backend.ctrl_transfer` on the device handle: works
  without root, with or without an AVFoundation stream.
- Exposure SET_CUR stalls in auto mode: write AE mode manual (1) first. Restore = manual,
  exposure and gain to GET_DEF, then AE mode to GET_DEF (8), and read back. Defaults:
  exposure 157 (A, C) or 156 (B); gain 0; AE mode 8; AE priority 0 (never changed).
- In auto mode, exposure GET_CUR returns the stored value, not the live AE exposure, so
  goal 1 (read exposure) fails that way.
- Settings survive stream start and stop; a replug resets them to defaults (all kinds).
- A process that streamed a camera before its replug gets 0 frames from that index after;
  a fresh process works from 3 s. The recorder relaunches in new processes, so it is fine.
- The wire format is always MJPEG (camera-side VS_COMMIT GET_CUR, 9+ reads); 800 B payload
  (alt 0x0320) at 640x480.
- Gain at 4 ms nearest auto, per camera's own scene: A 50, B 40, C none (4 ms is already
  brighter than auto for C at 352x288). Dim light is untested.
- Hook recommendation (REPORT.md): a supervisor pass by USB id and name string before each
  (re)launch, plus a read-back after the capture opens, reapplying on mismatch.

## Frame drops (FRAME_DROPS.md)
- Drop classes = kinds: A (views/ports 1, 3, 5) about 1 in 43 (spacing 42/41); C (4, 7) about
  1 in 32 (31/30); B (2, 6, 8) no periodic drop. Also seen one camera at a time, and in the
  probe (single-camera fps A 29.62, B 30.16, C 29.53).
- Gaps are exactly 2 grid slots (66 ms) on a 33.05 ms grid. Leading hypothesis (unmeasured):
  inside the camera, a sensor slower (~29.6 / 29.3 fps) than the USB-side frame grid
  (30.27), so an empty slot and no lost image. A Mac-side discard is not excluded.
- Definitive test: per-frame FID/PTS from the USB stream. It needs root plus a ctypes
  libusb isochronous reader on macOS (outside the brief; user's decision), or a Linux host.
  A cheap test (stopwatch in view; timestamp jump of 1 vs 2 periods across a gap) was
  suggested; the user has noted it elsewhere.
- **Resolved (developer's `avfoundation_probe_report_20261119.md`, root of the clone):**
  - No frame is lost on the Mac: the AVFoundation USB-clock stamps are uniform with no hole.
  - Each kind runs at its own wire rate (A 29.60, B 30.55, C 29.35). The beat is in the
    host's delivery pacing.
  - My "empty slot on the camera" hypothesis was withdrawn (FRAME_DROPS.md section 7).
  - The FID/PTS root/Linux test is dropped.

## Dim-light test on a deployment rig (prepared 2026-11-19)
- It must run on a deployment rig: the lighting cannot be reproduced in the lab. Each camera
  has its own lamps, IR-pass filter and light-scatter hood. The kinds there are unknown,
  probably A and C.
- **Report measurements only (luminance, noise, clipping, fps). Never judge whether an
  image is "usable"**: the downstream analysis team makes that assessment.
- Tooling:
  - `probe.py dimsweep` runs over indices 0..N-1 with every target kind as a candidate, at
    640x480 @ 30.
  - `gainsweep` now sweeps only the identified camera, with `--exposure` and `--kind any`.
  - Reader adds luminance Y statistics: mean, p1/p50/p99, temporal noise, snr, saturated %,
    black %.
  - Identification is relative only. The dark marker passes when brightness falls below
    0.45 x the baseline, and needs a baseline of 18 or more. Below 18 it uses a bright
    marker instead: 100 ms at maximum gain, passing at 1.5 x the baseline and +6 or more.
    `--marker dark|bright` forces one. The marker's restore is in a finally block (critic,
    2026-11-19), so a Ctrl-C mid-marker cannot leave a camera black.
  - Sweep exposures 2, 3, 4 and 10 ms (user, 2026-11-19); 10 ms warns as not under 5 ms,
    which is intended. About 2 min per camera.
  - Output labels the lab port numbers "lab-rig port"; the USB location is the reliable
    identifier on any Mac.
  - Lab hardware test before the push: dimsweep on all 8 (index k should be port k+1), plus
    a forced bright-marker gainsweep. A 100 ms exposure (beyond the frame interval) has
    never been tried on these cameras.
  - libusb is found at /opt/homebrew, then /usr/local, or via UVC_LIBUSB.
- Operator instructions: `uvc/DIM_LIGHT_TEST.md`. REPOSITORY-ADDRESS is a placeholder for
  the user.
- The user: ~/Desktop/temp is NOT a real recorder check, because users choose any temp path.
  Operators are simply told the recorder must not be running. The probe's guard stays as an
  extra (brief rule).
- The user plans to push the branch to GitHub so that the deployment Mac can clone it. I
  never push.

## Rig state and pending work
- Rig state: every camera written to was restored to its own defaults and read back OK
  (last: persist-C run 2, 12:33:47). The rig was on loan to the camera developer after that.
- Pending runs, when the user returns the rig (commands are in the conversation and in the
  probe header). If the developer moved cameras, ask for the new port layout first.
  - DONE 16:14-16:18: kind C at 640x480 (q1-C-run2, gainsweep-C-run2, persist-C-run3).
    The answers are the same as at 352x288: MJPEG, 29.3 fps, no compensation needed at
    4 ms, reverts on replug, no stall. Restored and read back OK. REPORT.md is updated.
  - Give run commands one at a time, each with `cd ~/Desktop/ratrixcam-uvc && ...` (the
    user's request);
  - Leave about 1 min between runs on the same camera. A quick reopen after another
    session closes can give almost no frames (developer's open question 3). That is the
    probable cause of the persist-C run 1 stall, 8 s after gainsweep-C.
  - `soak --minutes 20` is deferred, not needed unless a stall recurs after a pause.
- Open questions in REPORT.md:
  - dim-light compensation (no kind; needs the rig in recording light; the user's call).
    The deployment rigs are in another building, with kinds unknown: probably A and C,
    likely no B. The user will do the dim-light test after the known issues are settled;
    it should identify each kind there first;
  - replug run 1's lost marker (kind A, covered by the read-back-and-reapply hook);
  - (the C 640x480 reruns are done; no per-kind gap remains for Q1-Q3).
  - Still untested: settings rewritten after a replug surviving the relaunch's first stream
    start, on kinds B and C (tested on kind A only).
- `uvc/camera_config_5.json` is the probe's own config copy: all 8 entries at 640x480 @ 30
  (C entries moved from 352x288 to 640x480 at the critic's recommendation).
