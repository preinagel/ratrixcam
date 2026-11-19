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

## Blockers

1. **Recording check cannot be run from the sandbox.** `pgrep -fl ratrixcam` fails with
   "pgrep: Cannot get process list" (exit 3; sysmond is not reachable), so an empty result
   cannot be obtained. `~/Desktop/temp` does not exist.
2. **pyusb install failed.** `pip install pyusb` in `uvc/.venv` fails with
   `SSLCertVerificationError ... unable to get local issuer certificate` against pypi.org
   through the sandbox proxy. Not worked around (disabling TLS verification was not tried).
3. **libusb not found** in /usr/local/lib, /usr/local/include or /opt/local/lib.
4. `system_profiler SPCameraDataType` printed nothing from inside the sandbox.

## Answers

1. UVC requests while AVFoundation streams: not yet tested.
2. Settings persistence across stream start and re-enumeration: not yet tested.
3. Exposure mode, exposure time and gain as writable controls: not yet tested.

## Installed

- `uvc/.venv`: a Python 3.13.5 venv, currently empty (the pyusb install failed).

## Restored

Nothing; no camera control was changed.
