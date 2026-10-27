import argparse
import datetime as dt
import multiprocessing
import os
import shutil
import signal
import sys
import time
import tkinter as tk
import tkinter.font as font
from multiprocessing import Process
from multiprocessing.synchronize import Event
from tkinter import messagebox, ttk
from types import FrameType

import PIL
import PIL.ImageTk
from PIL import Image, ImageTk

import ratrixcam_multi
from ratrixcam_run_metadata import RunMetadata
from ratrixcam_naming import camera_name, still_path
from ratrixcam_utils import (
    BLANK_IMAGE,
    Config,
    ensure_config_file_exists,
    ensure_dir_exists,
    load_settings,
    reset_stills,
    stills_dir,
    temp_is_clear,
)


class State:
    def __init__(self):
        self.camera_process: Process | None = None
        self.current_window: tk.Tk | None = None
        self.run_metadata: RunMetadata | None = None


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
        seen_names.setdefault(key, idx)
        if camera.position in seen_positions:
            print(f"ERROR: cameras {seen_positions[camera.position]} and {idx} both have position {camera.position!r}")
            ok = False
        seen_positions.setdefault(camera.position, idx)
    return ok


def run_without_handlers(config: Config, stop_event: Event):
    _ = signal.signal(signal.SIGINT, signal.SIG_IGN)
    _ = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ratrixcam_multi.run(config, stop_event)


# Assumed write rate of the whole rig (8 cameras), used to estimate how long the save drive
# will last. A ballpark constant for now; it could be replaced by a rate measured during the run.
ASSUMED_WRITE_RATE = 1e9 / 60  # bytes per second (1 GB per minute)
ALMOST_FULL_SECONDS = 3600  # warn when the drive is expected to fill within an hour

FULL_INSTRUCTIONS = "Stop the recording now."  # placeholder; the operator instructions are TBD


def seconds_until_full(free_bytes: int) -> float:
    """Expected time until the save drive is full, at the assumed write rate."""
    return free_bytes / ASSUMED_WRITE_RATE


def time_left_text(seconds: float) -> str:
    """'~N min' under two hours, '~N.N h' otherwise."""
    if seconds < 7200:
        return f"~{seconds / 60:.0f} min"
    return f"~{seconds / 3600:.1f} h"


def drive_warning(free_bytes: int, time_slice: int) -> tuple[str, str] | None:
    """(text, background colour) of the warning to show for this much free space, or None when
    no warning is due. FULL = expected to fill within one clip (time_slice seconds); ALMOST FULL
    = within ALMOST_FULL_SECONDS."""
    seconds = seconds_until_full(free_bytes)
    if seconds < time_slice:
        return f"SSD FULL\n{FULL_INSTRUCTIONS}", "red"
    if seconds < ALMOST_FULL_SECONDS:
        return f"SSD ALMOST FULL\n{time_left_text(seconds)} of recording left", "orange"
    return None


def hdd_status_update_loop(
    window: tk.Tk,
    hdd_used: tk.Label,
    hdd_status: ttk.Progressbar,
    warning: tk.Label,
    warning_xy: tuple[int, int],
    out_path: str,
    time_slice: int,
):
    """Every 5 s: refresh the space-used line and progress bar, and show or hide the drive
    warning box."""
    try:
        total, used, free = shutil.disk_usage(out_path)
    except OSError:
        _ = hdd_used.config(text="SSD Space Used: drive not found")
        _ = warning.config(text=f"SSD NOT FOUND\n{out_path}", bg="red")
        warning.place(x=warning_xy[0], y=warning_xy[1])
    else:
        hdd_space_used = 100 * used / total  # local scope
        _ = hdd_status.config(value=used)  # bar maximum is the drive's total bytes
        _ = hdd_used.config(
            text=f"SSD Space Used: {round(hdd_space_used)}%  ({time_left_text(seconds_until_full(free))} left)"
        )
        due = drive_warning(free, time_slice)
        if due is None:
            warning.place_forget()
        else:
            _ = warning.config(text=due[0], bg=due[1])
            warning.place(x=warning_xy[0], y=warning_xy[1])
    _ = window.after(
        5000,
        hdd_status_update_loop,
        window,
        hdd_used,
        hdd_status,
        warning,
        warning_xy,
        out_path,
        time_slice,
    )


