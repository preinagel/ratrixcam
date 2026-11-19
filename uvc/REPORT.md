# UVC exposure/gain control on macOS: prototype report

Status: Q1 answered yes, at 1920x1080 (q1 run 2) and at 640x480 (q1 run 3). Reads and
writes work without root, with and without an AVFoundation stream. **Goal 1 by the UVC
exposure read: no.** In auto mode the camera returns the stored manual value, not the
exposure its AE loop uses (aetrack run 1). **Goal 3 compensation: at 4.0 ms, gain 50 matched
the auto brightness in the room's light at 640x480** (gainsweep run 1). Q3 is answered yes
for all three controls. **Q2, stream start and stop: settings held** (persist run 1, at
640x480). Open: Q2 re-enumeration (the unplug step did not happen in persist run 1),
compensation in dim light, and repeats at the user's format (below).

**Rig state: restored** (restore run 1, 2026-11-19 09:21:33; again after gainsweep run 1 at
09:28:22 and persist run 1 at 09:32:34). All three cameras read ae_mode 8 (auto), exposure 157
(15.7 ms), gain 0 and ae_priority 0. These are the cameras' defaults and the values found at
the start. aetrack run 1 had left the stored exposure at 40; see Restored.

**Stream format, and a correction.**
- Runs up to and including track run 1 streamed at the camera default, 1920x1080.
- From aetrack run 1 to persist run 1, the probes requested 640x480 @ 30. That came from
  `~/Desktop/camera_config.json`, which I picked as "the recorder's format" because the
  repo's `cam_start.applescript` and the GUI editor name that path. I read it without asking
  the user, and the user had not chosen it for this prototype. Those 640x480 runs are
  therefore **not at the format the user intends**.
- The user then supplied `camera_config_5.json` at the clone's root: 800x600 @ 30, 5 camera
  entries. The probe now takes its format from that file only.
- The config is used for the format only. Cameras are always selected by USB id; every run
  found the same 3 targets. The config's 5 entries do not match the rig's 3 cameras, which
  does not affect the probe.

Goals in the order the brief values them (since 2026-11-19): (1) read exposure time and gain,
(2) clear AE priority, (3) exposure under 5 ms with gain compensation, sticking across stream
start and re-enumeration. `probe.py controls` covers (1) read-only; `probe.py q1` tests (1)
and (2) while streaming and previews (3) at 4 ms without gain compensation.

## Rig inventory (system_profiler SPUSBDataType, 2026-11-18)

Target cameras (vendor 0x0c45, product 0x6369, "USB 2.0 Camera", manufacturer Sonix):

| Location ID | Serial |
|---|---|
| 0x01143000 | none reported |
| 0x01141000 | none reported |
| 0x00141000 | none reported |

Not ours to touch: sibling 0x0c45:0x6366 (Arducam, serial "SN0001" on every unit) at
0x01144000, 0x01142000, 0x00142000 and others on the 0x00 bus. No Logitech webcam was seen;
the only Logitech device is a mouse.

**Rig as of 2026-11-19 (system_profiler SPUSBDataType, after the user removed three
cameras):** five live cameras, three on one hub and two on the other. **They are not all the
target product:**

| Hub | Location | Product | Reported as | Serial |
|---|---|---|---|---|
| bus 0x01 hub | 0x01141000 | **0x6369 (target)** | USB 2.0 Camera / Sonix | none |
| bus 0x01 hub | 0x01143000 | **0x6369 (target)** | USB 2.0 Camera / Sonix | none |
| bus 0x01 hub | 0x01144000 | 0x6366 (sibling) | USB Camera / Arducam | SN0001 |
| bus 0x00 hub | 0x00141000 | **0x6369 (target)** | USB 2.0 Camera / Sonix | none |
| bus 0x00 hub | 0x00143000 | 0x6366 (sibling) | USB Camera / Arducam | SN0001 |

