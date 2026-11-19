# UVC exposure/gain control on macOS: prototype report

Status: Q1 run 1 refused, but the refusal was traced to the probe (pyusb's implicit
interface claim); run 2 pending. No control has been changed on any
camera, so nothing has needed restoring.

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

## Answers

1. **UVC requests while AVFoundation streams: open again.** Run 1's refusals were caused by
   the probe itself. pyusb tried to claim the VideoControl interface before each request,
   and macOS refused because Apple's UVC driver holds it exclusively (controls-debug run). The
   probe now sends requests on the open device handle without claiming; run 2 tests that.
2. Settings persistence across stream start and re-enumeration: not yet tested.
3. Exposure mode, exposure time and gain as writable controls: not yet tested.

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
