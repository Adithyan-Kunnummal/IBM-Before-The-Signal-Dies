"""Before the Signal Dies: prototype web app (standard library only).

Run from the repository root:
    python3 app/server.py                  # http://127.0.0.1:8050
    python3 app/server.py --host 0.0.0.0   # listen on all interfaces
"""
import argparse
import json
import mimetypes
import platform
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import Engine  # noqa: E402
import explain  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 64 * 1024

ENGINE = None


class Handler(BaseHTTPRequestHandler):
    server_version = "SignalDies/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    # ---------- helpers ----------
    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._send_json({"error": message}, status)

    def _send_file(self, path):
        try:
            resolved = path.resolve()
            resolved.relative_to(STATIC.resolve())
        except (ValueError, OSError):
            return self._error(HTTPStatus.NOT_FOUND, "Not found")
        if not resolved.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "Not found")
        body = resolved.read_bytes()
        ctype = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("Request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    # ---------- routes ----------
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            return self._send_file(STATIC / "index.html")
        if path.startswith("/static/"):
            return self._send_file(STATIC / path[len("/static/"):])
        if path == "/api/summary":
            return self._send_json({**ENGINE.summary_payload(), "granite": explain.granite_status(),
                                    "server_machine": platform.machine(),
                                    "server_python": platform.python_version()})
        if path == "/api/localities":
            return self._send_json(ENGINE.localities_payload())
        if path == "/api/sites":
            return self._send_json(ENGINE.sites_payload())
        if path.startswith("/api/locality/"):
            sal = path[len("/api/locality/"):]
            loc = ENGINE.by_sal.get(sal)
            if not loc:
                return self._error(HTTPStatus.NOT_FOUND, f"No locality with code {sal}")
            return self._send_json({"locality": loc,
                                    "explanation": explain.explain(loc, len(ENGINE.localities))})
        return self._error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/simulate":
            return self._error(HTTPStatus.NOT_FOUND, "Not found")
        try:
            body = self._read_json()
            hazard = body.get("hazard")
            if hazard is not None:
                hazard = {"lat": float(hazard["lat"]), "lon": float(hazard["lon"]),
                          "radius_km": max(1.0, min(400.0, float(hazard["radius_km"]))),
                          "failure_share": float(hazard.get("failure_share", 100))}
            started = time.perf_counter()
            result = ENGINE.simulate(
                hazard=hazard,
                manual_ids=[str(x) for x in body.get("manual_site_ids", [])][:500],
                coverage_km=body.get("coverage_km", 10),
                units=body.get("units", 0),
                unit_radius_km=body.get("unit_radius_km", 15),
            )
            result["compute_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return self._send_json(result)
        except (KeyError, TypeError, ValueError) as exc:
            return self._error(HTTPStatus.BAD_REQUEST, f"Invalid simulation request: {exc}")


def main():
    global ENGINE
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8050)
    args = parser.parse_args()

    started = time.perf_counter()
    ENGINE = Engine()
    print(f"Loaded {len(ENGINE.localities)} localities and {len(ENGINE.sites)} Optus sites "
          f"in {time.perf_counter() - started:.1f}s", flush=True)
    status = explain.granite_status()
    print("Granite:", f"connected at {status['url']}" if status["connected"]
          else "not configured (set GRANITE_URL to enable)", flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving on http://{args.host}:{args.port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
