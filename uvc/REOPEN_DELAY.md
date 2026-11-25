# How long to wait before reopening a released camera (plan, 2026-11-20)

Status: **complete (2026-11-24): arms 1, 2 (with a kind C rerun) and 4 run; arms 3 and 5 skipped by the user. See Conclusion.** The plan
was written on 2026-11-20 while the rig was on loan; it was handed back on 2026-11-24. This
file holds the results; the earlier reports (`REPORT.md`, `FRAME_DROPS.md`,
`DIM_LIGHT_TEST.md`) are frozen.

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

### Check run (2026-11-24 10:46:44; `uvc/logs/reopen-check-run1.log`)

Kind A only, delays 2 and 8 s, one trial each.
- It tested the process handling on the rig: reopen delays of 2.00 and 8.01 s actual, both
  normal (286 and 296 frames in the first 10 s).
- Identification confirmed index 0 = kind A at lab port 1, restored and read back at its
  defaults.

### Arm 1: short stream, camera alone (2026-11-24 10:53:41 to 12:07:43; `uvc/logs/reopen-short-alone-run1.log`)

`uvc/.venv/bin/python uvc/reopen_delay.py run --arm short`: 5 s of normal streaming, clean
release, exit; reopen in a new process after 1, 2, 4, 8, 15, 30 or 60 s; 5 repeats each, in
randomised order (logged); kinds A, B, C on lab ports 1, 2, 4.

**No reopen stalled: 0 of 105.**

| Kind (lab port, index) | Stalled, each delay 1/2/4/8/15/30/60 s | First-10-s frames, all trials | Open call | First frame after the call | Longest gap |
|---|---|---|---|---|---|
| A (1, 0) | 0 of 5 at every delay | 285-296 | 0.80-1.32 s | 1.18-1.38 s | 0.07 s (0.38-0.40 s in two 60 s trials) |
| B (2, 1) | 0 of 5 at every delay | 297-306 | 1.35-1.88 s | 1.43-1.90 s | 0.04-0.08 s (0.36 s once) |
| C (4, 3) | 0 of 5 at every delay | 292-293 | 1.19-1.26 s | 1.20-1.32 s | 0.07 s |

- **The delays were exact:** within 0.01 s of the target in every trial.
- **Each trial's first stream is a reopen too.** It opened 5 s after the previous trial's
  reopen was released, plus process start-up, and it was also normal every time: first
  frame at 1.15-1.26 s (A), 1.41-1.89 s (B) and 1.18-1.29 s (C).
- Counting both kinds of reopen, **210 reopens 1-60 s after a release, with no stall.**
- **Identification at the end of each sweep confirmed all three mappings:**
  - index 0 = kind A, lab port 1: 136.4 -> 0.9;
  - index 1 = kind B, lab port 2: 120.9 -> 1.9;
  - index 3 = kind C, lab port 4: 107.0 -> 10.4.
  Each marker was restored and read back at its camera's defaults (9 OK read-backs, no
  MISMATCH). No other control was written.
- The rates match the kinds' known wire rates: A and C about 29.3-29.6 fps, B about 30.5.

**What this shows, and what it does not:**
- With one camera streaming, a clean release after a short stream and a reopen in a new
  process are safe from 1 s on. This was so in all 105 trials; with 5 trials per delay
  and kind, a rare event cannot be excluded.
- So the stalls seen before are not explained by release-then-reopen alone, under these
  conditions.
  - **persist-C run 1** stopped about 1 s after a normal start (32 frames, then none). It
    did not fail to start. It came 8 s after a process that had held a libusb handle on
    the camera and written controls (gainsweep-C run 1), at 352x288.
  - **The developer's two cases** followed an AVFoundation session of their own, not an
    OpenCV one.
  - These conditions are not part of this experiment's design.
- Arms 2 (load), 3 (long stream) and 4 (kill) test the remaining factors in the plan.

### Arm 2: short stream, other seven cameras streaming (2026-11-24 12:28:30 to 13:53:33; `uvc/logs/reopen-short-load-run1.log`)

`uvc/.venv/bin/python uvc/reopen_delay.py run --arm short --load`: as arm 1, with seven
reader processes streaming the other cameras during each camera's sweep.

**Summary as logged** (stalled = under 230 frames in the first 10 s):

| Kind | 1 s | 2 s | 4 s | 8 s | 15 s | 30 s | 60 s |
|---|---|---|---|---|---|---|---|
| A (port 1) | 0/5 | 0/5 | 0/5 | 0/5 | 0/5 | 0/5 | 0/5 |
| B (port 2) | 0/5 | 0/5 | 0/5 | 0/5 | 0/5 | 0/5 | 0/5 |
| C (port 4) | 0/5 | 0/4 + 1 invalid | 0/4 + 1 invalid | 0/5 | **1/5** | **1/4** + 1 invalid | 0/5 |

