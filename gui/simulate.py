#!/usr/bin/env python3
"""Pretend a camera run is in progress, from a sample temp folder, and serve the viewer on it.

    python3 simulate.py <sample temp folder> [--scenario NAME] [--port N] [--end-after SECONDS]

The sample folder is copied (without clips) into a scratch folder; every second the stills
are touched so they look fresh and a status file is written the way the camera program
writes it. Scenarios:

    normal        ample room on both drives, no banners (default)
    fyi           less than a day of recording left (orange)
    nearly_full   less than an hour left (red)
    save_full     save drive not accepting clips, clips piling up in temp (red)
    no_space      recording stopped by the launcher: temp drive nearly full (red)
    crash         stills and status stop after 15 s (red: no still, no shutdown reported)
    shutdown      stop clicked after 15 s: "stopping", then "ended" with a message, then
                  the temp folder disappears (run ended)

--end-after SECONDS runs the shutdown scenario after that many seconds whatever the scenario.
Nothing in the sample folder is modified. Python standard library only.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from typing import cast

GUI_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(GUI_DIR))
# imported under lowercase aliases: the type checker forbids redefining an uppercase name in the fallback
try:
    from ratrixcam_naming import STATUS_FILENAME as shared_status_filename
    from ratrixcam_utils import ASSUMED_WRITE_RATE as shared_write_rate
except ImportError:  # the viewer folder copied on its own
    shared_status_filename = "ratrixcam_status.json"
    shared_write_rate = 1e9 / 60  # bytes per second, the camera code's assumed rig write rate
STATUS_FILENAME = shared_status_filename
WRITE_RATE = shared_write_rate
TB = 10**12
GB = 10**9


def scenario_drives(name: str):
    """(save drive total, save free, temp free) in bytes for a scenario. At the assumed
    write rate (1 GB per minute) a day of recording is 1.44 TB, so a normal run needs a
    big save drive to be more than a day from full."""
    return {
        "normal": (10 * TB, 8 * TB, 600 * GB),
        "fyi": (2 * TB, 7 * 3600 * WRITE_RATE, 600 * GB),
        "nearly_full": (2 * TB, 45 * 60 * WRITE_RATE, 600 * GB),
        "save_full": (2 * TB, 0, 35 * 60 * WRITE_RATE),
        "no_space": (2 * TB, 0, 200_000),
        "crash": (10 * TB, 8 * TB, 600 * GB),
        "shutdown": (10 * TB, 8 * TB, 600 * GB),
    }[name]


def stamp(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S.") + f"{t.microsecond // 1000:03d}"


def drive(path: str, total: float, free: float) -> dict[str, object]:
    return {
        "path": path, "found": True,
        "total_bytes": int(total), "used_bytes": int(total - free), "free_bytes": int(free),
        "seconds_until_full": int(free / WRITE_RATE),
    }


def write_status(temp_dir: str, started: datetime, run_id: str, config: dict[str, object], save_total: float, save_free: float, temp_free: float,
                 run_state: str = "running", message: str = "", clips_waiting: int = 0, recording: bool = True):
    """The status file in the shape the camera program writes it (ratrixcam_status.py)."""
    save = drive(str(config.get("save_path", "")), save_total, save_free)
    save["accepting_clips"] = save_free > 0
    temp = drive(str(config.get("temp_path", "")), 1 * TB, temp_free)
    temp["clips_waiting"] = clips_waiting
    status = {
        "schema": 1,
        "written": stamp(datetime.now()),
        "recording": recording,
        "run": {"run_id": run_id, "started": stamp(started), "state": run_state, "message": message,
                "machine_name": str(config.get("machine_name") or "Simulated Mac")},
        "save_drive": save,
        "temp_drive": temp,
    }
    tmp = os.path.join(temp_dir, STATUS_FILENAME + ".tmp")
    with open(tmp, "w") as f:
        json.dump(status, f, indent=2)
    os.replace(tmp, os.path.join(temp_dir, STATUS_FILENAME))


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    _ = parser.add_argument("sample", help="a sample temp folder, e.g. sample_data/temp")
    _ = parser.add_argument("--scenario", default="normal", choices=["normal", "fyi", "nearly_full", "save_full", "no_space", "crash", "shutdown"])
    _ = parser.add_argument("--port", type=int, default=8000)
    _ = parser.add_argument("--end-after", type=float, default=None, metavar="SECONDS")
    args = parser.parse_args()
    scenario = cast(str, args.scenario)
    port = cast(int, args.port)
    end_after_arg = cast("float | None", args.end_after)

    sample = os.path.abspath(cast(str, args.sample))
    if not os.path.isdir(sample):
        print(f"ERROR: sample folder not found: {sample}")
        return 1
    scratch = tempfile.mkdtemp(prefix="ratrixcam_sim_")
    temp_dir = os.path.join(scratch, "temp")
    _ = shutil.copytree(sample, temp_dir, ignore=shutil.ignore_patterns("*.mp4", ".DS_Store"))

    config: dict[str, object] = {}
    meta = os.path.join(temp_dir, "ratrixcam_run_metadata")
    for name in sorted(os.listdir(meta)):
        if name.endswith(".json"):
            with open(os.path.join(meta, name)) as f:
                config = json.load(f)
    run_id = "ratrixcam_simulated"
    for name in os.listdir(temp_dir):  # the sample's terminal log names its run
        if name.startswith("terminal_") and name.endswith(".log"):
            run_id = name[len("terminal_"):-len(".log")]
    started = datetime.now()
    stills = [os.path.join(temp_dir, "status", n) for n in os.listdir(os.path.join(temp_dir, "status"))]
    save_total, save_free, temp_free = scenario_drives(scenario)
    end_after = end_after_arg if end_after_arg is not None else (15 if scenario in ("crash", "shutdown") else None)

    server = subprocess.Popen([sys.executable, os.path.join(GUI_DIR, "serve.py"), temp_dir, "--port", str(port)])
    print(f"simulating '{scenario}' from {sample}\n  in {temp_dir}\n  viewer: http://127.0.0.1:{port}/   (Ctrl-C to stop)")

    stopped = False

    def stop(*_: object) -> None:
        raise KeyboardInterrupt  # so that the finally clause below stops the server and cleans up

    _ = signal.signal(signal.SIGINT, stop)
    _ = signal.signal(signal.SIGTERM, stop)
    try:
        t0 = time.time()
        tick = 0
        while True:
            elapsed = time.time() - t0
            if end_after is not None and elapsed >= end_after and not stopped:
                stopped = True
                if scenario == "crash":
                    print("  simulating a crash: stills and status stop")
                else:
                    print("  simulating a stop click: 'stopping' now, 'ended' in 15 s, temp folder gone 3 s later")
                    write_status(temp_dir, started, run_id, config, save_total, save_free, temp_free, run_state="stopping")
                    time.sleep(15)
                    write_status(temp_dir, started, run_id, config, save_total, save_free, temp_free, run_state="ended",
                                 message=f"Run metadata moved to <save>/ratrixcam_run_metadata/{run_id}")
                    time.sleep(3)
                    shutil.rmtree(temp_dir)
                    print("  temp folder removed; the viewer should show 'run ended'")
            if not stopped:
                now = time.time()
                for still in stills:
                    os.utime(still, (now, now))
                write_status(temp_dir, started, run_id, config, save_total, save_free, temp_free,
                             clips_waiting=int(elapsed) if scenario in ("save_full", "no_space") else 0,
                             recording=not (scenario == "no_space" and elapsed > 5))
            tick += 1
            time.sleep(max(0, t0 + tick - time.time()))
    except KeyboardInterrupt:
        pass
    finally:
        server.terminate()
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
