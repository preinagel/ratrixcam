#!/usr/bin/env python3
"""How long after a camera is released must a new process wait before reopening it, to avoid
the state where the camera delivers almost no frames for a long time?

RUN THIS: see the commands in uvc/REOPEN_DELAY.md (they need the rig; the recorder must not
be running).

One trial on one camera:
  1. HOLD: a new process opens the camera's cv2 index and streams it until it has streamed
     normally for --stream seconds without a break. Then:
     - in the release arm (`short`, `long`), it calls capture.release() and exits, as the
       recorder's server does when it gives up on a camera;
     - in the `kill` arm, the parent kills it with SIGKILL while it streams, as the
       supervisor's 600 s shutdown timeout would.
     A stall at the start of HOLD (it is itself a reopen) is logged as extra data.
  2. REOPEN: another new process, started right after HOLD ends, imports cv2, waits until
     exactly <delay> seconds after the release (or the kill), calls VideoCapture() and
     counts frames in 1 s bins for 10 s. If the first 10 s hold fewer than STALL_FRAMES
     frames, it keeps watching up to 60 s and records when frames came back.
  3. A rest of REST seconds, then the next trial.
The delays run in a randomised order per camera, with the order logged. With --load, the
other seven cameras stream in their own reader processes (read only, no encoding) while one
camera is tested. Identification (the marker method, restored and read back) runs once at
the end of each camera's sweep, after a rest, and confirms the expected index; a mismatch
invalidates that camera's sweep. No other control request is written.

Measurements only; the summary gives, per kind and delay, how many of the trials stalled
and how long the stalls lasted.
"""

import argparse
import json
import os
import random
import signal
import statistics
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import probe  # noqa: E402  (the probe's selection, capture and restore helpers)

# The three test cameras (rulings, 2026-11-20): lab port -> (kind, cv2 index). Index k =
# port k+1 on this rig, confirmed by markers in dimsweep-lab-run1.
TEST = {"A": (1, 0), "B": (2, 1), "C": (4, 3)}
ALL_INDICES = range(8)
WIDTH, HEIGHT, FPS = 640, 480, 30
FIRST_WINDOW = 10.0      # seconds counted after the reopen
MAX_WATCH = 60.0         # keep watching up to this long when the first window is short
STALL_FRAMES = 230       # first 10 s below this = stalled (normal is about 293 to 303)
RECOVER_BIN = 20         # a 1 s bin with at least this many frames counts as delivering
RECOVER_RUN = 3          # recovered = this many delivering bins in a row
REST = 5.0               # seconds between a trial's REOPEN release and the next HOLD
ID_REST = 60.0           # seconds before the end-of-sweep identification
HOLD_LIMIT = 180.0       # HOLD gives up if it cannot stream normally for this long
ARMS = {
    "short": {"stream": 5.0, "delays": [1, 2, 4, 8, 15, 30, 60], "release": True},
    "long": {"stream": 180.0, "delays": [2, 8, 30], "release": True},
    "kill": {"stream": 5.0, "delays": [8], "release": False},
}
PY = sys.executable
SELF = os.path.abspath(__file__)

LOAD_WARMUP = 30.0
LOAD_REST = 60.0         # rest after the previous camera was released, before the load starts


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def capture_args(index):
    return argparse.Namespace(index=index, config=None, width=WIDTH, height=HEIGHT, fps=FPS)


def open_quiet(cv2, index):
    """VideoCapture as the recorder opens it, without the probe's prints."""
    cap = cv2.VideoCapture(index)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FPS)
    return cap


def emit(**kw):
    print(json.dumps(kw), flush=True)


# ---------------------------------------------------------------- child processes

def child_hold(a):
    """Stream until --stream seconds of normal delivery without a break, then release and
    exit (release arm) or announce READY and keep streaming until killed (kill arm)."""
    cv2 = probe.import_cv2()
    t_open = time.time()
    cap = open_quiet(cv2, a.index)
    t_opened = time.time()
    if not cap.isOpened():
        emit(event="hold_failed", reason="not opened")
        return
    first = None
    run_start = None      # start of the current run without a gap over 0.5 s
    last = None
    frames = 0
    while True:
        ok, _ = cap.read()
        t = time.time()
        if not ok:
            run_start = None
            if t - t_opened > HOLD_LIMIT:
                emit(event="hold_failed", reason="no normal stream", frames=frames)
                cap.release()
                return
            continue
        frames += 1
        first = first or t
        if last is None or t - last > 0.5:
            run_start = t
        last = t
        if t - run_start >= a.stream:
            break
        if t - t_opened > HOLD_LIMIT + a.stream:
            emit(event="hold_failed", reason="no normal stream", frames=frames)
            cap.release()
            return
    startup = {"open_s": round(t_opened - t_open, 3),
               "first_frame_s": round(first - t_open, 3),
               "normal_from_s": round(run_start - t_open, 3), "frames": frames}
    if a.release:
        cap.release()
        emit(event="released", t=time.time(), **startup)
        return
    emit(event="ready", t=time.time(), **startup)
    while True:  # streams until the parent's SIGKILL
        cap.read()


