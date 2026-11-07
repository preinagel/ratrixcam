import argparse
import datetime as dt
import multiprocessing
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import tkinter as tk
import tkinter.font as font
from collections.abc import Callable
from multiprocessing import Process
from typing import cast
from multiprocessing.synchronize import Event
from tkinter import messagebox
from types import FrameType

import ratrixcam_multi
from ratrixcam_run_metadata import RunMetadata, save_drive_problem
from ratrixcam_server import TRANSFER_PAUSE, TRANSFER_RETRY_WINDOW
from ratrixcam_status import StatusWriter
from ratrixcam_utils import (
    Config,
    ensure_config_file_exists,
    ensure_dir_exists,
    load_settings,
    not_writable,
    seconds_until_full,
    stills_dir,
    temp_is_clear,
    write_rate,
)


class State:
    def __init__(self):
        self.camera_process: Process | None = None
        self.current_window: tk.Tk | None = None
        self.run_metadata: RunMetadata | None = None
        self.config: Config | None = None
        self.abort: bool = False  # the run ends without a usable save drive: the cleanup leaves temp untouched
        self.stopping: bool = False  # a stop request is being handled (the Replace SSD dialog may be up)
        self.viewer: subprocess.Popen[bytes] | None = None  # the viewer's server, gui/serve.py, for the run


def cameras_are_distinct(config: Config) -> bool:
    """False, with a message, if two cameras share a subject ID and view (they would write
    identical file names) or share a display position."""
    ok = True
    seen_names: dict[tuple[str, str], int] = {}
    seen_positions: dict[str, int] = {}
    for idx, camera in enumerate(config.cameras, start=1):
        key = (camera.subjID, camera.view)
        if key in seen_names:
            print(f"ERROR: cameras {seen_names[key]} and {idx} both have subjID {camera.subjID!r} and view {camera.view!r}")
            ok = False
        _ = seen_names.setdefault(key, idx)
        if camera.position in seen_positions:
            print(f"ERROR: cameras {seen_positions[camera.position]} and {idx} both have position {camera.position!r}")
            ok = False
        _ = seen_positions.setdefault(camera.position, idx)
    return ok


def run_without_handlers(config: Config, stop_event: Event, abort_event: Event):
    _ = signal.signal(signal.SIGINT, signal.SIG_IGN)
    _ = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ratrixcam_multi.run(config, stop_event, abort_event)


def time_left_text(seconds: float) -> str:
    """'~N min' under two hours, '~N.N h' otherwise."""
    if seconds < 7200:
        return f"~{seconds / 60:.0f} min"
    return f"~{seconds / 3600:.1f} h"


def status_update_loop(window: tk.Tk, state: State, status: StatusWriter, interval_ms: int) -> None:
    """Every preview_interval: write the status file the viewer reads, with the drive checks
    and the one-line terminal notices of the save drive's transitions that come with it."""
    status.write(cameras_alive=state.camera_process is not None and state.camera_process.is_alive())
    _ = window.after(interval_ms, status_update_loop, window, state, status, interval_ms)


VIEWER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui", "serve.py")
VIEWER_PORTS = range(8000, 8010)  # the first free one is used
VIEWER_START_WAIT = 3.0  # seconds to wait for the server to answer on its port