- Every probe run found exactly the three 0x6369 cameras, which is correct: it selects by
  vendor and product id. No control request has ever been sent to a 0x6366.
- The brief's statement that "the rig holds only target cameras; any camera the probe opens
  by index is a target" does not hold for this rig. Two cv2 indices belong to 0x6366
  cameras. Every streaming run used index 0, which the brightness response identified as
  the target 0x01141000 each time it was checked (q1 runs 2 and 3, aetrack run 1, gainsweep
  run 1). track run 1 and persist run 1 are consistent with that but did not check it.
- persist run 1 saw no target disappear during its 120 s wait (09:30:34-09:32:34). If the
  camera the user unplugged was a 0x6366, the probe was right to ignore it.

Note for the hook question: the 0x6369 units report **no serial number**, and the 0x6366
units all share the same one, so neither can be told apart by serial. A by-device pass would
have to key on USB location ID (the physical port), which changes if a cable is moved.

## Setup notes

- Recording check: per the revised brief, the absence of `~/Desktop/temp` is the whole
  check (pgrep cannot list processes in the sandbox). Confirmed absent on 2026-11-18.
- pyusb: installed in `uvc/.venv` by the user (my own `pip install` failed on
  `SSLCertVerificationError` through the sandbox proxy; not worked around).
- libusb: at `/opt/homebrew/lib/libusb-1.0.dylib` (user-supplied path).
- OpenCV: 4.11.0, in the system Python 3.13 site-packages (not the venv).

## Sandbox probes (2026-11-18)

### system_profiler SPCameraDataType

```
$ system_profiler SPCameraDataType          -> no output, exit 0
$ system_profiler -json SPCameraDataType    -> {"SPCameraDataType" : [ ]}, exit 0
```

No error text, and the harness reported no sandbox violation, so **no service or path name
is available**. The camera list is empty even though SPUSBDataType shows the cameras on
the bus. The empty list therefore looks like a quiet refusal, not an absence of cameras.
The unified log could not be checked: `/usr/bin/log show` answers "log: Cannot run while
sandboxed".

### cv2.VideoCapture open + one read on a target camera: not run

cv2 opens AVFoundation devices by index only. With the camera report empty, nothing maps an
index to vendor/product 0x0c45:0x6369. Opening an index would select by position, which
the brief forbids, and could open a sibling 0x6366, which is off limits. Stopped to ask.

## Goal 1 (read exposure and gain): the controls can be read, but in auto mode the exposure read is not the live exposure

The table below shows the controls are readable as an ordinary user. aetrack run 1 (in
Evidence) shows that in auto mode the exposure value read back is the stored manual value,
so it cannot warn of long exposures. In manual mode the read equals what was written,
which makes it known by construction.

### Readable: controls run 2

`probe.py controls`, run 2, 2026-11-19 08:41:33 (`uvc/logs/controls-run2.log`). All three
cameras gave identical values; each request took 0.5-0.9 ms. libusb printed no warnings.

| Control | INFO | MIN | MAX | RES | DEF | CUR |
|---|---|---|---|---|---|---|
| AE mode (CT 0x02) | 3 (get, set) | stall | stall | 9 = manual + aperture-priority | 8 (aperture-priority = auto) | 8 |
| AE priority (CT 0x03) | 3 (get, set) | stall | stall | stall | stall | **0** (constant frame rate) |
| Exposure time absolute (CT 0x04), 100 us units | 3 (get, set) | 1 (0.1 ms) | 5000 (500 ms) | 1 (0.1 ms) | 157 (15.7 ms) | **157 (15.7 ms), over the 5 ms limit** |
| Gain (PU 0x04), unitless | 3 (get, set) | 0 | 100 | 1 | 0 | 0 |

"Stall" is `USBError errno=32 code=-9 Pipe error`, the camera rejecting a request that the
UVC spec does not require for that control (AE mode and AE priority are not ranged). It is
not an access problem.

