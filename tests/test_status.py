"""The status file the viewer reads (ratrixcam_status): every field always present, written
atomically, the drive facts from python's own rules, one terminal line per transition."""

import json
import os
import platform
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypedDict, cast

import pytest

from helpers import VIDEOPROC_DIR

sys.path.insert(0, str(VIDEOPROC_DIR.parent))
import ratrixcam_status  # noqa: E402
from ratrixcam_manifest import write_row  # noqa: E402
from ratrixcam_naming import STATUS_FILENAME, manifest_filename, temp_manifests_dir  # noqa: E402
from ratrixcam_status import StatusWriter, WriteRateEstimate, percentile  # noqa: E402
from ratrixcam_utils import CameraConfig, write_rate  # noqa: E402
from test_run_metadata import Usage, make_config  # noqa: E402

STARTED = datetime(2026, 1, 2, 3, 4, 5)
CAMERA = "subj001_cam1"  # make_config's one camera, by its name
MB = 1_000_000


def list_clip(w: StatusWriter, index: int, size: int, where: str = "save", camera: str = CAMERA) -> None:
    """Append a manifest row for the camera's clip number `index` (clip 1 starts with the run,
    each next one a clip length later) and put a file of `size` bytes where the row says
    ("save"), in the camera's temp folder ("temp"), or nowhere ("missing")."""
    day_folder = f"Test_{camera}_20260102"
    started = STARTED + timedelta(seconds=(index - 1) * w.config.time_slice)
    file_name = f"{camera}_{started:%Y%m%d_%H-%M-%S}.mp4"
    manifest = temp_manifests_dir(w.config.temp_path) / manifest_filename(camera)
    subjID, view = camera[len("subj"):].split("_", 1)
    write_row(str(manifest), f"{day_folder}/{file_name}", subjID, view, f"{started:%Y-%m-%d %H:%M:%S}.000", 25)
    if where == "save":
        clip = Path(w.config.save_path) / day_folder / file_name
    elif where == "temp":
        clip = Path(w.config.temp_path) / f"Test_{camera}" / file_name
    else:
        return
    clip.parent.mkdir(parents=True, exist_ok=True)
    _ = clip.write_bytes(b"x" * size)


def run_time(w: StatusWriter, seconds: float) -> None:
    """Pin the estimator's clock this long after the run started. With 60-s clips, clip n has
    settled (its length plus CLIP_SETTLE_S) at n * 60 + 13 s."""
    w.write_rate.now = lambda: STARTED + timedelta(seconds=seconds)


def save_free(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    """Both drives report this much free space (the save drive's room picks the rate)."""
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**13, 10**13 - free, free))


class Status(TypedDict):
    """The status file's shape, as the tests read it."""

    schema: int
    written: str
    recording: bool
    run: dict[str, object]
    save_drive: dict[str, object]
    temp_drive: dict[str, object]


def writer(tmp_path: Path, **config_changes: object) -> StatusWriter:
    config, _ = make_config(tmp_path)
    config = config.model_copy(update=config_changes)
    return StatusWriter(config, "ratrixcam_20260102_030405_abcdef01", STARTED)


def read(w: StatusWriter) -> Status:
    return cast(Status, json.loads(w.path.read_text()))


def snap(w: StatusWriter, cameras_alive: bool) -> Status:
    return cast(Status, cast(object, w.snapshot(cameras_alive)))