**Kind C: five sessions started and then stopped.**
- Each one delivered 18-26 frames (about 0.7 s) and then nothing.
  - **2 reopens**: trial 4 (30 s delay) gave 22 frames, then none for the rest of the 60 s
    watch; trial 34 (15 s delay) gave 26 frames, then none.
  - **3 first streams** (trials 13, 21 and 23, opened 5 s after a release) gave 18-22
    frames, then never reached 5 s of normal streaming within the 180 s limit. These are
    the "invalid" trials.
- **In every case the next new process, 5 s after the stalled one was released, opened
  the camera normally.** So this state belongs to one session, not to the device, and
  waiting longer did not avoid it: it occurred at 5, 15 and 30 s after a release, and not
  in the 1 s trials.
- This is the pattern of persist-C run 1 (32 frames, then none). It is different from a
  slow start.
- In arm 1, the same camera alone had 0 such events in 70 sessions.

**Kind B, port 2 (index 1): a load stream delivered nothing for the whole kind C sweep, 34
minutes.**
- The load reader on index 1 reported 0.0 fps (minimum and median) over all 68 windows of
  30 s. Only the minimum and median are logged; a short burst at the start is possible.
- How it was opened:
  - It opened about 1 s after kind B's end-of-sweep identification released the same
    camera (13:19:11).
  - That identification runs in the main experiment process. It writes the marker
    through libusb, streams through OpenCV, releases, and **stays alive**.
  - The new reader process opened the camera at once, with no rest.
- The same sequence on kind A's camera at 12:53:37 (the identification, then the load
  reader on index 0) streamed normally: median 29.2 fps, minimum 28.0. So it failed on 1 of
  2.
- **Kind C's five stalls all happened while this dead stream was on the same hub** (lab
  ports 1-4). During the kind A and B sweeps, all seven load streams delivered (minimum
  27.8 fps), and A and B had no stall.
- **This sequence was not part of the plan.** The experiment created it by running the
  identification in the main process and starting the load at once. That confounds arm
  2's kind C result: under load, or next to a dead stream on the same hub?

**Restored:** each sweep ended with the identification marker (manual, 0.1 ms, gain 0) on
the tested camera, restored and read back at its own defaults (A 12:53, B 13:19, C 13:53;
9 OK read-backs, no MISMATCH). All three mappings were CONFIRMED (A 120.5 -> 1.6;
B 173.4 -> 18.1; C 108.6 -> 10.5). No other control was written.

**What arm 2 shows, as measured:**
1. No stall on kinds A and B under load (0 of 70 sessions).
2. A start-then-stop event on kind C (5 of about 70 sessions), all during the 34 minutes
   when kind B's camera on the same hub was delivering nothing. A longer delay did not
   prevent it, and an immediate new session cleared it.
3. A reopen within about 1 s of a release by a process that stays alive (and had written
   controls through libusb) gave a stream that delivered nothing for 34 minutes, on 1 of 2
   occasions.

### Changes to the tool after arm 2 (2026-11-24; offline)

- **The end-of-sweep identification runs in its own process, which exits**, like every
  other stream. The start-up presence check also runs in its own process. The main
  experiment process now never opens a camera or a USB handle.
- **A 60 s rest before the load readers start**, so no load stream opens soon after a
  release.
- **Every 30 s load window is logged**, not just the minimum and median.
- The offline test still passes (17 of 17).

### Arm 2 rerun, kind C only, clean load (2026-11-24 15:44:56 to 16:16:41; `uvc/logs/reopen-short-load-C-run2.log`)

`uvc/.venv/bin/python uvc/reopen_delay.py run --arm short --load --kinds C`, with the changed
tool: the load starts after a 60 s rest, and identification runs in its own process.

**The load was clean.** All seven load streams delivered in every 30 s window for the whole
sweep (61 windows each):
- index 0: 29.3-29.6 fps;
- index 1: 30.2-30.6 fps;
- index 2: 29.3-29.6 fps;
- index 4: 29.1-29.8 fps;
- index 5: 30.1-30.6 fps;
- index 6: 29.0-29.4 fps;
- index 7: 30.1-30.6 fps.

There was no dead stream this time.

**Kind C still had start-then-stop sessions: 3.** Each delivered 22-26 frames (about 0.8 s)
and then nothing:
- trial 3, first stream (opened 5 s after a release): 22 frames, then no normal stream
  within 180 s (invalid trial);
