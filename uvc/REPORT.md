# UVC exposure/gain control on macOS: prototype report

Status: reads work without root (goal 1, no stream open). Q1 run 1's refusal was traced
to the probe (pyusb's implicit interface claim) and fixed. q1 run 2, the first while-streaming
reads and first writes, is pending. No control has been changed on any
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

1. **UVC requests while AVFoundation streams: open again.** Run 1's refusals were caused by
   the probe itself. pyusb tried to claim the VideoControl interface before each request,
   and macOS refused because Apple's UVC driver holds it exclusively (controls-debug run). The
   probe now sends requests on the open device handle without claiming; run 2 tests that.
2. Settings persistence across stream start and re-enumeration: not yet tested.
3. **Exposed and reported settable: yes. Actually writable: not yet shown.** AE mode
   (manual or aperture-priority), exposure time absolute (0.1-500 ms in 0.1 ms steps,
   default 15.7 ms) and gain (0-100, unitless, step 1, default 0) all report get+set in
   GET_INFO; ranges are in the goal 1 table. No SET_CUR has been sent yet; q1 run 2 is the
   first write.

## Evidence

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

## Installed

- `uvc/.venv`: a Python 3.13.5 venv with pyusb 1.3.1 (installed by the user; my sandboxed
  `pip install` failed on the proxy certificate). cv2 4.11.0 is borrowed from the base
  interpreter's site-packages at runtime, not installed.
- libusb: `/opt/homebrew/lib/libusb-1.0.dylib`, pre-installed by the user.

## Restored

Nothing; no camera control was changed (q1 run 1 sent no SET_CUR: every read was refused).