def time_recorded_update_loop(
    window: tk.Tk,
    time_label: tk.Label,
    date_label: tk.Label,
    recorded_label: tk.Label,
    start_recording_date: dt.datetime,
):
    date_now = dt.datetime.now()
    _ = time_label.config(text=f"{date_now:%H:%M:%S}")
    _ = date_label.config(text=f"{date_now:%A, %B %d, %Y}")
    time_recorded = date_now - start_recording_date
    time_sec = time_recorded.total_seconds()

    _ = recorded_label.config(
        text=f"Total recorded: {divmod(time_sec, 86400)[0]} days, {divmod(time_sec, 3600)[0]} hours"
    )
    _ = window.after(
        1000,
        time_recorded_update_loop,
        window,
        time_label,
        date_label,
        recorded_label,
        start_recording_date,
    )


def get_camera_still_from_file(
    stills_folder: str, camera_name: str, cam_x: int, cam_y: int
) -> Image.Image | None:
    still = still_path(stills_folder, camera_name)
    if not os.path.exists(still):
        return

    # file is frequently updated, so could be truncated if mid-write
    for _ in range(10):  # try for one sec
        try:
            img = Image.open(still, mode="r")
            img = img.resize((cam_x, cam_y), resample=2)
            return img
        except Exception as _:
            # If the error message indicates a truncated file, wait and retry.
            # print(f"Attempt {attempt+1} to load {stillname} failed: {e}")
            time.sleep(0.1)


global_images = []


def camera_image_update_loop(
    window: tk.Tk,
    cam_refresh: int,
    default_img: PIL.ImageTk.PhotoImage,
    cam_image: tk.Label,
    stills_path: str,
    cam_name: str,
    cam_x: int,
    cam_y: int,
):
    new_image = get_camera_still_from_file(stills_path, cam_name, cam_x, cam_y)
    if new_image is None:
        photo_img = default_img
    else:
        photo_img = ImageTk.PhotoImage(new_image)

    _ = cam_image.config(image=photo_img)
    cam_image.image = photo_img  # pyright: ignore[reportAttributeAccessIssue]  (keeps the image alive)

    _ = window.after(
        cam_refresh,
        camera_image_update_loop,
        window,
        cam_refresh,
        default_img,
        cam_image,
        stills_path,
        cam_name,
        cam_x,
        cam_y,
    )


# display cells for the eight camera positions, laid out  A B | E F  over  C D | G H
# (grid column 2 is left empty as a gap between the two blocks)
GRID_CELLS = {
    "A": (0, 0), "B": (0, 1), "E": (0, 3), "F": (0, 4),
    "C": (1, 0), "D": (1, 1), "G": (1, 3), "H": (1, 4),
}


def grid_cell(position: str) -> tuple[int, int]:
    """(row, column) on the display for a camera's position letter."""
    return GRID_CELLS[position]


