"""The launcher's window-side pieces: the Replace SSD dialog, the stop request, the stopping
window and its wait, the viewer's server at launch."""

import pytest

io = pytest.importorskip("ratrixcam_IO")

RATE = pytest.importorskip("ratrixcam_utils").ASSUMED_WRITE_RATE  # the assumed 8-camera write rate


def test_time_left_text_switches_to_hours_at_two_hours():
    assert io.time_left_text(90 * 60) == "~90 min"
    assert io.time_left_text(2 * 3600) == "~2.0 h"
    assert io.time_left_text(9.25 * 3600) == "~9.2 h"


# ------------------------------------------------- the Replace SSD dialog at the stop click

@pytest.fixture(autouse=True)
def no_window_on_screen(monkeypatch):
    """Every Tk window a test makes is created withdrawn, so nothing flashes on the screen
    of whoever runs the suite; the widgets inside behave the same."""
    tk = pytest.importorskip("tkinter")

    class HiddenTk(tk.Tk):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.withdraw()

    class HiddenToplevel(tk.Toplevel):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.withdraw()

        def grab_set(self):
            pass  # a withdrawn window cannot take the grab, and the tests do not need it

    monkeypatch.setattr(io.tk, "Tk", HiddenTk)
    monkeypatch.setattr(io.tk, "Toplevel", HiddenToplevel)


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


def one_camera(tmp_path):
    """A stand-in config with one camera and tmp_path as the temp folder."""
    from types import SimpleNamespace

    return SimpleNamespace(temp_path=str(tmp_path), cameras=[object()])


@pytest.fixture
def no_check_pause(monkeypatch):
    monkeypatch.setattr(io, "CHECK_PAUSE", 0)


def test_replace_ssd_dialog_returns_the_choice_pressed(tmp_path, no_check_pause):
    tk, root = hidden_root()
    results = []
    for label in io.REPLACE_SSD_CHOICES:
        root.after(50, press_in_dialog, tk, root, label)
        results.append(io.replace_ssd_dialog(root, PROBLEM, lambda: None, one_camera(tmp_path)))  # the check passes
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
    result = io.replace_ssd_dialog(root, PROBLEM, lambda: next(outcomes), one_camera(tmp_path))
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
    free = int(RATE / 8 * 1800)  # 30 minutes at one camera's share of the 8-camera rate
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12 - free, free))
    text = io.replace_ssd_text(PROBLEM, one_camera(tmp_path))
    assert text.startswith(PROBLEM)
    assert "finish" not in text
    assert text.endswith("Continue Run returns to recording; the temp drive has room for ~30 min more.")


def test_replace_ssd_dialog_closed_by_its_close_box_means_continue(tmp_path):
    tk, root = hidden_root()

    root.after(50, lambda: the_dialog(tk, root).destroy())  # what the window manager does when the close box is used
    result = io.replace_ssd_dialog(root, "SSD drive not reachable", lambda: None, one_camera(tmp_path))
    root.destroy()
    assert result == "continue"


# ------------------------------------------------- the wait while the run stops

class RunningCameras:
    def is_alive(self):
        return True


def stopping_state(monkeypatch, problem):
    """A state with the cameras running and a save drive check that reports `problem`."""
    from types import SimpleNamespace

    state = io.State()
    state.config = SimpleNamespace(temp_path="/nowhere")  # only what the stop reads
    state.camera_process = RunningCameras()
    monkeypatch.setattr(io, "save_drive_problem", lambda _c: problem)
    return state


def test_stop_proceeds_when_the_check_passes(monkeypatch, capsys):
    state = stopping_state(monkeypatch, None)
    assert io.stop_requested(state, None) is True
    assert state.abort is False and state.stopping is False
    assert "save drive check passed: shutting down" in capsys.readouterr().out


def test_stop_without_a_window_aborts_when_the_check_fails(monkeypatch, capsys):
    state = stopping_state(monkeypatch, "SSD drive not reachable")
    assert io.stop_requested(state, None) is True
    assert state.abort is True
    assert "Save drive check failed (SSD drive not reachable): aborting the run" in capsys.readouterr().out


def test_stop_with_a_window_follows_the_dialog(monkeypatch, capsys):
    state = stopping_state(monkeypatch, "SSD drive not reachable")
    monkeypatch.setattr(io, "replace_ssd_dialog", lambda *a: "continue")
    assert io.stop_requested(state, object()) is False
    assert state.abort is False
    monkeypatch.setattr(io, "replace_ssd_dialog", lambda *a: "abort")
    assert io.stop_requested(state, object()) is True
    assert state.abort is True
    out = capsys.readouterr().out
    assert "Continue Run chosen" in out and "Abort Run chosen" in out


