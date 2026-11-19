#!/usr/bin/env python3
"""UVC control probe for the Arducam OV2710 camera (Sonix controller, USB 0x0c45:0x6369).

RUN THIS (in your own Terminal, from anywhere; not inside the Claude sandbox):

    cd ~/Desktop/ratrixcam-uvc && uvc/.venv/bin/python uvc/probe.py persist 2>&1 | tee uvc/logs/persist-run2.log

    (when told, unplug the kind A camera on port 3 or port 5, not port 1, and replug it into the same port)

Sub-commands:
    list      read-only, no device opened: full descriptors of every 0x0c45 camera (all
              products), with formats, frame sizes and rates, grouped by fingerprint
    controls  read every GET_* of the exposure/gain controls, no stream open; target only,
              or every 0x0c45 camera with --all-vendor (GET requests only)
    All other sub-commands act on the target product 0x6369 only.
    q1        question 1: control requests before and while a cv2.VideoCapture streams.
              On each target in turn, while streaming: reads every control (goal 1);
              clears AE priority, then restores it (goal 2); sets manual exposure at
              4 ms, then restores auto (goal 3 preview). Every write is restored and
              read back; fps and mean brightness are printed per window.
    track     read-only: stream index 0 and read exposure/gain/AE mode every second while
              you cover and uncover the lenses; shows whether the reads follow auto-exposure
    aetrack   on each target while index 0 streams: manual 4 ms, then back to auto with the
              exposure value left at 4 ms; reads exposure 4x/s while AE re-converges.
              Restores exposure and AE mode and reads them back.
    gainsweep goal 3: on each target while index 0 streams, manual 4 ms and gain stepped
              through its range; brightness per step vs the auto brightness just before
    persist   question 2: manual 4 ms + gain 10 set with no stream; read back after stream
              start, stream stop, and a cued unplug/replug of one camera (polls for it)
    restore   safety net: exposure, gain and AE mode to each camera's own defaults

Every sub-command that writes restores each target to its defaults in a finally block
(manual first, then exposure and gain defaults, then AE mode auto) and reads them back.

Streaming sub-commands (q1, track, aetrack, gainsweep, persist) open the capture as the recorder does
(ratrixcam_server.py:487-492): VideoCapture(index) on the default backend, then width,
height and fps from the defaults in uvc/camera_config_5.json (the probe's copy of the
user's config, set to 640x480 @ 30; --width/--height/--fps override). Each prints the
requested, reported and measured format.

Safety: refuses to run if ~/Desktop/temp exists (a recording is in progress). Never claims an
interface, detaches a driver or sets a configuration: only class requests on endpoint 0,
sent on the device handle without pyusb's implicit interface claim (run 1 did claim; see
REPORT.md).
Devices are selected by vendor and product id only.
"""

import argparse
import array
import json
import os
import sys
import threading
import time

VID, PID = 0x0C45, 0x6369
LIBUSB = "/opt/homebrew/lib/libusb-1.0.dylib"
RECORDING_FLAG = os.path.expanduser("~/Desktop/temp")
# The probe's own copy of the user's camera_config_5.json, next to this script, with the
# defaults changed to 640x480 at the user's request (2026-11-19). Used for the stream format
# only; cameras are always selected by USB vendor/product id.
RECORDER_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera_config_5.json")

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
EXPOSURE_LIMIT = 50  # 5 ms in 100 us units: the analysis needs exposure under this
TEST_EXPOSURE = 40   # 4 ms, the manual value tried in q1 (clamped to the camera's range)


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
        if name == "exposure" and not err and req in (GET_MIN, GET_MAX, GET_RES, GET_DEF, GET_CUR):
            extra = f"({res / 10:.1f} ms)"
            if req == GET_CUR:
                extra += " OVER 5 ms" if res >= EXPOSURE_LIMIT else " under 5 ms"
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


def recorder_format(args):
    """Width, height, fps as the recorder requests them: the config's defaults
    (ratrixcam_server.py:464-466), unless given on the command line."""
    src, w, h, f = "command line", args.width, args.height, args.fps
    if None in (w, h, f):
        with open(args.config) as fh:
            cfg = json.load(fh)
        src = args.config
        print(f"  config {args.config}: lists {len(cfg.get('cameras', []))} camera(s); used for the format only "
              f"(cameras are selected by USB id 0x{VID:04x}:0x{PID:04x})")
        overrides =[{k: c[k] for k in ("width", "height", "fps") if c.get(k) is not None}
                     for c in cfg.get("cameras", [])]
        if any(overrides):
            print(f"  NOTE: per-camera format overrides in the config: {overrides}; probe uses the defaults")
        w = w or cfg["default_width"]
        h = h or cfg["default_height"]
        f = f or cfg["default_fps"]
    return w, h, f, src


