"""gui/serve.py: the viewer's read-only server. The viewer folder is served at /, the temp
folder at /temp/ with JSON folder listings, neither root can be escaped, missing files are
answered with 404 without a log line, and the last status file served is offered again at
/last/."""

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

GUI_DIR = Path(__file__).resolve().parents[1]
SERVE = GUI_DIR / "serve.py"

# the smallest valid PNG (1x1 pixel), enough for a still
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478"
    + "9c6360f8cfc00000030101009a9c0e8e0000000049454e44ae426082"
)
LOG_NAME = "terminal_ratrixcam_20260101_120000_0123abcd.log"
Server = tuple[str, Path]  # base url, path of the server's stderr


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: object = s.getsockname()[1]
        assert isinstance(port, int)
        return port


@pytest.fixture(scope="module")
def temp_folder(tmp_path_factory: pytest.TempPathFactory) -> Path:
    temp = tmp_path_factory.mktemp("temp")
    (temp / "status").mkdir()
    _ = (temp / "status" / "cam_rat901_home_status.png").write_bytes(PNG)
    (temp / "ratrixcam_run_metadata" / "manifests").mkdir(parents=True)
    _ = (temp / "ratrixcam_run_metadata" / "camera_config.json").write_text('{"cameras": []}\n')
    _ = (temp / LOG_NAME).write_text("ratrixcam run started\n")
    return temp


@pytest.fixture(scope="module")
def server(temp_folder: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    """A serve.py running on temp_folder, its stderr captured to a file."""
    port = free_port()
    stderr_path = tmp_path_factory.mktemp("log") / "stderr.txt"
    with open(stderr_path, "w") as stderr:
        proc = subprocess.Popen(
            [sys.executable, str(SERVE), str(temp_folder), "--port", str(port)],
            stdout=subprocess.DEVNULL, stderr=stderr,
        )
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/").close()
            break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("serve.py did not start")
    yield base, stderr_path
    proc.terminate()
    _ = proc.wait(timeout=5)


def get(base: str, path: str) -> tuple[int, dict[str, str], bytes]:
    """(status code, lower-cased headers, body) for a GET of path."""
    try:
        with urllib.request.urlopen(base + path) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


# ---------- what is served ----------

def test_viewer_page_at_root(server: Server):
    base, _ = server
    code, headers, body = get(base, "/")
    assert code == 200
    assert headers["content-type"].startswith("text/html")
    assert b"viewer.js" in body


def test_still_served_as_png_without_caching(server: Server):
    base, _ = server
    code, headers, body = get(base, "/temp/status/cam_rat901_home_status.png")
    assert code == 200
    assert headers["content-type"] == "image/png"
    assert headers["cache-control"] == "no-store"
    assert body == PNG


def test_temp_folder_listing_is_json(server: Server):
    base, _ = server
    code, headers, body = get(base, "/temp/")
    assert code == 200
    assert headers["content-type"] == "application/json"
    entries = {e["name"]: e for e in json.loads(body)["entries"]}
    assert entries["status"]["is_dir"] is True
    assert entries[LOG_NAME]["is_dir"] is False
    assert entries[LOG_NAME]["size"] == len("ratrixcam run started\n")
    assert isinstance(entries[LOG_NAME]["mtime"], float)


def test_subfolder_listing_without_trailing_slash(server: Server):
    base, _ = server
    code, _, body = get(base, "/temp/status")
    assert code == 200
    assert [e["name"] for e in json.loads(body)["entries"]] == ["cam_rat901_home_status.png"]


def test_status_file_served_and_remembered(server: Server, temp_folder: Path):
    base, _ = server
    status = {"schema": 1, "run": {"run_id": "r1"}}
    _ = (temp_folder / "ratrixcam_status.json").write_text(json.dumps(status))
    code, headers, body = get(base, "/temp/ratrixcam_status.json")
    assert code == 200
    assert headers["content-type"] == "application/json"
    assert json.loads(body) == status
    code, _, remembered = get(base, "/last/status.json")
    assert code == 200
    assert json.loads(remembered) == status


def test_half_written_status_file_is_served_but_not_remembered(server: Server, temp_folder: Path):
    base, _ = server
    good = {"schema": 1, "run": {"run_id": "good"}}
    _ = (temp_folder / "ratrixcam_status.json").write_text(json.dumps(good))
    assert json.loads(get(base, "/temp/ratrixcam_status.json")[2]) == good
    _ = (temp_folder / "ratrixcam_status.json").write_text('{"schema": 1, "run": {"run_id": "tru')
    code, _, body = get(base, "/temp/ratrixcam_status.json")
    assert code == 200
    assert body.startswith(b'{"schema": 1, "run"')
    assert json.loads(get(base, "/last/status.json")[2]) == good


def test_terminal_log_is_served_as_a_plain_file_and_not_remembered(server: Server, temp_folder: Path):
    base, _ = server
    code, _, body = get(base, "/temp/" + LOG_NAME)  # its content type is the platform's guess for .log
    assert code == 200
    assert body == b"ratrixcam run started\n"
    assert get(base, "/last/log.txt")[0] == 404  # the viewer never reads the log


# ---------- what is refused ----------

@pytest.mark.parametrize("path", [
    "/temp/../pyrightconfig.json",
    "/temp/%2e%2e/pyrightconfig.json",
    "/temp/%2e%2e/%2e%2e/pyrightconfig.json",
    "/../pyrightconfig.json",
    "/%2e%2e/pyrightconfig.json",
    "/temp/status/../../../pyrightconfig.json",
])
def test_parent_folders_cannot_be_reached(server: Server, path: str):
    base, _ = server
    code, _, body = get(base, path)
    assert code == 404
    assert b"typeCheckingMode" not in body


def test_last_only_knows_the_status_file(server: Server):
    base, _ = server
    assert get(base, "/last/other")[0] == 404
    assert get(base, "/last/")[0] == 404


# ---------- missing files are quiet ----------

def test_missing_files_give_404_and_no_log_line(server: Server):
    base, stderr_path = server
    before = stderr_path.read_text()
    assert get(base, "/temp/missing.json")[0] == 404
    assert get(base, "/temp/ratrixcam_run_metadata/manifests/rat901_home_manifest.csv")[0] == 404
    assert get(base, "/temp/status/cam_rat000_face_status.png")[0] == 404
    assert get(base, "/nothing.html")[0] == 404
    time.sleep(0.2)
    assert stderr_path.read_text() == before


def test_requests_are_not_logged_at_all(server: Server):
    base, stderr_path = server
    before = stderr_path.read_text()
    assert get(base, "/temp/")[0] == 200
    assert get(base, "/temp/status/cam_rat901_home_status.png")[0] == 200
    time.sleep(0.2)
    assert stderr_path.read_text() == before