def start_viewer(temp_path: str) -> tuple[subprocess.Popen[bytes], str] | None:
    """Start the viewer's read-only server on the first free port of VIEWER_PORTS and return
    (process, address). None, with a WARNING, when it cannot start: the run goes on without
    the viewer. The server runs in its own session, so a Ctrl-C in the terminal does not
    reach it; the shutdown ends it after the temp folder is gone."""
    if not os.path.isfile(VIEWER_SCRIPT):
        print(f"WARNING: viewer not started: {VIEWER_SCRIPT} not found")
        return None
    for port in VIEWER_PORTS:
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
        except OSError:
            continue  # in use
        try:
            # its output is discarded: it inherits the terminal log's pipe otherwise, and the log
            # could not close until the server ended; the address is printed by the caller
            server = subprocess.Popen(
                [sys.executable, VIEWER_SCRIPT, temp_path, "--port", str(port)],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            print(f"WARNING: viewer not started: {e.strerror or e}")
            return None
        deadline = time.monotonic() + VIEWER_START_WAIT
        while server.poll() is None and time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                return server, f"http://127.0.0.1:{port}/"  # it answers: it is serving
            except OSError:
                time.sleep(0.1)
        if server.poll() is None:
            server.terminate()
            print(f"WARNING: viewer not started: the server did not answer on port {port}")
            return None
    print(f"WARNING: viewer not started: no free port in {VIEWER_PORTS.start}-{VIEWER_PORTS.stop - 1}")
    return None


def forced_exit(state: State, stop_event: Event, abort_event: Event) -> Callable[[int, FrameType | None], None]:
    """The handler for a second Ctrl-C: end the process at once, as the default action would,
    but first set the abort and stop events, so that the camera servers, which ignore the
    terminal's signal, wind down on their own instead of recording on with no launcher, and
    take the viewer's server with it, since it runs in its own session and would outlive
    the launcher otherwise."""

    def handler(_sig: int, _frame: FrameType | None) -> None:
        abort_event.set()
        stop_event.set()
        stop_viewer(state)
        os._exit(130)

    return handler


def stop_viewer(state: State) -> None:
    """End the viewer's server, at the very end of the shutdown."""
    if state.viewer is None:
        return
    state.viewer.terminate()
    try:
        _ = state.viewer.wait(timeout=5)
    except subprocess.TimeoutExpired:
        state.viewer.kill()
    state.viewer = None


REPLACE_SSD_CHOICES = {"SSD Ready": "ready", "Abort Run": "abort", "Continue Run": "continue"}
STOP_BANNER = "=" * 72  # printed before the stop click's lines, so they can be found in the log


CHECK_PAUSE = 0.7  # seconds the dialog shows "Checking the drive..." so that a press is seen to do something


def stop_requested(state: State, window: tk.Tk | None) -> bool:
    """Every way a running recording is stopped comes through here: the stop button and the
    window's close box (window given: the Replace SSD dialog can be shown), Ctrl-C and
    SIGTERM (no window: no dialog). The save drive is checked; a drive that passes means
    the shutdown proceeds. One that fails means the dialog, where given, decides between
    SSD Ready, Abort Run and Continue Run; without a dialog the run is aborted. Returns
    True when the shutdown is to proceed, with state.abort saying how, False when the
    operator chose to go on recording. A second request from the window while one is being
    handled is ignored (the dialog's grab does not stop the close box); a signal arriving
    then counts as Abort."""
    if state.stopping:
        if window is not None:
            return False
        print("Signal received while the Replace SSD dialog is up: aborting the run; the cleanup will move nothing")
        state.abort = True
        return True
    config = state.config
    if config is None:
        return True
    state.stopping = True
    try:
        problem = save_drive_problem(config)
        if problem is None:
            print("Stop requested, save drive check passed: shutting down")
            return True
        if window is None:
            print(f"Save drive check failed ({problem}): aborting the run; the cleanup will move nothing")
            state.abort = True
            return True
        print(f"Stop requested, save drive check failed: {problem}")
        # SSD Ready re-runs the check inside the dialog, never the cached result; the
        # dialog returns 'ready' only once a check has passed
        choice = replace_ssd_dialog(window, problem, lambda: save_drive_problem(config), config)
        if choice == "continue":
            print("Continue Run chosen: recording goes on")
            return False
        if choice == "abort":
            print("Abort Run chosen: the run ends; the cleanup will move nothing")
            state.abort = True
            return True
        print("Stop requested, save drive check passed: shutting down")
        return True
    finally:
        state.stopping = False


def temp_time_left_text(config: Config) -> str:
    """How long the cameras can go on before the temp drive is full, at the assumed write
    rate scaled to this rig's camera count, or 'unknown' if the drive cannot be queried."""
    try:
        free = shutil.disk_usage(config.temp_path).free
    except OSError:
        return "unknown"
    return time_left_text(seconds_until_full(free, write_rate(len(config.cameras))))


def replace_ssd_text(problem: str, config: Config) -> str:
    """The body of the Replace SSD dialog for this problem."""
    return (
        f"{problem}\n\n"
        "This drive cannot take the files waiting in the temp folder. Recording continues while this window is up.\n"
        "Eject it and mount a drive with enough space at the same path, then press SSD Ready.\n"
        "Abort Run ends the run with every file left in the temp folder.\n"
        f"Continue Run returns to recording; the temp drive has room for {temp_time_left_text(config)} more."
    )


def replace_ssd_dialog(window: tk.Tk, problem: str, check: Callable[[], str | None], config: Config) -> str:
    """Block on a small window telling the operator what is wrong with the save drive, with
    three buttons; returns 'ready' once a check has passed, else 'abort' or 'continue'. SSD
    Ready runs the check again from inside the window: the buttons go dead, "Checking the
    drive..." shows for a moment, and on failure the window stays where it is with the new
    result and a count of the checks made, so a press is never mistaken for nothing. No
    timeout: the window stays until a button is pressed. Its close box counts as Continue
    Run, the choice that changes nothing. A message box cannot carry three custom buttons,
    hence a Toplevel."""
    dialog = tk.Toplevel(window)
    dialog.title("Replace SSD")
    dialog.transient(window)
    dialog.resizable(False, False)
    choice = {"value": "continue"}
    checks = {"count": 0}

    def choose(value: str):
        choice["value"] = value
        dialog.destroy()

    dialog.protocol("WM_DELETE_WINDOW", lambda: choose("continue"))
    message = tk.Label(dialog, text=replace_ssd_text(problem, config), justify="left", wraplength=520, padx=16, pady=12)
    message.pack()
    status = tk.Label(dialog, text="", justify="left", wraplength=520, padx=16, fg="#a00000")
    status.pack()
    row = tk.Frame(dialog)
    row.pack(pady=(0, 12))
    buttons: list[tk.Button] = []

    def ready():
        checks["count"] += 1
        for button in buttons:
            _ = button.config(state="disabled")
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)  # inert while the check runs: the update below services the close box too
        _ = status.config(text="Checking the drive...")
        dialog.update()
        time.sleep(CHECK_PAUSE)
        found = check()
        if found is None:
            choose("ready")
            return
        print(f"SSD Ready pressed, save drive check failed again: {found}")
        _ = message.config(text=replace_ssd_text(found, config))
        _ = status.config(text=f"Checked {checks['count']} time(s), last at {dt.datetime.now():%H:%M:%S}: still {found}")
        dialog.protocol("WM_DELETE_WINDOW", lambda: choose("continue"))
        for button in buttons:
            _ = button.config(state="normal")

    actions = {"ready": ready, "abort": lambda: choose("abort"), "continue": lambda: choose("continue")}
    for text, value in REPLACE_SSD_CHOICES.items():
        button = tk.Button(row, text=text, width=14, command=actions[value])
        button.pack(side="left", padx=8)
        buttons.append(button)
    # KNOWN: taken before the window is mapped. Fine on macOS, the only target; X11 would
    # want wait_visibility() first, which the tests' withdrawn windows could never satisfy
    dialog.grab_set()
    window.wait_window(dialog)
    return choice["value"]


