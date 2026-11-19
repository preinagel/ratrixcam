# UVC exposure/gain control on macOS: prototype report

Status: Q1 answered yes (q1 run 2). Reads and writes work without root, with and without
an AVFoundation stream. Open: whether the exposure read tracks auto-exposure (goal 1),
gain writes, and Q2 (stream start, re-enumeration). No control has been changed on any
camera, so nothing has needed restoring.

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

## Goal 1 (read exposure and gain): YES, with no stream open, as an ordinary user

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
- AE priority already reads 0 with no stream open. If it stays 0 while streaming, the
  frame-rate drops the recorder sees are not explained by this control: either the firmware
  ignores it, or something sets it to 1 at stream start. q1 run 2 reads it while streaming.
- Exposure's INFO does not set the "disabled due to automatic mode" bit (D2) while AE mode
  is auto (8), even though the camera chooses the value itself.
- Exposure 15.7 ms equals the default. With no stream open the value may be stale; the
  while-streaming reads in q1 will show whether it tracks the scene.

## Answers

1. **UVC requests while AVFoundation streams: YES**, as an ordinary user, with no root. In q1
   run 2, cv2 streamed index 0 at 1920x1080 / ~29.7 fps. Reads and writes succeeded on all
   three cameras, including the streamed one, and the streamed camera's image responded to
   the write (brightness -68%) with 0 failed reads and no fps change. Run 1's refusals were
   caused by the probe itself: pyusb's implicit interface claim, now removed.
2. Settings persistence across stream start and re-enumeration: not yet tested.
3. **Yes for AE mode and exposure time; gain is reported settable but not yet written.**
   AE mode (manual or aperture-priority) and exposure time absolute (0.1-500 ms in 0.1 ms
   steps, default 15.7 ms) were written and read back in q1 run 2, with a visible effect on
   the image. Gain (0-100, unitless, step 1, default 0) reports get+set in GET_INFO but has
   not been written. Ranges are in the goal 1 table.

## Evidence

### Q1, run 2 (2026-11-19 08:45:26, user's Terminal, not root)

`uvc/.venv/bin/python uvc/probe.py q1 --index 0` (full output in `uvc/logs/q1-run2.log`).

- No stream: all reads OK (0.7-1.0 ms). The no-op write `ae_mode:=8` was accepted (0.7-0.9 ms).
- `VideoCapture(0, CAP_AVFOUNDATION)` opened in 672 ms and reported 1920x1080 @ 30 fps. The
  warm-up was 88 frames in 3.0 s (29.3 fps, 0 failed reads).
- While streaming, on each camera in turn:

| Camera (location) | ae_priority:=0 | ae_mode:=manual, exposure:=4.0 ms, read back | stream brightness before / during / 2 s after restore | fps during |
|---|---|---|---|---|
| 0x01143000 | accepted, read back 0 | 1 / 40 | 142.0 / 142.0 / 142.0 | 29.7 |
| **0x01141000 (the streamed one)** | accepted, read back 0 | 1 / 40 | **142.3 / 45.2 / 127.1 (-68%)** | 29.6 |
| 0x00141000 | accepted, read back 0 | 1 / 40 | 131.2 / 131.6 / 131.9 | 29.6 |

  Brightness drops by -68% where 4.0/15.7 ms predicts about -75% for a linear sensor.
  After the restore it had recovered to 127.1 within 2 s, as auto-exposure re-converged.
  Writes took 4.5-4.9 ms; a few on the streamed camera took 20-27 ms. No frame was lost
  in any window.
- AE priority read 0 before the stream and while streaming: stream start does not set it
  to 1. The write of 0 was a no-op, so a change to AE priority has not been demonstrated.
- After the stream was released, all three cameras read ae_mode 8, ae_priority 0,
  exposure 157 and gain 0, identical to Phase A.

**Open problem for goal 1: in auto mode, exposure GET_CUR may not be the camera's live
exposure.** It read 157 (15.7 ms) on all three cameras, with and without a stream, in
scenes of different brightness (131 vs 142). That is exactly the GET_DEF value. So in
aperture-priority mode the camera may return the stored manual value, not the exposure
its AE loop is actually using. If so, a read cannot warn of long exposures. `probe.py
track` tests this by reading exposure every 0.5 s while the light changes.

### Q1, run 1 (2026-11-18 16:56:59, user's Terminal, not sandboxed, not root)

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

### Goal 1 follow-up: track run 1 (2026-11-19 08:56:53), inconclusive

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