def open_capture(cv2, args):
    """Open exactly as ratrixcam_server.py:487-492 does: default backend, then width, height, fps."""
    width, height, fps, src = recorder_format(args)
    t0 = time.perf_counter()
    cap = cv2.VideoCapture(args.index)
    _ = cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    _ = cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    _ = cap.set(cv2.CAP_PROP_FPS, fps)
    ms = (time.perf_counter() - t0) * 1000
    print(f"  cv2 {cv2.__version__} VideoCapture({args.index}) isOpened={cap.isOpened()} "
          f"backend={cap.getBackendName() if cap.isOpened() else '-'} open+set took {ms:.0f} ms")
    print(f"  FORMAT requested {width}x{height} @ {fps} fps (from {src}); capture reports "
          f"{cap.get(cv2.CAP_PROP_FRAME_WIDTH):.0f}x{cap.get(cv2.CAP_PROP_FRAME_HEIGHT):.0f} "
          f"@ {cap.get(cv2.CAP_PROP_FPS):.1f} fps")
    return cap


def print_measured_size(reader):
    shape = reader.shape
    print(f"  FORMAT measured frame size: {f'{shape[1]}x{shape[0]}' if shape else 'no frame yet'}")


class Reader(threading.Thread):
    def __init__(self, cap):
        super().__init__(daemon=True)
        self.cap, self.samples, self.stop_evt, self.shape = cap, [], threading.Event(), None

    def run(self):
        while not self.stop_evt.is_set():
            ok, frame = self.cap.read()
            if ok and self.shape is None:
                self.shape = frame.shape[:2]
            self.samples.append((time.monotonic(), ok, float(frame.mean()) if ok else None))

    def window(self, t0, t1):
        s = [x for x in self.samples if t0 <= x[0] < t1]
        good = [x for x in s if x[1]]
        fps = (len(good) - 1) / (good[-1][0] - good[0][0]) if len(good) > 1 and good[-1][0] > good[0][0] else 0.0
        mean = sum(x[2] for x in good) / len(good) if good else float("nan")
        return len(good), len(s) - len(good), fps, mean


def watch(reader, label_, seconds, settle=0.0):
    """Measure over `seconds`, after first discarding `settle` seconds of frames."""
    time.sleep(settle)
    t0 = time.monotonic()
    time.sleep(seconds)
    n, bad, fps, mean = reader.window(t0, time.monotonic())
    print(f"    stream [{label_}] {seconds:.1f}s: frames={n} failed_reads={bad} fps={fps:.1f} mean_brightness={mean:.1f}")
    return mean


# ---------------------------------------------------------------- sub-commands

CT_BIT_NAMES = ["scanning mode", "AE mode", "AE priority", "exposure abs", "exposure rel",
                "focus abs", "focus rel", "iris abs", "iris rel", "zoom abs", "zoom rel",
                "pan/tilt abs", "pan/tilt rel", "roll abs", "roll rel", "reserved15",
                "reserved16", "focus auto", "privacy"]
PU_BIT_NAMES = ["brightness", "contrast", "hue", "saturation", "sharpness", "gamma",
                "WB temperature", "WB component", "backlight comp", "gain", "power line freq",
                "hue auto", "WB temperature auto", "WB component auto", "digital multiplier",
                "digital mult limit", "analog video std", "analog lock status", "contrast auto"]
VS_SUBTYPES = {0x04: "UNCOMPRESSED", 0x06: "MJPEG", 0x10: "FRAME_BASED"}
VS_FRAME_SUBTYPES = {0x05: "UNCOMPRESSED", 0x07: "MJPEG", 0x11: "FRAME_BASED"}


def bit_names(bits, names):
    return ", ".join(names[b] if b < len(names) else f"bit{b}" for b in range(bits.bit_length()) if bits >> b & 1)


def rig_port(dev):
    """The user's port number on this rig: hub on bus 1 = ports 1-4, hub on bus 0 = ports 5-8,
    by the last hub port (confirmed by the user 2026-11-19; rig-specific)."""
    ports = dev.port_numbers or ()
    if len(ports) != 3 or dev.bus not in (0, 1):
        return "?"
    return str(ports[2] + (0 if dev.bus == 1 else 4))


