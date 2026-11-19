#!/usr/bin/env python3
"""UVC control probe for the Arducam OV2710 camera (Sonix controller, USB 0x0c45:0x6369).

RUN THIS (in your own Terminal, from anywhere; not inside the Claude sandbox):

    cd ~/Desktop/ratrixcam-uvc && LIBUSB_DEBUG=3 uvc/.venv/bin/python uvc/probe.py controls 2>&1 | tee uvc/logs/controls-run2.log && LIBUSB_DEBUG=3 uvc/.venv/bin/python uvc/probe.py q1 --index 0 2>&1 | tee uvc/logs/q1-run2.log

Sub-commands:
    list      enumerate 0x0c45:0x6369 devices and parse their VideoControl descriptors
    controls  read every GET_* of the exposure/gain controls, no stream open
    q1        question 1: control requests before and while a cv2.VideoCapture streams;
              changes AE mode and exposure time on each target in turn, watches the
              stream's brightness, then restores AE mode to auto and reads it back

Safety: refuses to run if ~/Desktop/temp exists (a recording is in progress). Never claims an
interface, detaches a driver or sets a configuration: only class requests on endpoint 0,
sent on the device handle without pyusb's implicit interface claim (run 1 did claim; see
REPORT.md).
Devices are selected by vendor and product id only.
"""

import argparse
import array
import os
import sys
import threading
import time

VID, PID = 0x0C45, 0x6369
LIBUSB = "/opt/homebrew/lib/libusb-1.0.dylib"
RECORDING_FLAG = os.path.expanduser("~/Desktop/temp")

# UVC 1.1 constants
CS_INTERFACE = 0x24
VC_INPUT_TERMINAL, VC_PROCESSING_UNIT, VC_EXTENSION_UNIT = 0x02, 0x05, 0x06
ITT_CAMERA = 0x0201
SET_CUR = 0x01
GET_CUR, GET_MIN, GET_MAX, GET_RES, GET_LEN, GET_INFO, GET_DEF = 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87
REQ_NAME = {GET_CUR: "CUR", GET_MIN: "MIN", GET_MAX: "MAX", GET_RES: "RES",
            GET_LEN: "LEN", GET_INFO: "INFO", GET_DEF: "DEF", SET_CUR: "SET_CUR"}

# name -> (entity kind, selector, length, signed, bmControls bit, unit note)
CONTROLS = {
    "ae_mode":     ("CT", 0x02, 1, False, 1, "bitmap 1=manual 2=auto 4=shutter-prio 8=aperture-prio"),
    "ae_priority": ("CT", 0x03, 1, False, 2, "0=constant frame rate, 1=frame rate may vary"),
    "exposure":    ("CT", 0x04, 4, False, 3, "units of 100 us"),
    "gain":        ("PU", 0x04, 2, False, 9, "unitless"),
}
AE_MANUAL, AE_AUTO, AE_APERTURE = 0x01, 0x02, 0x08


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def guard():
    if os.path.exists(RECORDING_FLAG):
        sys.exit(f"REFUSED: {RECORDING_FLAG} exists, a recording may be in progress.")
    print(f"[{now()}] recording check: {RECORDING_FLAG} absent, ok")


# ---------------------------------------------------------------- USB side

def usb_backend():
    import usb.backend.libusb1 as libusb1
    be = libusb1.get_backend(find_library=lambda _name: LIBUSB)
    if be is None:
        sys.exit(f"libusb backend not loaded from {LIBUSB}")
    return be


def find_targets(be):
    import usb.core
    return list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID, backend=be))


def location_id(dev):
    loc = (dev.bus or 0) << 24
    for i, port in enumerate(dev.port_numbers or ()):
        loc |= port << (20 - 4 * i)
    return loc


def label(dev):
    return f"loc 0x{location_id(dev):08x} (bus {dev.bus} addr {dev.address} ports {dev.port_numbers})"