def create_recording_window(state: State, bgcolor: str, config: Config) -> tk.Tk:
    window = tk.Tk()
    _ = window.configure(background=bgcolor)
    window.geometry("1424x1200")
    right_row = 1150
    window.title("ratrixcam")

    cam_refresh = 1000
    cam_x, cam_y = 280, 215

    small_font = font.Font(family="Helvitica", size=12)
    study_font = font.Font(family="Helvitica", size=14)
    date_font = font.Font(family="Helvitica", size=11)
    hdd_font = font.Font(family="Helvitica", size=17)
    button_font = font.Font(family="Helvitica", size=15)

    date_now = dt.datetime.now()
    start_recording_date = date_now

    # Create Label to display the Date
    date_label = tk.Label(
        window,
        text=f"{date_now:%A, %B %d, %Y}",
        bg=bgcolor,
        fg="#ffffff",
        font=date_font,
    )
    date_label.place(x=right_row, y=10)
    time_label = tk.Label(
        window,
        text=f"{date_now:%H:%M:%S}",
        bg=bgcolor,
        fg="#ffffff",
        font=date_font,
    )
    time_label.place(x=right_row + 165, y=10)

    # HDD status progressbar
    total, used, _free = shutil.disk_usage(config.save_path)
    hdd_used_label = tk.Label(
        window,
        text=f"SSD Drive Space Used: {round(100 * used / total, 1)}%",
        borderwidth=0,
        bg=bgcolor,
        font=hdd_font,
        fg="#ffffff",
    )
    hdd_used_label.place(x=right_row, y=140)

    _hdd_capacity_label = tk.Label(
        window,
        text=f"SSD Drive Capacity: {round((total // (2**30)) / 1000, 1)} TB",
        borderwidth=0,
        bg=bgcolor,
        font=small_font,
        fg="#ffffff",
    ).place(x=right_row, y=165)

    hdd_status_progress_bar = ttk.Progressbar(
        window,
        orient="horizontal",
        style="red.Horizontal.TProgressbar",
        length=220,
        mode="determinate",
        takefocus=True,
        maximum=total,
        value=used,
    )
    # drive warning box: hidden until the drive is almost full, full, or missing
    drive_warning_label = tk.Label(
        window,
        text="",
        bg="red",
        fg="#ffffff",
        font=hdd_font,
        wraplength=260,
        justify="left",
        padx=8,
        pady=8,
    )
    hdd_status_update_loop(
        window,
        hdd_used_label,
        hdd_status_progress_bar,
        drive_warning_label,
        (right_row, 380),
        config.save_path,
        config.time_slice,
    )

    # Showing recording duration
    time_recorded = start_recording_date - dt.datetime.now()
    time_recorded_label = time_recorded.total_seconds()
    start_time_label_01 = tk.Label(
        window,
        text="Recording started at: ",
        bg=bgcolor,
        fg="#ffffff",
        font=small_font,
    )
    start_time_label_01.place(x=right_row, y=220)
    start_time_label_02 = tk.Label(
        window,
        text=f"{start_recording_date:%A, %B %d, %Y, %H:%M:%S}",
        bg=bgcolor,
        fg="#ffffff",
        font=small_font,
    )
    start_time_label_02.place(x=right_row, y=245)
    recorded_time = (
        "Total recorded: "
        + str(divmod(time_recorded_label, 86400)[0])
        + " days, "
        + str(divmod(time_recorded_label, 3600)[0])
        + " hours"
    )
    recorded_label = tk.Label(
        window,
        text=str(recorded_time),
        borderwidth=0,
        bg=bgcolor,
        font=small_font,
        fg="#ffffff",
    )
    recorded_label.place(x=right_row + 1, y=270)

    time_recorded_update_loop(
        window,
        time_label,
        date_label,
        recorded_label,
        start_recording_date,
    )

    # Setup for the camera view display
    tk.Label(
        window,
        text=f"Study Name: {config.study_label}",
        bg=bgcolor,
        fg="#ffffff",
        font=study_font,
    ).place(x=right_row, y=295)

    tk.Label(
        window,
        text=f"Recording: {config.default_width}x{config.default_height} @ {config.default_fps}fps (default), {config.time_slice // 60} min",
        bg=bgcolor,
        fg="#ffffff",
        font=small_font,
    ).place(x=right_row, y=330)

    # Set up camera display
    # initialize to blank images
    no_signal = Image.open(BLANK_IMAGE)
    no_signal_r = no_signal.resize((cam_x, cam_y), resample=2)
    resized_no_signal = ImageTk.PhotoImage(no_signal_r)

    stills_folder = stills_dir(config.temp_path)
    for camera in config.cameras:
        row, column = grid_cell(camera.position)
        image_label = tk.Label(window, image=resized_no_signal)
        image_label.image = resized_no_signal  # pyright: ignore[reportAttributeAccessIssue]  (keeps the image alive)
        image_label.grid(row=row, column=column)
        camera_image_update_loop(
            window,
            cam_refresh,
            resized_no_signal,
            image_label,
            stills_folder,
            camera_name(config.subject_prefix, camera.subjID, camera.view),
            cam_x,
            cam_y,
        )

    def stop_recording():
        if state.camera_process is None or not state.camera_process.is_alive():
            confirmation = messagebox.askquestion(
                "Cameras appear to be off already", "Are you sure?", icon="warning"
            )
            if confirmation == "no":
                return
        else:
            confirmation = messagebox.askquestion(
                "STOP all recordings", "Are you sure?", icon="warning"
            )
            if confirmation == "no":
                return
        window.destroy()

    tk.Button(
        window,
        text="Stop Recording",
        bg="red",
        fg="red",
        bd=0,
        height=3,
        width=19,
        font=button_font,
        command=stop_recording,
    ).place(x=550, y=500)

    # Realtime updates
    # time_refresh_record(start_recording_date,recorded_label,window_record) # passing params causes crashes?

    return window