def test_the_file_has_every_field_and_no_temporary_name_left(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    w = writer(tmp_path)
    w.write(cameras_alive=True, force=True)
    status = read(w)
    assert status["schema"] == 1
    assert status["recording"] is True
    assert sorted(status["run"]) == ["machine_name", "message", "run_id", "started", "state"]
    assert status["run"]["run_id"] == "ratrixcam_20260102_030405_abcdef01"
    assert status["run"]["started"] == "2026-01-02 03:04:05.000"
    assert status["run"]["state"] == "running" and status["run"]["message"] == ""
    assert status["run"]["machine_name"] == platform.node()  # no machine_name in the config
    for name, drive in (("save_drive", status["save_drive"]), ("temp_drive", status["temp_drive"])):
        assert sorted(drive) == sorted(["path", "found", "total_bytes", "used_bytes", "free_bytes", "seconds_until_full"] + (["accepting_clips"] if name == "save_drive" else ["clips_waiting"]))
        assert drive["found"] is True and cast(int, drive["free_bytes"]) > 0
    assert status["save_drive"]["accepting_clips"] is True
    assert status["temp_drive"]["clips_waiting"] == 0
    assert sorted(p.name for p in Path(w.config.temp_path).iterdir()) == [STATUS_FILENAME, "status"]  # no .tmp beside it
    assert capsys.readouterr().out == ""  # a healthy drive says nothing


def test_machine_name_comes_from_the_config(tmp_path: Path):
    w = writer(tmp_path, machine_name="Mac 4")
    w.write(cameras_alive=True, force=True)
    assert read(w)["run"]["machine_name"] == "Mac 4"


def test_seconds_until_full_uses_the_rate_scaled_to_the_camera_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12 - 6 * 10**9, 6 * 10**9))
    w = writer(tmp_path)  # one camera
    status = snap(w, True)
    assert status["save_drive"]["seconds_until_full"] == round(6 * 10**9 / write_rate(1))
    assert status["save_drive"]["seconds_until_full"] == 8 * round(6 * 10**9 / write_rate(8))


def test_the_average_rate_is_all_bytes_so_far_over_the_recorded_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    w = writer(tmp_path)  # one camera, time_slice 60
    list_clip(w, 1, 6 * MB)
    list_clip(w, 2, 12 * MB)
    run_time(w, 150)  # both clips settled; a third is being written and does not count
    save_free(monkeypatch, 10**12)  # room for weeks
    status = snap(w, True)
    assert w.rate == 18 * MB / 120  # 150 kB/s: both clips over the two clip lengths they cover
    assert status["save_drive"]["seconds_until_full"] == round(10**12 / (18 * MB / 120))
    assert status["temp_drive"]["seconds_until_full"] == status["save_drive"]["seconds_until_full"]  # the same rate for both
    _ = snap(w, True)
    out = capsys.readouterr().out
    assert out.count("Write rate measured from the run's clips: 0.15 MB/s for the rig") == 1


def test_with_one_clip_the_average_and_the_conservative_rate_agree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    w = writer(tmp_path)
    list_clip(w, 1, 6 * MB)
    run_time(w, 100)
    save_free(monkeypatch, 10**12)
    assert w.rate == 6 * MB / 60
    save_free(monkeypatch, 1)
    assert w.rate == 6 * MB / 60


def test_the_conservative_rate_takes_over_under_a_day_left_on_the_save_drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    w = writer(tmp_path)
    list_clip(w, 1, 6 * MB)
    list_clip(w, 2, 12 * MB)  # the 90th-percentile clip of the two: 200 kB/s over a 60-s clip
    run_time(w, 150)
    average, conservative = 18 * MB / 120, 12 * MB / 60
    a_day = ratrixcam_status.CONSERVATIVE_BELOW_S
    save_free(monkeypatch, int(average * a_day * 2))  # two days left at the average
    assert w.rate == average
    save_free(monkeypatch, int(average * a_day / 2))  # half a day left
    assert w.rate == conservative
    shutil.rmtree(w.config.save_path)  # the drive cannot be queried: the conservative rate
    monkeypatch.undo()
    assert w.rate == conservative