def vc_layout(dev):
    """Parse the VideoControl interface's class-specific descriptors (first configuration)."""
    out = {"ifnum": None, "CT": None, "PU": None, "CT_bits": 0, "PU_bits": 0, "XU": []}
    cfg = dev[0]
    for intf in cfg:
        if intf.bInterfaceClass == 0x0E and intf.bInterfaceSubClass == 0x01:
            out["ifnum"] = intf.bInterfaceNumber
            ex = bytes(intf.extra_descriptors)
            i = 0
            while i + 2 < len(ex):
                blen, dtype, sub = ex[i], ex[i + 1], ex[i + 2]
                if blen == 0:
                    break
                d = ex[i:i + blen]
                if dtype == CS_INTERFACE:
                    if sub == VC_INPUT_TERMINAL and int.from_bytes(d[4:6], "little") == ITT_CAMERA:
                        size = d[14]
                        out["CT"], out["CT_bits"] = d[3], int.from_bytes(d[15:15 + size], "little")
                    elif sub == VC_PROCESSING_UNIT:
                        size = d[7]
                        out["PU"], out["PU_bits"] = d[3], int.from_bytes(d[8:8 + size], "little")
                    elif sub == VC_EXTENSION_UNIT:
                        out["XU"].append((d[3], d[4:20].hex()))
                i += blen
            break
    return out


def xfer(dev, lay, name, req, value=None):
    """One class request. Returns (int value or None, error string or None, milliseconds)."""
    import usb.core
    kind, sel, length, signed, _bit, _unit = CONTROLS[name]
    entity = lay[kind]
    if entity is None:
        return None, f"no {kind} entity in descriptors", 0.0
    if req == GET_INFO:
        length = 1
    elif req == GET_LEN:
        length = 2
    w_value, w_index = sel << 8, (entity << 8) | lay["ifnum"]
    # Not dev.ctrl_transfer(): for interface-recipient class requests pyusb first claims the
    # interface (usb/core.py, managed_claim_interface), which macOS refuses because Apple's UVC
    # driver holds it exclusively. Go to the backend on the open device handle instead; libusb
    # sends endpoint-0 requests through the device, with no interface claim.
    ctx = dev._ctx
    t0 = time.perf_counter()
    try:
        ctx.managed_open()
        if req == SET_CUR:
            data = array.array("B", int(value).to_bytes(length, "little", signed=signed))
            ctx.backend.ctrl_transfer(ctx.handle, 0x21, SET_CUR, w_value, w_index, data, 1000)
            res = value
        else:
            buf = array.array("B", bytes(length))
            n = ctx.backend.ctrl_transfer(ctx.handle, 0xA1, req, w_value, w_index, buf, 1000)
            res = int.from_bytes(bytes(buf[:n]), "little", signed=signed)
        return res, None, (time.perf_counter() - t0) * 1000
    except usb.core.USBError as e:
        return None, f"USBError errno={e.errno} code={e.backend_error_code}: {e}", (time.perf_counter() - t0) * 1000


def fmt(res, err, ms):
    return f"ERR {err} [{ms:.1f} ms]" if err else f"{res} [{ms:.1f} ms]"


def info_bits(v):
    if v is None:
        return ""
    names = ["get", "set", "disabled-by-auto", "autoupdate", "async"]
    return "(" + ",".join(n for b, n in enumerate(names) if v >> b & 1) + ")"


def print_layout(dev, lay):
    print(f"  {label(dev)}  manufacturer/product strings not read (would open the device)")
    print(f"  VC interface {lay['ifnum']}; camera terminal id {lay['CT']} bmControls 0x{lay['CT_bits']:x}; "
          f"processing unit id {lay['PU']} bmControls 0x{lay['PU_bits']:x}")
    for name, (kind, _sel, _len, _s, bit, unit) in CONTROLS.items():
        adv = (lay[f"{kind}_bits"] >> bit) & 1
        print(f"    {name:12s} {kind} bit {bit}: {'advertised' if adv else 'NOT advertised'}  ({unit})")
    for uid, guid in lay["XU"]:
        print(f"    extension unit id {uid} guid {guid}")


def read_all(dev, lay, name, reqs=(GET_INFO, GET_MIN, GET_MAX, GET_RES, GET_DEF, GET_CUR)):
    parts = []
    for req in reqs:
        res, err, ms = xfer(dev, lay, name, req)
        extra = info_bits(res) if req == GET_INFO and not err else ""
        parts.append(f"{REQ_NAME[req]}={fmt(res, err, ms)}{extra}")
    print(f"    {name:12s} " + "  ".join(parts))