def describe(dev):
    """Every line of the descriptors that could differ between cameras, location excluded.
    Read from the descriptors libusb caches at enumeration; the device is not opened."""
    lines = [f"device: usb {dev.bcdUSB >> 8}.{dev.bcdUSB & 0xff:02x} bcdDevice {dev.bcdDevice >> 8}.{dev.bcdDevice & 0xff:02x} "
             f"class {dev.bDeviceClass:#04x}/{dev.bDeviceSubClass:#04x}/{dev.bDeviceProtocol:#04x} "
             f"ep0 {dev.bMaxPacketSize0} configs {dev.bNumConfigurations} "
             f"string indices manufacturer {dev.iManufacturer} product {dev.iProduct} serial {dev.iSerialNumber}"]
    cfg = dev[0]
    lines.append(f"config: {cfg.bNumInterfaces} interfaces, attributes {cfg.bmAttributes:#04x}, max power {cfg.bMaxPower * 2} mA")
    for intf in cfg:
        eps = " ".join(f"ep{e.bEndpointAddress:#04x}/{e.bmAttributes & 3}/{e.wMaxPacketSize:#06x}" for e in intf)
        lines.append(f"interface {intf.bInterfaceNumber} alt {intf.bAlternateSetting}: class "
                     f"{intf.bInterfaceClass:#04x}/{intf.bInterfaceSubClass:#04x} endpoints [{eps}]")
        ex = bytes(intf.extra_descriptors)
        i = 0
        while i + 2 < len(ex):
            blen, dtype, sub = ex[i], ex[i + 1], ex[i + 2]
            if blen == 0:
                break
            d = ex[i:i + blen]
            i += blen
            if dtype != CS_INTERFACE or intf.bInterfaceClass != 0x0E:
                continue
            if intf.bInterfaceSubClass == 0x01:  # VideoControl
                if sub == 0x01:
                    lines.append(f"  VC header: UVC {d[4]}.{d[3]:02x}, clock {int.from_bytes(d[7:11], 'little')} Hz")
                elif sub == VC_INPUT_TERMINAL:
                    ttype = int.from_bytes(d[4:6], "little")
                    if ttype == ITT_CAMERA:
                        bits = int.from_bytes(d[15:15 + d[14]], "little")
                        lines.append(f"  camera terminal id {d[3]}: bmControls {bits:#x} = {bit_names(bits, CT_BIT_NAMES)}")
                    else:
                        lines.append(f"  input terminal id {d[3]} type {ttype:#06x}")
                elif sub == 0x03:
                    lines.append(f"  output terminal id {d[3]} type {int.from_bytes(d[4:6], 'little'):#06x} source {d[7]}")
                elif sub == VC_PROCESSING_UNIT:
                    bits = int.from_bytes(d[8:8 + d[7]], "little")
                    lines.append(f"  processing unit id {d[3]} source {d[4]}: bmControls {bits:#x} = {bit_names(bits, PU_BIT_NAMES)}")
                elif sub == VC_EXTENSION_UNIT:
                    n_pins = d[21]
                    size_at = 22 + n_pins
                    bits = int.from_bytes(d[size_at + 1:size_at + 1 + d[size_at]], "little")
                    lines.append(f"  extension unit id {d[3]} guid {d[4:20].hex()} controls {d[20]} bmControls {bits:#x}")
                else:
                    lines.append(f"  VC subtype {sub:#04x} id {d[3]}")
            elif intf.bInterfaceSubClass == 0x02 and intf.bAlternateSetting == 0:  # VideoStreaming
                if sub in VS_SUBTYPES:
                    lines.append(f"  format {d[3]} {VS_SUBTYPES[sub]}" + (f" guid {d[5:21].hex()}" if sub != 0x06 else "")
                                 + f": {d[4]} frame sizes")
                elif sub in VS_FRAME_SUBTYPES:
                    w, h = int.from_bytes(d[5:7], "little"), int.from_bytes(d[7:9], "little")
                    # Uncompressed/MJPEG: dwDefaultFrameInterval at 21, bFrameIntervalType at 25,
                    # intervals from 26. Frame-based (0x11): default at 17, type at 21, then
                    # dwBytesPerLine at 22-25, intervals from 26 (list run 2 misread this).
                    n_at = 25 if sub != 0x11 else 21
                    iv_at = 26
                    default = int.from_bytes(d[21:25], "little") if sub != 0x11 else int.from_bytes(d[17:21], "little")
                    n = d[n_at]
                    count = 3 if n == 0 else n
                    ivs = [int.from_bytes(d[iv_at + 4 * k:iv_at + 4 + 4 * k], "little") for k in range(count)]
                    off = iv_at - 1
                    fps = lambda v: f"{1e7 / v:g}" if v else "INVALID(0)"
                    if n == 0:
                        rates = f"continuous {fps(ivs[1])}-{fps(ivs[0])} fps step {ivs[2]}"
                    else:
                        rates = "/".join(fps(v) for v in ivs) + " fps"
                    malformed = (len(d) < off + 1 + 4 * count or 0 in ivs[: 2 if n == 0 else n]
                                 or default == 0)
                    lines.append(f"    frame {d[3]} {w}x{h}: {rates} (default {fps(default)} fps)"
                                 + (f"  MALFORMED bLength={d[0]} raw={d.hex()}" if malformed else ""))
                elif sub == 0x03:
                    sizes = [f"{int.from_bytes(d[4 + 4 * k:6 + 4 * k], 'little')}x{int.from_bytes(d[6 + 4 * k:8 + 4 * k], 'little')}"
                             for k in range(d[3])]
                    lines.append(f"    still image sizes: {' '.join(sizes)}")
                elif sub == 0x0D:
                    lines.append(f"    color matching: primaries {d[3]} transfer {d[4]} matrix {d[5]}")
                elif sub == 0x01:
                    lines.append(f"  VS input header: {d[3]} formats, endpoint {d[6]:#04x}, still method {d[9]}")
                else:
                    lines.append(f"  VS subtype {sub:#04x}")
    return lines


