import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from client import (
    crawl_headless,
    extract_headless,
    map_headless,
    research_headless,
    search_headless,
    validate_crawl_body,
    validate_extract_body,
    validate_map_body,
    validate_research_body,
    validate_search_body,
)

STATIC_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
)
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

_tasks = {}
_tasks_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    def _file(self, relpath):
        if relpath in ("", "/"):
            relpath = "index.html"
        path = os.path.normpath(os.path.join(STATIC_DIR, relpath))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            CONTENT_TYPES.get(os.path.splitext(path)[1], "application/octet-stream"),
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/research":
            self._research_status(parse_qs(parsed.query))
        else:
            self._file(parsed.path[1:])

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            payload = self._body()
            if parsed.path == "/api/search":
                self._json(search_headless(**validate_search_body(payload)))
            elif parsed.path == "/api/extract":
                self._json(extract_headless(**validate_extract_body(payload)))
            elif parsed.path == "/api/crawl":
                self._json(crawl_headless(**validate_crawl_body(payload)))
            elif parsed.path == "/api/map":
                self._json(map_headless(**validate_map_body(payload)))
            elif parsed.path == "/api/research":
                self._start_research(payload)
            else:
                self._json({"error": "not found"}, 404)
        except (ValueError, RuntimeError) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    def _start_research(self, payload):
        query, params, files_payload, files_meta = validate_research_body(payload)
        task_id = uuid4().hex
        with _tasks_lock:
            _tasks[task_id] = {
                "status": "pending",
                "query": query,
                "model": params["model"],
                "params": params,
                "files_meta": files_meta,
            }
        thread = threading.Thread(
            target=_run_research,
            args=(task_id, query, params, files_payload),
            daemon=True,
        )
        thread.start()
        self._json({"request_id": task_id}, 201)

    def _research_status(self, query):
        task_id = (query.get("id") or [None])[0]
        with _tasks_lock:
            task = _tasks.get(task_id)
            snapshot = dict(task) if task else None
        if not snapshot:
            self._json({"error": "unknown task"}, 404)
        else:
            self._json(snapshot)


def _run_research(task_id, query, params, files_payload):
    def on_progress(progress):
        with _tasks_lock:
            task = _tasks.get(task_id)
            if task:
                task["status"] = "running"
                task["phase"] = progress.get("phase")
                task["progress"] = progress

    try:
        kwargs = {
            k: v for k, v in params.items() if k != "model" and v not in (None, [], {})
        }
        if files_payload:
            kwargs["files"] = files_payload
        result = research_headless(
            query, model=params["model"], on_progress=on_progress, **kwargs
        )
        with _tasks_lock:
            task = _tasks[task_id]
            prog = task.get("progress")
            if isinstance(prog, dict):
                task["progress"] = {k: v for k, v in prog.items() if k != "draft"}
            task.update(status="completed", result=result)
    except Exception as exc:
        with _tasks_lock:
            _tasks[task_id].update(status="failed", error=str(exc))


def run(host="127.0.0.1", port=8000):
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"tavily-headless page: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