- trial 18, first stream: 22 frames, the same;
- trial 21, reopen at a **2 s** delay: 26 frames, then none for the rest of the 60 s watch.

As before, **the next new session after each one was normal** (trials 4, 19 and 22: 292-293
frames in the first 10 s).

| Kind C, clean load | 1 s | 2 s | 4 s | 8 s | 15 s | 30 s | 60 s |
|---|---|---|---|---|---|---|---|
| stalled reopens | 0/5 | **1/5** | 0/5 | 0/4 + 1 invalid | 0/5 | 0/4 + 1 invalid | 0/5 |

**Identification** (own process, exited): index 3 = kind C at lab port 4, 110.4 -> 9.0,
CONFIRMED. The marker was restored and read back at C's defaults (3 OK read-backs, no
MISMATCH). No other control was written.

### Kind C under load, arm 2 and the rerun together (measurements)

- **Start-then-stop sessions: 8 of about 140 under load (5 + 3), against 0 of 70 alone
  (arm 1).** Kinds A and B had none, alone or under load (0 of 140 sessions each).
- **The rerun rules out the dead neighbouring stream as the cause:** all its load streams
  were healthy, and the event still occurred.
- **It is not delay-dependent in this data.** It occurred on reopens at 2, 15 and 30 s
  after a release and on first streams 5 s after a release, and not in the 1, 4, 8 or 60 s
  reopens. Waiting longer did not avoid it.
- **A new session cleared it every time:** each of the 8 was followed, 5 s after its
  release, by a normal session.
- **The form is the same each time:** 18-26 frames (0.6-0.9 s), then no frame at all for as
  long as the session was kept open (60 s or 180 s). This is the form of persist-C run 1 (32
  frames, then none, camera alone, at 352x288). It is not the slow start the experiment was
  designed for.
- Session counts are approximate. The "first stream" of each trial (the stream before
  release) is counted as a session, alongside the reopens.

What this suggests for the recorder, not tested here: a server whose kind C camera stops
this way would see failed reads, leave its loop and exit, and the supervisor's relaunch
would open a new session. In every case here, a new session 5 s later was normal.

### Arm 4: killed process, camera alone (2026-11-24 16:50:52 to 17:02:02; `uvc/logs/reopen-kill-alone-run1.log`)

`uvc/.venv/bin/python uvc/reopen_delay.py run --arm kill`: the first stream ran normally for
5 s, then its process was killed with SIGKILL while streaming, without `release()`. A new
process reopened the camera 8 s later. 5 repeats per kind.

**No reopen stalled: 0 of 15.**

| Kind (lab port) | Stalled at 8 s | First-10-s frames | First frame after the open call |
|---|---|---|---|
| A (port 1) | 0 of 5 | 285-296 | 1.22-1.32 s |
| B (port 2) | 0 of 5 | 298-307 | 1.86-1.96 s |
| C (port 4) | 0 of 5 | 293-294 | 1.22-1.31 s |

- Every trial's first stream also started normally (first frame at 1.16-1.93 s).
- **Identification** ran in its own process after each sweep and confirmed all three
  mappings: A 128.7 -> 1.2, B 114.8 -> 0.7, C 108.8 -> 8.7. Each marker was restored and
  read back at its camera's defaults (9 OK read-backs, no MISMATCH). No other control was
  written.

## Arms not run (user's decision, 2026-11-24)

- Arm 3 (180 s stream before release) and arm 5 (arm 3 under load).
- The proposed arm in which the releasing process stays alive (the condition of arm 2's
  34-minute dead stream).

The questions they would answer stay open: does a long stream before release change the
result, and is the dead stream after an in-process release repeatable?

## Conclusion (measurements as above; one lab rig, 640x480)

- **No delay-dependent stall was found.** A new process reopening a camera 1 to 60 s after a
  clean release, or 8 s after its previous process was killed, streamed normally:
  - 105 of 105 reopens with the camera alone;
  - 70 of 70 for kinds A and B under load;
  - 15 of 15 after a kill.
  In this data, waiting longer than 1 s gives no benefit.
- **Kind C has a separate fault under load.** In about 6% of sessions (8 of about 140) it
  delivers about 0.8 s of frames and then none, for as long as the session is kept open.
  - It was not seen with kind C alone (0 of 70), nor on kinds A and B.
  - It was not avoided by waiting before the open.
  - A new session 5 s later was normal every time.
- **The long dead stream (34 minutes) was seen once**, on kind B. A new process opened the
  camera about 1 s after a release by a process that stayed alive. The same sequence on kind
  A was normal. It was not reproduced, because the targeted arm was not run.
