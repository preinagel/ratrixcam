#!/usr/bin/env python3
"""Offline test of uvc/reopen_delay.py. It cannot reach a camera:
- a fake `cv2` module is installed in sys.modules before anything imports cv2, so the real
  OpenCV is never loaded;
- subprocess.Popen and the probe's USB backend are replaced by functions that raise, so no
  child process is started and no USB request is sent;
- everything runs in this one process.

    uvc/.venv/bin/python -I uvc/offline_tests/test_reopen_logic.py

It checks the stall and recovery definitions, the summary, and the two camera-side
functions (the stream before release, and the reopen with its longer watch) against a fake
capture with a scripted stall.
"""

import io
import json
import os
import subprocess
import sys
import time
import types
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
UVC = os.path.dirname(HERE)


class FakeCapture:
    """Scripted stream: nothing for `stall` seconds after opening, then about 29 fps."""
    stall = 0.0
    opened = []

    def __init__(self, index):
        self.index, self.t0 = index, time.time()
        FakeCapture.opened.append(index)

    def isOpened(self):
        return True

    def set(self, *_):
        return True

    def read(self):
        if time.time() - self.t0 < FakeCapture.stall:
            time.sleep(0.25)
            return False, None
        time.sleep(1 / 29.0)
        return True, None

    def release(self):
        pass


fake_cv2 = types.ModuleType("cv2")
fake_cv2.__version__ = "fake"
fake_cv2.VideoCapture = FakeCapture
fake_cv2.CAP_PROP_FRAME_WIDTH, fake_cv2.CAP_PROP_FRAME_HEIGHT, fake_cv2.CAP_PROP_FPS = 3, 4, 5
sys.modules["cv2"] = fake_cv2


def refuse(*_a, **_k):
    raise RuntimeError("offline test: starting a process or a USB request is not allowed")


subprocess.Popen = refuse
sys.path.insert(0, UVC)
import probe  # noqa: E402
probe.usb_backend = refuse
import reopen_delay as rd  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def run_child(fn, **kw):
    out = io.StringIO()
    with redirect_stdout(out):
        fn(types.SimpleNamespace(**kw))
    return [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]


# 1. definitions
normal = {"opened": True, "first10": 290, "bins": [29] * 10, "watched_s": 10.0}
stall = {"opened": True, "first10": 30, "bins": [0] * 7 + [10, 25, 29] + [29] * 10, "watched_s": 60.0}
never = {"opened": True, "first10": 0, "bins": [0] * 60, "watched_s": 60.0}
check("normal reopen is not a stall", not rd.stalled(normal))
check("short first 10 s is a stall", rd.stalled(stall))
check("recovery = first of 3 delivering seconds", rd.recovery_s(stall) == 8, rd.recovery_s(stall))
check("no recovery within the watch gives None", rd.recovery_s(never) is None)
check("a capture that did not open is a stall", rd.stalled({**normal, "opened": False}))

# 2. summary wording
results = [
    {"kind": "A", "delay": 1, "valid": True, "stalled": True, "recovery_s": 8, "reopen": stall,
     "hold": {"first_frame_s": 0.9}},
    {"kind": "A", "delay": 1, "valid": True, "stalled": True, "recovery_s": None, "reopen": never,
     "hold": {"first_frame_s": 1.1}},
    {"kind": "A", "delay": 30, "valid": True, "stalled": False, "recovery_s": None, "reopen": normal,
     "hold": {"first_frame_s": 1.0}},
    {"kind": "A", "delay": 30, "valid": False, "hold": None},
]
lines = []
rd.summarize(results, lines.append)
text = "\n".join(lines)
print(text)
check("summary counts stalls per delay", "delay  1 s: stalled 2 of 2" in text)
check("summary gives stall lengths, including not recovered", "8 s, >60 s" in text)
check("summary counts invalid trials apart", "stalled 0 of 1 (1 invalid)" in text)

# 3. the reopen child with a scripted 3 s stall (watch shortened for the test)
rd.FIRST_WINDOW, rd.MAX_WATCH, rd.STALL_FRAMES = 3.0, 8.0, 60
FakeCapture.stall = 3.0
t_rel = time.time()
ev = run_child(rd.child_reopen, index=99, at=t_rel + 0.5, delay=0.5)
r = ev[-1]
check("reopen waits for the delay", 0.45 <= r["delay_actual"] <= 0.7, r["delay_actual"])
check("stalled first window extends the watch", r["watched_s"] == 8.0 and rd.stalled(r), r["first10"])
check("recovery found after the stall", rd.recovery_s(r) in (3, 4), (rd.recovery_s(r), r["bins"]))
FakeCapture.stall = 0.0
r = run_child(rd.child_reopen, index=99, at=time.time(), delay=0.0)[-1]
check("normal reopen stops after the first window", r["watched_s"] == 3.0 and not rd.stalled(r), r["first10"])

# 4. the stream-before-release child: waits for an unbroken normal run, then releases
FakeCapture.stall = 1.0
e = run_child(rd.child_hold, index=99, stream=1.0, release=True)[-1]
check("hold releases after a normal run", e["event"] == "released", e)
check("hold logs its own start-up stall", e["first_frame_s"] >= 1.0 and e["normal_from_s"] >= 1.0, e)

check("only the fake capture was ever opened", set(FakeCapture.opened) == {99}, FakeCapture.opened)
print("\n" + ("ALL PASSED" if not failures else f"FAILED: {failures}"))
sys.exit(1 if failures else 0)
