#!/usr/bin/env python3
"""Serve the ratrixcam viewer together with a camera temp folder, read-only, on localhost.

    python3 serve.py <temp folder> [--port N]

The viewer's own files (this folder) are served at /, the temp folder at /temp/. A request
for a folder under /temp/ returns a JSON listing of its entries, which is how the page finds
the run's config file without knowing its name. Nothing is ever written.

The status file is remembered as last served and offered again at /last/status.json: the
camera program deletes the temp folder at the end of a run, and the page uses it to
describe the run that just ended. The terminal log is a human record; the viewer reads
only the status file and the stills.
Python standard library only.
"""

import argparse
import json
import os
import posixpath
import sys
import threading
import time
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast, final, override
from urllib.parse import unquote, urlsplit

GUI_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(GUI_DIR))
# imported under a lowercase alias: the type checker forbids redefining an uppercase name in the fallback
try:
    from ratrixcam_naming import STATUS_FILENAME as shared_status_filename
except ImportError:  # the viewer folder copied on its own
    shared_status_filename = "ratrixcam_status.json"
STATUS_FILENAME = shared_status_filename
TEMP_PREFIX = "/temp/"
LAST_PREFIX = "/last/"

# what was last read from the temp folder, by key: "status.json" (bytes, valid JSON only);
# shared by all request threads, whole values swapped atomically
last_served: dict[str, bytes] = {}
LAST_TYPES = {"status.json": "application/json"}


@final
class ViewerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, temp_dir: str, **kwargs: Any):  # pyright: ignore[reportExplicitAny, reportAny]  # the base class's own untyped arguments
        self.temp_dir: str = temp_dir
        super().__init__(*args, directory=GUI_DIR, **kwargs)  # pyright: ignore[reportAny]

    @override
    def translate_path(self, path: str) -> str:
        """Map /temp/... onto the temp folder and everything else onto the viewer folder,
        dropping any '..' components so neither root can be escaped."""
        path = unquote(urlsplit(path).path)
        if path == TEMP_PREFIX.rstrip("/"):
            path = TEMP_PREFIX
        if path.startswith(TEMP_PREFIX):
            root, rest = self.temp_dir, path[len(TEMP_PREFIX):]
        else:
            root, rest = GUI_DIR, path
        rest = posixpath.normpath("/" + rest)
        parts = [p for p in rest.split("/") if p and p not in (".", "..")]
        full = os.path.join(root, *parts)
        # a request for a folder must keep its trailing slash, or the base class redirects forever
        if path.endswith("/") and os.path.isdir(full):
            full += "/"
        return full

    @override
    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path.startswith(LAST_PREFIX):
            key = path[len(LAST_PREFIX):]
            body = last_served.get(key)
            if body is None or key not in LAST_TYPES:
                self.send_error(HTTPStatus.NOT_FOUND, "Nothing remembered")
                return
            self.send_bytes(body, LAST_TYPES[key])
            return
        if path.startswith(TEMP_PREFIX):
            if path[len(TEMP_PREFIX):] == STATUS_FILENAME:
                key = "status.json"
                try:
                    with open(self.translate_path(self.path), "rb") as f:
                        body = f.read()
                except OSError:
                    self.send_error(HTTPStatus.NOT_FOUND, "File not found")
                    return
                try:
                    json.loads(body)
                except ValueError:
                    pass  # half-written: served as is (the page keeps its previous copy), not remembered
                else:
                    last_served[key] = body
                self.send_bytes(body, LAST_TYPES[key])
                return
        super().do_GET()

    def send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _ = self.wfile.write(body)

    @override
    def list_directory(self, path: "str | os.PathLike[str]"):
        """JSON listing for folders under /temp/; the viewer folder keeps the default HTML."""
        if not unquote(urlsplit(self.path).path).startswith(TEMP_PREFIX):
            return super().list_directory(path)
        path = os.fspath(path)
        entries: list[dict[str, object]] = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    entries.append(
                        {"name": entry.name, "is_dir": entry.is_dir(), "size": st.st_size, "mtime": st.st_mtime}
                    )
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "No permission to list directory")
            return None
        entries.sort(key=lambda e: str(e["name"]))
        body = json.dumps({"entries": entries}).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _ = self.wfile.write(body)
        return None

    @override
    def end_headers(self) -> None:
        # stills and the status file change every second: never let the browser cache anything
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    @override
    def log_message(self, format: str, *args: object) -> None:
        # one line per still per second would drown the terminal; errors still go through log_error
        pass

    @override
    def log_error(self, format: str, *args: object) -> None:
        # a missing file is normal here: the viewer polls for the status file and the stills
        # before the camera program has written them
        if args and args[0] == HTTPStatus.NOT_FOUND:
            return
        _ = sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def exit_with_parent(server: ThreadingHTTPServer) -> None:
    """Stop serving once the process that started this one is gone: the camera program
    runs the server in its own session, so a forced exit of the program would otherwise
    leave it holding its port and showing a run that has ended."""
    parent = os.getppid()
    while os.getppid() == parent:
        time.sleep(2)
    server.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the ratrixcam viewer for one camera temp folder.")
    _ = parser.add_argument("temp_folder", help="the running cameras' temp folder (temp_path in the config)")
    _ = parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    temp_dir = os.path.abspath(os.path.expanduser(cast(str, args.temp_folder)))
    port = cast(int, args.port)
    if not os.path.isdir(temp_dir):
        print(f"ERROR: temp folder not found: {temp_dir}")
        return 1
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(ViewerHandler, temp_dir=temp_dir))
    print(f"ratrixcam viewer: http://127.0.0.1:{port}/   (temp folder {temp_dir}; Ctrl-C to stop)")
    threading.Thread(target=exit_with_parent, args=(server,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