def cmd_list(_args):
    """Read-only, no device opened: descriptors of every camera with the Sonix vendor id,
    grouped by identical descriptors (location excluded)."""
    import hashlib
    import usb.core
    be = usb_backend()
    devs = sorted(usb.core.find(find_all=True, idVendor=VID, backend=be), key=location_id)
    print(f"[{now()}] {len(devs)} device(s) with vendor 0x{VID:04x}; target product is 0x{PID:04x}")
    groups = {}
    for dev in devs:
        try:
            lines = describe(dev)
        except Exception as e:  # one camera's odd descriptors must not hide the others
            lines = [f"DESCRIPTOR DECODE FAILED: {type(e).__name__}: {e}"]
        fp = hashlib.sha256("\n".join([f"{dev.idProduct:#06x}"] + lines).encode()).hexdigest()[:12]
        groups.setdefault(fp, []).append(dev)
        tag = "TARGET" if dev.idProduct == PID else "not target"
        print(f"\n  port {rig_port(dev)}  {label(dev)}  product 0x{dev.idProduct:04x} ({tag})  fingerprint {fp}")
        for line in lines:
            print(f"    {line}")
    print(f"\n[{now()}] {len(groups)} distinct descriptor set(s):")
    for fp, members in groups.items():
        print(f"  {fp}: product 0x{members[0].idProduct:04x}, ports "
              + ", ".join(f"{rig_port(d)} (0x{location_id(d):08x})" for d in members))


def cmd_controls(args):
    """Read-only GETs. --all-vendor: every 0x0c45 camera (approved by the user for reads only)."""
    guard()
    be = usb_backend()
    if args.all_vendor:
        import usb.core
        devs = sorted(usb.core.find(find_all=True, idVendor=VID, backend=be), key=location_id)
        print(f"[{now()}] {len(devs)} camera(s) with vendor 0x{VID:04x} (all products); GET requests only; no stream open")
    else:
        devs = find_targets(be)
        print(f"[{now()}] {len(devs)} target(s); no stream open")
    import usb.util
    for dev in devs:
        print(f"\n  port {rig_port(dev)}  product 0x{dev.idProduct:04x} ({'TARGET' if dev.idProduct == PID else 'not target'})")
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
        prio, _e3, _ = xfer(dev, lay, "ae_priority", GET_CUR)
        original.append((mode, expo, prio))
        if err is None:
            print(f"    no-op write ae_mode:=current {mode}: {fmt(*xfer(dev, lay, 'ae_mode', SET_CUR, mode))}")

    cv2 = import_cv2()
    print(f"\n[{now()}] PHASE B: stream open")
    cap = open_capture(cv2, args)
    if not cap.isOpened():
        print("  Capture did not open. If macOS showed a camera-permission dialog for Terminal, allow it and rerun.")
        return
    reader = Reader(cap)
    reader.start()
    changed = []  # restore callables for writes in flight, run in the finally if anything fails
    try:
        watch(reader, "warm-up", 3.0)
        print_measured_size(reader)
        for (dev, lay), (mode0, expo0, prio0) in zip(zip(devs, lays), original):
            print(f"  {label(dev)} while streaming")
            for name in CONTROLS:
                read_all(dev, lay, name, (GET_INFO, GET_CUR))
            mode, err, _ = xfer(dev, lay, "ae_mode", GET_CUR)
            if err:
                print("    ae_mode unreadable while streaming; skipping writes on this device")
                continue
            print(f"    no-op write ae_mode:=current {mode}: {fmt(*xfer(dev, lay, 'ae_mode', SET_CUR, mode))}")

            # Goal 2: clear AE priority (camera may no longer lower its frame rate).
            prio, p_err, _ = xfer(dev, lay, "ae_priority", GET_CUR)
            if p_err:
                print("    ae_priority unreadable; skipping the AE-priority test")
            else:
                # Restore to the value read just before this write (the streaming value), which
                # may differ from the no-stream value prio0 if stream start changes it.
                restore_p = prio
                if prio != prio0:
                    print(f"    NOTE ae_priority changed from {prio0} (no stream) to {prio} (streaming)")
                watch(reader, "before ae_priority change", 2.0)
                changed.append(lambda d=dev, l=lay, v=restore_p: restore_priority(d, l, v))
                print(f"    write ae_priority:=0: {fmt(*xfer(dev, lay, 'ae_priority', SET_CUR, 0))}")
                read_all(dev, lay, "ae_priority", (GET_CUR,))
                watch(reader, "ae_priority=0", 3.0)
                changed.pop()()
                watch(reader, "after ae_priority restore", 2.0)

            # Goal 3 preview: manual exposure at 4 ms (gain untouched; compensation comes later).
            emin, e_err, _ = xfer(dev, lay, "exposure", GET_MIN)
            emax, e_err2, _ = xfer(dev, lay, "exposure", GET_MAX)
            if e_err or e_err2:
                print("    exposure range unreadable; skipping the manual-exposure test")
                continue
            target = min(max(TEST_EXPOSURE, emin), emax)
            before = watch(reader, "before exposure change", 2.0)
            changed.append(lambda d=dev, l=lay: restore_defaults(d, l))
            print(f"    write ae_mode:=manual(1): {fmt(*xfer(dev, lay, 'ae_mode', SET_CUR, AE_MANUAL))}")
            print(f"    write exposure:={target} ({target / 10:.1f} ms): "
                  f"{fmt(*xfer(dev, lay, 'exposure', SET_CUR, target))}")
            read_all(dev, lay, "ae_mode", (GET_CUR,))
            read_all(dev, lay, "exposure", (GET_CUR,))
            during = watch(reader, f"manual, exposure {target / 10:.1f} ms", 3.0)
            changed.pop()()
            after = watch(reader, "after exposure restore", 2.0)
            change = (during - before) / before * 100 if before and before == before else float("nan")
            print(f"    brightness before/during/after = {before:.1f}/{during:.1f}/{after:.1f} "
                  f"(change during {change:+.0f}%)")
    finally:
        for undo in changed:
            undo()
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


