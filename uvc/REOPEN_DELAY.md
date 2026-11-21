# How long to wait before reopening a released camera (plan, 2026-11-20)

Status: **plan and tooling only; nothing has run on the rig.** The rig is on loan to the
camera developer. This file will receive the results; the earlier reports
(`REPORT.md`, `FRAME_DROPS.md`, `DIM_LIGHT_TEST.md`) are frozen.

## The question

After a camera is released, how long must a new process wait before opening it, so that it
does not get the state where the camera delivers almost no frames for a long time?

What is known (all one camera at a time, 640x480):
- `uvc/logs/persist-C-run1.log`: a stream stalled about 1 s after an open that came 8 s after
  another process released the same camera.
- The developer's AVFoundation report: twice, almost no frames right after an AVFoundation
  session on the same device closed (26 frames in 60 s once, 29 in 8 s once).
- After a replug, a fresh process streams from 3 s, so re-enumeration clears the state.

Not known: whether the length depends on the number of cameras streaming, on how long the
camera streamed before its release, or on the kind. The mechanism is not known either; it
is host-side state on the device, since a replug clears it.

## The experiment (rulings of 2026-11-20)

`uvc/reopen_delay.py`. One trial on one camera:
1. **Stream, then release.** A new process opens the camera and streams it until it has
   streamed normally, without a break of more than 0.5 s, for the arm's stream length. Then
   it calls `capture.release()` and exits.
   - This matches the recorder: after three failed reads its server leaves the frame loop,
     calls `capture.release()`, closes its clip, waits for its transfers and exits.
   - This was checked in `ratrixcam_server.py` on branch origin/dev as fetched in this
     clone (3b8a1b4, 2026-11-17): the `break` in the frame loop, then the lines after it.
     The current dev head was not reachable from here.
   - This first stream opens 5 s after the previous trial's reopen was released, so it is
     itself a reopen. Its own start-up is logged as extra data, and it only releases after
     an unbroken normal run.
2. **Reopen after the delay.** Another new Python process, as the recorder's relaunch is,
   opens the camera exactly <delay> s after `release()` returned. The delay is measured to
   the `VideoCapture()` call, and the actual value is logged.
   - It counts frames in 1 s bins for 10 s, from when the open returns.
   - **Stalled** = fewer than 230 frames in those 10 s. Normal is about 293 on kinds A and
     C, and about 303 on B.
   - On a stall it keeps watching up to 60 s. **Stall length** = the time to the first of 3
     seconds in a row with 20 or more frames each, or ">60 s".
   - It also logs the open time, the time to the first frame, the longest gap, and every
     1 s count.
3. **Rest** 5 s, then the next trial.

Per camera, the delays run in a randomised order (logged), 5 repeats each. **Identification
(the marker, restored and read back) runs once, at the end of each camera's sweep**, after
60 s of rest.
- It confirms the established mapping: kind A at lab port 1 = index 0, kind B at port 2 =
  index 1, kind C at port 4 = index 3.
- A mismatch invalidates that camera's sweep, and the summary says so.
- No other control is written.

Arms, in the ruled order:

| Order | Arm | Stream before release | Release | Delays (s) | Repeats | Load | Time (about) |
|---|---|---|---|---|---|---|---|
| 1 | short | 5 s | clean release, exit | 1, 2, 4, 8, 15, 30, 60 | 5 | alone | 1 h 20 min |
| 2 | short | 5 s | clean release, exit | same | 5 | other seven streaming | 1 h 30 min |
| 3 | long | 180 s | clean release, exit | 2, 8, 30 | 3 | alone | 1 h 40 min |
| 4 | kill | 5 s | SIGKILL while streaming, no release | 8 | 5 | alone | 10 min |
| (5) | long | 180 s | clean release, exit | 2, 8, 30 | 3 | other seven streaming | only if arm 3 differs from arm 1 |

The times assume no stalls; each stall adds up to 50 s.

Load: in the load arms, the other seven cameras stream in their own reader processes
(640x480, 30 fps requested). They are started 30 s before the tested camera's sweep and
stopped after it, and each one's frame rate is logged every 30 s.

**Limits, to be repeated in the results:**
- The load is lighter than the recorder's: the readers read frames, but do not encode,
  write clips or transfer files.
- The load cameras' indices are taken from the established mapping and are not identified,
  because no control is written to them.
- The experiment measures release-then-reopen, which is a floor for the safe delay, not
  necessarily all of it. The recorder's real case is a server that exits because its camera
  stopped delivering, and that state cannot be produced on demand.

## Commands (for when the rig is handed back; the recorder must not be running)

Run one at a time, and wait for each to finish. Leave the rig untouched; the runs can go
unattended overnight.

RUN THIS (arm 1):
```
cd ~/Desktop/ratrixcam-uvc && uvc/.venv/bin/python uvc/reopen_delay.py run --arm short 2>&1 | tee uvc/logs/reopen-short-alone-run1.log
```

RUN THIS (arm 2):
```
cd ~/Desktop/ratrixcam-uvc && uvc/.venv/bin/python uvc/reopen_delay.py run --arm short --load 2>&1 | tee uvc/logs/reopen-short-load-run1.log
```

RUN THIS (arm 3):
```
cd ~/Desktop/ratrixcam-uvc && uvc/.venv/bin/python uvc/reopen_delay.py run --arm long --repeats 3 2>&1 | tee uvc/logs/reopen-long-alone-run1.log
```

RUN THIS (arm 4):
```
cd ~/Desktop/ratrixcam-uvc && uvc/.venv/bin/python uvc/reopen_delay.py run --arm kill 2>&1 | tee uvc/logs/reopen-kill-alone-run1.log
```

Arm 5 is decided after arm 3 has been read.

## Offline test

`uvc/offline_tests/test_reopen_logic.py` checks:
- the stall and recovery definitions and the summary;
- the stream-before-release and reopen functions, against a scripted fake capture.

It cannot reach a camera:
- it installs a fake `cv2` module before anything imports OpenCV;
- starting a process or sending a USB request raises an error;
- it runs in one process.

All 17 checks pass (2026-11-20). The parent's process handling (starting, timing and
killing the child processes) was exercised earlier the same day with stand-in cameras
inside the script. Under the new rule that mode has been removed, and the handling will
first run for real on the rig.

## Results

(none yet)
