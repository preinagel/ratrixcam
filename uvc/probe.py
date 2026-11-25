#!/usr/bin/env python3
"""UVC control probe for the Arducam OV2710 cameras (Sonix controller, USB vendor 0x0c45;
kinds A, B and C, below).

RUN THIS: the deployment dim-light test is in uvc/DIM_LIGHT_TEST.md (operator instructions,
including installation). The lab rig's last pending command was the soak test:

    cd ~/Desktop/ratrixcam-uvc && uvc/.venv/bin/python uvc/probe.py soak --minutes 20 2>&1 | tee uvc/logs/soak-run1.log

Sub-commands:
    list      read-only, no device opened: full descriptors of every 0x0c45 camera (all
              products), with formats, frame sizes and rates, grouped by fingerprint
    controls  read every GET_* of the exposure/gain controls, no stream open; kind A only,
              or every 0x0c45 camera with --all-vendor (GET requests only)
    The streaming sub-commands act on --kind (A, B, C, or any for all three).
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
    gainsweep goal 3: on the camera identified behind --index, while it streams: manual
              exposures (--exposures, default 2, 3, 4 and 10 ms; 10 ms warns as not
              under 5 ms) and, at each, gain stepped through its range, with
              auto before and after; per step the luminance statistics (Y mean and
              percentiles and maximum, temporal noise, snr, fractions at Y >= 240, Y >= 250 and Y <= 5, fps).
              Measurements only: no judgement of usability is made
    dimsweep  gainsweep on every cv2 index 0..N-1 of a rig whose layout is unknown (every
              target camera of any kind is a candidate), at 640x480 @ 30; per-index summary
    persist   question 2: manual 4 ms + gain 10 set with no stream; read back after stream
              start, stream stop, and a cued unplug/replug of one camera (polls for it)
    scan      cv2 only, no control requests: open indices --first..--last in turn and
              report frames and brightness for each
    replug    marks the camera on --port dark (manual, 0.1 ms), cues its unplug/replug,
              then at 3/30/60/120 s compares a same-process reopen of index 0 with a
              fresh-process scan of indices 0-9, to find where the marked camera went
    soak      kind C stall rate: identify each --streams camera (kind:port:index; default
              C:4:3 C:7:6 A:1:0), then stream each in its own new process for --minutes,
              read-only, logging every gap of --stall s without a frame and whether frames
              return; per-camera summary at the end (soakone is its per-camera child)
    restore   safety net: exposure, gain and AE mode to each camera's own defaults

Every sub-command that writes restores each target to its defaults in a finally block
(manual first, then exposure and gain defaults, then AE mode auto) and reads them back.

Streaming sub-commands (q1, track, aetrack, gainsweep, persist) open the capture as the recorder does
(ratrixcam_server.py:487-492): VideoCapture(index) on the default backend, then width,
height and fps from the defaults in uvc/camera_config_5.json (the probe's copy of the
user's config, set to 640x480 @ 30; --width/--height/--fps override). Each prints the
requested, reported and measured format.

Safety: the recorder must not be running; the operator makes sure of that. As an extra
guard the probe refuses to run if ~/Desktop/temp exists (the lab recorder's default temp
folder; recorders configured with another temp folder are not detected). Never claims an
interface, detaches a driver or sets a configuration: only class requests on endpoint 0,
sent on the device handle without pyusb's implicit interface claim (run 1 did claim; see
REPORT.md).
Kinds (brief, 2026-11-19): A = 0x6369 "USB 2.0 Camera", B = 0x6366 "USB 2.0 Camera",
C = 0x6366 "USB Camera". Cameras are selected by vendor id, product id and name string
(--kind, default A), optionally narrowed to one rig port (--port); never by list position.
Every streaming sub-command first identifies which selected camera is behind the index it
opens (marker brightness response; "index N is kind K at 0x<location>"), and stops before any
experiment write if that fails. Writes go only to the selected cameras.
"""

import argparse
import array
import json
import os
import shutil
import subprocess
import sys
import threading
import time

VID, PID = 0x0C45, 0x6369  # PID: kind A, the product the prototype was developed on
# The three kinds on the rig (brief, "Which cameras", 2026-11-19): product id and USB product
# name string. B and C share every USB id field; only the name string tells them apart.
KINDS = {"A": (0x6369, "USB 2.0 Camera"), "B": (0x6366, "USB 2.0 Camera"), "C": (0x6366, "USB Camera")}
KIND_OF = {}  # location id -> kind, filled by find_kind, used to label read-backs
# libusb: the lab Mac's hand install, then Homebrew's two default prefixes (Apple silicon,
# Intel); UVC_LIBUSB overrides, for another Mac with libusb elsewhere.
LIBUSB_CANDIDATES = [p for p in (os.environ.get("UVC_LIBUSB"),
                                 "/opt/homebrew/lib/libusb-1.0.dylib",
                                 "/usr/local/lib/libusb-1.0.dylib") if p]
LIBUSB = next((p for p in LIBUSB_CANDIDATES if os.path.exists(p)), LIBUSB_CANDIDATES[0])
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
        sys.exit(f"libusb backend not loaded from {LIBUSB} (looked for: {', '.join(LIBUSB_CANDIDATES)}; "
                 f"set UVC_LIBUSB to the path of libusb-1.0.dylib)")
    return be


def product_string(dev):
    """The USB product name string: a standard GET_DESCRIPTOR(string) request to the device
    (device recipient, so pyusb claims no interface). None if it cannot be read."""
    import usb.util
    try:
        s = usb.util.get_string(dev, dev.iProduct)
        return s.strip() if s else None
    except Exception:
        return None


def find_kind(be, kind, port=None):
    """Cameras of `kind`, by vendor id, product id and name string, optionally only the one on
    rig `port`; sorted by location. Never by position in a list."""
    import usb.core
    pid, name = KINDS[kind]
    out = []
    for dev in usb.core.find(find_all=True, idVendor=VID, idProduct=pid, backend=be):
        if product_string(dev) != name:
            continue
        if port is not None and rig_port(dev) != str(port):
            continue
        KIND_OF[location_id(dev)] = kind
        out.append(dev)
    return sorted(out, key=location_id)


def find_selected(be, args):
    """The cameras a run may write to: --kind (or every target kind for "any"), narrowed to
    --port when given."""
    port = getattr(args, "port", None)
    if args.kind == "any":
        devs = sorted((d for k in sorted(KINDS) for d in find_kind(be, k, port)), key=location_id)
        what = "any target kind (A, B, C)"
    else:
        devs = find_kind(be, args.kind, port)
        pid, name = KINDS[args.kind]
        what = f"kind {args.kind} (0x{VID:04x}:0x{pid:04x} \"{name}\")"
    what += f" on port {port}" if port else ""
    if not devs:
        sys.exit(f"no camera of {what}")
    print(f"[{now()}] selected {len(devs)} camera(s) of {what}: "
          + ", ".join(f"kind {KIND_OF[location_id(d)]} at 0x{location_id(d):08x} (lab-rig port {rig_port(d)})" for d in devs))
    return devs


def kind_label(dev):
    """Kind and USB location (reliable on any Mac), plus the lab rig's port number (derived
    from this lab Mac's hub layout; meaningless on another Mac)."""
    return f"kind {KIND_OF.get(location_id(dev), '?')} at 0x{location_id(dev):08x} (lab-rig port {rig_port(dev)})"


def find_targets(be):
    """Kind A only; kept for the read-only `controls` sub-command's default."""
    return find_kind(be, "A")


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
    """Width, height, fps as the recorder requests them for cv2 index args.index: entry
    args.index of the config's camera list (the recorder assigns indices in list order,
    ratrixcam_server.py:461), each field falling back to the config default
    (ratrixcam_server.py:464-466). Command-line values override."""
    src, w, h, f = "command line", args.width, args.height, args.fps
    if None in (w, h, f):
        with open(args.config) as fh:
            cfg = json.load(fh)
        cams = cfg.get("cameras", [])
        if args.index >= len(cams):
            sys.exit(f"config {args.config} has {len(cams)} camera entries; none for index {args.index}")
        cam = cams[args.index]
        src = f"{args.config}, entry {args.index}"
        print(f"  config {args.config}: {len(cams)} camera entries; entry {args.index} = {cam} "
              f"(format only; control requests select cameras by kind: vendor id, product id and name string)")
        w = w or cam.get("width") or cfg["default_width"]
        h = h or cam.get("height") or cfg["default_height"]
        f = f or cam.get("fps") or cfg["default_fps"]
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