def create_recording_window(state: State, bgcolor: str, config: Config, viewer_address: str | None) -> tk.Tk:
    """The small window that owns the run: it says the cameras are running, where the viewer
    is, and carries the Stop button. The stills and the accounting are the viewer's; the
    status file it reads is written from this window's poll."""
    window = tk.Tk()
    _ = window.configure(background=bgcolor)
    window.title("ratrixcam")
    window.resizable(False, False)
    text_font = font.Font(family="Helvetica", size=14)
    button_font = font.Font(family="Helvetica", size=15)

    tk.Label(window, text="ratrixcam running", bg=bgcolor, fg="#ffffff", font=text_font).pack(padx=32, pady=(20, 4))
    tk.Label(
        window,
        text=f"Viewer: {viewer_address}" if viewer_address else "Viewer not started; see the terminal",
        bg=bgcolor,
        fg="#ffffff",
        font=text_font,
    ).pack(padx=32, pady=(0, 16))
    if state.run_metadata is not None:
        status_update_loop(window, state, state.run_metadata.status, int(config.preview_interval * 1000))

    def stop_recording():
        """The stop button, and the window's close box. With the cameras running, the save
        drive is checked instead of asking 'are you sure' (stop_requested); with no cameras
        running, the old 'are you sure' question is all there is to ask."""
        if state.camera_process is None or not state.camera_process.is_alive():
            confirmation = messagebox.askquestion(
                "Cameras appear to be off already", "Are you sure?", icon="warning"
            )
            if confirmation == "no":
                return
        else:
            if not state.stopping:
                print(STOP_BANNER)
            if not stop_requested(state, window):
                return
        close_window(window)

    window.protocol("WM_DELETE_WINDOW", stop_recording)  # the close box takes the same path as the button

    tk.Button(
        window,
        text="Stop Recording",
        bg="red",
        fg="black",
        bd=0,
        height=3,
        width=19,
        font=button_font,
        command=stop_recording,
    ).pack(padx=32, pady=(0, 20))

    return window