def child_reopen(a):
    """Wait until --at (epoch), open, count frames in 1 s bins; see the module docstring."""
    cv2 = probe.import_cv2()
    late = time.time() - a.at
    if late < 0:
        time.sleep(-late)
    t0 = time.time()
    cap = open_quiet(cv2, a.index)
    t_opened = time.time()
    # The windows and bins count from when the open returns: the open itself takes 0.8-2 s
    # on this rig, which would otherwise eat into the 10 s. The time to the first frame is
    # also reported from the VideoCapture() call (first_frame_s).
    bins = [0] * int(MAX_WATCH)
    first = None
    longest_gap, last = 0.0, t_opened
    opened = cap.isOpened()
    watch = FIRST_WINDOW
    while opened:
        ok, _ = cap.read()
        t = time.time()
        elapsed = t - t_opened
        if elapsed >= watch:
            if watch == FIRST_WINDOW and sum(bins[:int(FIRST_WINDOW)]) < STALL_FRAMES:
                watch = MAX_WATCH
            else:
                break
        if ok:
            first = first if first is not None else t - t0
            bins[int(elapsed)] += 1
            longest_gap = max(longest_gap, t - last)
            last = t
    if opened:
        longest_gap = max(longest_gap, time.time() - last)
    cap.release()
    emit(event="reopen", delay_actual=round(t0 - (a.at - a.delay), 3), opened=opened,
         open_s=round(t_opened - t0, 3), first_frame_s=None if first is None else round(first, 3),
         first10=sum(bins[:int(FIRST_WINDOW)]), watched_s=watch,
         bins=bins[:int(watch)], longest_gap_s=round(longest_gap, 3))


def child_load(a):
    """Stream one load camera until SIGTERM; report frames per 30 s."""
    cv2 = probe.import_cv2()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    cap = open_quiet(cv2, a.index)
    if not cap.isOpened():
        emit(event="load_failed", index=a.index)
        return
    n, t_mark = 0, time.time()
    while not stop.is_set():
        ok, _ = cap.read()
        n += ok
        if time.time() - t_mark >= 30:
            emit(event="load", index=a.index, fps=round(n / (time.time() - t_mark), 2))
            n, t_mark = 0, time.time()
    cap.release()


# ---------------------------------------------------------------- parent

