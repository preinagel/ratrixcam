#!/usr/bin/env python3
"""Pretend a camera run is in progress, from a sample temp folder, and serve the viewer on it.

    python3 simulate.py <sample temp folder> [--scenario NAME] [--port N] [--end-after SECONDS]

The sample folder is copied (without clips) into a scratch folder; every second the stills
are touched so they look fresh and a status file is written the way python will. Scenarios:

    normal        ample room on both drives, no banners (default)
    fyi           less than a day of recording left (orange)
    nearly_full   less than an hour left (red)
    save_full     save drive full, temp drive caching (red)
    no_space      save full, temp without room: the status file stops updating (red, after 60 s)
    crash         stills and status stop after 15 s (red)
    shutdown      graceful shutdown after 15 s, then the temp folder disappears (run ended)

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

GUI_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_FILENAME = "ratrixcam_status.json"
TB = 10**12
GB = 10**9
WRITE_RATE = 1e9 / 60  # bytes per second, the camera code's assumed rig write rate


def scenario_drives(name: str):
    """(save free bytes, temp free bytes) for a scenario."""
    return {
        "normal": (1.4 * TB, 600 * GB),
        "fyi": (7 * 3600 * WRITE_RATE, 600 * GB),
        "nearly_full": (45 * 60 * WRITE_RATE, 600 * GB),
        "save_full": (0, 35 * 60 * WRITE_RATE),
        "no_space": (0, 200_000),
        "crash": (1.4 * TB, 600 * GB),
        "shutdown": (1.4 * TB, 600 * GB),
    }[name]


def stamp(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S.") + f"{t.microsecond // 1000:03d}"


def drive(path: str, total: float, free: float) -> dict:
    return {
        "path": path, "found": True,
        "total_bytes": int(total), "used_bytes": int(total - free), "free_bytes": int(free),
        "seconds_until_full": int(free / WRITE_RATE),
    }


def write_status(temp_dir: str, started: datetime, run_id: str, config: dict, save_free: float, temp_free: float):
    status = {
        "schema": 1,
        "written": stamp(datetime.now()),
        "run": {"run_id": run_id, "started": stamp(started), "machine_name": "Simulated Mac"},
        "save_drive": drive(config.get("save_path", ""), 2 * TB, save_free),
        "temp_drive": drive(config.get("temp_path", ""), 1 * TB, temp_free),
    }
    tmp = os.path.join(temp_dir, STATUS_FILENAME + ".tmp")
    with open(tmp, "w") as f:
        json.dump(status, f, indent=2)
    os.replace(tmp, os.path.join(temp_dir, STATUS_FILENAME))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("sample", help="a sample temp folder, e.g. sample_data/temp")
    parser.add_argument("--scenario", default="normal", choices=["normal", "fyi", "nearly_full", "save_full", "no_space", "crash", "shutdown"])
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--end-after", type=float, default=None, metavar="SECONDS")
    args = parser.parse_args()

    sample = os.path.abspath(args.sample)
    if not os.path.isdir(sample):
        print(f"ERROR: sample folder not found: {sample}")
        return 1
    scratch = tempfile.mkdtemp(prefix="ratrixcam_sim_")
    temp_dir = os.path.join(scratch, "temp")
    shutil.copytree(sample, temp_dir, ignore=shutil.ignore_patterns("*.mp4", ".DS_Store"))

    config, log_name, run_id = {}, None, None
    meta = os.path.join(temp_dir, "ratrixcam_run_metadata")
    for name in sorted(os.listdir(meta)):
        if name.endswith(".json"):
            with open(os.path.join(meta, name)) as f:
                config = json.load(f)
    for name in os.listdir(temp_dir):
        if name.startswith("terminal_") and name.endswith(".log"):
            log_name, run_id = name, name[len("terminal_"):-len(".log")]
    if run_id is None:
        run_id = "ratrixcam_simulated"
        log_name = f"terminal_{run_id}.log"
        open(os.path.join(temp_dir, log_name), "a").close()
    started = datetime.now()
    stills = [os.path.join(temp_dir, "status", n) for n in os.listdir(os.path.join(temp_dir, "status"))]
    save_free, temp_free = scenario_drives(args.scenario)
    end_after = args.end_after if args.end_after is not None else (15 if args.scenario in ("crash", "shutdown") else None)

    server = subprocess.Popen([sys.executable, os.path.join(GUI_DIR, "serve.py"), temp_dir, "--port", str(args.port)])
    print(f"simulating '{args.scenario}' from {sample}\n  in {temp_dir}\n  viewer: http://127.0.0.1:{args.port}/   (Ctrl-C to stop)")

    stopped = False

    def stop(*_):
        raise KeyboardInterrupt  # so that the finally clause below stops the server and cleans up

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        t0 = time.time()
        tick = 0
        while True:
            elapsed = time.time() - t0
            if end_after is not None and elapsed >= end_after and not stopped:
                stopped = True
                if args.scenario == "crash":
                    print("  simulating a crash: stills and status stop")
                else:
                    print("  simulating a graceful shutdown; temp folder goes away in 20 s")
                    with open(os.path.join(temp_dir, log_name), "a") as f:
                        f.write("ratrixcam IO: attempting graceful shutdown...\n")
                        f.write("Multicam attempting to shut down nicely\n")
                        f.write(f"Collecting the run metadata as ratrixcam_run_metadata/{run_id}\n")
                        f.write("Manifest rows added at cleanup: 0\n")
                        f.write(f"Run metadata folder moved to <save>/ratrixcam_run_metadata/{run_id}\n")
                        f.write("ratrixcam multi: shutdown complete\n")
                    time.sleep(20)
                    shutil.rmtree(temp_dir)
                    print("  temp folder removed; the viewer should show 'run ended'")
            if not stopped:
                now = time.time()
                for still in stills:
                    os.utime(still, (now, now))
                if not (args.scenario == "no_space" and elapsed > 5):
                    write_status(temp_dir, started, run_id, config, save_free, temp_free)
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