VS_COMMIT_CONTROL = 0x02


def vs_formats(dev):
    """(interface number, {format index: (type, {frame index: (w, h)})}) of the first
    VideoStreaming interface, from the cached descriptors (no request)."""
    for intf in dev[0]:
        if intf.bInterfaceClass == 0x0E and intf.bInterfaceSubClass == 0x02 and intf.bAlternateSetting == 0:
            formats, current = {}, None
            ex = bytes(intf.extra_descriptors)
            i = 0
            while i + 2 < len(ex) and ex[i]:
                d = ex[i:i + ex[i]]
                i += ex[i]
                if d[1] != CS_INTERFACE:
                    continue
                if d[2] in VS_SUBTYPES:
                    current = formats.setdefault(d[3], (VS_SUBTYPES[d[2]], {}))
                elif d[2] in VS_FRAME_SUBTYPES and current is not None:
                    current[1][d[3]] = (int.from_bytes(d[5:7], "little"), int.from_bytes(d[7:9], "little"))
            return intf.bInterfaceNumber, formats
    return None, {}


def vs_commit(dev):
    """GET_CUR of VS_COMMIT_CONTROL: the format, frame size and interval the camera was told
    to stream. Read-only, like the GETs on the controls. UVC 1.0 commit block is 26 bytes."""
    import usb.core
    ifnum, formats = vs_formats(dev)
    if ifnum is None:
        return "no VideoStreaming interface"
    ctx = dev._ctx
    try:
        ctx.managed_open()
        buf = array.array("B", bytes(26))
        n = ctx.backend.ctrl_transfer(ctx.handle, 0xA1, GET_CUR, VS_COMMIT_CONTROL << 8, ifnum, buf, 1000)
    except usb.core.USBError as e:
        return f"ERR USBError errno={e.errno}: {e}"
    raw = bytes(buf[:n])
    fmt_i, frame_i = raw[2], raw[3]
    interval = int.from_bytes(raw[4:8], "little")
    kind, frames = formats.get(fmt_i, ("unknown format", {}))
    w, h = frames.get(frame_i, (0, 0))
    rate = f"{1e7 / interval:.1f} fps" if interval else "interval 0"
    max_frame = int.from_bytes(raw[18:22], "little") if n >= 22 else 0
    payload = int.from_bytes(raw[22:26], "little") if n >= 26 else 0
    return (f"format {fmt_i} = {kind}, frame {frame_i} = {w}x{h}, {rate}; "
            f"max frame {max_frame} B, max payload {payload} B")


def print_measured_size(reader, devs=()):
    """Frame size measured from the frames; what cv2 reports as FOURCC; and what each target
    camera says it was committed to stream (the camera's own record of the negotiated format)."""
    shape = reader.shape
    print(f"  FORMAT measured frame size: {f'{shape[1]}x{shape[0]}' if shape else 'no frame yet'}")
    import cv2
    code = int(reader.cap.get(cv2.CAP_PROP_FOURCC))
    text = "".join(chr((code >> 8 * k) & 0xFF) for k in range(4)) if code else ""
    print(f"  FORMAT cv2 CAP_PROP_FOURCC: {code} {text!r} (what the backend reports; may be its output, not the wire format)")
    for dev in devs:
        print(f"  FORMAT camera-side commit, port {rig_port(dev)} (0x{location_id(dev):08x}): {vs_commit(dev)}")


