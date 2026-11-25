#!/usr/bin/env python3
"""Offline test of the dimsweep results folder (stills, frame pair, clip, notes, system
lists, log, zip). It cannot reach a camera:
- a fake `cv2` module is installed in sys.modules before probe is imported, so the real
  OpenCV is never loaded; its capture returns synthetic frames;
- the USB side (backend, camera selection, identification, control transfers, restore) is
  replaced by stand-ins in this process; no USB request is sent;
- subprocess.run is replaced, so system_profiler is not run;
- the results go to a temporary folder, not uvc/results.

    uvc/.venv/bin/python -I uvc/offline_tests/test_dim_outputs.py
"""

import io
import os
import subprocess
import sys
import tempfile
import time
import types
import zipfile

try:
    import numpy as np
except ImportError:  # numpy lives in the base interpreter's site-packages, as in probe.import_cv2
    sys.path.append(os.path.join(sys.base_prefix, "lib", f"python{sys.version_info.major}.{sys.version_info.minor}",
                                 "site-packages"))
    import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
UVC = os.path.dirname(HERE)


class FakeCapture:
    opened = []

    def __init__(self, index):
        self.index = index
        FakeCapture.opened.append(index)
        self.rng = np.random.default_rng(index)

    def isOpened(self):
        return True

    def set(self, *_):
        return True

    def get(self, *_):
        return 0

    def getBackendName(self):
        return "FAKE"

    def read(self):
        time.sleep(1 / 30.0)
        return True, self.rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)

    def release(self):
        pass


class FakeWriter:
    def __init__(self, path, api, fourcc, fps, size, params):
        self.path, self.n = path, 0
        FakeWriter.last = self

    def isOpened(self):
        return True

    def write(self, frame):
        self.n += 1

    def release(self):
        with open(self.path, "wb") as fh:
            fh.write(b"AVI" * self.n)


def fake_imwrite(path, frame, params=None):
    with open(path, "wb") as fh:
        fh.write(b"IMG" + bytes(str(frame.shape), "ascii"))
    return True


cv2 = types.ModuleType("cv2")
cv2.__version__ = "fake"
cv2.VideoCapture, cv2.VideoWriter, cv2.imwrite = FakeCapture, FakeWriter, fake_imwrite
cv2.VideoWriter_fourcc = lambda *c: 0
cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT, cv2.CAP_PROP_FPS, cv2.CAP_PROP_FOURCC = 3, 4, 5, 6
cv2.CAP_OPENCV_MJPEG, cv2.VIDEOWRITER_PROP_QUALITY, cv2.IMWRITE_JPEG_QUALITY = 2200, 1, 1
sys.modules["cv2"] = cv2


def refuse(*_a, **_k):
    raise RuntimeError("offline test: starting a process or a USB request is not allowed")


subprocess.Popen = refuse
sys.path.insert(0, UVC)
import probe  # noqa: E402

tmp = tempfile.mkdtemp(prefix="dimtest-", dir=os.environ.get("TMPDIR"))
probe.RESULTS_ROOT = tmp
probe.guard = lambda: None
probe.usb_backend = lambda: object()
probe.import_cv2 = lambda: cv2
LOCS = {0: 0x01141000, 1: 0x01142000}
devs = [types.SimpleNamespace(loc=v) for v in LOCS.values()]
probe.find_selected = lambda be, args: devs
probe.location_id = lambda dev: dev.loc
probe.KIND_OF.update({v: "C" for v in LOCS.values()})
probe.identify_index = lambda cv2_, args, devs_: (devs[args.index], None)
RANGES = {("gain", probe.GET_MIN): 0, ("gain", probe.GET_MAX): 100,
          ("exposure", probe.GET_MIN): 1, ("exposure", probe.GET_MAX): 5000}
writes = []


def fake_xfer(dev, lay, name, req, value=None):
    if req == probe.SET_CUR:
        writes.append((dev.loc, name, value))
    return RANGES.get((name, req), value if value is not None else 0), None, 0.1