class StoppingWindow:
    """A small window shown while the run stops, in place of the recording window: it names
    the phase and counts the seconds. It is serviced from the wait loops below, so it never
    freezes. Without a display it does nothing and the terminal lines carry the story."""

    def __init__(self, title: str):
        self.started: float = time.monotonic()
        self.root: tk.Tk | None = None
        self.label: tk.Label | None = None
        try:
            self.root = tk.Tk()
            self.root.title(title)
            self.root.geometry("+600+400")
            self.root.protocol("WM_DELETE_WINDOW", lambda: None)  # closing it would stop nothing
            self.label = tk.Label(self.root, text="", justify="left", padx=24, pady=18, font=("Helvetica", 14))
            self.label.pack()
            self.root.update()
        except tk.TclError:
            self.root = None

    def show(self, text: str) -> None:
        """Refresh the window with this text and the time elapsed since the stop began."""
        if self.root is None or self.label is None:
            return
        minutes, seconds = divmod(int(time.monotonic() - self.started), 60)
        try:
            _ = self.label.config(text=f"{text}\n\nElapsed {minutes}:{seconds:02d}")
            self.root.update()
        except tk.TclError:
            self.root = None

    def close(self) -> None:
        if self.root is not None:
            try:
                self.root.destroy()
            except tk.TclError:
                pass
            self.root = None


def wait_until(done: Callable[[], bool], timeout: float, tick: Callable[[], None], interval: float = 0.1) -> bool:
    """Call tick() every interval seconds until done() is true or timeout seconds have passed;
    True if done() came true. The tick keeps a window alive during a long wait."""
    deadline = time.monotonic() + timeout
    while not done():
        if time.monotonic() >= deadline:
            return False
        tick()
        time.sleep(interval)
    return True


def close_window(window: tk.Tk) -> None:
    """Destroy the recording window after cancelling its pending timers (the stills, clock
    and drive-poll loops reschedule themselves with after). Left pending, they fire later
    from the stopping window's refreshes when the old event loop is still on the stack, as
    on a Ctrl-C during the Replace SSD dialog, and each prints an 'invalid command name'
    complaint into the log."""
    try:
        timers = cast(tuple[str, ...], window.tk.call("after", "info"))  # Tk returns untyped
        for timer in timers:
            window.after_cancel(timer)
    except tk.TclError:
        pass
    window.destroy()


def graceful_shutdown(state: State, stop_event: Event, abort_event: Event):
    _ = signal.signal(signal.SIGINT, forced_exit(state, stop_event, abort_event))
    _ = signal.signal(signal.SIGTERM, signal.SIG_DFL)

    if state.current_window is not None:
        print("ratrixcam IO: closing GUI window")
        close_window(state.current_window)
        state.current_window = None

    camera_process = state.camera_process
    if camera_process is None:
        print("No camera process to stop")
        if state.run_metadata is not None and state.run_metadata.terminal_log is not None:
            state.run_metadata.terminal_log.stop()
        stop_viewer(state)
        return

    verb = "Aborting the run" if state.abort else "Stopping the run"
    stopping = StoppingWindow(f"ratrixcam: {verb.lower()}")
    print("ratrixcam IO: attempting graceful shutdown...")
    if state.abort:
        abort_event.set()  # before the stop, so a server reaching its shutdown sees the abort at once
    stop_event.set()
    status = state.run_metadata.status if state.run_metadata is not None else None
    if status is not None:
        status.write(cameras_alive=True, state="aborted" if state.abort else "stopping", force=True)

    def tick(text: str, cameras_alive: bool) -> None:
        stopping.show(text)
        if status is not None:
            status.write(cameras_alive=cameras_alive)  # at most once per preview interval

    timeout = 600 #transcoding takes ~50% video duration, so wait 10min before killing unfinished processes
                  #would be better to set this based on the slice duration but this is not available info
    print("Waiting for child processes to terminate...")
    phase = (
        f"{verb}.\nStopping the cameras; their pending transfers are being stopped."
        if state.abort
        else f"{verb}.\nStopping the cameras and waiting for the clips in transfer to finish or give up\n"
        + f"(up to {time_left_text(TRANSFER_RETRY_WINDOW + TRANSFER_PAUSE)})."
    )
    if not wait_until(lambda: not camera_process.is_alive(), timeout, lambda: tick(phase, camera_process.is_alive())):
        print("ratrixcam IO: timed out waiting for child processes to terminate, killing")
        camera_process.kill()
    print("ratrixcam IO: shutdown complete")

    # cameras are down: gather the run's artifacts (terminal log, stills, loose temp files),
    # or on an abort leave temp as it is. The collection runs on a thread so that the
    # window can be serviced meanwhile; it can take minutes when clips are re-requested
    run_metadata = state.run_metadata
    if run_metadata is not None:
        phase = "Cameras stopped.\n" + (
            "Leaving the temp folder as it is and closing the log."
            if state.abort
            else "Moving the run's files to the save drive; the terminal shows the details."
        )
        failure: list[Exception] = []

        def collect():
            try:
                run_metadata.collect_at_shutdown(abort=state.abort)
            except Exception as e:
                failure.append(e)

        worker = threading.Thread(target=collect)
        worker.start()
        _ = wait_until(lambda: not worker.is_alive(), float("inf"), lambda: tick(phase, False))
        if failure:
            print(f"WARNING: run metadata collection failed: {failure[0]}")

    stopping.close()
    stop_viewer(state)  # after the temp folder is gone: the viewer has shown the run-ended screen by now
    sys.exit(0)


