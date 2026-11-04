#!/usr/bin/env python3
"""Serve the camera config editor on localhost and do its file work.

    python3 config_editor.py [--port N]

The page (config_editor.html) is a form whose fields are drop-down menus fed from a list of
legal values kept on this machine in local_options.json (next to this script, not in git).
The server reads and writes that file, reads a config file for editing and writes the one
the form produces, after checking it with the camera code's own Config class when that can
be imported. It never touches a temp folder. Python standard library only.
"""

import argparse
import json
import os
import socket
import sys
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast, final, override
from urllib.parse import parse_qs, unquote, urlsplit

GUI_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(GUI_DIR)
OPTIONS_FILE = os.path.join(GUI_DIR, "local_options.json")
POSITIONS = ["A", "B", "C", "D", "E", "F", "G", "H"]

# the camera code's own validation, when available (it needs pydantic); the fallback below
# checks the same things more loosely
sys.path.insert(0, REPO_DIR)
try:
    from ratrixcam_utils import Config as CameraConfigClass
except Exception:  # noqa: BLE001  (any import problem: pydantic missing, repo moved)
    CameraConfigClass = None

# legal values shipped with a fresh options file; the user extends them with "Add new...".
# The per-camera override lists (camera_*) start empty: a camera uses the run's defaults
# unless the user deliberately adds a value to choose from. Resolutions are one value
# ("640x480"), written to the file as width and height.
PLACEHOLDER_OPTIONS: dict[str, list[Any]] = {
    "study_label": ["Test"],
    "subject_prefix": ["rat"],
    "subjID": ["000", "999"],
    "view": ["face", "buddy", "home"],
    "time_slice": [60, 600],
    "save_path": ["/Volumes/data/"],
    "temp_path": [os.path.expanduser("~/Desktop/temp/")],
    "default_crf": [25],
    "default_fps": [30],
    "default_resolution": ["640x480"],
    "default_cam_exposure": [-8.0],
    "preview_interval": [1],
    "camera_crf": [],
    "camera_fps": [],
    "camera_resolution": [],
    "camera_exposure": [],
}
# what the advanced fields are set to on a fresh form
PRESETS: dict[str, Any] = {
    "default_crf": 25,
    "default_fps": 30,
    "default_resolution": "640x480",
    "default_cam_exposure": -8.0,
    "preview_interval": 1,
}
# the file's key order: run-level fields, the cameras, then the advanced defaults at the end
FILE_KEY_ORDER = ["save_path", "temp_path", "study_label", "subject_prefix", "time_slice", "cameras",
                  "default_crf", "default_fps", "default_width", "default_height", "default_cam_exposure", "preview_interval"]
CAMERA_KEY_ORDER = ["subjID", "view", "position", "crf", "fps", "width", "height", "exposure"]


def machine_id() -> str:
    """The first network interface's hardware address, or the host name when the platform
    gives none (uuid.getnode then returns a random number with its multicast bit set)."""
    node = uuid.getnode()
    if node & (1 << 40):
        return "host:" + socket.gethostname()
    return ":".join(f"{(node >> shift) & 0xFF:02x}" for shift in range(40, -1, -8))