class Reader(threading.Thread):
    """Reads frames continuously. Per frame: time, ok, mean brightness (mean of all B, G, R
    values, as in every earlier log), and image statistics of the luminance
    Y = 0.299 R + 0.587 G + 0.114 B (0-255) on every second pixel in each direction:
    - temporal noise: the standard deviation of Y minus the previous frame's Y, divided by
      sqrt(2), i.e. the noise of one frame. A static scene is assumed; motion adds to it,
      which is why windows report the median over frame pairs;
    - Y percentiles 1, 50 and 99, and Y mean;
    - y_max: the frame's highest Y;
    - ge240, ge250: fractions of pixels with Y >= 240 and Y >= 250; black: fraction with Y <= 5.
      Fixed levels, no assumed ceiling: in dimsweep-lab-run1 bright areas piled up at about
      244-245 on kinds A and B and at 255 on kind C, while a few stray pixels reached
      247-255, so a "near the maximum" count missed the A/B pile-up.
    Measurements only; nothing here judges whether an image is usable."""

    def __init__(self, cap):
        super().__init__(daemon=True)
        self.cap, self.samples, self.stop_evt, self.shape = cap, [], threading.Event(), None
        self.prev = None
        self.last_frame = None  # the latest good frame, as decoded (for stills)
        self.sink, self.sink_lock = None, threading.Lock()  # sink(frame) is called per good frame (clips)

    def run(self):
        while not self.stop_evt.is_set():
            ok, frame = self.cap.read()
            t = time.monotonic()
            if ok:
                self.last_frame = frame
                with self.sink_lock:
                    if self.sink is not None:
                        self.sink(frame)
            if ok and self.shape is None:
                self.shape = frame.shape[:2]
            quality = None
            if ok:
                import numpy as np  # cv2's own dependency, importable once cv2 is
                sub = frame[::2, ::2].astype(np.float32)
                y = 0.114 * sub[..., 0] + 0.587 * sub[..., 1] + 0.299 * sub[..., 2]  # BGR order
                noise = None
                if self.prev is not None and self.prev.shape == y.shape:
                    noise = float((y - self.prev).std()) / 1.41421356
                p1, p50, p99 = (float(v) for v in np.percentile(y, (1, 50, 99)))
                quality = (noise, float((y >= 240).mean()), float((y <= 5).mean()),
                           float(y.mean()), p1, p50, p99, float(y.max()), float((y >= 250).mean()))
                self.prev = y
            else:
                self.prev = None
            self.samples.append((t, ok, float(frame.mean()) if ok else None, quality))

    def window(self, t0, t1):
        s = [x for x in self.samples if t0 <= x[0] < t1]
        good = [x for x in s if x[1]]
        fps = (len(good) - 1) / (good[-1][0] - good[0][0]) if len(good) > 1 and good[-1][0] > good[0][0] else 0.0
        mean = sum(x[2] for x in good) / len(good) if good else float("nan")
        return len(good), len(s) - len(good), fps, mean

    def quality(self, t0, t1):
        """Window statistics: median temporal noise over frame pairs; means over frames of
        the >= 240, >= 250 and black fractions, Y mean, Y percentiles 1, 50, 99 and Y maximum."""
        q = [x[3] for x in self.samples if t0 <= x[0] < t1 and x[1] and x[3]]
        nan = float("nan")
        noises = sorted(r[0] for r in q if r[0] is not None)
        out = {"noise": noises[len(noises) // 2] if noises else nan}
        for i, key in enumerate(("ge240", "black", "y_mean", "y_p1", "y_p50", "y_p99", "y_max", "ge250"), start=1):
            out[key] = sum(r[i] for r in q) / len(q) if q else nan
        return out


def watch(reader, label_, seconds, settle=0.0):
    """Measure over `seconds`, after first discarding `settle` seconds of frames."""
    time.sleep(settle)
    t0 = time.monotonic()
    time.sleep(seconds)
    n, bad, fps, mean = reader.window(t0, time.monotonic())
    print(f"    stream [{label_}] {seconds:.1f}s: frames={n} failed_reads={bad} fps={fps:.1f} mean_brightness={mean:.1f}")
    return mean


def watch_quality(reader, label_, seconds, settle=0.0):
    """As watch, plus the luminance statistics of Reader. Returns a dict of the figures.
    snr = Y mean / temporal noise. Measurements only, no verdict."""
    time.sleep(settle)
    t0 = time.monotonic()
    time.sleep(seconds)
    t1 = time.monotonic()
    n, bad, fps, mean = reader.window(t0, t1)
    q = reader.quality(t0, t1)
    q["snr"] = q["y_mean"] / q["noise"] if q["noise"] and q["noise"] == q["noise"] else float("nan")
    print(f"    stream [{label_}] {seconds:.1f}s: frames={n} failed_reads={bad} fps={fps:.1f} "
          f"mean_brightness={mean:.1f} Y mean={q['y_mean']:.1f} p1/p50/p99/max={q['y_p1']:.0f}/{q['y_p50']:.0f}/{q['y_p99']:.0f}/{q['y_max']:.0f} "
          f"noise={q['noise']:.2f} snr={q['snr']:.1f} ge240={q['ge240'] * 100:.2f}% ge250={q['ge250'] * 100:.2f}% black={q['black'] * 100:.2f}%")
    q.update({"brightness": mean, "fps": fps, "frames": n, "failed": bad})
    return q


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
    devs = find_selected(be, args)
    lays = [vc_layout(d) for d in devs]
    for dev, lay in zip(devs, lays):
        print_layout(dev, lay)
    cv2 = import_cv2()
    identified_or_exit(cv2, args, devs)

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
        print_measured_size(reader, devs)
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
    devs = find_selected(be, args)
    lays = [vc_layout(d) for d in devs]
    print(f"[{now()}] {len(devs)} target(s); reading every {args.interval:.1f}s for {args.seconds:.0f}s")
    print(f"  cue: at t={args.cover:.0f}s COVER ALL LENSES (or switch off the room light); "
          f"at t={args.uncover:.0f}s UNCOVER")
    cv2 = import_cv2()
    identified_or_exit(cv2, args, devs)
    cap = open_capture(cv2, args)
    if not cap.isOpened():
        sys.exit("capture did not open")
    reader = Reader(cap)
    reader.start()
    start, cued = time.monotonic(), set()
    try:
        time.sleep(1.0)
        print_measured_size(reader, devs)
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
    devs = find_selected(be, args)
    lays = [vc_layout(d) for d in devs]
    print(f"[{now()}] {len(devs)} target(s)")
    cv2 = import_cv2()
    identified_or_exit(cv2, args, devs)
    cap = open_capture(cv2, args)
    if not cap.isOpened():
        sys.exit("capture did not open")
    reader = Reader(cap)
    reader.start()
    changed = []
    try:
        watch(reader, "warm-up", 3.0)
        print_measured_size(reader, devs)
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


def cmd_restore(args):
    """Safety net: cameras of --kind (A, B, C or all; narrowed by --port) to their own defaults
    (each camera's GET_DEF: exposure, gain, then AE mode = auto), read back per kind."""
    guard()
    be = usb_backend()
    kinds = list(KINDS) if args.kind == "all" else [args.kind]
    results = []
    for kind in kinds:
        devs = find_kind(be, kind, args.port)
        print(f"[{now()}] kind {kind}: {len(devs)} camera(s), ports {', '.join(rig_port(d) for d in devs) or '-'}")
        for dev in devs:
            lay = vc_layout(dev)
            print(f"  {kind_label(dev)}  {label(dev)}")
            results.append(restore_defaults(dev, lay))
            read_all(dev, lay, "ae_priority", (GET_CUR,))
    print(f"[{now()}] restore {'OK on every camera restored' if all(results) else 'INCOMPLETE: see MISMATCH lines'}")


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
    print(f"    restore [{kind_label(dev)}]: ae_mode:=manual(1) first, so exposure accepts the write: "
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
        print(f"    read-back [{kind_label(dev)}] {name}={fmt(got, err, ms)} {'OK' if good else f'MISMATCH (want {want})'}")
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
    print(f"    [{when}] {kind_label(dev)} {location_id(dev):08x} ae_mode={got[0]} exposure={got[1]} ({exp_s}) gain={got[2]}: {verdict}")
    return got == want


def defaults_of(dev, lay):
    """(ae_mode, exposure, gain) defaults from the camera's own GET_DEF (kind B's exposure
    default is 156, A's and C's 157)."""
    mode = xfer(dev, lay, "ae_mode", GET_DEF)[0]
    if mode is None:
        mode = auto_mode_for(dev, lay)
    return (mode, xfer(dev, lay, "exposure", GET_DEF)[0], xfer(dev, lay, "gain", GET_DEF)[0])


DARK_MIN_BASE = 18      # the dark marker's floor is about 8, so it needs a baseline of 18+
BRIGHT_EXPOSURE = 1000  # 100 ms (100 us units): three frame intervals, beyond any auto
                        # exposure capped at the frame interval (AE priority 0)


def identify_index(cv2, args, devs):
    """Which selected camera is behind cv2 index args.index? Opens the index as the recorder
    would and measures brightness. Then, for each candidate in turn, it sets a marker:
    - dark (manual, exposure 0.1 ms, minimum gain), when the baseline is 18 or more;
    - bright (manual, exposure 100 ms, maximum gain), when the scene is dimmer than that.
    It watches the stream and restores that camera to its defaults with read-back. The
    restore is in a finally block, so it also happens on Ctrl-C or an error. A candidate is
    the camera behind the index when the stream drops below 45% of the baseline (dark) or
    rises to 1.5x the baseline and by 6 or more (bright). Prints "index N is kind K at
    0x<location>" and returns (dev, lay); None if no candidate matches or a restore fails.
    --marker dark|bright forces one marker. Writes only to the candidates, which are the
    cameras named in the run."""
    print(f"\n[{now()}] IDENTIFY: which of the {len(devs)} selected camera(s) is behind index {args.index}")
    cap = open_capture(cv2, args)
    if not cap.isOpened():
        print(f"IDENTIFICATION FAILED: index {args.index} did not open")
        return None
    reader = Reader(cap)
    reader.start()
    try:
        watch(reader, "identify: warm-up", 2.0)
        base = watch(reader, "identify: baseline", 1.5)
        if not base == base:
            print("IDENTIFICATION FAILED: no frames in the baseline window")
            return None
        mode = getattr(args, "marker", "auto")
        if mode == "auto":
            mode = "dark" if base >= DARK_MIN_BASE else "bright"
        if mode == "dark" and base < DARK_MIN_BASE:
            print(f"IDENTIFICATION FAILED: baseline brightness {base:.1f} is too dark for the dark marker")
            return None
        print(f"  marker: {mode} (baseline {base:.1f})")
        for dev in devs:
            lay = vc_layout(dev)
            print(f"  candidate {kind_label(dev)} {label(dev)}")
            gmin = xfer(dev, lay, "gain", GET_MIN)[0] or 0
            if mode == "dark":
                exposure, gain = 1, gmin
            else:
                emax = xfer(dev, lay, "exposure", GET_MAX)[0] or BRIGHT_EXPOSURE
                gain = xfer(dev, lay, "gain", GET_MAX)[0] or gmin
                exposure = min(BRIGHT_EXPOSURE, emax)
            restored = False
            try:
                set_manual(dev, lay, exposure, gain)
                seen = watch(reader, f"identify: {mode} marker ({exposure / 10:g} ms, gain {gain}) on {kind_label(dev)}",
                             1.5, settle=0.5 if mode == "dark" else 1.0)
            finally:
                restored = restore_defaults(dev, lay)
            if not restored:
                print("IDENTIFICATION FAILED: the candidate's restore did not read back")
                return None
            # Relative tests only: an absolute threshold would also pass a camera that is
            # not behind the index when the scene itself is dim.
            if mode == "dark":
                hit = seen == seen and seen < 0.45 * base
            else:
                hit = seen == seen and seen >= 1.5 * base and seen - base >= 6
            if hit:
                kind = KIND_OF.get(location_id(dev), "?")
                print(f"index {args.index} is kind {kind} at 0x{location_id(dev):08x} (lab-rig port {rig_port(dev)}): "
                      f"brightness {base:.1f} -> {seen:.1f} with the {mode} marker")
                return dev, lay
            print(f"    {kind_label(dev)}: stream brightness {base:.1f} -> {seen:.1f}, not this camera")
        print(f"IDENTIFICATION FAILED: no selected camera responded to the {mode} marker on index {args.index}; "
              f"stopping before any experiment write")
        return None
    finally:
        reader.stop_evt.set()
        reader.join(timeout=3)
        cap.release()


def identified_or_exit(cv2, args, devs):
    found = identify_index(cv2, args, devs)
    if found is None:
        sys.exit(f"[{now()}] stopped: index {args.index} not identified as a selected camera")
    return found


QUALITY_COLUMNS = ("brightness", "y_mean", "y_p1", "y_p50", "y_p99", "y_max", "noise", "snr", "ge240", "ge250", "black", "fps")


def quality_table(rows):
    """rows: [(label, figures dict)]. A fixed-width table of the measured figures, no verdict."""
    print(f"      {'setting':<16}" + "".join(f"{c:>11}" for c in QUALITY_COLUMNS))
    for name, q in rows:
        cells = []
        for c in QUALITY_COLUMNS:
            v = q.get(c, float("nan"))
            cells.append(f"{v * 100:10.2f}%" if c in ("ge240", "ge250", "black") else f"{v:11.2f}")
        print(f"      {name:<16}" + "".join(cells))


# ---------------------------------------------------------------- dimsweep outputs
# Pictures for the dim-light test (2026-11-25): what each view contains and what each setting
# looks like. All are the frames as OpenCV delivers them (decoded from the camera's MJPEG).
STILL_GAINS = (0, 50, 100)  # one still per exposure at these gains (if in the camera's range)
CLIP_SECONDS = 3.0          # one clip per camera, in auto, before the sweep
CLIP_QUALITY = 90           # the clip is re-encoded as MJPEG at this quality (not bit-identical)


def save_still(cv2, reader, path):
    """The latest decoded frame as a lossless PNG, full size, no overlay (a second lossy
    compression on top of the camera's MJPEG would blur the judgement). Prints the file name or why not."""
    frame = reader.last_frame
    if frame is None:
        print(f"    still: no frame to save for {os.path.basename(path)}")
        return
    ok = cv2.imwrite(path, frame)
    print(f"    still: {os.path.basename(path)}{'' if ok else ' NOT WRITTEN'}")


def save_pair(cv2, reader, folder, label_):
    """Two consecutive decoded frames as lossless PNG: the exact pixels the noise column is
    computed from (frame-to-frame difference), for checking it by eye or in other software."""
    got, seen = [], None
    deadline = time.monotonic() + 2.0
    while len(got) < 2 and time.monotonic() < deadline:
        f = reader.last_frame
        if f is not None and f is not seen:
            got.append(f)
            seen = f
        time.sleep(0.005)
    for k, f in enumerate(got, start=1):
        cv2.imwrite(os.path.join(folder, f"{label_}_pair{k}.png"), f)
    print(f"    frame pair: {len(got)} consecutive frame(s) saved as {label_}_pair1/2.png")


def record_clip(cv2, reader, path, seconds, fps):
    """`seconds` of the stream as an MJPEG .avi (OpenCV's own writer, quality CLIP_QUALITY).
    Re-encoded, so compression differs slightly from what the camera sent."""
    shape = reader.shape
    if shape is None:
        print("    clip: no frame yet, not recorded")
        return
    size = (shape[1], shape[0])
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    writer = cv2.VideoWriter(path, cv2.CAP_OPENCV_MJPEG, fourcc, fps, size,
                             [cv2.VIDEOWRITER_PROP_QUALITY, CLIP_QUALITY])
    if not writer.isOpened():
        print(f"    clip: writer did not open for {os.path.basename(path)}; not recorded")
        return
    count = [0]

    def sink(frame):
        writer.write(frame)
        count[0] += 1
    with reader.sink_lock:
        reader.sink = sink
    time.sleep(seconds)
    with reader.sink_lock:
        reader.sink = None
    writer.release()
    print(f"    clip: {os.path.basename(path)}, {count[0]} frames in {seconds:g} s (re-encoded MJPEG, quality {CLIP_QUALITY})")


def sweep_index(cv2, args, devs):
    """Identify the camera behind args.index among devs, then, on that camera only, while
    the index streams: for each manual exposure in args.exposures, step gain through its
    range, measuring the luminance statistics at each step, and in auto before and after.
    With args.out_dir set (dimsweep), also saves into a folder for this index: a still in
    auto before and at each exposure at gains STILL_GAINS, a clip and a frame pair in auto.
    Restores the camera to its own defaults (also on Ctrl-C or an error) and reads it back.
    Returns a summary dict, or None if not identified."""
    found = identify_index(cv2, args, devs)
    if found is None:
        return None
    dev, lay = found
    kind = KIND_OF.get(location_id(dev), "?")
    emin, _, _ = xfer(dev, lay, "exposure", GET_MIN)
    emax, _, _ = xfer(dev, lay, "exposure", GET_MAX)
    exposures = [max(emin or 1, min(e, emax or e)) for e in args.exposures]
    print(f"\n[{now()}] GAIN SWEEP: index {args.index}, kind {kind} at 0x{location_id(dev):08x}; manual exposures "
          f"{', '.join(f'{e / 10:g} ms' for e in exposures)}; gain step {args.step}")
    for e in exposures:
        if e >= EXPOSURE_LIMIT:
            print(f"  WARNING: exposure {e / 10:g} ms is NOT under 5 ms (the specification's limit); "
                  f"included for comparison")
    cap = open_capture(cv2, args)
    if not cap.isOpened():
        print("capture did not open; nothing written")
        return None
    reader = Reader(cap)
    reader.start()
    rows, written = [], False
    out = getattr(args, "out_dir", None)
    pics = None
    if out:
        pics = os.path.join(out, f"index{args.index}-0x{location_id(dev):08x}")
        tag0 = f"0x{location_id(dev):08x}_{kind}"  # every file name says which camera it is
        os.makedirs(pics, exist_ok=True)
        print(f"  pictures for this camera go to {os.path.relpath(pics, out)}/")
    try:
        watch(reader, "warm-up", 3.0)
        print_measured_size(reader, [dev])
        gmin, e1, _ = xfer(dev, lay, "gain", GET_MIN)
        gmax, e2, _ = xfer(dev, lay, "gain", GET_MAX)
        if e1 or e2:
            print("    gain range unreadable; nothing written")
            return None
        rows.append(("auto (before)", watch_quality(reader, "auto (before)", args.dwell * 2)))
        if pics:
            save_still(cv2, reader, os.path.join(pics, f"{tag0}_auto_before.png"))
            save_pair(cv2, reader, pics, f"{tag0}_auto")
            record_clip(cv2, reader, os.path.join(pics, f"{tag0}_auto_clip.avi"), CLIP_SECONDS, args.fps or 30)
        gains = list(range(gmin, gmax + 1, args.step))
        if gains[-1] != gmax:
            gains.append(gmax)
        written = True
        for exposure in exposures:
            set_manual(dev, lay, exposure, gmin)
            for g in gains:
                w = xfer(dev, lay, "gain", SET_CUR, g)
                rb, rb_err, _ = xfer(dev, lay, "gain", GET_CUR)
                tag = f"{exposure / 10:g} ms, gain {g} (write {fmt(*w)}, read back {'ERR' if rb_err else rb})"
                rows.append((f"{exposure / 10:g} ms gain {g}", watch_quality(reader, tag, args.dwell, settle=args.settle)))
                if pics and g in STILL_GAINS:
                    save_still(cv2, reader, os.path.join(pics, f"{tag0}_{exposure / 10:g}ms_gain{g}.png"))
        restore_defaults(dev, lay)
        written = False
        rows.append(("auto (after)", watch_quality(reader, "auto (after)", args.dwell * 2, settle=1.0)))
        if pics:
            save_still(cv2, reader, os.path.join(pics, f"{tag0}_auto_after.png"))
            n_files = len(os.listdir(pics))
            print(f"    pictures: {n_files} files in {os.path.basename(pics)}/")
    finally:
        if written:
            restore_defaults(dev, lay)
        reader.stop_evt.set()
        reader.join(timeout=3)
        cap.release()
    print(f"    table, index {args.index}, kind {kind} at 0x{location_id(dev):08x} (measurements only; "
          f"brightness = mean of B, G, R; Y = luminance; noise = temporal, one frame; snr = Y mean / noise):")
    quality_table(rows)
    readback_line(dev, lay, defaults_of(dev, lay), "after the sweep, stream released, expected = this camera's defaults")
    return {"index": args.index, "kind": kind, "loc": location_id(dev), "exposures": exposures, "rows": rows}


def cmd_gainsweep(args):
    """Gain sweep at a fixed manual exposure on the camera behind --index (see sweep_index)."""
    guard()
    be = usb_backend()
    devs = find_selected(be, args)
    cv2 = import_cv2()
    if sweep_index(cv2, args, devs) is None:
        sys.exit(f"[{now()}] stopped: index {args.index} not swept")
    print(f"\n[{now()}] final read-back, every selected camera")
    for dev in devs:
        lay = vc_layout(dev)
        readback_line(dev, lay, defaults_of(dev, lay), "final, expected = this camera's defaults")


RESULTS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
NOTES_QUESTIONS = [
    ("rig", "Which Mac, room and stations is this (for example: mac 1, stations 9 and 10)?"),
    ("lighting", "Are the lamps, IR filters and hoods as for a recording? If not, what differs?"),
    ("animals", "Are animals present, or will anything move in view during the test?"),
    ("other", "Anything else to note (press Return if nothing)?"),
]


class Tee:
    """Writes to the terminal and to the run's log file."""

    def __init__(self, stream, fh):
        self.stream, self.fh = stream, fh

    def write(self, s):
        self.stream.write(s)
        self.fh.write(s)
        self.fh.flush()

    def flush(self):
        self.stream.flush()
        self.fh.flush()


def make_results_dir(root=None):
    """A new folder named after the date and time: results/dim-YYYY-MM-DD-HHMMSS."""
    root = root or RESULTS_ROOT
    name = time.strftime("dim-%Y-%m-%d-%H%M%S")
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=False)
    return path


def save_system_reports(out):
    """The USB device list and the macOS camera list, as system_profiler prints them. These
    list what is plugged in; neither opens a camera."""
    for kind, fname in (("SPUSBDataType", "usb-devices.txt"), ("SPCameraDataType", "cameras.txt")):
        try:
            r = subprocess.run(["system_profiler", kind], capture_output=True, text=True, timeout=60)
            text = r.stdout + r.stderr
        except (OSError, subprocess.SubprocessError) as e:
            text = f"system_profiler {kind} failed: {e}\n"
        with open(os.path.join(out, fname), "w") as fh:
            fh.write(text)
        print(f"[{now()}] saved {fname} (system_profiler {kind}, {len(text.splitlines())} lines)")


def ask_notes(out):
    """Asks the operator the NOTES_QUESTIONS and writes the answers to notes.txt."""
    lines = [f"date and time: {now()}"]
    print("\nBefore the test starts, a few questions for the notes (type the answer, then press Return):")
    for key, q in NOTES_QUESTIONS:
        try:
            a = input(f"  {q}\n  > ")
        except EOFError:
            a = "(no answer: no keyboard input)"
        lines.append(f"{key}: {a.strip()}")
    with open(os.path.join(out, "notes.txt"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"[{now()}] saved notes.txt\n")


def pack_results(out):
    """Zips the results folder next to it (results/<name>.zip); returns the zip's path."""
    return shutil.make_archive(out, "zip", root_dir=os.path.dirname(out), base_dir=os.path.basename(out))


def cmd_dimsweep(args):
    """For a rig whose port layout is unknown: every target camera (kinds A, B, C) is a
    candidate; cv2 indices 0 .. N-1 (N = target cameras found, or --count) are each
    identified and swept in turn. Results per index and kind, never pooled.
    Everything goes into one new folder, uvc/results/dim-<date>-<time>/: the log
    (dim-sweep.log), the USB and camera lists, notes.txt (asked at the start unless
    --no-notes), and per camera its stills, frame pair and clip. At the end, also on
    Ctrl-C or an error, the folder is zipped to uvc/results/dim-<date>-<time>.zip: the one
    file the operator sends."""
    guard()
    out = make_results_dir()
    log_fh = open(os.path.join(out, "dim-sweep.log"), "w")
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = Tee(old_out, log_fh), Tee(old_err, log_fh)
    try:
        print(f"[{now()}] probe.py dimsweep  python {sys.version.split()[0]}  pid {os.getpid()}")
        print(f"[{now()}] results folder: {out}")
        if not args.no_notes:
            ask_notes(out)
        save_system_reports(out)
        args.out_dir = out
        _dimsweep_body(args)
    finally:
        print(f"\n[{now()}] packing the results folder ...")
        sys.stdout.flush()
        sys.stdout, sys.stderr = old_out, old_err
        log_fh.close()
        try:
            z = pack_results(out)
            print(f"[{now()}] RESULTS FILE TO SEND: {z} ({os.path.getsize(z) / 1e6:.1f} MB)")
        except OSError as e:
            print(f"[{now()}] packing failed ({e}); send the whole folder {out} instead")


def _dimsweep_body(args):
    be = usb_backend()
    args.kind, args.port = "any", None
    devs = find_selected(be, args)
    count = args.count or len(devs)
    cv2 = import_cv2()
    results = []
    for i in range(count):
        args.index = i
        r = sweep_index(cv2, args, devs)
        results.append(r or {"index": i})
    print(f"\n[{now()}] SUMMARY (measurements only, per index; no pooling across cameras or kinds)")
    for r in results:
        if "kind" not in r:
            print(f"  index {r['index']}: not identified or not swept (see above)")
            continue
        print(f"  index {r['index']}: kind {r['kind']} at 0x{r['loc']:08x}, exposures "
              f"{', '.join(f'{e / 10:g} ms' for e in r['exposures'])}")
        quality_table(r["rows"])
    print(f"\n[{now()}] final read-back, every target camera")
    for dev in devs:
        lay = vc_layout(dev)
        readback_line(dev, lay, defaults_of(dev, lay), "final, expected = this camera's defaults")


def wait_for_count(finder, want, timeout, poll=0.5):
    """Poll finder() until it returns `want` cameras; return them, or None on timeout."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        devs = finder()
        if len(devs) == want:
            return devs
        time.sleep(poll)
    return None

def cmd_persist(args):
    """Q2: do manual settings survive stream start, stream stop, and unplug/replug?"""
    guard()
    import usb.util
    be = usb_backend()
    state = {"devs": find_selected(be, args)}
    state["lays"] = [vc_layout(d) for d in state["devs"]]
    n = len(state["devs"])
    finder = lambda: find_kind(be, args.kind, args.port)
    want = (AE_MANUAL, TEST_EXPOSURE, args.gain)
    print(f"[{now()}] manual setting under test: exposure {TEST_EXPOSURE / 10:.1f} ms, gain {args.gain}")
    cv2 = import_cv2()
    identified_or_exit(cv2, args, state["devs"])

    print(f"\n[{now()}] PHASE 1: auto baseline brightness (stream at the recorder's format)")
    cap = open_capture(cv2, args)
    if not cap.isOpened():
        sys.exit("capture did not open")
    reader = Reader(cap)
    reader.start()
    watch(reader, "warm-up", 3.0)
    print_measured_size(reader, state["devs"])
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
        # Manual 4 ms can be darker (kinds A, B) or brighter (kind C in its scene) than auto,
        # so the test is a clear change in either direction (persist-C run 2 fixed this).
        changed = abs(b_manual - b_auto) > 0.15 * b_auto
        print(f"    brightness: auto baseline {b_auto:.1f}, after stream start with manual {b_manual:.1f}: "
              + (f"changed ({(b_manual - b_auto) / b_auto * 100:+.0f}%), consistent with the manual exposure being in force"
                 if changed else "NOT changed: the manual exposure may not be in force on the capture"))
        for dev, lay in zip(state["devs"], state["lays"]):
            readback_line(dev, lay, want, "after stream stop")

        if args.no_replug:
            return
        print(f"\n[{now()}] PHASE 4: unplug and replug")
        for dev in state["devs"]:
            usb.util.dispose_resources(dev)
        before = {location_id(d) for d in state["devs"]}
        target_ports = ", ".join(rig_port(d) for d in sorted(state["devs"], key=lambda d: int(rig_port(d)) if rig_port(d).isdigit() else 99))
        print(f">>> UNPLUG the kind {args.kind} camera on PORT {target_ports} now"
              + (" (one of them)" if n > 1 else "")
              + ". Other ports are not watched. Wait about 5 s, then plug it back into the SAME port.")
        print(f"    waiting up to {args.timeout:.0f} s for it to disappear...")
        gone = wait_for_count(finder, n - 1, args.timeout)
        if gone is None:
            print("    no camera disappeared; skipping the replug test")
            return
        missing = before - {location_id(d) for d in gone}
        print(f"[{now()}] disappeared: {', '.join(f'0x{m:08x}' for m in missing)}. Now plug it back in.")
        back = wait_for_count(finder, n, args.timeout)
        if back is None:
            state["devs"], state["lays"] = gone, [vc_layout(d) for d in gone]
            print(f"!!! the camera did not come back within {args.timeout:.0f} s. Plug it in, then run "
                  f"`uvc/.venv/bin/python uvc/probe.py restore --kind {args.kind}`.")
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
        print(f"\n[{now()}] RESTORE every camera written to, to its own defaults")
        results = []
        for dev, lay in zip(state["devs"], state["lays"]):
            print(f"  {kind_label(dev)}  {label(dev)}")
            results.append(restore_defaults(dev, lay))
        print(f"[{now()}] restore {'OK on every camera written to' if all(results) else 'INCOMPLETE: see MISMATCH lines'}")


def scan_index(cv2, config_path, idx, seconds):
    """Open cv2 index idx as the recorder would (format from config entry idx, or the
    defaults for an index beyond the list), read for `seconds`, release. cv2 only: no
    control requests. Returns (opened, frames, failed, mean brightness)."""
    with open(config_path) as fh:
        cfg = json.load(fh)
    cams = cfg.get("cameras", [])
    cam = cams[idx] if idx < len(cams) else {}
    a = argparse.Namespace(index=idx, config=config_path,
                           width=cam.get("width") or cfg["default_width"],
                           height=cam.get("height") or cfg["default_height"],
                           fps=cam.get("fps") or cfg["default_fps"])
    cap = open_capture(cv2, a)
    if not cap.isOpened():
        cap.release()
        return False, 0, 0, float("nan")
    reader = Reader(cap)
    reader.start()
    t0 = time.monotonic()
    time.sleep(seconds)
    n, bad, _fps, mean = reader.window(t0, time.monotonic())
    reader.stop_evt.set()
    reader.join(timeout=3)
    cap.release()
    return True, n, bad, mean


def cmd_scan(args):
    """Read-only (cv2 only, no control requests): open each index in turn, report what it
    delivers, and end with one machine-readable SCAN_RESULT line for a parent process."""
    guard()
    cv2 = import_cv2()
    results = {}
    for idx in range(args.first, args.last + 1):
        opened, n, bad, mean = scan_index(cv2, args.config, idx, args.seconds)
        results[idx] = [opened, n, bad, None if mean != mean else round(mean, 1)]
        print(f"  SCAN index {idx}: opened={opened} frames={n} failed_reads={bad} "
              f"mean_brightness={'-' if mean != mean else f'{mean:.1f}'}")
    print("SCAN_RESULT " + json.dumps(results))


def scan_fresh_process(args, label_):
    """Run `probe.py scan` in a NEW Python process, as the recorder runs each camera server in
    a new process; echo its output and return {index: [opened, frames, failed, mean]}."""
    import subprocess
    cmd = [sys.executable, os.path.abspath(__file__), "scan", "--config", args.config,
           "--first", "0", "--last", str(args.scan_last), "--seconds", str(args.scan_seconds)]
    print(f"  [{now()}] {label_}: fresh-process scan of indices 0-{args.scan_last}")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    result = {}
    for line in out.stdout.splitlines():
        if line.startswith("SCAN_RESULT "):
            result = {int(k): v for k, v in json.loads(line[len("SCAN_RESULT "):]).items()}
        elif "SCAN index" in line or "FORMAT requested" in line:
            print(f"    | {line.strip()}")
    if out.returncode != 0:
        print(f"    | scan exited {out.returncode}: {out.stderr.strip()[-300:]}")
    print(f"    {label_}: {marker_verdict(result)}")
    return result


def dark_indices(result):
    """Indices whose stream delivered and was far darker than the median of those delivering."""
    live = {i: v[3] for i, v in result.items() if v[0] and v[1] > 0 and v[3] is not None}
    if not live:
        return []
    vals = sorted(live.values())
    median = vals[len(vals) // 2]
    return sorted(i for i, b in live.items() if b < 0.4 * median or b < 25)


def marker_verdict(result):
    """Which index shows the dark marker: delivering, and far darker than the median."""
    live = {i: v[3] for i, v in result.items() if v[0] and v[1] > 0 and v[3] is not None}
    if not live:
        return "no index delivered frames"
    vals = sorted(live.values())
    median = vals[len(vals) // 2]
    dark = dark_indices(result)
    silent = [i for i, v in result.items() if v[0] and v[1] == 0]
    text = (f"delivering indices {sorted(live)}; dark marker at index {dark if dark else 'none'}"
            f" (median brightness {median:.1f})")
    if silent:
        text += f"; opened but NO frames: {silent}"
    return text


MARK = (AE_MANUAL, 1, 0)  # the replug marker: manual, exposure 0.1 ms, gain 0 (near-black image)


def fresh_hold(args, dev, lay, label_):
    """A NEW process streams args.index for 6 s; meanwhile this process reads the marked
    camera's controls (at about 3 s, once the child's capture is open), then reports the
    child's frames and brightness. Shows whether a fresh stream start resets the controls."""
    import subprocess
    cmd = [sys.executable, os.path.abspath(__file__), "scan", "--config", args.config,
           "--first", str(args.index), "--last", str(args.index), "--seconds", "6"]
    print(f"  [{now()}] {label_}: fresh process streams index {args.index} for 6 s; controls read while it streams")
    child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(3.0)
    readback_line(dev, lay, MARK, f"{label_}: DURING fresh-process stream")
    out, err = child.communicate(timeout=60)
    for line in out.splitlines():
        if "SCAN index" in line:
            print(f"    | {line.strip()}")
    if child.returncode != 0:
        print(f"    | hold exited {child.returncode}: {err.strip()[-300:]}")
    readback_line(dev, lay, MARK, f"{label_}: after fresh-process stream")


def in_process_index(cv2, args, label_):
    """Open args.index in THIS process (as persist run 2 did after the replug)."""
    opened, n, bad, mean = scan_index(cv2, args.config, args.index, 2.0)
    print(f"  [{now()}] {label_}: same-process VideoCapture({args.index}): opened={opened} frames={n} "
          f"failed_reads={bad} mean_brightness={'-' if mean != mean else f'{mean:.1f}'}")


def cmd_replug(args):
    """Reproduce persist run 2's empty stream after a replug: mark the camera on --port dark,
    unplug/replug it, then at several delays compare a same-process reopen of --index with a
    fresh-process scan of every index, and find which index the marked camera is on."""
    guard()
    import usb.util
    be = usb_backend()
    cv2 = import_cv2()
    if args.port is None:
        if args.kind != "A":
            sys.exit("replug needs --port for kinds B and C (it writes to that one camera only)")
        args.port = 1
    state = {"devs": find_selected(be, args)}  # the one camera on --port
    state["lays"] = [vc_layout(d) for d in state["devs"]]
    finder = lambda: find_kind(be, args.kind, args.port)

    def marked():
        for d, l in zip(state["devs"], state["lays"]):
            if rig_port(d) == str(args.port):
                return d, l
        return None, None

    dev, lay = marked()
    if dev is None:
        sys.exit(f"no kind {args.kind} camera on port {args.port}")
    print(f"[{now()}] CLEAN START: the camera on port {args.port} to its defaults, read back, before anything else")
    clean = [restore_defaults(d, l) for d, l in zip(state["devs"], state["lays"])]
    if not all(clean):
        sys.exit("clean start incomplete (see MISMATCH lines); not starting the test")
    print(f"[{now()}] marked camera: port {args.port} {label(dev)}; marker = manual, exposure 1 (0.1 ms), gain 0")
    try:
        set_manual(dev, lay, 1, 0)
        print(f"\n[{now()}] BEFORE the unplug")
        in_process_index(cv2, args, "before")
        readback_line(dev, lay, MARK, "before: after same-process open")
        fresh_hold(args, dev, lay, "before")
        before = scan_fresh_process(args, "before")
        readback_line(dev, lay, MARK, "before: after fresh-process scan")
        if dark_indices(before) != [args.index]:
            print(f"IDENTIFICATION FAILED: the marked camera (port {args.port}) is dark at indices "
                  f"{dark_indices(before)}, not exactly [{args.index}]; stopping before the replug")
            return
        print(f"index {args.index} is kind {args.kind} at port {args.port} (0x{location_id(dev):08x}): "
              f"the only dark index in the fresh-process scan")

        for d in state["devs"]:
            usb.util.dispose_resources(d)
        n = len(state["devs"])
        print(f"\n>>> UNPLUG the kind {args.kind} camera on PORT {args.port} now, wait about 5 s, then plug it back into port {args.port}.")
        gone = wait_for_count(finder, n - 1, args.timeout)
        if gone is None:
            print(f"    the camera on port {args.port} did not disappear; stopping")
            return
        print(f"[{now()}] disappeared: {sorted(f'0x{location_id(d):08x}' for d in state['devs'] if location_id(d) not in {location_id(g) for g in gone})}")
        back = wait_for_count(finder, n, args.timeout)
        if back is None:
            state["devs"], state["lays"] = gone, [vc_layout(d) for d in gone]
            print(f"!!! not back within {args.timeout:.0f} s; plug it in, then run `uvc/.venv/bin/python uvc/probe.py restore --kind {args.kind} --port {args.port}`")
            return
        t_back = time.monotonic()
        state["devs"], state["lays"] = back, [vc_layout(d) for d in back]
        print(f"[{now()}] re-enumerated (t=0 for the checks below)")
        dev, lay = marked()
        if dev is None:
            print(f"    port {args.port} not among the re-enumerated targets; stopping")
            return
        readback_line(dev, lay, defaults_of(dev, lay), "after replug, expected = this camera's defaults")
        set_manual(dev, lay, 1, 0)

        for at in args.checks:
            wait = t_back + at - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            print(f"\n[{now()}] CHECK at t={time.monotonic() - t_back:.0f} s after re-enumeration")
            readback_line(dev, lay, MARK, f"t+{at:g}s: start of check")
            in_process_index(cv2, args, f"t+{at:g}s")
            readback_line(dev, lay, MARK, f"t+{at:g}s: after same-process open")
            fresh_hold(args, dev, lay, f"t+{at:g}s")
            scan_fresh_process(args, f"t+{at:g}s")
            readback_line(dev, lay, MARK, f"t+{at:g}s: after fresh-process scan")
    finally:
        print(f"\n[{now()}] RESTORE every camera written to, to its own defaults")
        results = []
        for d, l in zip(state["devs"], state["lays"]):
            print(f"  {kind_label(d)}  {label(d)}")
            results.append(restore_defaults(d, l))
        print(f"[{now()}] restore {'OK on all targets present' if all(results) else 'INCOMPLETE: see MISMATCH lines'}")


def parse_streams(specs):
    """'C:4:3' -> ('C', 4, 3): kind, rig port, cv2 index."""
    out = []
    for spec in specs:
        kind, port, index = spec.split(":")
        if kind not in KINDS:
            sys.exit(f"--streams {spec}: kind must be one of {sorted(KINDS)}")
        out.append((kind, int(port), int(index)))
    return out


def cmd_soakone(args):
    """Child of `soak`, read-only (cv2 only, no control requests): stream one index for
    --minutes in this fresh process, report every --report s, log every gap of at least
    --stall s between good frames, keep reading through a stall to see if frames return,
    and end with one SOAK_SUMMARY line. Stops early if a recording starts."""
    cv2 = import_cv2()
    ns = argparse.Namespace(index=args.index, config=args.config, width=None, height=None, fps=None)
    cap = open_capture(cv2, ns)
    tag = args.label
    if not cap.isOpened():
        print(f"SOAK_SUMMARY {json.dumps({'label': tag, 'opened': False})}", flush=True)
        return
    t_start = time.monotonic()
    t_end = t_start + args.minutes * 60
    last_good = t_start
    stalls = []          # [start offset s, duration s, recovered]
    in_stall = None      # start offset of an ongoing stall
    frames = failed = 0
    win = {"frames": 0, "failed": 0, "sum": 0.0, "max_gap": 0.0}
    next_report = t_start + args.report
    stop_reason = "time"
    try:
        while True:
            now_t = time.monotonic()
            if now_t >= t_end:
                break
            ok, frame = cap.read()
            t = time.monotonic()
            if ok:
                gap = t - last_good
                win["max_gap"] = max(win["max_gap"], gap)
                if in_stall is not None:
                    stalls[-1][1], stalls[-1][2] = round(t - t_start - in_stall, 2), True
                    print(f"  [{now()}] {tag} STALL ENDED after {stalls[-1][1]:.1f} s: frames resumed", flush=True)
                    in_stall = None
                last_good = t
                frames += 1
                win["frames"] += 1
                win["sum"] += float(frame.mean())
            else:
                failed += 1
                win["failed"] += 1
            if in_stall is None and t - last_good >= args.stall:
                in_stall = round(last_good - t_start, 2)
                stalls.append([in_stall, None, False])
                print(f"  [{now()}] {tag} STALL: no frame for {args.stall:g} s "
                      f"(last good frame at +{in_stall:.1f} s)", flush=True)
            if t >= next_report:
                span = args.report
                mean = win["sum"] / win["frames"] if win["frames"] else float("nan")
                print(f"  [{now()}] {tag} +{(t - t_start) / 60:4.1f} min: frames={win['frames']} "
                      f"fps={win['frames'] / span:4.1f} failed_reads={win['failed']} "
                      f"mean_brightness={mean:5.1f} longest_gap={win['max_gap']:.2f} s"
                      + (" (IN STALL)" if in_stall is not None else ""), flush=True)
                win = {"frames": 0, "failed": 0, "sum": 0.0, "max_gap": 0.0}
                next_report += args.report
                if os.path.exists(RECORDING_FLAG):
                    stop_reason = "recording started"
                    print(f"  [{now()}] {tag} STOPPING: {RECORDING_FLAG} appeared", flush=True)
                    break
    except KeyboardInterrupt:
        stop_reason = "interrupted"
    finally:
        cap.release()
    if in_stall is not None:
        stalls[-1][1] = round(time.monotonic() - t_start - in_stall, 2)
    dur = time.monotonic() - t_start
    summary = {"label": tag, "opened": True, "seconds": round(dur, 1), "frames": frames,
               "expected_at_30fps": int(dur * 30), "failed_reads": failed, "stalls": stalls,
               "stall_seconds": round(sum(s[1] or 0 for s in stalls), 2), "stop": stop_reason}
    print(f"SOAK_SUMMARY {json.dumps(summary)}", flush=True)


def cmd_soak(args):
    """Read-only soak after identification: each --streams camera is identified (marker,
    restored and read back: the only writes), then streamed by a new process per camera for
    --minutes, counting stalls. Default: kind C ports 4 and 7, with kind A port 1 as the
    reference."""
    import subprocess
    guard()
    be = usb_backend()
    cv2 = import_cv2()
    streams = parse_streams(args.streams)
    print(f"[{now()}] SOAK {args.minutes:g} min; streams (kind:port:index) {args.streams}; "
          f"stall = no frame for {args.stall:g} s; report every {args.report:g} s")
    for kind, port, index in streams:
        devs = find_kind(be, kind, port)
        if not devs:
            sys.exit(f"no kind {kind} camera on port {port}")
        ns = argparse.Namespace(index=index, config=args.config, width=None, height=None, fps=None)
        if identify_index(cv2, ns, devs) is None:
            sys.exit(f"[{now()}] stopped: index {index} not identified as kind {kind} at port {port}; nothing streamed")
    print(f"\n[{now()}] all {len(streams)} identified; starting the soak (read-only from here on)")
    children = []
    for kind, port, index in streams:
        label = f"kind {kind} port {port} index {index}"
        cmd = [sys.executable, os.path.abspath(__file__), "soakone", "--index", str(index),
               "--config", args.config, "--minutes", str(args.minutes), "--report", str(args.report),
               "--stall", str(args.stall), "--label", label]
        children.append((label, subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)))
    summaries = {}

    def relay(label, proc):
        for line in proc.stdout:
            if line.startswith("SOAK_SUMMARY "):
                summaries[label] = json.loads(line[len("SOAK_SUMMARY "):])
            elif "FORMAT" in line or "STALL" in line or " min:" in line or "STOPPING" in line:
                print(line.rstrip(), flush=True)

    threads = [threading.Thread(target=relay, args=c, daemon=True) for c in children]
    for t in threads:
        t.start()
    try:
        for _, proc in children:
            proc.wait()
    except KeyboardInterrupt:
        print(f"[{now()}] interrupted; stopping the streams")
        for _, proc in children:
            proc.send_signal(2)
        for _, proc in children:
            proc.wait()
    for t in threads:
        t.join(timeout=5)
    print(f"\n[{now()}] SOAK SUMMARY (per camera; never pooled)")
    for label, _ in children:
        s = summaries.get(label)
        if not s:
            print(f"  {label}: no summary (child failed)")
        elif not s["opened"]:
            print(f"  {label}: did not open")
        else:
            stall_txt = "; ".join(f"at +{st[0]:.0f} s for {st[1]:.1f} s{'' if st[2] else ' (NOT recovered)'}" for st in s["stalls"]) or "none"
            print(f"  {label}: {s['seconds']:.0f} s, frames {s['frames']} of ~{s['expected_at_30fps']} at 30 fps, "
                  f"failed reads {s['failed_reads']}, stalls {len(s['stalls'])} ({s['stall_seconds']:.1f} s): {stall_txt}; "
                  f"stopped by {s['stop']}")


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
    stream.add_argument("--kind", choices=sorted(KINDS) + ["any"], default="A",
                        help="camera kind to select and write to (A 0x6369, B/C 0x6366 by name string; "
                             "any = all three kinds); default A")
    stream.add_argument("--port", type=int, help="limit the run to the camera of --kind on this rig port (lab rig only)")
    stream.add_argument("--marker", choices=["auto", "dark", "bright"], default="auto",
                        help="identification marker: dark (0.1 ms, min gain), bright (100 ms, max gain), "
                             "or auto = dark unless the baseline brightness is under 18")
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
    sweep = argparse.ArgumentParser(add_help=False)
    sweep.add_argument("--exposures", type=int, nargs="+", default=[20, 30, 40, 100],
                       help="manual exposures in 100 us units, each with a full gain sweep (default 20 30 40 100 "
                            "= 2, 3, 4 and 10 ms; the specification's limit is under 50 = 5 ms, so 10 ms warns)")
    sweep.add_argument("--step", type=int, default=10, help="gain step through the camera's range")
    sweep.add_argument("--dwell", type=float, default=1.0, help="seconds measured per gain step")
    sweep.add_argument("--settle", type=float, default=0.5, help="seconds discarded after each gain write")
    gs = sub.add_parser("gainsweep", parents=[stream, sweep])
    gs.set_defaults(fn=cmd_gainsweep)
    ds = sub.add_parser("dimsweep", parents=[stream, sweep],
                        help="gain sweep on every camera of an unknown rig, indices 0..N-1")
    ds.add_argument("--count", type=int, help="number of cv2 indices to sweep (default: target cameras found)")
    ds.add_argument("--no-notes", action="store_true", help="do not ask the notes questions at the start")
    ds.set_defaults(fn=cmd_dimsweep, width=640, height=480, fps=30)
    ps = sub.add_parser("persist", parents=[stream])
    ps.add_argument("--gain", type=int, default=10, help="manual gain under test (non-default, so a revert shows)")
    ps.add_argument("--no-replug", action="store_true", help="stop after the stream start/stop phases")
    ps.add_argument("--timeout", type=float, default=120.0, help="seconds to wait for unplug, and again for replug")
    ps.add_argument("--settle-after-replug", type=float, default=2.0)
    ps.set_defaults(fn=cmd_persist)
    sc = sub.add_parser("scan")
    sc.add_argument("--config", default=RECORDER_CONFIG)
    sc.add_argument("--first", type=int, default=0)
    sc.add_argument("--last", type=int, default=9)
    sc.add_argument("--seconds", type=float, default=1.0)
    sc.set_defaults(fn=cmd_scan)
    rp = sub.add_parser("replug", parents=[stream])
    rp.add_argument("--checks", type=float, nargs="+", default=[3, 30, 60, 120],
                    help="seconds after re-enumeration at which to check")
    rp.add_argument("--scan-last", type=int, default=9, help="highest index in the fresh-process scans")
    rp.add_argument("--scan-seconds", type=float, default=1.0)
    rp.add_argument("--timeout", type=float, default=120.0)
    rp.set_defaults(fn=cmd_replug)
    sk = sub.add_parser("soak")
    sk.add_argument("--streams", nargs="+", default=["C:4:3", "C:7:6", "A:1:0"],
                    help="kind:port:index for each camera to soak (default C:4:3 C:7:6 A:1:0)")
    sk.add_argument("--minutes", type=float, default=20.0)
    sk.add_argument("--report", type=float, default=60.0, help="seconds between status lines")
    sk.add_argument("--stall", type=float, default=1.0, help="a gap of this many seconds without a frame is a stall")
    sk.add_argument("--config", default=RECORDER_CONFIG)
    sk.set_defaults(fn=cmd_soak)
    so = sub.add_parser("soakone")
    so.add_argument("--index", type=int, required=True)
    so.add_argument("--minutes", type=float, required=True)
    so.add_argument("--report", type=float, default=60.0)
    so.add_argument("--stall", type=float, default=1.0)
    so.add_argument("--label", default="")
    so.add_argument("--config", default=RECORDER_CONFIG)
    so.set_defaults(fn=cmd_soakone)
    rs = sub.add_parser("restore")
    rs.add_argument("--kind", choices=sorted(KINDS) + ["all"], default="A")
    rs.add_argument("--port", type=int, help="only the camera on this rig port")
    rs.set_defaults(fn=cmd_restore)
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # cues must appear live through tee
    print(f"[{now()}] probe.py {args.cmd}  python {sys.version.split()[0]}  pid {os.getpid()}")
    args.fn(args)


if __name__ == "__main__":
    main()