probe.xfer = fake_xfer
probe.set_manual = lambda dev, lay, e, g: writes.append((dev.loc, "manual", e, g))
restores = []
probe.restore_defaults = lambda dev, lay: restores.append(dev.loc) or True
probe.readback_line = lambda *a, **k: None
probe.defaults_of = lambda dev, lay: (8, 157, 0)
probe.vc_layout = lambda dev: None
probe.print_measured_size = lambda reader, devs_=(): None
probe.CLIP_SECONDS = 0.4
probe.subprocess.run = lambda cmd, **k: types.SimpleNamespace(stdout=f"FAKE {cmd[1]}\n", stderr="")

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), file=sys.__stdout__)
    if not cond:
        failures.append(name)


def args(**kw):
    a = types.SimpleNamespace(exposures=[20, 30], step=50, dwell=0.1, settle=0.0, count=None, no_notes=False,
                              width=640, height=480, fps=30, config=None, marker="auto", kind="any", port=None)
    a.__dict__.update(kw)
    return a


# 1. a full run with notes answered from stdin
sys.stdin = io.StringIO("mac 1, stations 9 and 10\nyes\nno animals\n\n")
probe.cmd_dimsweep(args())
runs = sorted(d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d)))
check("one results folder, auto-named", len(runs) == 1 and runs[0].startswith("dim-20"), runs)
out = os.path.join(tmp, runs[0])
top = sorted(os.listdir(out))
check("log, lists and notes at the top", {"dim-sweep.log", "usb-devices.txt", "cameras.txt", "notes.txt"} <= set(top), top)
notes = open(os.path.join(out, "notes.txt")).read()
check("notes hold the answers", "rig: mac 1, stations 9 and 10" in notes and "animals: no animals" in notes, notes)
log = open(os.path.join(out, "dim-sweep.log")).read()
check("log holds the sweep and the tables", "GAIN SWEEP: index 1" in log and "auto (after)" in log and "final read-back" in log)
for i, loc in LOCS.items():
    t = f"0x{loc:08x}_C"
    want = {f"{t}_{s}" for s in ("auto_before.png", "auto_after.png", "auto_pair1.png", "auto_pair2.png",
                                 "auto_clip.avi", "2ms_gain0.png", "2ms_gain50.png", "2ms_gain100.png",
                                 "3ms_gain0.png", "3ms_gain50.png", "3ms_gain100.png")}
    d = os.path.join(out, f"index{i}-0x{loc:08x}")
    have = set(os.listdir(d)) if os.path.isdir(d) else set()
    check(f"index {i}: stills, pair and clip", have == want, sorted(have ^ want))
check("clip has frames", os.path.getsize(os.path.join(out, "index0-0x01141000", "0x01141000_C_auto_clip.avi")) > 3 * 5)
z = out + ".zip"
names = set(zipfile.ZipFile(z).namelist()) if os.path.exists(z) else set()
check("zip made next to the folder, holding everything",
      f"{runs[0]}/dim-sweep.log" in names and f"{runs[0]}/index1-0x01142000/0x01142000_C_auto_clip.avi" in names, len(names))
check("each camera restored once after its sweep", restores == [0x01141000, 0x01142000], restores)
check("only the fake captures were opened", set(FakeCapture.opened) == {0, 1}, FakeCapture.opened)

# 2. an error mid-sweep: the camera is restored and the folder is still zipped
restores.clear()
time.sleep(1.1)  # a new second, so a new folder name
real_still = probe.save_still
calls = [0]


def failing_still(cv2_, reader, path):
    calls[0] += 1
    if calls[0] == 3:
        raise KeyboardInterrupt
    real_still(cv2_, reader, path)


probe.save_still = failing_still
try:
    probe.cmd_dimsweep(args(no_notes=True))
    check("interrupt propagates", False)
except KeyboardInterrupt:
    pass
probe.save_still = real_still
runs2 = sorted(d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d)))
check("second run has its own folder", len(runs2) == 2, runs2)
check("interrupted run still zipped", os.path.exists(os.path.join(tmp, runs2[1]) + ".zip"))
check("interrupted camera restored", restores == [0x01141000], restores)
check("no notes asked with --no-notes", not os.path.exists(os.path.join(tmp, runs2[1], "notes.txt")))

print("\n" + ("ALL PASSED" if not failures else f"FAILED: {failures}"), file=sys.__stdout__)
print(f"(temporary results in {tmp})", file=sys.__stdout__)
sys.exit(1 if failures else 0)