def cmd_track(args):
    """Read-only: does exposure GET_CUR follow the auto-exposure loop when the light changes?"""
    guard()
    be = usb_backend()
    devs = find_targets(be)
    lays = [vc_layout(d) for d in devs]
    print(f"[{now()}] {len(devs)} target(s); reading every {args.interval:.1f}s for {args.seconds:.0f}s")
    print(f"  cue: at t={args.cover:.0f}s COVER ALL LENSES (or switch off the room light); "
          f"at t={args.uncover:.0f}s UNCOVER")
    cv2 = import_cv2()
    cap = open_capture(cv2, args)
    if not cap.isOpened():
        sys.exit("capture did not open")
    reader = Reader(cap)
    reader.start()
    start, cued = time.monotonic(), set()
    try:
        time.sleep(1.0)
        print_measured_size(reader)
        start = time.monotonic()
        while (elapsed := time.monotonic() - start) < args.seconds:
            for at, msg in ((args.cover, "COVER ALL LENSES NOW"), (args.uncover, "UNCOVER NOW")):
                if elapsed >= at and at not in cued:
                    cued.add(at)
                    print(f">>> t={elapsed:.1f}s {msg}")
            t0 = time.monotonic()
            time.sleep(args.interval)
            n, bad, fps, mean = reader.window(t0, time.monotonic())
            cols = []
            for dev, lay in zip(devs, lays):
                m, me, _ = xfer(dev, lay, "ae_mode", GET_CUR)
                e, ee, _ = xfer(dev, lay, "exposure", GET_CUR)
                g, ge, _ = xfer(dev, lay, "gain", GET_CUR)
                exp_s = "ERR" if ee else f"{e / 10:.1f}ms"
                cols.append(f"{location_id(dev):08x} ae={'ERR' if me else m} exp={exp_s} "
                            f"gain={'ERR' if ge else g}")
            print(f"t={time.monotonic() - start:5.1f}s frames={n:2d} bad={bad} fps={fps:4.1f} "
                  f"bright={mean:5.1f} | " + " | ".join(cols))
    finally:
        reader.stop_evt.set()
        reader.join(timeout=3)
        cap.release()
    print(f"[{now()}] done (read-only; nothing to restore)")


def cmd_aetrack(args):
    """Does exposure GET_CUR follow the AE loop? Darken with manual 4 ms, hand back to auto
    without resetting the exposure value, and read it while AE re-converges."""
    guard()
    be = usb_backend()
    devs = find_targets(be)
    lays = [vc_layout(d) for d in devs]
    print(f"[{now()}] {len(devs)} target(s)")
    cv2 = import_cv2()
    cap = open_capture(cv2, args)
    if not cap.isOpened():
        sys.exit("capture did not open")
    reader = Reader(cap)
    reader.start()
    changed = []
    try:
        watch(reader, "warm-up", 3.0)
        print_measured_size(reader)
        for dev, lay in zip(devs, lays):
            print(f"  {label(dev)}")
            mode0, m_err, _ = xfer(dev, lay, "ae_mode", GET_CUR)
            expo0, e_err, _ = xfer(dev, lay, "exposure", GET_CUR)
            if m_err or e_err:
                print("    unreadable; skipped")
                continue
            auto = mode0 if mode0 in (AE_AUTO, AE_APERTURE) else auto_mode_for(dev, lay)
            changed.append(lambda d=dev, l=lay: restore_defaults(d, l))
            watch(reader, "auto, before", 2.0)
            print(f"    write ae_mode:=manual(1): {fmt(*xfer(dev, lay, 'ae_mode', SET_CUR, AE_MANUAL))}")
            print(f"    write exposure:={TEST_EXPOSURE}: {fmt(*xfer(dev, lay, 'exposure', SET_CUR, TEST_EXPOSURE))}")
            watch(reader, f"manual {TEST_EXPOSURE / 10:.1f} ms", 2.0)
            print(f"    write ae_mode:={auto} (auto), exposure value left at {TEST_EXPOSURE}: "
                  f"{fmt(*xfer(dev, lay, 'ae_mode', SET_CUR, auto))}")
            t_auto = time.monotonic()
            while (dt := time.monotonic() - t_auto) < args.seconds:
                t0 = time.monotonic()
                time.sleep(args.interval)
                n, bad, fps, mean = reader.window(t0, time.monotonic())
                e, ee, ms = xfer(dev, lay, "exposure", GET_CUR)
                g, ge, _ = xfer(dev, lay, "gain", GET_CUR)
                exp_s = f"ERR {ee}" if ee else f"{e} ({e / 10:.1f} ms)"
                print(f"    +{dt + args.interval:4.2f}s bright={mean:5.1f} fps={fps:4.1f} "
                      f"exposure={exp_s} gain={'ERR' if ge else g}")
            changed.pop()()
            watch(reader, "after restore", 2.0)
    finally:
        for undo in changed:
            undo()
        reader.stop_evt.set()
        reader.join(timeout=3)
        cap.release()
    print(f"\n[{now()}] final read-back, stream released")
    for dev, lay in zip(devs, lays):
        print(f"  {label(dev)}")
        for name in CONTROLS:
            read_all(dev, lay, name, (GET_CUR,))


