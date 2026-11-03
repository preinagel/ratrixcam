"""The save-drive warning shown on the recording window: FULL within one clip, ALMOST FULL
within an hour, nothing otherwise (at the assumed write rate)."""

import pytest

io = pytest.importorskip("ratrixcam_IO")

TIME_SLICE = 600  # seconds per clip
RATE = io.ASSUMED_WRITE_RATE


def test_plenty_of_space_gives_no_warning():
    assert io.drive_warning(int(RATE * 5 * 3600), TIME_SLICE) is None


def test_under_an_hour_is_almost_full():
    due = io.drive_warning(int(RATE * 1800), TIME_SLICE)
    assert due is not None
    text, colour = due
    assert text.startswith("SSD ALMOST FULL")
    assert "~30 min" in text
    assert colour == "orange"


def test_under_one_clip_is_full():
    due = io.drive_warning(int(RATE * (TIME_SLICE - 1)), TIME_SLICE)
    assert due is not None
    text, colour = due
    assert text.startswith("SSD FULL")
    assert io.FULL_INSTRUCTIONS in text
    assert colour == "red"


def test_empty_drive_is_full():
    assert io.drive_warning(0, TIME_SLICE)[0].startswith("SSD FULL")


def test_time_left_text_switches_to_hours_at_two_hours():
    assert io.time_left_text(90 * 60) == "~90 min"
    assert io.time_left_text(2 * 3600) == "~2.0 h"
    assert io.time_left_text(9.25 * 3600) == "~9.2 h"


# ------------------------------------------------- the Replace SSD dialog at the stop click

def hidden_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    return tk, root


def press_in_dialog(tk, root, label: str):
    dialog = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]
    buttons = [b for frame in dialog.winfo_children() for b in frame.winfo_children() if isinstance(b, tk.Button)]
    next(b for b in buttons if b.cget("text") == label).invoke()


def test_replace_ssd_dialog_returns_the_choice_pressed():
    tk, root = hidden_root()
    results = []
    for label in io.REPLACE_SSD_CHOICES:
        root.after(50, press_in_dialog, tk, root, label)
        results.append(io.replace_ssd_dialog(root, "SSD drive full: 1.0 GB needed, 0.1 GB free"))
    root.destroy()
    assert results == ["ready", "abort", "continue"]


def test_replace_ssd_dialog_closed_by_its_close_box_means_continue():
    tk, root = hidden_root()

    def close_box():
        dialog = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]
        dialog.destroy()  # what the window manager does when the close box is used, after the protocol handler

    root.after(50, close_box)
    result = io.replace_ssd_dialog(root, "SSD drive not reachable")
    root.destroy()
    assert result == "continue"