def auto_mode_for(dev, lay):
    """The auto AE mode this camera supports, from GET_RES (aperture priority first, as webcams use)."""
    res, err, _ = xfer(dev, lay, "ae_mode", GET_RES)
    if err or res is None:
        return AE_APERTURE
    for mode in (AE_APERTURE, AE_AUTO):
        if res & mode:
            return mode
    return AE_APERTURE


# ---------------------------------------------------------------- cv2 side

def import_cv2():
    try:
        import cv2
    except ImportError:
        # cv2 lives in the base interpreter's site-packages, not in this venv; append (venv wins).
        ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
        sys.path.append(os.path.join(sys.base_prefix, "lib", ver, "site-packages"))
        import cv2
    return cv2


class Reader(threading.Thread):
    def __init__(self, cap):
        super().__init__(daemon=True)
        self.cap, self.samples, self.stop_evt = cap, [], threading.Event()

    def run(self):
        while not self.stop_evt.is_set():
            ok, frame = self.cap.read()
            self.samples.append((time.monotonic(), ok, float(frame.mean()) if ok else None))

    def window(self, t0, t1):
        s = [x for x in self.samples if t0 <= x[0] < t1]
        good = [x for x in s if x[1]]
        fps = (len(good) - 1) / (good[-1][0] - good[0][0]) if len(good) > 1 and good[-1][0] > good[0][0] else 0.0
        mean = sum(x[2] for x in good) / len(good) if good else float("nan")
        return len(good), len(s) - len(good), fps, mean


def watch(reader, label_, seconds):
    t0 = time.monotonic()
    time.sleep(seconds)
    n, bad, fps, mean = reader.window(t0, time.monotonic())
    print(f"    stream [{label_}] {seconds:.1f}s: frames={n} failed_reads={bad} fps={fps:.1f} mean_brightness={mean:.1f}")
    return mean


# ---------------------------------------------------------------- sub-commands

def cmd_list(_args):
    be = usb_backend()
    devs = find_targets(be)
    print(f"[{now()}] {len(devs)} device(s) with 0x{VID:04x}:0x{PID:04x}")
    for dev in devs:
        print_layout(dev, vc_layout(dev))


def cmd_controls(_args):
    guard()
    be = usb_backend()
    devs = find_targets(be)
    print(f"[{now()}] {len(devs)} target(s); no stream open")
    import usb.util
    for dev in devs:
        lay = vc_layout(dev)
        print_layout(dev, lay)
        for name in CONTROLS:
            read_all(dev, lay, name)
        usb.util.dispose_resources(dev)


