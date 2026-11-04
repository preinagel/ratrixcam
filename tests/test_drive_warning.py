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


def the_dialog(tk, root):
    return [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]


def press_in_dialog(tk, root, label: str):
    buttons = [b for frame in the_dialog(tk, root).winfo_children() for b in frame.winfo_children() if isinstance(b, tk.Button)]
    next(b for b in buttons if b.cget("text") == label).invoke()


def labels_in_dialog(tk, root) -> list[str]:
    return [w.cget("text") for w in the_dialog(tk, root).winfo_children() if isinstance(w, tk.Label)]


PROBLEM = "SSD drive full: 1.0 GB needed for the files in temp, 0.1 GB free"


@pytest.fixture
def no_check_pause(monkeypatch):
    monkeypatch.setattr(io, "CHECK_PAUSE", 0)


def test_replace_ssd_dialog_returns_the_choice_pressed(tmp_path, no_check_pause):
    tk, root = hidden_root()
    results = []
    for label in io.REPLACE_SSD_CHOICES:
        root.after(50, press_in_dialog, tk, root, label)
        results.append(io.replace_ssd_dialog(root, PROBLEM, lambda: None, str(tmp_path)))  # the check passes
    root.destroy()
    assert results == ["ready", "abort", "continue"]


def test_ssd_ready_checks_again_in_place_until_the_check_passes(tmp_path, no_check_pause):
    """Two failing checks, then a pass: the same window stays up, restating each result with
    a count of the checks, and closes with 'ready' on the third press."""
    tk, root = hidden_root()
    outcomes = iter(["SSD drive full: 1.0 GB needed for the files in temp, 0.2 GB free", "SSD drive not reachable", None])
    seen: list[list[str]] = []
    dialogs: list[object] = []

    def press_and_look():
        dialogs.append(the_dialog(tk, root))
        press_in_dialog(tk, root, "SSD Ready")
        if the_dialog(tk, root).winfo_exists():
            seen.append(labels_in_dialog(tk, root))
            root.after(50, press_and_look)

    root.after(50, press_and_look)
    result = io.replace_ssd_dialog(root, PROBLEM, lambda: next(outcomes), str(tmp_path))
    root.destroy()
    assert result == "ready"
    assert len(set(map(id, dialogs))) == 1  # one window throughout, never recreated
    assert len(seen) == 2
    assert seen[0][0].startswith("SSD drive full: 1.0 GB needed for the files in temp, 0.2 GB free")
    assert seen[0][1].startswith("Checked 1 time(s), last at ") and seen[0][1].endswith(": still SSD drive full: 1.0 GB needed for the files in temp, 0.2 GB free")
    assert seen[1][0].startswith("SSD drive not reachable")
    assert seen[1][1].startswith("Checked 2 time(s), last at ") and seen[1][1].endswith(": still SSD drive not reachable")


def test_replace_ssd_text_says_how_long_temp_can_go_on(tmp_path, monkeypatch):
    import shutil
    from collections import namedtuple

    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12 - int(RATE * 1800), int(RATE * 1800)))
    text = io.replace_ssd_text(PROBLEM, str(tmp_path))
    assert text.startswith(PROBLEM)
    assert "finish" not in text
    assert text.endswith("Continue Run returns to recording; the temp drive has room for ~30 min more.")


def test_replace_ssd_dialog_closed_by_its_close_box_means_continue(tmp_path):
    tk, root = hidden_root()

    root.after(50, lambda: the_dialog(tk, root).destroy())  # what the window manager does when the close box is used
    result = io.replace_ssd_dialog(root, "SSD drive not reachable", lambda: None, str(tmp_path))
    root.destroy()
    assert result == "continue"