Observations:
- AE priority reads 0 with and without a stream (q1 runs 2 and 3), so stream start does
  not set it to 1. The frame-rate drops the recorder sees are therefore not explained by
  this control being 1. Either the firmware ignores it, or the drops have another cause.
  This has not been reproduced in dim light.
- Exposure's INFO was 3 (get, set) at first. After the first manual-to-auto hand-back
  (aetrack run 1) it reads 15 (get, set, **disabled-by-auto**, **autoupdate**) while in auto.
  "Disabled by auto" matches the observed behaviour: exposure SET_CUR stalls in auto
  mode. "Autoupdate" claims the value changes on its own, yet it did not change in 5 s
  while AE visibly re-converged.

## Answers

1. **UVC requests while AVFoundation streams: YES**, as an ordinary user, with no root. In q1
   run 2 (1920x1080) and q1 run 3 (the recorder's 640x480 @ 30, opened as the recorder
   opens it), reads and writes succeeded on all three cameras, including the streamed one.
   The streamed camera's image responded to manual 4 ms (brightness -68% at 1080p, -44% at
   480p), with 0 failed reads and no fps change. Run 1's refusals were caused by the probe
   itself: pyusb's implicit interface claim, now removed.
2. **Stream start and stop: YES, settings held** (persist run 1, 640x480): manual exposure 4.0 ms
   and gain 10, set by USB location before any capture existed, read back unchanged after
   AVFoundation opened, streamed and released the camera, and the image was darker
   accordingly. **Re-enumeration (unplug/replug): not yet tested.**
3. **YES: AE mode, exposure time and gain are all writable**, with a visible effect on the
   image. AE mode is manual (1) or aperture-priority auto (8). Exposure time absolute is
   0.1-500 ms in 0.1 ms steps, default 15.7 ms. Gain is 0-100, unitless, step 1, default 0.
   AE mode and exposure were written in q1 runs 2 and 3; gain at 0-100 in gainsweep run 1.
   All were read back equal to the written value. **Exposure accepts writes only in manual
   mode**: in auto, SET_CUR stalls (aetrack run 1), so a setter must select manual first.
   Ranges are in the goal 1 table.

## Evidence

### Q1, run 2 (2026-11-19 08:45:26, user's Terminal, not root; stream 1920x1080 @ 30, camera default, not the recorder's format)

`uvc/.venv/bin/python uvc/probe.py q1 --index 0` (full output in `uvc/logs/q1-run2.log`). The
probe then opened with `VideoCapture(0, CAP_AVFOUNDATION)` and set no format.

- No stream: all reads OK (0.7-1.0 ms). The no-op write `ae_mode:=8` was accepted (0.7-0.9 ms).
- `VideoCapture(0, CAP_AVFOUNDATION)` opened in 672 ms and reported 1920x1080 @ 30 fps. The
  warm-up was 88 frames in 3.0 s (29.3 fps, 0 failed reads).
- While streaming, on each camera in turn:

