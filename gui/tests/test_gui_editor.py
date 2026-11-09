"""gui/config_editor.py: the config editor's server. It keeps the legal values in the file
named by --options, reads a config for editing, and writes the one the form produces only
after the camera code's own checks pass, through a temporary file and a rename, into a
folder that exists, never inside the temp or save folder."""

import http.client
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

GUI_DIR = Path(__file__).resolve().parents[1]
EDITOR = GUI_DIR / "config_editor.py"
Server = tuple[str, Path]  # base url, options file


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = cast(object, s.getsockname()[1])
        assert isinstance(port, int)
        return port


def open_url(request: str | urllib.request.Request) -> http.client.HTTPResponse:
    return cast(http.client.HTTPResponse, urllib.request.urlopen(request))


def decode(body: bytes) -> dict[str, object]:
    return cast(dict[str, object], json.loads(body or b"null"))


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    options_file = tmp_path_factory.mktemp("options") / "local_options.json"
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, str(EDITOR), "--port", str(port), "--options", str(options_file), "--no-browser"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            open_url(base + "/").close()
            break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("config_editor.py did not start")
    yield base, options_file
    proc.terminate()
    _ = proc.wait(timeout=5)


def call(base: str, method: str, path: str, body: object = None) -> tuple[int, dict[str, object]]:
    """(status code, decoded JSON body) for a request to the editor's API."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, method=method, data=data, headers={"Content-Type": "application/json"})
    try:
        with open_url(req) as r:
            return r.status, decode(r.read())
    except urllib.error.HTTPError as e:
        return e.code, decode(e.read())


def good_config(tmp_path: Path) -> dict[str, object]:
    return {
        "machine_name": "Test Mac",
        "save_path": str(tmp_path / "save") + "/",
        "temp_path": str(tmp_path / "temp") + "/",
        "study_label": "Test",
        "subject_prefix": "subj",
        "time_slice": 60,
        "cameras": [
            {"subjID": "000", "view": "view1", "position": "A"},
            {"subjID": "999", "view": "view1", "position": "B", "width": 640, "height": 480},
        ],
        "default_crf": 25,
        "default_fps": 30,
        "default_width": 640,
        "default_height": 480,
        "default_cam_exposure": 0,
        "preview_interval": 1,
    }


def problems(reply: dict[str, object]) -> str:
    found = reply.get("problems")
    assert isinstance(found, list)
    return "\n".join(str(p) for p in cast(list[object], found))


# ---------- writing ----------

def test_good_config_is_written_through_a_temporary_file(server: Server, tmp_path: Path):
    base, _ = server
    target = tmp_path / "camera_config.json"
    code, reply = call(base, "PUT", f"/api/config?path={target}", good_config(tmp_path))
    assert code == 200, reply
    assert reply == {"ok": True, "path": str(target)}
    assert target.is_file()
    assert not (tmp_path / "camera_config.json.tmp").exists(), "the temporary file must be renamed away"
    text = target.read_text()
    assert json.loads(text) == good_config(tmp_path)
    lines = text.splitlines()
    assert lines[1].startswith('  "machine_name"'), "run-level fields first"
    assert lines[-2].startswith('  "preview_interval"'), "advanced defaults last"
    camera_lines = [l for l in lines if l.startswith('    {"subjID"')]
    assert len(camera_lines) == 2, "one camera per line"


def test_tilde_in_paths_is_expanded_in_the_file(server: Server, tmp_path: Path):
    base, _ = server
    config = good_config(tmp_path)
    config["temp_path"] = "~/Desktop/temp/"
    target = tmp_path / "cfg.json"
    code, _ = call(base, "PUT", f"/api/config?path={target}", config)
    assert code == 200
    written = decode(target.read_bytes())
    assert written["temp_path"] == str(Path.home() / "Desktop" / "temp") + "/"


def test_name_not_ending_in_json_is_refused(server: Server, tmp_path: Path):
    base, _ = server
    target = tmp_path / "camera_config.txt"
    code, reply = call(base, "PUT", f"/api/config?path={target}", good_config(tmp_path))
    assert code == 400
    assert "must end in .json" in problems(reply)
    assert not target.exists()


@pytest.mark.parametrize("folder", ["temp", "save"])
def test_location_inside_the_temp_or_save_folder_is_refused(server: Server, tmp_path: Path, folder: str):
    base, _ = server
    (tmp_path / folder).mkdir()
    target = tmp_path / folder / "camera_config.json"
    code, reply = call(base, "PUT", f"/api/config?path={target}", good_config(tmp_path))
    assert code == 400
    assert "inside the temp or save folder" in problems(reply)
    assert not target.exists()


def test_missing_folder_is_refused_not_created(server: Server, tmp_path: Path):
    base, _ = server
    target = tmp_path / "typo" / "camera_config.json"
    code, reply = call(base, "PUT", f"/api/config?path={target}", good_config(tmp_path))
    assert code == 400
    assert "does not exist" in problems(reply)
    assert not (tmp_path / "typo").exists()


def test_duplicate_camera_is_refused_with_both_reasons(server: Server, tmp_path: Path):
    base, _ = server
    config = good_config(tmp_path)
    cameras = cast(list[dict[str, object]], config["cameras"])
    cameras.append(dict(cameras[0]))
    target = tmp_path / "dup.json"
    code, reply = call(base, "PUT", f"/api/config?path={target}", config)
    assert code == 400
    text = problems(reply)
    assert "repeats subjID '000' and view 'view1'" in text
    assert "repeats position A" in text
    assert not target.exists()


def test_missing_field_is_refused(server: Server, tmp_path: Path):
    base, _ = server
    config = good_config(tmp_path)
    del config["time_slice"]
    code, reply = call(base, "PUT", f"/api/config?path={tmp_path / 'x.json'}", config)
    assert code == 400
    assert "time_slice" in problems(reply)


def test_non_object_is_refused(server: Server, tmp_path: Path):
    base, _ = server
    code, reply = call(base, "PUT", f"/api/config?path={tmp_path / 'x.json'}", [1, 2])
    assert code == 400
    assert problems(reply)


# ---------- reading ----------

def test_existing_config_is_read_back_with_its_problems(server: Server, tmp_path: Path):
    base, _ = server
    config = good_config(tmp_path)
    cameras = cast(list[dict[str, object]], config["cameras"])
    cameras.append(dict(cameras[1]))
    target = tmp_path / "read.json"
    _ = target.write_text(json.dumps(config))
    code, reply = call(base, "GET", f"/api/config?path={target}")
    assert code == 200
    assert reply["exists"] is True
    assert reply["config"] == config
    assert "repeats position B" in problems(reply)


def test_missing_config_reports_absence(server: Server, tmp_path: Path):
    base, _ = server
    code, reply = call(base, "GET", f"/api/config?path={tmp_path / 'none.json'}")
    assert code == 200
    assert reply["exists"] is False


# ---------- legal values ----------

def test_options_file_is_created_with_placeholders(server: Server):
    base, options_file = server
    code, reply = call(base, "GET", "/api/options")
    assert code == 200
    assert options_file.is_file()
    options = cast(dict[str, list[object]], reply["options"])
    assert options["time_slice"] == [60, 600]
    assert options["camera_crf"] == []
    assert len(options["machine_name"]) == 1
    assert reply["machine_mismatch"] is False
    assert "machine_name" not in reply or reply.get("machine_name") is None


def test_options_round_trip_and_reset(server: Server):
    base, _ = server
    _, reply = call(base, "GET", "/api/options")
    options = cast(dict[str, list[object]], reply["options"])
    options["view"].append("tail")
    code, _ = call(base, "PUT", "/api/options", reply)
    assert code == 200

    def views() -> list[object]:
        return cast(dict[str, list[object]], call(base, "GET", "/api/options")[1]["options"])["view"]

    assert "tail" in views()
    code, _ = call(base, "PUT", "/api/options?reset=1", {})
    assert code == 200
    assert "tail" not in views()


def test_malformed_options_are_refused(server: Server):
    base, _ = server
    assert call(base, "PUT", "/api/options", {"options": 3})[0] == 400
    assert call(base, "PUT", "/api/options", [])[0] == 400
