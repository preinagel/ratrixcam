# version 2503061247
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

import ratrix_multicam
from ratrix_utils import (
    Config,
    ensure_config_file_exists,
    ensure_dir_exists,
    load_settings,
)


class State:
    def __init__(self):
        self.camera_process: Process | None = None
        self.current_window: tk.Tk | None = None


def run_without_handlers(config: Config, stop_event: Event):
    _ = signal.signal(signal.SIGINT, signal.SIG_IGN)
    _ = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ratrix_multicam.run(config, stop_event)


def hdd_status_update_loop(
    window: tk.Tk, hdd_used: tk.Label, hdd_status: ttk.Progressbar, out_path: str
):
    total, used, _free = shutil.disk_usage(out_path)
    hdd_space_used = 100 * used / total  # local scope
    hdd_status.step(hdd_space_used)
    _ = hdd_used.config(text=f"SSD Space Used: {round(hdd_space_used)}%")
    _ = window.after(
        5000, hdd_status_update_loop, window, hdd_used, hdd_status, out_path
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


def update_config_file(
    config_file_path: str,
    config: Config,
):
    with open(config_file_path, "w") as file:
        _ = file.seek(0)
        _ = file.write(config.model_dump_json(indent=2))
        _ = file.truncate()


def get_camera_still_from_file(
    stills_folder: str,
    cam_number: int,
    cam_x: int,
    cam_y: int,
) -> Image.Image | None:
    # the still images are named cam_01_status.jpg etc
    stillname = os.path.join(
        stills_folder, f"cam_{str(cam_number).zfill(2)}_status.png"
    )
    if not os.path.exists(stillname):
        # file not created yet
        # print('cannot find file',stillname)
        return
    # file is frequently updated, so could be truncated if mid-write
    for attempt in range(10):  # try for one sec
        try:
            im = Image.open(stillname, mode="r")
            resized = im.resize((cam_x, cam_y), resample=2)
            return resized
        except Exception as e:
            # If the error message indicates a truncated file, wait and retry.
            # print(f"Attempt {attempt+1} to load {stillname} failed: {e}")
            time.sleep(0.1)


global_images = []


def camera_image_update_loop(
    window: tk.Tk,
    cam_refresh: int,
    default_img: PIL.ImageTk.PhotoImage,
    stillFolder: str,
    cam_image: tk.Label,
    cam_number: int,
    cam_x: int,
    cam_y: int,
):
    new_image = get_camera_still_from_file(stillFolder, cam_number, cam_x, cam_y)
    if new_image is None:
        new_image = default_img
    else:
        new_image = ImageTk.PhotoImage(new_image)

    _ = cam_image.config(image=new_image)
    cam_image.image = new_image

    _ = window.after(
        cam_refresh,
        camera_image_update_loop,
        window,
        cam_refresh,
        default_img,
        stillFolder,
        cam_image,
        cam_number,
        cam_x,
        cam_y,
    )


def create_config_editor(
    state: State, bgcolor: str, config: Config, config_path: str, stop_event: Event
) -> tk.Tk:
    window = tk.Tk()
    _ = window.configure(background=bgcolor)
    window.geometry("1024x600")
    window.title("Ratrix Cameras")

    entry_font = font.Font(family="Helvitica", size=19)
    version_font = font.Font(family="Helvitica", size=9)
    button_font = font.Font(family="Helvitica", size=15)

    date_now = dt.datetime.now()

    # Create Label to display the Date
    time_label = tk.Label(
        window,
        text=f"{date_now:%A, %B %d, %Y}",
        bg=bgcolor,
        fg="#ffffff",
        font=version_font,
    )
    time_label.place(x=570, y=40)

    study_label_title = tk.Label(
        window, text="Study Name:", bg=bgcolor, fg="#ffffff", font=entry_font
    )
    study_label_input = tk.StringVar(window, value=config.study_label)
    study_label_entry = tk.Entry(
        window,
        textvariable=study_label_input,
        bg=bgcolor,
        fg="#ffffff",
        font=entry_font,
    )
    _ = study_label_entry.configure(bg=bgcolor, insertbackground="white")
    study_label_title.place(x=65, y=20)
    study_label_entry.place(x=240, y=20)

    camera_label_values: list[tk.StringVar] = [
        tk.StringVar(window, value=name) for name in config.camera_names
    ]
    camera_label_entrys = [
        tk.Entry(
            window,
            textvariable=camera_label_value,
            insertbackground="white",
            bg=bgcolor,
            font=entry_font,
            fg="#ffffff",
        )
        for camera_label_value in camera_label_values
    ]

    # Layout for camera label input fields - this is a highly optional feature, it can be handled by editing json by hand
    row_step = 60
    pivot_point = 120
    x1 = [65, 540]
    x2 = [200, 675]
    columns = 2
    rows = config.Ncameras // 2
    for cl in range(columns):  # left vs right columns
        for rw in range(0, rows):  # rows
            ind = rows * cl + rw
            id_label_str = f"Cam {ind + 1} ID:"
            cameraID_label = tk.Label(
                window,
                text=id_label_str,
                bg=bgcolor,
                fg="#ffffff",
                font=entry_font,
            )
            cameraID_label.place(x=x1[cl], y=pivot_point + rw * row_step)
            camera_label_entrys[ind].place(x=x2[cl], y=pivot_point + rw * row_step)

    def submit():
        config.study_label = study_label_input.get()
        config.camera_names = [
            camera_label.get() for camera_label in camera_label_values
        ]
        update_config_file(config_path, config)

    tk.Button(
        window,
        text="Update Configuration",
        bd=0,
        height=2,
        width=20,
        font=button_font,
        command=submit,
    ).place(x=205, y=pivot_point + (rows + 1) * row_step)

    def start_recording():
        state.camera_process = Process(
            target=run_without_handlers, args=(config, stop_event)
        )
        state.camera_process.start()
        window.destroy()

    tk.Button(
        window,
        text="Start Recording",
        bg="green",
        fg="green",
        bd=0,
        height=3,
        width=19,
        font=button_font,
        command=start_recording,
    ).place(x=100, y=500)

    return window


def create_recording_window(state: State, bgcolor: str, config: Config) -> tk.Tk:
    window = tk.Tk()
    _ = window.configure(background=bgcolor)
    window.geometry("1424x1200")
    right_row = 1150
    window.title("Ratrix Camera System")

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
    total, used, _free = shutil.disk_usage(config.out_path)
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
    hdd_status_update_loop(
        window,
        hdd_used_label,
        hdd_status_progress_bar,
        config.out_path,
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
        text=f"Recording: {config.width}x{config.height} @ {config.fps}fps, {config.time_slice // 60} min",
        bg=bgcolor,
        fg="#ffffff",
        font=small_font,
    ).place(x=right_row, y=330)

    # Set up camera display
    # initialize to blank images
    no_signal = Image.open(config.blankImage)
    no_signal_r = no_signal.resize((cam_x, cam_y), resample=2)
    resized_no_signal = ImageTk.PhotoImage(no_signal_r)

    for i in range(config.Ncameras):
        image_label = tk.Label(window, image=resized_no_signal)
        image_label.image = resized_no_signal
        image_label.grid(row=config.camera_rows[i], column=config.camera_cols[i])
        camera_image_update_loop(
            window,
            cam_refresh,
            resized_no_signal,
            config.stillFolder,
            image_label,
            i + 1,  # camera numbers start at 1
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
        print("Ratrix IO: Closing GUI window")
        state.current_window.destroy()
        state.current_window = None

    if state.camera_process is None:
        print("No camera process to stop")
        return

    print("Ratrix IO attempting graceful shutdown...")
    stop_event.set()

    timeout = 10
    print("Waiting for child processes to terminate...")
    for _ in range(int(timeout / 0.1)):
        if not state.camera_process.is_alive():
            break
        time.sleep(0.1)  # wait a bit before trying again
    else:
        print("RatrixCam: Timed out waiting for child processes to terminate, killing")
        state.camera_process.kill()
    print("Ratrix Cam GUI: Shutdown complete")

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

    parser = argparse.ArgumentParser(description="Ratrix Camera Setup", add_help=False)
    _ = parser.add_argument("-c", "--config", type=str, required=True)
    args = vars(parser.parse_args())
    config_path: str = args["config"]

    state = State()
    ensure_config_file_exists(config_path)

    config = load_settings(config_path)
    if config is None:
        return

    # GUI
    bgcolor = "#3b0a0a"
    if os.environ.get("DISPLAY", "") == "":
        os.environ.__setitem__("DISPLAY", ":0.0")

    state.current_window = create_config_editor(
        state, bgcolor, config, config_path, stop_event
    )
    state.current_window.attributes("-fullscreen", True)
    # reestablish signal handlers since tkinter messes them up
    _ = signal.signal(signal.SIGINT, int_handler)
    _ = signal.signal(
        signal.SIGTERM,
        lambda sig, frame: graceful_shutdown(state, stop_event),
    )

    state.current_window.mainloop()
    state.current_window = None

    if state.camera_process is None:
        print("Session Ended, cameras not started")
        return

    if not ensure_dir_exists(config.out_path):
        print("ERROR: Recording folder does not exist and could not be created")
        return
    else:
        print(f"Recording folder: {config.out_path}")

    state.current_window = create_recording_window(state, bgcolor, config)
    state.current_window.attributes("-fullscreen", True)

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