def cmd_restore(_args):
    """Safety net: every target to its own defaults (exposure, gain, then AE mode = auto)."""
    guard()
    be = usb_backend()
    results = []
    for dev in find_targets(be):
        lay = vc_layout(dev)
        print(f"  {label(dev)}")
        results.append(restore_defaults(dev, lay))
        read_all(dev, lay, "ae_priority", (GET_CUR,))
    print(f"[{now()}] restore {'OK on all targets' if all(results) else 'INCOMPLETE: see MISMATCH lines'}")


def restore_defaults(dev, lay):
    """Exposure and gain to GET_DEF, then AE mode to GET_DEF (auto); read every one back.
    This camera stalls exposure writes in auto mode (aetrack run 1), so the writes go via
    manual mode. Returns True when every read-back matches."""
    defs = {}
    for name in ("exposure", "gain", "ae_mode"):
        d, err, _ = xfer(dev, lay, name, GET_DEF)
        if err and name == "ae_mode":
            d, err = auto_mode_for(dev, lay), None
        defs[name] = None if err else d
    print(f"    restore: ae_mode:=manual(1) first, so exposure accepts the write: "
          f"{fmt(*xfer(dev, lay, 'ae_mode', SET_CUR, AE_MANUAL))}")
    ok = True
    for name in ("exposure", "gain", "ae_mode"):
        if defs[name] is None:
            print(f"    restore {name}: no default readable; left as is")
            ok = False
            continue
        print(f"    restore {name}:={defs[name]} (default): {fmt(*xfer(dev, lay, name, SET_CUR, defs[name]))}")
    for name, want in defs.items():
        if want is None:
            continue
        got, err, ms = xfer(dev, lay, name, GET_CUR)
        good = got == want
        ok = ok and good
        print(f"    read-back {name}={fmt(got, err, ms)} {'OK' if good else f'MISMATCH (want {want})'}")
    return ok


def set_manual(dev, lay, exposure, gain):
    """Manual AE mode, then exposure and gain; read back. Exposure only accepts writes in manual."""
    print(f"    write ae_mode:=manual(1): {fmt(*xfer(dev, lay, 'ae_mode', SET_CUR, AE_MANUAL))}")
    print(f"    write exposure:={exposure} ({exposure / 10:.1f} ms): {fmt(*xfer(dev, lay, 'exposure', SET_CUR, exposure))}")
    print(f"    write gain:={gain}: {fmt(*xfer(dev, lay, 'gain', SET_CUR, gain))}")
    return readback_line(dev, lay, (AE_MANUAL, exposure, gain), "after write")


def readback_line(dev, lay, want, when):
    """One line: ae_mode/exposure/gain now, and whether they match `want` (mode, exposure, gain)."""
    got = tuple(xfer(dev, lay, n, GET_CUR)[0] for n in ("ae_mode", "exposure", "gain"))
    verdict = ("MATCHES" if got == want else "DIFFERS") + f" (expected ae_mode={want[0]} exposure={want[1]} gain={want[2]})"
    exp_s = f"{got[1] / 10:.1f} ms" if got[1] is not None else "ERR"
    print(f"    [{when}] {location_id(dev):08x} ae_mode={got[0]} exposure={got[1]} ({exp_s}) gain={got[2]}: {verdict}")
    return got == want