| Camera (location) | ae_priority:=0 | ae_mode:=manual, exposure:=4.0 ms, read back | stream brightness before / during / 2 s after restore | fps during |
|---|---|---|---|---|
| 0x01143000 | accepted, read back 0 | 1 / 40 | 142.0 / 142.0 / 142.0 | 29.7 |
| **0x01141000 (the streamed one)** | accepted, read back 0 | 1 / 40 | **142.3 / 45.2 / 127.1 (-68%)** | 29.6 |
| 0x00141000 | accepted, read back 0 | 1 / 40 | 131.2 / 131.6 / 131.9 | 29.6 |

  Brightness dropped by 68%. (An earlier version of this report compared that with "4.0/15.7
  ms predicts about -75%". That comparison is withdrawn: 15.7 ms is the stored value, not
  AE's live exposure; see aetrack run 1.) After the restore it had recovered to 127.1
  within 2 s, as auto-exposure re-converged.
  Writes took 4.5-4.9 ms; a few on the streamed camera took 20-27 ms. No frame was lost
  in any window.
- **Brightness is measured on the one stream (index 0) only.** The ~0% rows for 0x01143000
  and 0x00141000 mean those cameras were not the capture. They do not mean the writes had
  no effect: on those two cameras the writes were accepted and read back, but no image was
  watched.
- **Mapping found as a side effect (relevant to GitHub #60):** the brightness response
  identifies cv2 index 0 as USB location 0x01141000 (bus 1, ports 1-4-1) on this rig.
  Writing a control by USB location and watching which index's stream responds is a
  working index-to-device method. Not pursued further: with a target-only rig, the mapping
  does not matter for this prototype.
- AE priority read 0 before the stream and while streaming: stream start does not set it
  to 1. The write of 0 was a no-op, so a change to AE priority has not been demonstrated.
- After the stream was released, all three cameras read ae_mode 8, ae_priority 0,
  exposure 157 and gain 0, identical to Phase A.

This run raised the goal 1 question: exposure read 157 (15.7 ms), exactly GET_DEF, on all
three cameras with and without a stream. aetrack run 1 settles it: in auto mode the read is
the stored value.

### Goal 1: aetrack run 1 (2026-11-19 09:12:07; stream 640x480 @ 30, from the Desktop config, not the user's format)

`uvc/.venv/bin/python uvc/probe.py aetrack` (`uvc/logs/aetrack-run1.log`). The capture was
opened as the recorder opens it: it requested, reported and measured 640x480 @ 30. On each
camera, while streaming index 0: manual + exposure 40 for 2 s; then ae_mode back to 8
(auto) *with the stored exposure left at 40*; then exposure and gain read every 0.25 s
for 5 s.

| Camera | auto before | manual 4.0 ms | after hand-back to auto: brightness | exposure read during those 5 s |
|---|---|---|---|---|
| 0x01143000 (not the capture) | 139.3 | 139.3 | 139.2-139.6 | 40 (4.0 ms), all 20 reads |
| **0x01141000 (the capture)** | 139.5 | **79.9** | **115.9 at +0.25 s, 138.8 at +0.52 s, ~135.5 to +5.1 s** | **40 (4.0 ms), all 20 reads** |
| 0x00141000 (not the capture) | 135.5 | 135.6 | 135.5-135.7 | 40 (4.0 ms), all 20 reads |

- On the streamed camera, AE restored brightness within about 0.5 s, so its real exposure
  moved well above 4 ms. Yet exposure GET_CUR read 4.0 ms throughout. **In auto mode, the
  camera's exposure read is the stored manual value, not AE's live exposure.** Gain read 0
  throughout too, so the read does not show whether AE used gain either.
- So goal 1 cannot be met through the UVC exposure control on this camera. Possible routes
  not tried: (a) the Sonix extension unit (id 3, guid 7033f028...) may expose the OV2710's
  own exposure registers; that is undocumented, and writes there could misconfigure the
  sensor. (b) Brightness and fps measured from the frames. (c) Run in manual mode (goal 3),
  where exposure is known by construction.
- fps values of 25.9-26.3 in some 0.25 s windows are window quantization (7 frames instead
  of 8 per 0.25 s), not frame drops. The 2 s windows read 29.3-29.8 throughout, with 0
  failed reads.
- **Restore failed (probe bug).** The restore wrote exposure:=157 *after* the hand-back to
  auto, and the camera stalled it (`USBError errno=32 Pipe error`) on all three cameras.
  The restore's read-back checked only ae_mode (8, "OK"), which hid the failure. The final
  read-back shows exposure 40 on all three. Fixed: restores now select manual, write
  exposure and gain defaults, then AE mode, and read back every control.

### Q2: persist run 1 (2026-11-19 09:30:22; stream 640x480 @ 30, from the Desktop config, not the user's format)

`uvc/.venv/bin/python uvc/probe.py persist` (`uvc/logs/persist-run1.log`).

1. Auto baseline: index 0 streamed at 640x480 @ 30 (requested, reported and measured), mean
   brightness 140.5. Then the stream was released.
2. With **no stream open**, all three cameras were set to ae_mode 1, exposure 40 (4.0 ms)
   and gain 10. All read back MATCHES.
3. **Stream start:** the capture was reopened as the recorder opens it (1061 ms). All three
   cameras read back ae_mode 1 / exposure 40 / gain 10, MATCHES, right after the open and
   again after 4 s of streaming. The streamed image measured 89.5 vs the 140.5 auto
   baseline: darker, so the manual exposure was in force on the capture. That fits gain
   10's 88.2 in gainsweep run 1. There were 0 failed reads, at 29.6 fps.
4. **Stream stop:** all three cameras still read MATCHES after the release.
5. **Unplug/replug: not tested.** No camera disappeared within the 120 s wait, so the step
   was skipped.
6. Restore: all three cameras restored to defaults, read back OK.

**Q2, partial answer: YES, the manual settings survive stream start and stream stop.**
They were set by USB location before any capture existed, and AVFoundation's open did not
reset them. Re-enumeration after an unplug is still untested.

### Goal 3: gainsweep run 1 (2026-11-19 09:27:16; stream 640x480 @ 30, from the Desktop config, not the user's format)

`uvc/.venv/bin/python uvc/probe.py gainsweep` (`uvc/logs/gainsweep-run1.log`). Requested,
reported and measured 640x480 @ 30. On each camera while index 0 streamed:
1. 2 s of auto brightness as the reference.
2. Manual + exposure 40 (4.0 ms).
3. Gain 0 to 100 in steps of 10. At each step: write, read back, discard 0.5 s, measure 1 s.
4. Restore to defaults and read back.

Every gain write (33 of them, 5.5-14.8 ms each) read back equal to the value written.

On the capture (0x01141000; auto reference 139.2), mean brightness at exposure 4.0 ms:

| gain | 0 | 10 | 20 | 30 | 40 | **50** | 60 | 70 | 80 | 90 | 100 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| brightness | 75.6 | 88.2 | 101.2 | 114.0 | 127.0 | **139.2** | 151.7 | 161.7 | 171.0 | 179.7 | 188.5 |

- **At 4.0 ms, gain 50 reproduces the auto brightness (139.2 vs 139.2) in this room's
  light.** Brightness rises about 12.5 per 10 gain up to 60, then flattens to about 9 per
  10. No step saturated (max 188.5).
- fps was 29.2-30.3 at every step with 0 failed reads: neither manual exposure nor gain
  affected the frame rate.
- The other two cameras stayed flat (139.0-139.1 and 138.1-138.4) because they were not
  the capture. Their gain writes were accepted and read back.
- Limits of this result:
  - It holds for the room's lighting at 09:27, not for the dim light that causes the
    problem. Gain 100 gave 2.5x the gain-0 brightness, so at 4 ms in light more than about
    2.5x dimmer than gain 0 needs, the image will be darker than auto makes it. A fixed
    setting for dim conditions has to be measured in those conditions.
  - The cost in image noise of high gain was not measured: the probe records mean
    brightness only.

### Q1, run 3 (2026-11-19 09:12:45; stream 640x480 @ 30, from the Desktop config, not the user's format)

`uvc/.venv/bin/python uvc/probe.py q1` (`uvc/logs/q1-run3.log`). It ran straight after
aetrack, so every camera started with stored exposure 40 (the leftover above).

- The capture opened as the recorder opens it: 1171 ms; requested, reported and measured
  640x480 @ 30. Warm-up was 89 frames in 3.0 s (29.6 fps).
- All reads and writes succeeded with and without the stream (0.6-1.1 ms, writes 4.4-6.0
  ms, a few 20-27 ms on the streamed camera). There were 0 failed reads in every window.
- On the capture, still 0x01141000, so the index mapping is unchanged at 640x480: manual
  4.0 ms gave brightness 140.3 to 78.6 (-44%), then 130.7 two seconds after restore. The
  other two cameras stayed flat because they were not the capture.
- AE priority: 0 before and during the stream; the write of 0 was a no-op.
- The restore wrote exposure "back" to 40, the leftover it found, so the rig stayed at
  exposure 40 (see Restored).
- -44% here vs -68% at 1920x1080: the response to the same 4 ms differs by format. Since
  the live AE exposure cannot be read, the cause (different AE operating point, sensor
  binning) cannot be separated. So the gain compensation must be measured at 640x480.

### Q1, run 1 (2026-11-18 16:56:59, user's Terminal, not sandboxed, not root; stream 1920x1080 @ 30, camera default)

Command (full output in `uvc/logs/q1-run1.log`):

```
uvc/.venv/bin/python uvc/probe.py q1 --index 0
```

- Descriptors (read without opening the device) are identical on all three targets. They
  show VC interface 0, camera terminal id 1 (bmControls 0xe), processing unit id 2
  (bmControls 0x177f) and extension unit id 3 (guid 7033f02811632e4aba2c6890eb334016).
  The AE mode, AE priority, exposure time (absolute) and gain are all **advertised**. This
  is partial evidence for Q3: advertised does not yet mean writable, and no ranges were read.
- Phase A, no stream open: every class request (GET_INFO, GET_CUR on all four controls, all
  three cameras) failed with `USBError errno=13 code=-3: Access denied (insufficient
  permissions)` (LIBUSB_ERROR_ACCESS) within 0.1-0.2 ms. That is too fast to have reached
  the camera, so libusb/IOKit refused locally.
- Phase B, `cv2.VideoCapture(0, CAP_AVFOUNDATION)`: opened in 740 ms, reported 1920x1080 @
  30 fps, and streamed 89 frames in 3.0 s (29.6 fps, 0 failed reads, mean brightness 132.1).
  No permission dialog was reported. The same requests failed with the same error (0.2-0.6 ms).
- Phase C, stream released: same error.
- No SET_CUR was sent: the script only writes after a successful read.
- Method note: no viewer runs during probes. Visual effects are measured as numbers in the
  output (per-window frame count, failed reads, fps, mean frame brightness). With only
  target cameras attached, which index maps to which USB port does not matter here.

### Diagnostic: controls with LIBUSB_DEBUG=4 (2026-11-18 17:08:59, user's Terminal, not root)

Command (full output in `uvc/logs/controls-debug.log`):

```
LIBUSB_DEBUG=4 uvc/.venv/bin/python uvc/probe.py controls
```

- `darwin_open: device open for access` on all three cameras: opening the device succeeds.
- 72 lines (3 cameras x 4 controls x 6 GET requests) of
  `darwin_claim_interface: USBInterfaceOpen: another process has device opened for
  exclusive access`, and each request then fails with LIBUSB_ERROR_ACCESS.
- Cause: pyusb's `Device.ctrl_transfer` claims the interface for any interface-recipient,
  non-vendor request (`usb/core.py` line 1091, `managed_claim_interface`). The UVC requests
  never reached libusb's control-transfer path. So run 1 says nothing about macOS blocking
  control requests, and the earlier "macOS 12+ needs root" guess is withdrawn.
- Correction to the probe's safety claim: run 1 and this diagnostic *did* attempt interface
  claims, contrary to the script's docstring. Every claim failed, so no ownership changed
  and the stream in run 1 was unaffected (0 failed reads). The probe now calls the libusb
  backend directly on the device handle, with no claim.

### Goal 1 follow-up: track run 1 (2026-11-19 08:56:53; stream 1920x1080 @ 30, camera default), inconclusive

`uvc/.venv/bin/python uvc/probe.py track` (`uvc/logs/track-run1.log`). This was read-only.
It read every second for 40 s while streaming index 0, with cues to cover all lenses at
10 s and uncover at 25 s.

- Exposure read 157 (15.7 ms), gain 0 and AE mode 8 on all three cameras in every one of
  the 40 reads.
- Stream brightness stayed 136-138 through the "covered" window. There were one-second
  dips to 128.6 (t=16 s) and 116.3 (t=31 s), each recovering within about 3 s. fps was
  29.1-30.3 with 0 failed reads throughout.
- Since the streamed image never went dark for long, the run cannot tell "the read does
  not track AE" from "the light at the streamed camera barely changed". The dips do look
  like AE correcting a brief change while the read stayed at 15.7 ms, which is suggestive
  but not proof.
- Next: `probe.py aetrack` makes the change itself. It darkens with manual 4 ms, hands
  back to auto with the stored value left at 4 ms, and reads exposure 4x/s while
  brightness recovers.

## Installed

- `uvc/.venv`: a Python 3.13.5 venv with pyusb 1.3.1 (installed by the user; my sandboxed
  `pip install` failed on the proxy certificate). cv2 4.11.0 is borrowed from the base
  interpreter's site-packages at runtime, not installed.
- libusb: `/opt/homebrew/lib/libusb-1.0.dylib`, pre-installed by the user.

## Restored

- q1 run 1: nothing changed; no SET_CUR was sent.
- q1 run 2, on all three cameras:
  - ae_priority was written 0 (it already was 0), restored to 0, and read back 0, OK.
  - ae_mode was written 1 and restored to 8 (aperture-priority, auto), read back 8, OK.
  - exposure was written 40 and restored to 157, read back 157 after the stream closed.
  - gain was not written.
  The final state of all three (ae_mode 8, ae_priority 0, exposure 157, gain 0) equals the
  starting state.
- track run 1: read-only, nothing changed.
- **aetrack run 1: restore FAILED for exposure.** ae_mode was restored to 8 and read back
  OK. exposure:=157 stalled because the camera was already in auto, so all three cameras
  were left with stored exposure **40 (4.0 ms)**. gain and ae_priority were never changed.
- **q1 run 3** started from that leftover: it restored ae_mode 8 (OK) and ae_priority 0
  (OK), and wrote exposure back to 40, the value it found.
- Between aetrack run 1 (09:12) and restore run 1 (09:21), the rig was at ae_mode 8 (auto),
  ae_priority 0, gain 0, stored exposure 40. The auto loop ignored the stored exposure:
  brightness re-converged normally in aetrack and q1 run 3. No recording ran in that window
  (`~/Desktop/temp` was absent at both runs' checks).
- **restore run 1 (2026-11-19 09:21:33, `uvc/logs/restore-run1.log`)**, with the fixed
  restore, on each of the three cameras: ae_mode:=1, then exposure:=157 (accepted, 4.5 ms),
  then gain:=0 (accepted, 5.7 ms; the value was already 0), then ae_mode:=8. Read back:
  exposure 157 OK, gain 0 OK, ae_mode 8 OK, ae_priority 0. The script reported "restore OK
  on all targets". **The rig is back to its starting state** (ae_mode 8, ae_priority 0,
  exposure 157, gain 0) on all three cameras.
- **gainsweep run 1 (2026-11-19 09:27:16)**: on each camera, ae_mode, exposure and gain were
  changed (gain 0-100). Each was restored straight after its sweep: manual, exposure 157,
  gain 0, then ae_mode 8, with read-back OK on all three. After the stream was released,
  the final read-back on all three cameras matched the defaults (ae_mode 8, exposure 157,
  gain 0). ae_priority was not touched.
- **persist run 1 (2026-11-19 09:30:22)**: all three cameras set to ae_mode 1, exposure 40,
  gain 10 with no stream open. They were held through stream start and stop, then restored
  at 09:32:34: manual, exposure 157, gain 0, then ae_mode 8, read back OK on all three
  ("restore OK on all targets present"). ae_priority was not touched.