# END FUNCTION DEFS
# ------------------------------------------------------------------------
def main():
    stop_event = multiprocessing.Event()
    abort_event = multiprocessing.Event()  # set before stop_event on an abort; the camera servers read it at their shutdown
    state = State()  # exists before any handler that uses it is installed

    def stop_from_signal():
        """Ctrl-C and SIGTERM: the same stop as the button, with no dialog. With the cameras
        running the save drive is checked and a failure aborts the run; with no cameras
        running there is nothing to check."""
        cameras_running = state.camera_process is not None and state.camera_process.is_alive()
        if not cameras_running or stop_requested(state, None):
            graceful_shutdown(state, stop_event, abort_event)

    def int_handler(_sig: int, _frame: FrameType | None):
        print(STOP_BANNER)
        print(
            "Received signal to terminate, shutting down gracefully.\nTo force exit, press Ctrl+C again; "
            + "once the cameras have stopped, the collection finishes before the program exits."
        )
        _ = signal.signal(signal.SIGINT, forced_exit(state, stop_event, abort_event))
        stop_from_signal()

    def term_handler(_sig: int, _frame: FrameType | None):
        print(STOP_BANNER)
        print("Received SIGTERM, shutting down gracefully.")
        stop_from_signal()

    _ = signal.signal(signal.SIGINT, int_handler)
    _ = signal.signal(signal.SIGTERM, term_handler)

    parser = argparse.ArgumentParser(description="ratrixcam", add_help=False)
    _ = parser.add_argument("-c", "--config", type=str, required=True)
    args = vars(parser.parse_args())
    config_path: str = args["config"]  # pyright: ignore[reportAny]  # argparse hands back Any

    ensure_config_file_exists(config_path)

    config = load_settings(config_path)
    if config is None:
        return
    state.config = config
    if not cameras_are_distinct(config):
        return
    if not temp_is_clear(config.temp_path):
        print(f"ERROR: temp folder is not empty: {config.temp_path}")
        print("       it holds leftovers of an earlier run; rename it (for example temp_<date>) and relaunch")
        sys.exit(1)
    # the save folder must already exist: creating it here would put a folder of the drive's
    # name on the system disk whenever the drive is not mounted. Checked before the terminal
    # log starts, so a launch refused here leaves nothing in temp
    if not os.path.isdir(config.save_path):
        print(f"ERROR: Save drive folder not found: {config.save_path}")
        return
    reason = not_writable(config.save_path)
    if reason is not None:
        print(f"ERROR: Save drive folder is not writable: {reason}")
        return
    if not ensure_dir_exists(config.temp_path):
        print("ERROR: temp folder unavailable")
        sys.exit(1)

    # from here on, everything printed by this process and the camera processes it spawns
    # also goes to a log file in the temp folder; it moves to the output drive at shutdown
    state.run_metadata = RunMetadata(config, config_path)
    if not state.run_metadata.start_terminal_log():
        sys.exit(1)

    # GUI
    bgcolor = "#3b0a0a"
    if os.environ.get("DISPLAY", "") == "":
        os.environ.__setitem__("DISPLAY", ":0.0")

    # the folders themselves are named at the top of the terminal log
    stills_folder = stills_dir(config.temp_path)
    if not ensure_dir_exists(stills_folder):
        print("ERROR: Stills folder does not exist and could not be created")
        return

    if not ensure_dir_exists(config.temp_path):
        print("ERROR: Temp streaming folder does not exist and could not be created")
        return

    state.camera_process = Process(target=run_without_handlers, args=(config, stop_event, abort_event))
    state.camera_process.start()
    state.run_metadata.record_start()

    address: str | None = None
    viewer = start_viewer(config.temp_path)
    if viewer is not None:
        state.viewer, address = viewer
        print(f"Viewer: {address}")
        _ = webbrowser.open(address)

    state.current_window = create_recording_window(state, bgcolor, config, address)

    _ = signal.signal(signal.SIGINT, int_handler)
    _ = signal.signal(signal.SIGTERM, term_handler)

    state.current_window.mainloop()
    state.current_window = None
    graceful_shutdown(state, stop_event, abort_event)


if __name__ == "__main__":
    main()