def test_the_rig_is_unmeasured_until_every_camera_has_a_found_clip(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    second = CameraConfig(subjID="002", view="cam2", position="B")
    w = writer(tmp_path)
    w = StatusWriter(w.config.model_copy(update={"cameras": [*w.config.cameras, second]}), w.run_id, STARTED)
    run_time(w, 80)
    assert w.rate == write_rate(2)
    list_clip(w, 1, 6 * MB)  # the first camera only
    run_time(w, 140)  # the next manifest check
    assert w.rate == write_rate(2)
    assert "Write rate measured" not in capsys.readouterr().out
    list_clip(w, 1, 3 * MB, camera="subj002_cam2")
    run_time(w, 200)
    assert w.rate == 9 * MB / 60  # both first clips, over the one clip length they cover
    assert "Write rate measured from the run's clips" in capsys.readouterr().out


def test_a_clip_not_yet_transferred_is_found_in_temp(tmp_path: Path):
    w = writer(tmp_path)
    list_clip(w, 1, 3 * MB, where="temp")
    run_time(w, 80)
    assert w.rate == 3 * MB / 60


def test_a_listed_clip_found_nowhere_counts_as_not_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    w = writer(tmp_path)
    list_clip(w, 1, 30 * MB, where="missing")
    run_time(w, 80)
    assert w.rate == write_rate(1)  # nothing found yet: the assumed rate
    list_clip(w, 2, 6 * MB)
    run_time(w, 140)  # the next manifest check
    assert w.rate == 6 * MB / 120  # the missing clip adds nothing to the bytes, but its time counts
    save_free(monkeypatch, 1)
    assert w.rate == 6 * MB / 60  # nor to the conservative rate


def test_the_manifests_are_checked_once_per_clip_length_for_settled_clips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    reads: list[Path] = []
    original = WriteRateEstimate._listed_clips  # pyright: ignore[reportPrivateUsage]  # counting its calls

    def counting(manifest: Path) -> list[tuple[str, datetime]]:
        reads.append(manifest)
        return original(manifest)

    monkeypatch.setattr(WriteRateEstimate, "_listed_clips", staticmethod(counting))
    w = writer(tmp_path)
    list_clip(w, 1, 6 * MB)
    list_clip(w, 2, 6 * MB)
    run_time(w, 80)  # clip 1 has settled (73 s), clip 2 has not (133 s)
    assert w.rate == 6 * MB / 60 and len(reads) == 1
    assert len(w.write_rate.found[CAMERA]) == 1
    run_time(w, 135)  # clip 2 settled, but the next check is only due at 140 s
    _ = w.rate
    _ = w.rate
    assert len(reads) == 1 and len(w.write_rate.found[CAMERA]) == 1
    run_time(w, 140)
    assert w.rate == 12 * MB / 120 and len(reads) == 2
    assert len(w.write_rate.found[CAMERA]) == 2


def test_the_conservative_rate_uses_the_90th_percentile_clip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    w = writer(tmp_path)
    for index in range(1, 11):
        list_clip(w, index, index * MB)  # 1 .. 10 MB
    run_time(w, 620)  # the tenth clip settled at 613 s
    save_free(monkeypatch, 1)
    assert w.rate == 9 * MB / 60  # the 9th of ten by nearest rank
    assert percentile([], 90) == 0 and percentile([5], 90) == 5 and percentile([1, 2, 3, 4], 90) == 4




def test_a_missing_save_drive_is_reported_once_and_its_return_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    w = writer(tmp_path)
    shutil.rmtree(w.config.save_path)
    w.write(cameras_alive=True, force=True)
    status = read(w)
    assert status["save_drive"]["found"] is False and status["save_drive"]["accepting_clips"] is False
    assert status["save_drive"]["total_bytes"] is None and status["save_drive"]["seconds_until_full"] is None
    w.write(cameras_alive=True, force=True)  # still missing: no second line
    os.mkdir(w.config.save_path)
    w.write(cameras_alive=True, force=True)
    assert read(w)["save_drive"]["found"] is True
    out = capsys.readouterr().out
    assert out.count("WARNING: save drive not found") == 1
    assert out.count("Save drive back") == 1


def test_not_accepting_when_the_write_probe_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    w = writer(tmp_path)
    monkeypatch.setattr(ratrixcam_status, "PROBE_INTERVAL", 0)  # a probe on every write here
    monkeypatch.setattr(ratrixcam_status, "not_writable", lambda _f: "Read-only file system")
    w.write(cameras_alive=True, force=True)
    assert read(w)["save_drive"]["found"] is True and read(w)["save_drive"]["accepting_clips"] is False
    monkeypatch.setattr(ratrixcam_status, "not_writable", lambda _f: None)
    w.write(cameras_alive=True, force=True)
    assert read(w)["save_drive"]["accepting_clips"] is True
    out = capsys.readouterr().out
    assert out.count("WARNING: save drive no longer accepting clips") == 1
    assert out.count("Save drive accepting clips again") == 1


def test_not_accepting_when_free_space_is_under_the_transfer_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    w = writer(tmp_path)
    cam = Path(w.config.temp_path) / "Test_cam"
    cam.mkdir()
    _ = (cam / "a.mp4").write_bytes(b"x" * 2_000_000)  # the largest clip sets the size a transfer needs ten times over
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12 - 19_000_000, 19_000_000))
    assert snap(w, True)["save_drive"]["accepting_clips"] is False
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12 - 20_000_000, 20_000_000))
    assert snap(w, True)["save_drive"]["accepting_clips"] is True


