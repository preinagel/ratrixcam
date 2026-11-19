# UVC exposure/gain control on macOS: prototype report

Status: **blocked before any camera was touched.** No control request has been sent to any
camera, so nothing needed restoring.

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

1. UVC requests while AVFoundation streams: not yet tested.
2. Settings persistence across stream start and re-enumeration: not yet tested.
3. Exposure mode, exposure time and gain as writable controls: not yet tested.

## Installed

- `uvc/.venv`: a Python 3.13.5 venv, currently empty (the pyusb install failed).

## Restored

Nothing; no camera control was changed.