def cmd_q1(args):
    guard()
    import usb.util
    be = usb_backend()
    devs = find_targets(be)
    print(f"[{now()}] {len(devs)} target(s) 0x{VID:04x}:0x{PID:04x}")
    if not devs:
        sys.exit("no target camera found")
    lays = [vc_layout(d) for d in devs]
    for dev, lay in zip(devs, lays):
        print_layout(dev, lay)

    print(f"\n[{now()}] PHASE A: no stream open")
    original = []
    for dev, lay in zip(devs, lays):
        print(f"  {label(dev)}")
        for name in CONTROLS:
            read_all(dev, lay, name, (GET_INFO, GET_CUR))
        mode, err, _ = xfer(dev, lay, "ae_mode", GET_CUR)
        expo, _e2, _ = xfer(dev, lay, "exposure", GET_CUR)
        original.append((mode, expo))
        if err is None:
            print(f"    no-op write ae_mode:=current {mode}: {fmt(*xfer(dev, lay, 'ae_mode', SET_CUR, mode))}")

    cv2 = import_cv2()
    print(f"\n[{now()}] PHASE B: cv2 {cv2.__version__} VideoCapture({args.index}, CAP_AVFOUNDATION)")
    t0 = time.perf_counter()
    cap = cv2.VideoCapture(args.index, cv2.CAP_AVFOUNDATION)
    print(f"  isOpened={cap.isOpened()} backend={cap.getBackendName() if cap.isOpened() else '-'} "
          f"open took {(time.perf_counter() - t0) * 1000:.0f} ms")
    if not cap.isOpened():
        print("  Capture did not open. If macOS showed a camera-permission dialog for Terminal, allow it and rerun.")
        return
    print(f"  reported {cap.get(cv2.CAP_PROP_FRAME_WIDTH):.0f}x{cap.get(cv2.CAP_PROP_FRAME_HEIGHT):.0f} "
          f"@ {cap.get(cv2.CAP_PROP_FPS):.1f} fps")
    reader = Reader(cap)
    reader.start()
    changed = []  # (dev, lay, mode_to_restore, exposure_to_restore)
    try:
        watch(reader, "warm-up", 3.0)
        for (dev, lay), (mode0, expo0) in zip(zip(devs, lays), original):
            print(f"  {label(dev)} while streaming")
            for name in CONTROLS:
                read_all(dev, lay, name, (GET_INFO, GET_CUR))
            mode, err, _ = xfer(dev, lay, "ae_mode", GET_CUR)
            if err:
                print("    ae_mode unreadable while streaming; skipping writes on this device")
                continue
            print(f"    no-op write ae_mode:=current {mode}: {fmt(*xfer(dev, lay, 'ae_mode', SET_CUR, mode))}")
            emin, e_err, _ = xfer(dev, lay, "exposure", GET_MIN)
            if e_err:
                print("    exposure GET_MIN failed; skipping the manual-exposure test on this device")
                continue
            restore_mode = mode0 if mode0 in (AE_AUTO, AE_APERTURE) else auto_mode_for(dev, lay)
            restore_expo = expo0 if expo0 is not None else None
            before = watch(reader, "before change", 1.5)
            changed.append((dev, lay, restore_mode, restore_expo))
            print(f"    write ae_mode:=manual(1): {fmt(*xfer(dev, lay, 'ae_mode', SET_CUR, AE_MANUAL))}")
            print(f"    write exposure:=min({emin}): {fmt(*xfer(dev, lay, 'exposure', SET_CUR, emin))}")
            read_all(dev, lay, "ae_mode", (GET_CUR,))
            read_all(dev, lay, "exposure", (GET_CUR,))
            during = watch(reader, "manual, min exposure", 2.0)
            restore_one(dev, lay, restore_mode, restore_expo)
            changed.pop()
            after = watch(reader, "after restore", 2.0)
            drop = (before - during) / before if before and before == before else 0.0
            verdict = "write changed the stream" if drop > 0.2 else "no stream change (not the streamed camera, or no effect)"
            print(f"    brightness before/during/after = {before:.1f}/{during:.1f}/{after:.1f} "
                  f"(drop {drop * 100:.0f}%): {verdict}")
    finally:
        for dev, lay, m, e in changed:
            restore_one(dev, lay, m, e)
        reader.stop_evt.set()
        reader.join(timeout=3)
        cap.release()

    print(f"\n[{now()}] PHASE C: stream released; final read-back")
    for dev, lay in zip(devs, lays):
        print(f"  {label(dev)}")
        for name in CONTROLS:
            read_all(dev, lay, name, (GET_CUR,))
        usb.util.dispose_resources(dev)
    print(f"[{now()}] done")


def restore_one(dev, lay, mode, expo):
    if expo is not None:
        print(f"    restore exposure:={expo}: {fmt(*xfer(dev, lay, 'exposure', SET_CUR, expo))}")
    print(f"    restore ae_mode:={mode} (auto): {fmt(*xfer(dev, lay, 'ae_mode', SET_CUR, mode))}")
    res, err, ms = xfer(dev, lay, "ae_mode", GET_CUR)
    ok = "OK" if res == mode else "MISMATCH"
    print(f"    read-back ae_mode={fmt(res, err, ms)} {ok}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("controls").set_defaults(fn=cmd_controls)
    q1 = sub.add_parser("q1")
    q1.add_argument("--index", type=int, default=0, help="cv2 camera index to stream (any index is a target)")
    q1.set_defaults(fn=cmd_q1)
    args = ap.parse_args()
    print(f"[{now()}] probe.py {args.cmd}  python {sys.version.split()[0]}  pid {os.getpid()}")
    args.fn(args)


if __name__ == "__main__":
    main()