def cmd_gainsweep(args):
    """Goal 3 compensation: at manual 4 ms, step gain through its range and measure brightness,
    to find the gain that matches the brightness auto-exposure gives at the same moment."""
    guard()
    be = usb_backend()
    devs = find_targets(be)
    lays = [vc_layout(d) for d in devs]
    print(f"[{now()}] {len(devs)} target(s); exposure fixed at {TEST_EXPOSURE / 10:.1f} ms, gain step {args.step}")
    cv2 = import_cv2()
    cap = open_capture(cv2, args)
    if not cap.isOpened():
        sys.exit("capture did not open")
    reader = Reader(cap)
    reader.start()
    changed = []
    try:
        watch(reader, "warm-up", 3.0)
        print_measured_size(reader)
        for dev, lay in zip(devs, lays):
            print(f"  {label(dev)}")
            gmin, e1, _ = xfer(dev, lay, "gain", GET_MIN)
            gmax, e2, _ = xfer(dev, lay, "gain", GET_MAX)
            if e1 or e2:
                print("    gain range unreadable; skipped")
                continue
            b_auto = watch(reader, "auto (reference)", 2.0)
            changed.append(lambda d=dev, l=lay: restore_defaults(d, l))
            set_manual(dev, lay, TEST_EXPOSURE, gmin)
            gains = list(range(gmin, gmax + 1, args.step))
            if gains[-1] != gmax:
                gains.append(gmax)
            rows = []
            for g in gains:
                w = xfer(dev, lay, "gain", SET_CUR, g)
                rb, rb_err, _ = xfer(dev, lay, "gain", GET_CUR)
                tag = f"gain {g} (write {fmt(*w)}, read back {'ERR' if rb_err else rb})"
                b = watch(reader, tag, args.dwell, settle=args.settle)
                rows.append((g, b))
            changed.pop()()
            watch(reader, "after restore", 2.0)
            bs = [b for _, b in rows]
            if max(bs) - min(bs) < 3:
                print(f"    gain sweep: brightness flat ({min(bs):.1f}-{max(bs):.1f}): not the capture")
                continue
            best = min(rows, key=lambda r: abs(r[1] - b_auto))
            sat = [g for g, b in rows if b >= 250]
            print(f"    gain sweep on the capture: auto reference {b_auto:.1f}; at {TEST_EXPOSURE / 10:.1f} ms "
                  f"the nearest is gain {best[0]} -> {best[1]:.1f}"
                  + (f"; saturated (>=250) from gain {sat[0]}" if sat else ""))
            print("    table gain:brightness " + " ".join(f"{g}:{b:.1f}" for g, b in rows))
    finally:
        for undo in changed:
            undo()
        reader.stop_evt.set()
        reader.join(timeout=3)
        cap.release()
    print(f"\n[{now()}] final read-back, stream released")
    for dev, lay in zip(devs, lays):
        readback_line(dev, lay, (AE_APERTURE, 157, 0), "final, expected = defaults")


