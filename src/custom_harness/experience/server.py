"""`chh exp ui`: the local web server of the UI (GOAL 18.7). Standard library only. docs/ui.md.

It binds to 127.0.0.1 and nothing else, and it calls the `Experience` class and nothing else: `ROUTES` is a closed list,
so no route reaches the store, the Kernel or held-out data, and none takes a human approval (`approve` only points to
the step, as it does on the command line).

Three checks stand in front of every route, because any web page the browser has open can send requests to localhost:
  - the Host header must be this server, which stops DNS rebinding;
  - an Origin, when a browser sends one, must be this server;
  - anything that changes state needs the token baked into the page. Another site can neither read the page nor set
    the header.

Differences from GOAL 18.7, stated in docs/ui.md: plain HTTP on the loopback interface, because the standard library
cannot make a certificate, and server-sent events instead of WebSocket, because it has no WebSocket.

`Experience` and its store stay on one worker thread, so several browser tabs cannot interleave calls.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from . import schemas as M
from .api import Refused

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
UI_DIST = Path(__file__).resolve().parents[3] / "ui" / "dist"
MAX_BODY = 1 << 20
LIVE_EVERY_S = 1.5
# name -> the query parameters of a read, which writes nothing and spends nothing
READS: dict[str, tuple[str, ...]] = {
    "meta": (), "whoami": (), "waiting": (), "cases": (), "sessions": (), "frozen": (), "trust": (), "record": ("query",),
    "resolve": ("target",), "lab": ("session_id",), "lab_log": ("session_id",), "rehearsal_details": ("promotion_id",),
    "approve": ("item_id",)}
# name -> what a POST body holds. `case` and `technical` are POST because opening a case counts as a look.
ACTS: dict[str, type[BaseModel] | tuple[str, ...]] = {
    "case": ("case_id",), "technical": ("case_id",), "set_researcher": ("name",), "rehearse": M.RehearseIn,
    "promote_preview": M.PromoteIn, "promote": M.PromoteIn, "steer": M.SteerIn, "explore": M.ExploreIn,
    "share": M.ShareIn, "paper": M.PaperIn}
ROUTES = frozenset(READS) | frozenset(ACTS)


class ServerRefused(RuntimeError):
    pass


def _dump(out: Any) -> Any:
    return out.model_dump(mode="json") if isinstance(out, BaseModel) else out


class App:
    """The routes, with no HTTP in them, so a test can call them directly."""

    def __init__(self, make: Callable[[], Any], dist: Path = UI_DIST):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chh-exp")
        self.x: Any = self.pool.submit(make).result()
        self.dist = dist
        self.token = secrets.token_urlsafe(24)
        self.closing = False

    def on_worker(self, fn: Callable[[], Any]) -> Any:
        return self.pool.submit(fn).result()

    def read(self, name: str, query: dict[str, str]) -> Any:
        params = READS[name]
        missing = [p for p in params if p not in query and p != "query"]
        if missing:
            raise ServerRefused(f"{name} needs {missing}")
        args = [query.get(p) for p in params]
        return _dump(self.on_worker(lambda: getattr(self.x, name)(*args)))

    def act(self, name: str, body: dict[str, Any]) -> Any:
        shape = ACTS[name]
        if isinstance(shape, tuple):
            if set(body) != set(shape):
                raise ServerRefused(f"{name} takes {list(shape)}")
            args: list[Any] = [body[p] for p in shape]
        else:
            args = [shape.model_validate(body)]
        return _dump(self.on_worker(lambda: getattr(self.x, name)(*args)))

    def page(self) -> bytes:
        html = (self.dist / "index.html").read_text(encoding="utf-8")
        return html.replace("__CHH_TOKEN__", self.token).encode("utf-8")

    def asset(self, path: str) -> Path | None:
        """A built file, or None. Nothing outside the build folder can be named."""
        root = self.dist.resolve()
        want = (root / path.lstrip("/")).resolve()
        return want if want.is_relative_to(root) and want.is_file() else None


class Handler(BaseHTTPRequestHandler):
    server_version = "chh"
    sys_version = ""
    app: App
    port: int

    def log_message(self, format: str, *args: Any) -> None:
        pass  # request lines hold case ids and links: they stay out of the terminal's scrollback

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, doc: Any) -> None:
        self._send(code, json.dumps(doc, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _refuse(self, code: int, kind: str, message: str) -> None:
        self._json(code, {"code": kind, "message": message})

    def _same_site(self) -> bool:
        here = {f"{HOST}:{self.port}", f"localhost:{self.port}"}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in here and (origin is None or origin in {f"http://{h}" for h in here})

    def _guarded(self, fn: Callable[[], Any]) -> None:
        try:
            self._json(200, fn())
        except Refused as e:
            self._refuse(409, e.code, str(e))
        except (ServerRefused, ValidationError, json.JSONDecodeError) as e:
            detail = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()) \
                if isinstance(e, ValidationError) else str(e)
            self._refuse(400, "BadRequest", detail)
        except Exception:  # noqa: BLE001  a browser gets no traceback; the page says the machine broke
            self._refuse(500, "Broken", "Something broke on this machine. Nothing was decided. Run `chh exp doctor`.")

    def do_GET(self) -> None:  # noqa: N802
        if not self._same_site():
            return self._refuse(403, "NotThisSite", "This server answers its own page only.")
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/api/live":
            return self._live(dict(urllib.parse.parse_qsl(url.query)))
        if url.path.startswith("/api/"):
            name = url.path.removeprefix("/api/")
            if name not in READS:
                return self._refuse(404, "NoRoute", f"no read named {name!r}")
            query = dict(urllib.parse.parse_qsl(url.query))
            return self._guarded(lambda: self.app.read(name, query))
        return self._static(url.path)

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length") or 0)
        if size > MAX_BODY:
            self.close_connection = True
            return self._refuse(413, "TooLarge", "That request is too large.")
        raw = self.rfile.read(size)  # read first: a refusal over an unread body is lost as a reset on Windows
        if not self._same_site() or not secrets.compare_digest(self.headers.get("X-CHH-Token", ""), self.app.token):
            return self._refuse(403, "NotThisSite", "This server takes changes from its own page only.")
        name = urllib.parse.urlsplit(self.path).path.removeprefix("/api/")
        if name not in ACTS:
            return self._refuse(404, "NoRoute", f"no action named {name!r}")
        return self._guarded(lambda: self.app.act(name, json.loads(raw or b"{}")))

    def _static(self, path: str) -> None:
        if not (self.app.dist / "index.html").is_file():
            return self._refuse(503, "NotBuilt", "The UI is not built. Run scripts/install.ps1.")
        want = self.app.asset(path) if path != "/" else None
        if want is None or want.name == "index.html":  # one page: the app routes on the hash
            return self._send(200, self.app.page(), "text/html; charset=utf-8")
        ctype = mimetypes.guess_type(want.name)[0] or "application/octet-stream"
        return self._send(200, want.read_bytes(), ctype)

    def _live(self, query: dict[str, str]) -> None:
        """Server-sent events: one `lab` event whenever the session's Story grew. It names no content."""
        sid = query.get("session_id")
        if not sid:
            return self._refuse(400, "BadRequest", "live needs session_id")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seen = -1
        try:
            while not self.app.closing:
                now = len(self.app.read("lab_log", {"session_id": sid})["lines"])
                self.wfile.write(b"event: lab\ndata: {}\n\n" if now != seen else b": still here\n\n")
                self.wfile.flush()
                seen = now
                time.sleep(LIVE_EVERY_S)
        except (OSError, Refused):
            pass  # the tab closed, or the session is gone
        return None


def make_server(make: Callable[[], Any], port: int = DEFAULT_PORT, *, dist: Path = UI_DIST) -> ThreadingHTTPServer:
    """A server on the loopback interface. `port` 0 picks a free port. There is no parameter for the address."""
    app = App(make, dist)
    handler = type("BoundHandler", (Handler,), {"app": app, "port": port})
    srv = ThreadingHTTPServer((HOST, port), handler)
    srv.daemon_threads = True
    handler.port = srv.server_address[1]  # type: ignore[attr-defined]
    return srv


def serve(make: Callable[[], Any], port: int = DEFAULT_PORT) -> None:
    srv = make_server(make, port)
    print(f"The UI is at http://{HOST}:{srv.server_address[1]}/  Press Ctrl+C to stop.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.RequestHandlerClass.app.closing = True  # type: ignore[attr-defined]
        srv.server_close()