def graceful_shutdown(state: State, stop_event: Event):
    _ = signal.signal(signal.SIGINT, signal.SIG_DFL)
    _ = signal.signal(signal.SIGTERM, signal.SIG_DFL)

    if state.current_window is not None:
        print("ratrixcam IO: closing GUI window")
        state.current_window.destroy()
        state.current_window = None

    if state.camera_process is None:
        print("No camera process to stop")
        if state.run_metadata is not None and state.run_metadata.terminal_log is not None:
            state.run_metadata.terminal_log.stop()
        return

    print("ratrixcam IO: attempting graceful shutdown...")
    stop_event.set()

    timeout = 600 #transcoding takes ~50% video duration, so wait 10min before killing unfinished processes
                  #would be better to set this based on the slice duration but this is not available info
    print("Waiting for child processes to terminate...")
    for _ in range(int(timeout / 0.1)):
        if not state.camera_process.is_alive():
            break
        time.sleep(0.1)  # wait a bit before trying again
    else:
        print("ratrixcam IO: timed out waiting for child processes to terminate, killing")
        state.camera_process.kill()
    print("ratrixcam IO: shutdown complete")

    # cameras are down: gather the run's artifacts (terminal log, stills, loose temp files)
    if state.run_metadata is not None:
        try:
            state.run_metadata.collect_at_shutdown()
        except Exception as e:
            print(f"WARNING: run metadata collection failed: {e}")

    sys.exit(0)


# END FUNCTION DEFS
# ------------------------------------------------------------------------
def main():
    stop_event = multiprocessing.Event()

    def int_handler(_sig: int, _frame: FrameType | None):
        print(
            "Received signal to terminate, shutting down gracefully.\nTo force exit, press Ctrl+C again."
        )
        _ = signal.signal(signal.SIGINT, signal.SIG_DFL)
        graceful_shutdown(state, stop_event)

    _ = signal.signal(signal.SIGINT, int_handler)
    _ = signal.signal(
        signal.SIGTERM,
        lambda sig, frame: graceful_shutdown(state, stop_event),
    )

    parser = argparse.ArgumentParser(description="ratrixcam", add_help=False)
    _ = parser.add_argument("-c", "--config", type=str, required=True)
    args = vars(parser.parse_args())
    config_path: str = args["config"]

    state = State()
    ensure_config_file_exists(config_path)

    config = load_settings(config_path)
    if config is None:
        return
    if not cameras_are_distinct(config):
        return
    if not temp_is_clear(config.temp_path):
        print(f"ERROR: temp folder is not empty: {config.temp_path}")
        print("       it holds leftovers of an earlier run; rename it (for example temp_<date>) and relaunch")
        sys.exit(1)
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
    # the save folder must already exist: creating it here would put a folder of the drive's
    # name on the system disk whenever the drive is not mounted
    if not os.path.isdir(config.save_path):
        print(f"ERROR: Save drive folder not found: {config.save_path}")
        return

    stills_folder = stills_dir(config.temp_path)
    if not ensure_dir_exists(stills_folder):
        print("ERROR: Stills folder does not exist and could not be created")
        return

    if not ensure_dir_exists(config.temp_path):
        print("ERROR: Temp streaming folder does not exist and could not be created")
        return

    # get rid of any old still images
    print("Removing any old still images")
    reset_stills(config)

    state.camera_process = Process(target=run_without_handlers, args=(config, stop_event))
    state.camera_process.start()
    state.run_metadata.record_start()

    state.current_window = create_recording_window(state, bgcolor, config)

    _ = signal.signal(signal.SIGINT, int_handler)
    _ = signal.signal(
        signal.SIGTERM,
        lambda sig, frame: graceful_shutdown(state, stop_event),
    )

    state.current_window.mainloop()
    state.current_window = None
    graceful_shutdown(state, stop_event)


if __name__ == "__main__":
    main()