def wait_for_count(be, want, timeout, poll=0.5):
    """Poll until the number of targets equals `want`; return the targets, or None on timeout."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        devs = find_targets(be)
        if len(devs) == want:
            return devs
        time.sleep(poll)
    return None


def cmd_persist(args):
    """Q2: do manual settings survive stream start, stream stop, and unplug/replug?"""
    guard()
    import usb.util
    be = usb_backend()
    state = {"devs": find_targets(be)}
    state["lays"] = [vc_layout(d) for d in state["devs"]]
    n = len(state["devs"])
    want = (AE_MANUAL, TEST_EXPOSURE, args.gain)
    print(f"[{now()}] {n} target(s); manual setting under test: exposure {TEST_EXPOSURE / 10:.1f} ms, gain {args.gain}")
    cv2 = import_cv2()

    print(f"\n[{now()}] PHASE 1: auto baseline brightness (stream at the recorder's format)")
    cap = open_capture(cv2, args)
    if not cap.isOpened():
        sys.exit("capture did not open")
    reader = Reader(cap)
    reader.start()
    watch(reader, "warm-up", 3.0)
    print_measured_size(reader)
    b_auto = watch(reader, "auto baseline", 2.0)
    reader.stop_evt.set()
    reader.join(timeout=3)
    cap.release()

    try:
        print(f"\n[{now()}] PHASE 2: set manual with NO stream open")
        for dev, lay in zip(state["devs"], state["lays"]):
            print(f"  {label(dev)}")
            set_manual(dev, lay, TEST_EXPOSURE, args.gain)

        print(f"\n[{now()}] PHASE 3: stream start (opened as the recorder opens it)")
        cap = open_capture(cv2, args)
        reader = Reader(cap)
        reader.start()
        for dev, lay in zip(state["devs"], state["lays"]):
            readback_line(dev, lay, want, "right after open")
        b_manual = watch(reader, "streaming with manual set beforehand", 3.0, settle=1.0)
        for dev, lay in zip(state["devs"], state["lays"]):
            readback_line(dev, lay, want, "after 4 s streaming")
        reader.stop_evt.set()
        reader.join(timeout=3)
        cap.release()
        darker = b_manual < 0.85 * b_auto
        print(f"    brightness: auto baseline {b_auto:.1f}, after stream start with manual {b_manual:.1f}: "
              + ("darker, consistent with the manual exposure being in force"
                 if darker else "NOT darker: the manual exposure may not be in force on the capture"))
        for dev, lay in zip(state["devs"], state["lays"]):
            readback_line(dev, lay, want, "after stream stop")

        if args.no_replug:
            return
        print(f"\n[{now()}] PHASE 4: unplug and replug")
        for dev in state["devs"]:
            usb.util.dispose_resources(dev)
        before = {location_id(d) for d in state["devs"]}
        print(f">>> UNPLUG ONE target camera now (any one), wait about 5 s, then plug it back into the SAME port.")
        print(f"    waiting up to {args.timeout:.0f} s for it to disappear...")
        gone = wait_for_count(be, n - 1, args.timeout)
        if gone is None:
            print("    no camera disappeared; skipping the replug test")
            return
        missing = before - {location_id(d) for d in gone}
        print(f"[{now()}] disappeared: {', '.join(f'0x{m:08x}' for m in missing)}. Now plug it back in.")
        back = wait_for_count(be, n, args.timeout)
        if back is None:
            state["devs"], state["lays"] = gone, [vc_layout(d) for d in gone]
            print(f"!!! the camera did not come back within {args.timeout:.0f} s. Plug it in, then run "
                  "`uvc/.venv/bin/python uvc/probe.py restore` (it may still be in manual).")
            return
        time.sleep(args.settle_after_replug)
        state["devs"], state["lays"] = back, [vc_layout(d) for d in back]
        print(f"[{now()}] re-enumerated: {', '.join(f'0x{location_id(d):08x}' for d in back)}")
        for dev, lay in zip(state["devs"], state["lays"]):
            note = " (the replugged camera)" if location_id(dev) in missing else ""
            print(f"  {label(dev)}{note}")
            readback_line(dev, lay, want, "after re-enumeration, no stream")
        cap = open_capture(cv2, args)
        reader = Reader(cap)
        reader.start()
        watch(reader, "stream after re-enumeration", 3.0, settle=1.0)
        for dev, lay in zip(state["devs"], state["lays"]):
            readback_line(dev, lay, want, "after re-enumeration + stream start")
        reader.stop_evt.set()
        reader.join(timeout=3)
        cap.release()
    finally:
        print(f"\n[{now()}] RESTORE every target to its defaults")
        results = []
        for dev, lay in zip(state["devs"], state["lays"]):
            print(f"  {label(dev)}")
            results.append(restore_defaults(dev, lay))
        print(f"[{now()}] restore {'OK on all targets present' if all(results) else 'INCOMPLETE: see MISMATCH lines'}")


def restore_priority(dev, lay, value):
    print(f"    restore ae_priority:={value}: {fmt(*xfer(dev, lay, 'ae_priority', SET_CUR, value))}")
    res, err, ms = xfer(dev, lay, "ae_priority", GET_CUR)
    print(f"    read-back ae_priority={fmt(res, err, ms)} {'OK' if res == value else 'MISMATCH'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    ct = sub.add_parser("controls")
    ct.add_argument("--all-vendor", action="store_true",
                    help="every 0x0c45 camera, not only the target product (GET requests only)")
    ct.set_defaults(fn=cmd_controls)
    stream = argparse.ArgumentParser(add_help=False)
    stream.add_argument("--index", type=int, default=0, help="cv2 camera index to stream (any index is a target)")
    stream.add_argument("--config", default=RECORDER_CONFIG, help="recorder config to take the format from")
    stream.add_argument("--width", type=int, help="override the config's default_width")
    stream.add_argument("--height", type=int, help="override the config's default_height")
    stream.add_argument("--fps", type=int, help="override the config's default_fps")
    q1 = sub.add_parser("q1", parents=[stream])
    q1.set_defaults(fn=cmd_q1)
    tr = sub.add_parser("track", parents=[stream])
    tr.add_argument("--seconds", type=float, default=40.0)
    tr.add_argument("--interval", type=float, default=1.0)
    tr.add_argument("--cover", type=float, default=10.0, help="seconds in: cue to cover the lenses")
    tr.add_argument("--uncover", type=float, default=25.0, help="seconds in: cue to uncover")
    tr.set_defaults(fn=cmd_track)
    at = sub.add_parser("aetrack", parents=[stream])
    at.add_argument("--seconds", type=float, default=5.0, help="how long to follow AE after handing back")
    at.add_argument("--interval", type=float, default=0.25)
    at.set_defaults(fn=cmd_aetrack)
    gs = sub.add_parser("gainsweep", parents=[stream])
    gs.add_argument("--step", type=int, default=10, help="gain step through the camera's range")
    gs.add_argument("--dwell", type=float, default=1.0, help="seconds measured per gain step")
    gs.add_argument("--settle", type=float, default=0.5, help="seconds discarded after each gain write")
    gs.set_defaults(fn=cmd_gainsweep)
    ps = sub.add_parser("persist", parents=[stream])
    ps.add_argument("--gain", type=int, default=10, help="manual gain under test (non-default, so a revert shows)")
    ps.add_argument("--no-replug", action="store_true", help="stop after the stream start/stop phases")
    ps.add_argument("--timeout", type=float, default=120.0, help="seconds to wait for unplug, and again for replug")
    ps.add_argument("--settle-after-replug", type=float, default=2.0)
    ps.set_defaults(fn=cmd_persist)
    sub.add_parser("restore").set_defaults(fn=cmd_restore)
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # cues must appear live through tee
    print(f"[{now()}] probe.py {args.cmd}  python {sys.version.split()[0]}  pid {os.getpid()}")
    args.fn(args)


if __name__ == "__main__":
    main()