def load_options() -> dict[str, Any]:
    """The options file, created with placeholders on first use; a list or preset this
    version knows and the file lacks is added, so an older file keeps working."""
    data: dict[str, Any]
    try:
        with open(OPTIONS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {
            "machine_id": machine_id(),
            "machine_name": socket.gethostname().split(".")[0],
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    options = data.setdefault("options", {})
    for key, values in PLACEHOLDER_OPTIONS.items():
        options.setdefault(key, list(values))
    for key in [k for k in options if k not in PLACEHOLDER_OPTIONS]:
        del options[key]  # a list an earlier version of the editor kept
    for key in ("save_path", "temp_path"):
        options[key] = [os.path.expanduser(p) if isinstance(p, str) else p for p in options[key]]
    presets = data.setdefault("presets", {})
    for key, value in PRESETS.items():
        presets.setdefault(key, value)
    write_json(OPTIONS_FILE, data)
    return data


def config_text(data: dict[str, Any]) -> str:
    """The config as written: keys in FILE_KEY_ORDER, one camera per line, like the
    hand-written configs."""
    lines = ["{"]
    keys = [k for k in FILE_KEY_ORDER if k in data] + [k for k in data if k not in FILE_KEY_ORDER]
    for i, key in enumerate(keys):
        comma = "," if i < len(keys) - 1 else ""
        if key == "cameras":
            cams: list[str] = []
            for cam in cast(list[dict[str, Any]], data["cameras"]):
                ordered = {k: cam[k] for k in CAMERA_KEY_ORDER if k in cam}
                ordered.update({k: v for k, v in cam.items() if k not in ordered})
                cams.append("    " + json.dumps(ordered, separators=(", ", ": ")))
            lines.append('  "cameras": [')
            lines.append(",\n".join(cams))
            lines.append("  ]" + comma)
        else:
            lines.append(f"  {json.dumps(key)}: {json.dumps(data[key])}{comma}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def write_json(path: str, data: Any) -> None:
    """Write pretty JSON through a temporary file in the same folder, then rename, so a
    reader never sees a half-written file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        _ = f.write("\n")
    os.replace(tmp, path)


def expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def check_config(data: object) -> list[str]:
    """Problems that would stop the camera program from loading this config, as messages;
    empty when it is fine."""
    if CameraConfigClass is not None:
        try:
            _ = CameraConfigClass.model_validate(data)
        except Exception as e:  # noqa: BLE001  (pydantic's ValidationError, or a TypeError)
            return [line.strip() for line in str(e).splitlines() if line.strip()]
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["the config must be a JSON object"]
    fields = cast(dict[str, object], data)
    for key in ("cameras", "study_label", "subject_prefix", "default_fps", "default_width", "default_height",
                "default_cam_exposure", "time_slice", "preview_interval", "save_path", "temp_path"):
        if key not in fields:
            problems.append(f"missing field: {key}")
    cameras_field = fields.get("cameras")
    cameras: list[object]
    if isinstance(cameras_field, list):
        cameras = cast(list[object], cameras_field)
    else:
        problems.append("cameras must be a list")
        cameras = []
    seen_names: set[tuple[str, str]] = set()
    seen_positions: set[str] = set()
    for idx, cam_item in enumerate(cameras, start=1):
        if not isinstance(cam_item, dict):
            problems.append(f"camera {idx} is not an object")
            continue
        cam = cast(dict[str, object], cam_item)
        key = (str(cam.get("subjID")), str(cam.get("view")))
        if key in seen_names:
            problems.append(f"camera {idx} repeats subjID {key[0]!r} and view {key[1]!r}")
        seen_names.add(key)
        pos = cam.get("position")
        if pos not in POSITIONS:
            problems.append(f"camera {idx} position must be one of {''.join(POSITIONS)}")
        elif pos in seen_positions:
            problems.append(f"camera {idx} repeats position {pos}")
        if isinstance(pos, str):
            seen_positions.add(pos)
    if not cameras:
        problems.append("at least one camera is needed")
    return problems


@final
class EditorHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=GUI_DIR, **kwargs)

    # ----- helpers -----

    def send_json(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _ = self.wfile.write(body)

    def read_body(self) -> object:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"null")

    def query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    @override
    def end_headers(self) -> None:
        # the page files change between sessions: never let the browser cache them
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass

    # ----- routes -----

    @override
    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path == "/":
            self.path = "/config_editor.html"
        if path == "/api/options":
            data = load_options()
            data["machine_id_now"] = machine_id()
            data["machine_mismatch"] = data.get("machine_id") != data["machine_id_now"]
            data["options_file"] = OPTIONS_FILE
            self.send_json(data)
            return
        if path == "/api/config":
            target = expand(self.query().get("path", ""))
            if not os.path.isfile(target):
                self.send_json({"path": target, "exists": False})
                return
            try:
                with open(target) as f:
                    text = f.read()
                data: object = json.loads(text)
            except (OSError, ValueError) as e:
                self.send_json({"path": target, "exists": True, "error": str(e)})
                return
            self.send_json({"path": target, "exists": True, "config": data, "problems": check_config(data)})
            return
        super().do_GET()

    def do_PUT(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path == "/api/options":
            if self.query().get("reset") == "1":
                current = load_options()
                fresh = {
                    "machine_id": machine_id(),
                    "machine_name": current.get("machine_name", ""),
                    "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "options": {k: list(v) for k, v in PLACEHOLDER_OPTIONS.items()},
                    "presets": dict(PRESETS),
                }
                write_json(OPTIONS_FILE, fresh)
                self.send_json({"ok": True})
                return
            body = self.read_body()
            if not isinstance(body, dict):
                self.send_json({"error": "options must be an object"}, HTTPStatus.BAD_REQUEST)
                return
            data = cast(dict[str, object], body)
            if not isinstance(data.get("options"), dict):
                self.send_json({"error": "options must be an object"}, HTTPStatus.BAD_REQUEST)
                return
            data["machine_id"] = machine_id()  # saving from this machine binds the file to it
            for key in ("machine_id_now", "machine_mismatch", "options_file"):
                _ = data.pop(key, None)
            write_json(OPTIONS_FILE, data)
            self.send_json({"ok": True})
            return
        if path == "/api/config":
            target = expand(self.query().get("path", ""))
            body = self.read_body()
            problems = check_config(body)
            config = cast(dict[str, object], body) if isinstance(body, dict) else None
            if config is not None:
                # the camera code does not expand ~: paths go into the file absolute
                for key in ("temp_path", "save_path"):
                    value = config.get(key)
                    if isinstance(value, str) and value.startswith("~"):
                        config[key] = os.path.expanduser(value)
            if not target.lower().endswith(".json"):
                problems.append("the file name must end in .json")
            if config is not None:
                folders = {expand(str(config[k])) for k in ("temp_path", "save_path") if config.get(k)}
                if os.path.dirname(target) in folders:
                    problems.append("the config must not be saved inside the temp or save folder")
            if problems or config is None:
                self.send_json({"ok": False, "problems": problems}, HTTPStatus.BAD_REQUEST)
                return
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                tmp = target + ".tmp"
                with open(tmp, "w") as f:
                    _ = f.write(config_text(config))
                os.replace(tmp, target)
            except OSError as e:
                self.send_json({"ok": False, "problems": [f"could not write {target}: {e.strerror or e}"]}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"ok": True, "path": target})
            return
        self.send_error(HTTPStatus.NOT_FOUND)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the ratrixcam camera config editor.")
    _ = parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    port = int(cast(int, args.port))
    _ = load_options()
    server = ThreadingHTTPServer(("127.0.0.1", port), EditorHandler)
    print(f"ratrixcam config editor: http://127.0.0.1:{port}/   (options in {OPTIONS_FILE}; Ctrl-C to stop)")
    if CameraConfigClass is None:
        print("note: the camera code's Config class could not be imported; configs are checked loosely")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
