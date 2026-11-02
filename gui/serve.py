#!/usr/bin/env python3
"""Serve the ratrixcam viewer together with a camera temp folder, read-only, on localhost.

    python3 serve.py <temp folder> [--port N]

The viewer's own files (this folder) are served at /, the temp folder at /temp/. A request
for a folder under /temp/ returns a JSON listing of its entries, which is how the page finds
the run's config file without knowing its name. Nothing is ever written.
Python standard library only.
"""

import argparse
import json
import os
import posixpath
import sys
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

GUI_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_PREFIX = "/temp/"


class ViewerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, temp_dir: str, **kwargs):
        self.temp_dir = temp_dir
        super().__init__(*args, directory=GUI_DIR, **kwargs)

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

    def list_directory(self, path: str):
        """JSON listing for folders under /temp/; the viewer folder keeps the default HTML."""
        if not unquote(urlsplit(self.path).path).startswith(TEMP_PREFIX):
            return super().list_directory(path)
        entries = []
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
        body = json.dumps({"entries": sorted(entries, key=lambda e: e["name"])}).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return None

    def end_headers(self):
        # stills and the status file change every second: never let the browser cache anything
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args):
        # one line per still per second would drown the terminal; errors still go through log_error
        pass

    def log_error(self, format: str, *args):
        # a missing file is normal here: the viewer polls for the status file and for manifests
        # that the cameras may not have written yet
        if args and args[0] == HTTPStatus.NOT_FOUND:
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the ratrixcam viewer for one camera temp folder.")
    parser.add_argument("temp_folder", help="the running cameras' temp folder (temp_path in the config)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    temp_dir = os.path.abspath(os.path.expanduser(args.temp_folder))
    if not os.path.isdir(temp_dir):
        print(f"ERROR: temp folder not found: {temp_dir}")
        return 1
    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(ViewerHandler, temp_dir=temp_dir))
    print(f"ratrixcam viewer: http://127.0.0.1:{args.port}/   (temp folder {temp_dir}; Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