def test_a_second_window_request_during_the_dialog_is_ignored_but_a_signal_aborts(monkeypatch, capsys):
    state = stopping_state(monkeypatch, "SSD drive not reachable")
    state.stopping = True  # the dialog is up
    assert io.stop_requested(state, object()) is False
    assert state.abort is False
    assert io.stop_requested(state, None) is True
    assert state.abort is True
    assert "Signal received while the Replace SSD dialog is up: aborting the run" in capsys.readouterr().out


def test_wait_until_ticks_until_done():
    ticks = []
    assert io.wait_until(lambda: len(ticks) >= 3, timeout=5, tick=lambda: ticks.append(1), interval=0) is True
    assert len(ticks) == 3


def test_wait_until_gives_up_at_the_timeout():
    ticks = []
    assert io.wait_until(lambda: False, timeout=0.05, tick=lambda: ticks.append(1), interval=0.01) is False
    assert ticks


def test_stopping_window_shows_the_phase_and_the_elapsed_time():
    tk, root = hidden_root()
    root.destroy()  # the stopping window makes its own root, as it does after the recording window is gone
    window = io.StoppingWindow("ratrixcam: stopping the run")
    if window.root is None:
        pytest.skip("no display")
    window.show("Stopping the run.\nWaiting for the cameras.")
    assert window.label is not None
    assert window.label.cget("text") == "Stopping the run.\nWaiting for the cameras.\n\nElapsed 0:00"
    window.close()
    assert window.root is None
    window.show("after close")  # harmless


def test_close_window_cancels_the_pending_timers_before_destroying():
    tk, root = hidden_root()
    fired = []
    root.after(50, lambda: fired.append(1))
    root.after(50, lambda: fired.append(2))
    assert len(root.tk.call("after", "info")) == 2
    io.close_window(root)
    with pytest.raises(tk.TclError):
        root.winfo_exists()  # destroyed
    assert fired == []


def test_an_abort_sets_the_abort_event_before_the_stop_event():
    order = []

    class Event:
        def __init__(self, name):
            self.name = name

        def set(self):
            order.append(self.name)

    class Gone:  # the camera process has already exited
        def is_alive(self):
            return False

    state = io.State()
    state.camera_process = Gone()
    state.abort = True
    with pytest.raises(SystemExit):
        io.graceful_shutdown(state, Event("stop"), Event("abort"))
    assert order == ["abort", "stop"]


# ------------------------------------------------- the viewer's server at launch

FAKE_SERVER = """
import socket, sys, time
port = int(sys.argv[sys.argv.index("--port") + 1])
s = socket.socket(); s.bind(("127.0.0.1", port)); s.listen()
time.sleep(60)
"""


@pytest.fixture
def fake_viewer(tmp_path, monkeypatch):
    """A stand-in for gui/serve.py that holds its port, on high test ports, with no start wait."""
    script = tmp_path / "serve.py"
    script.write_text(FAKE_SERVER)
    monkeypatch.setattr(io, "VIEWER_SCRIPT", str(script))
    monkeypatch.setattr(io, "VIEWER_PORTS", range(18760, 18763))
    monkeypatch.setattr(io, "VIEWER_START_WAIT", 0.3)
    return script


def test_the_viewer_starts_on_the_first_free_port(tmp_path, fake_viewer):
    import socket

    taken = socket.socket()
    taken.bind(("127.0.0.1", 18760))
    try:
        started = io.start_viewer(str(tmp_path))
        assert started is not None
        server, address = started
        assert address == "http://127.0.0.1:18761/"
        assert server.poll() is None
        state = io.State()
        state.viewer = server
        io.stop_viewer(state)
        assert state.viewer is None and server.poll() is not None
    finally:
        taken.close()


def test_a_missing_viewer_script_is_a_warning_not_a_stop(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(io, "VIEWER_SCRIPT", str(tmp_path / "absent.py"))
    assert io.start_viewer(str(tmp_path)) is None
    assert "WARNING: viewer not started" in capsys.readouterr().out


def test_no_free_port_is_a_warning_not_a_stop(tmp_path, fake_viewer, monkeypatch, capsys):
    import socket

    held = []
    try:
        for port in (18760, 18761, 18762):
            s = socket.socket()
            s.bind(("127.0.0.1", port))
            held.append(s)
        assert io.start_viewer(str(tmp_path)) is None
        assert "WARNING: viewer not started: no free port in 18760-18762" in capsys.readouterr().out
    finally:
        for s in held:
            s.close()


def test_a_second_ctrl_c_sets_both_events_before_the_forced_exit(monkeypatch):
    order = []

    class Event:
        def __init__(self, name):
            self.name = name

        def set(self):
            order.append(self.name)

    exits = []
    monkeypatch.setattr(io.os, "_exit", lambda code: exits.append(code))
    state = io.State()
    io.forced_exit(state, Event("stop"), Event("abort"))(2, None)
    assert order == ["abort", "stop"]  # the servers wind down and leave their clips in temp
    assert exits == [130]