class Child:
    def __init__(self, *args):
        self.p = subprocess.Popen([PY, SELF, *map(str, args)], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        self.lines = []
        self.th = threading.Thread(target=self._read, daemon=True)
        self.th.start()

    def _read(self):
        for line in self.p.stdout:
            self.lines.append(line.rstrip("\n"))

    def events(self):
        out = []
        for line in list(self.lines):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def wait_event(self, name, timeout):
        end = time.time() + timeout
        while time.time() < end:
            for e in self.events():
                if e.get("event") in (name, "hold_failed"):
                    return e
            if self.p.poll() is not None:
                time.sleep(0.2)
                return next((e for e in self.events() if e.get("event") in (name, "hold_failed")), None)
            time.sleep(0.02)
        return None

    def finish(self, timeout=90):
        try:
            self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        self.th.join(timeout=2)

    def other_output(self):
        return [ln for ln in self.lines if not ln.startswith("{")]


def stalled(r):
    return (not r["opened"]) or r["first10"] < STALL_FRAMES


def recovery_s(r):
    """Seconds from the end of the open call to the first of RECOVER_RUN delivering bins in
    a row; None if not within the watch."""
    b = r["bins"]
    for i in range(len(b) - RECOVER_RUN + 1):
        if all(x >= RECOVER_BIN for x in b[i:i + RECOVER_RUN]):
            return i
    return None


def trial(kind, index, arm, delay, n, log):
    spec = ARMS[arm]
    hold = Child("hold", "--index", index, "--stream", spec["stream"],
                 *([] if spec["release"] else ["--no-release"]))
    e = hold.wait_event("released" if spec["release"] else "ready", HOLD_LIMIT + spec["stream"] + 60)
    if not e or e["event"] == "hold_failed":
        hold.p.kill()
        hold.finish()
        log(f"  trial {n}: HOLD failed ({e}); trial skipped")
        time.sleep(REST)
        return {"kind": kind, "arm": arm, "delay": delay, "n": n, "valid": False, "hold": e}
    if spec["release"]:
        t_rel = e["t"]
    else:
        hold.p.send_signal(signal.SIGKILL)
        t_rel = time.time()
    reopen = Child("reopen", "--index", index, "--at", f"{t_rel + delay:.3f}", "--delay", delay)
    hold.finish()
    r = reopen.wait_event("reopen", delay + MAX_WATCH + 60)
    reopen.finish()
    if r is None:
        log(f"  trial {n}: REOPEN gave no result; output: {reopen.other_output()[-3:]}")
        time.sleep(REST)
        return {"kind": kind, "arm": arm, "delay": delay, "n": n, "valid": False, "hold": e}
    rec = recovery_s(r)
    res = {"kind": kind, "arm": arm, "delay": delay, "n": n, "valid": True, "hold": e, "reopen": r,
           "stalled": stalled(r), "recovery_s": rec}
    log(f"  trial {n}: delay {delay:>2} s (actual {r['delay_actual']:.2f}) | hold start: first frame "
        f"{e['first_frame_s']} s, normal from {e['normal_from_s']} s | reopen: open {r['open_s']} s, "
        f"first frame {r['first_frame_s']} s, first 10 s {r['first10']} frames, longest gap "
        f"{r['longest_gap_s']} s -> {'STALLED' if res['stalled'] else 'normal'}"
        + (f", recovered at {rec} s" if res["stalled"] and rec is not None else "")
        + (f", NOT recovered within {r['watched_s']:.0f} s" if res["stalled"] and rec is None else ""))
    log("  JSON " + json.dumps(res))
    time.sleep(REST)
    return res


def child_identify(a):
    """The marker identification in its own process, which exits afterwards (as every other
    stream in this experiment does). Restored and read back by probe."""
    port, _ = TEST[a.kind]
    be = probe.usb_backend()
    devs = probe.find_kind(be, a.kind, port)
    if len(devs) != 1:
        print(f"IDENTIFICATION: expected one kind {a.kind} camera on lab port {port}, found {len(devs)}")
        emit(event="identified", ok=False)
        return
    cv2 = probe.import_cv2()
    found = probe.identify_index(cv2, capture_args(a.index), devs)
    for dev in devs:
        lay = probe.vc_layout(dev)
        probe.readback_line(dev, lay, probe.defaults_of(dev, lay), "after identification, expected = defaults")
    emit(event="identified", ok=found is not None, t=time.time())


def child_present(a):
    """Which test cameras are on their lab ports (USB id and name string). Runs in its own
    process so that the main process never holds a USB handle on a camera."""
    be = probe.usb_backend()
    for kind in a.kinds:
        port, _ = TEST[kind]
        emit(event="present", kind=kind, port=port, found=len(probe.find_kind(be, kind, port)))


def identify_end(kind, index, log):
    """Runs child_identify in a new process, logs its output, waits for it to exit."""
    c = Child("identify", "--kind", kind, "--index", index)
    c.finish(timeout=180)
    for line in c.other_output():
        log("    | " + line)
    e = next((e for e in c.events() if e.get("event") == "identified"), None)
    ok = bool(e and e["ok"])
    log(f"IDENTIFICATION (own process, exited) at the end of kind {kind}'s sweep: index {index} "
        + ("CONFIRMED" if ok else "NOT CONFIRMED: this camera's sweep is invalid"))
    return ok


def start_load(test_index, log):
    log(f"  rest {LOAD_REST:g} s before starting the load (no camera has been opened by then "
        f"for at least that long)")
    time.sleep(LOAD_REST)
    loads = [Child("load", "--index", i) for i in ALL_INDICES if i != test_index]
    log(f"  load: {len(loads)} reader processes on indices "
        f"{', '.join(str(i) for i in ALL_INDICES if i != test_index)}; {LOAD_WARMUP:g} s warm-up")
    time.sleep(LOAD_WARMUP)
    return loads


def stop_load(loads, log):
    for c in loads:
        if c.p.poll() is None:
            c.p.send_signal(signal.SIGTERM)
    for c in loads:
        c.finish(timeout=15)
    for c in loads:
        ev = [e for e in c.events() if e.get("event") == "load"]
        fail = [e for e in c.events() if e.get("event") == "load_failed"]
        idx = c.p.args[c.p.args.index("--index") + 1]
        rates = [e["fps"] for e in ev]
        log(f"  load index {idx}: "
            + ("FAILED to open" if fail else
               f"{len(rates)} x 30 s windows, fps min {min(rates):.1f} median {statistics.median(rates):.1f} "
               f"max {max(rates):.1f}"
               if rates else "no 30 s window completed"))
        if rates:
            log(f"    every window, from the start: {' '.join(f'{r:.1f}' for r in rates)}")


def summarize(results, log):
    log(f"\n[{now()}] SUMMARY (per kind and delay; stalled = first 10 s under {STALL_FRAMES} frames; "
        f"recovery = first {RECOVER_RUN} s in a row with {RECOVER_BIN}+ frames each)")
    for kind in sorted({r["kind"] for r in results}):
        for delay in sorted({r["delay"] for r in results if r["kind"] == kind}):
            rs = [r for r in results if r["kind"] == kind and r["delay"] == delay]
            valid = [r for r in rs if r["valid"]]
            st = [r for r in valid if r["stalled"]]
            lens = ", ".join(f"{r['recovery_s']} s" if r["recovery_s"] is not None else
                             f">{r['reopen']['watched_s']:.0f} s" for r in st)
            f10 = [r["reopen"]["first10"] for r in valid]
            log(f"  kind {kind}  delay {delay:>2} s: stalled {len(st)} of {len(valid)}"
                + (f" ({len(rs) - len(valid)} invalid)" if len(rs) != len(valid) else "")
                + (f"; first-10-s frames {min(f10)}-{max(f10)}" if f10 else "")
                + (f"; stall lengths {lens}" if st else ""))
        hs = [r["hold"]["first_frame_s"] for r in results if r["kind"] == kind and r.get("valid")]
        if hs:
            log(f"  kind {kind}: HOLD start (reopen {REST:g} s after the previous trial): first frame "
                f"{min(hs):.2f}-{max(hs):.2f} s")


def cmd_run(a):
    probe.guard()
    log_lines = []

    def log(s):
        print(s, flush=True)
        log_lines.append(s)

    spec = ARMS[a.arm]
    log(f"[{now()}] reopen_delay {a.arm} arm, load {'ON (other seven cameras streaming)' if a.load else 'off'}; "
        f"stream before release {spec['stream']:g} s; {'clean release then exit' if spec['release'] else 'SIGKILL, no release'}; "
        f"delays {a.delays or spec['delays']}; repeats {a.repeats}; kinds {' '.join(a.kinds)}; seed {a.seed}")
    pc = Child("present", "--kinds", *a.kinds)
    pc.finish(timeout=60)
    for e in pc.events():
        if e.get("event") == "present":
            log(f"  kind {e['kind']}: lab port {e['port']}: "
                f"{'present' if e['found'] == 1 else str(e['found']) + ' found'} "
                f"(USB id and name string, read by a process that has exited; no stream opened)")
    rng = random.Random(a.seed)
    results = []
    for kind in a.kinds:
        port, index = TEST[kind]
        delays = list(a.delays or spec["delays"]) * a.repeats
        rng.shuffle(delays)
        log(f"\n[{now()}] kind {kind}, lab port {port}, index {index}: {len(delays)} trials, order {delays}")
        loads = start_load(index, log) if a.load else []
        mine = []
        try:
            for n, d in enumerate(delays, 1):
                mine.append(trial(kind, index, a.arm, d, n, log))
            log(f"  rest {ID_REST:g} s before identification")
            time.sleep(ID_REST)
            ok = identify_end(kind, index, log)
        finally:
            if loads:
                stop_load(loads, log)
        for r in mine:
            r["identified"] = ok
            if not ok:
                r["valid"] = False
        results.extend(mine)
    summarize(results, log)
    log(f"\n[{now()}] done")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--arm", choices=sorted(ARMS), required=True)
    r.add_argument("--load", action="store_true", help="stream the other seven cameras meanwhile")
    r.add_argument("--kinds", nargs="+", choices=sorted(TEST), default=["A", "B", "C"])
    r.add_argument("--repeats", type=int, default=5)
    r.add_argument("--delays", type=float, nargs="+", help="override the arm's delays (seconds)")
    r.add_argument("--seed", type=int, default=20261120)
    r.set_defaults(fn=cmd_run)
    h = sub.add_parser("hold")
    h.add_argument("--index", type=int, required=True)
    h.add_argument("--stream", type=float, required=True)
    h.add_argument("--no-release", dest="release", action="store_false")
    h.set_defaults(fn=child_hold)
    o = sub.add_parser("reopen")
    o.add_argument("--index", type=int, required=True)
    o.add_argument("--at", type=float, required=True)
    o.add_argument("--delay", type=float, required=True)
    o.set_defaults(fn=child_reopen)
    pr = sub.add_parser("present")
    pr.add_argument("--kinds", nargs="+", choices=sorted(TEST), required=True)
    pr.set_defaults(fn=child_present)
    idp = sub.add_parser("identify")
    idp.add_argument("--kind", choices=sorted(TEST), required=True)
    idp.add_argument("--index", type=int, required=True)
    idp.set_defaults(fn=child_identify)
    ld = sub.add_parser("load")
    ld.add_argument("--index", type=int, required=True)
    ld.set_defaults(fn=child_load)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