def test_with_no_clip_in_temp_the_assumed_clip_size_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    w = writer(tmp_path)  # one camera, time_slice 60: a clip is write_rate(1) * 60 bytes
    clip = write_rate(1) * 60
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12 - int(clip * 9), int(clip * 9)))
    assert snap(w, True)["save_drive"]["accepting_clips"] is False
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(10**12, 10**12 - int(clip * 11), int(clip * 11)))
    assert snap(w, True)["save_drive"]["accepting_clips"] is True


def test_clips_waiting_excludes_the_open_clip_of_each_running_camera(tmp_path: Path):
    w = writer(tmp_path)  # one camera
    cam = Path(w.config.temp_path) / "Test_cam"
    cam.mkdir()
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        _ = (cam / name).write_bytes(b"x")
    (Path(w.config.temp_path) / "ratrixcam_run_metadata").mkdir()
    _ = (Path(w.config.temp_path) / "ratrixcam_run_metadata" / "not_a_clip.mp4").write_bytes(b"x")  # the metadata folder is not a camera folder
    assert snap(w, True)["temp_drive"]["clips_waiting"] == 2
    assert snap(w, False)["temp_drive"]["clips_waiting"] == 3


def test_writes_are_limited_to_one_per_preview_interval_unless_forced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    w = writer(tmp_path)  # preview_interval 1 s
    replaced: list[str] = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (replaced.append(str(b)), real_replace(a, b)))
    w.write(cameras_alive=True)
    w.write(cameras_alive=True)
    assert len(replaced) == 1
    w.write(cameras_alive=True, state="stopping", force=True)
    assert len(replaced) == 2
    assert read(w)["run"]["state"] == "stopping"


def test_a_state_given_to_a_limited_write_is_kept_for_the_next(tmp_path: Path):
    w = writer(tmp_path)
    w.write(cameras_alive=True, force=True)
    w.write(cameras_alive=True, state="stopping", message="m")  # within the interval: nothing written
    assert read(w)["run"]["state"] == "running"
    w.write(cameras_alive=True, force=True)
    assert read(w)["run"]["state"] == "stopping" and read(w)["run"]["message"] == "m"


def test_a_closed_writer_writes_nothing_and_remove_takes_the_file(tmp_path: Path):
    w = writer(tmp_path)
    w.write(cameras_alive=True, force=True)
    w.close()
    w.path.unlink()
    w.write(cameras_alive=True, force=True)
    assert not w.path.exists()
    w.remove()  # already gone: silent
    assert not w.path.exists()


def test_an_unwritable_temp_folder_is_reported_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    w = writer(tmp_path)

    def no_room(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("builtins.open", no_room)
    w.write(cameras_alive=True, force=True)
    w.write(cameras_alive=True, force=True)
    assert capsys.readouterr().out.count("WARNING: could not write the status file") == 1


def test_the_write_probe_runs_once_per_interval_and_at_once_after_the_drive_was_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    w = writer(tmp_path)
    probes: list[int] = []
    monkeypatch.setattr(ratrixcam_status, "not_writable", lambda _f: (probes.append(1), None)[1])
    _ = snap(w, True)
    _ = snap(w, True)
    assert len(probes) == 1  # within the interval: the first result is reused
    shutil.rmtree(w.config.save_path)
    _ = snap(w, True)  # missing: no probe
    os.mkdir(w.config.save_path)
    _ = snap(w, True)  # back: probed at once
    assert len(probes) == 2


def test_a_drive_back_but_read_only_reports_both_transitions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    w = writer(tmp_path)
    monkeypatch.setattr(ratrixcam_status, "PROBE_INTERVAL", 0)
    shutil.rmtree(w.config.save_path)
    w.write(cameras_alive=True, force=True)  # gone: one line, "not accepting" goes without saying
    gone = capsys.readouterr().out
    assert gone.count("WARNING: save drive not found") == 1 and "accepting" not in gone
    monkeypatch.setattr(ratrixcam_status, "not_writable", lambda _f: "Read-only file system")
    os.mkdir(w.config.save_path)
    w.write(cameras_alive=True, force=True)  # back, but read-only: both lines
    back = capsys.readouterr().out
    assert back.count("Save drive back") == 1
    assert back.count("WARNING: save drive no longer accepting clips") == 1
