"""The Lab egress gateway program (lab/egress_proxy.py, GOAL 18.6 network rule), run on this machine against local
servers. It refuses non-public destinations, records every connection, keeps queries out of the record. Loopback is
refused by the program, so the forwarding tests name their local server in the owner allow-list, exactly as a local
model server is named. The network half of the rule (no route out of the box) is in tests/test_lab_network_real.py."""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import socket
import threading
from typing import ClassVar

import pytest

from custom_harness.lab import egress_proxy as E


@pytest.mark.parametrize("ip,bad", [
    ("1.1.1.1", False), ("93.184.216.34", False), ("2606:4700:4700::1111", False),
    ("10.0.0.1", True), ("172.16.5.5", True), ("192.168.1.1", True), ("127.0.0.1", True), ("169.254.169.254", True),
    ("100.64.0.1", True), ("0.0.0.0", True), ("224.0.0.1", True), ("192.168.65.254", True), ("255.255.255.255", True),
    ("::1", True), ("fe80::1%eth0", True), ("fd00::1", True), ("::ffff:10.0.0.1", True), ("::ffff:127.0.0.1", True),
    ("2002:0a00:0001::", True), ("64:ff9b::a00:1", True), ("not an ip", True)])
def test_only_public_addresses_pass(ip, bad):
    assert (E.address_problem(ip) is not None) == bad


def _answers(monkeypatch, *ips):
    monkeypatch.setattr(E.socket, "getaddrinfo",
                        lambda h, p, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, p)) for ip in ips])


def test_a_name_with_any_private_answer_is_refused_whole(monkeypatch):
    _answers(monkeypatch, "1.1.1.1", "192.168.1.1")
    with pytest.raises(E.Refused):
        E.choose_address("rebind.example", 443, frozenset())


def test_the_chosen_address_is_a_checked_one(monkeypatch):
    _answers(monkeypatch, "1.1.1.1")
    assert E.choose_address("ok.example", 443, frozenset()) == "1.1.1.1"


def test_the_allow_list_is_an_exact_host_and_port(monkeypatch):
    _answers(monkeypatch, "192.168.65.254")
    allow = E.allowed_pairs("host.docker.internal:11434")
    assert E.choose_address("HOST.docker.internal", 11434, allow) == "192.168.65.254"
    with pytest.raises(E.Refused):
        E.choose_address("host.docker.internal", 11435, allow)
    with pytest.raises(E.Refused):
        E.choose_address("other.internal", 11434, allow)


@pytest.mark.parametrize("target", ["nohost", ":80", "h:", "h:0", "h:70000", "h:8a"])
def test_bad_connect_targets_are_refused(target):
    with pytest.raises(E.Refused):
        E.split_target(target)


def test_connect_target_forms():
    assert E.split_target("example.com:443") == ("example.com", 443)
    assert E.split_target("[2606:4700::1]:443") == ("2606:4700::1", 443)


class _Origin(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, str, bytes, dict]]] = []

    def _reply(self, body: bytes) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        got = self.rfile.read(n)
        _Origin.seen.append((self.command, self.path, got, dict(self.headers)))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._reply(b"origin says hello")

    def do_POST(self) -> None:
        self._reply(b"posted")

    def log_message(self, *a) -> None:
        pass


@pytest.fixture
def world():
    _Origin.seen = []
    origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    oport = origin.server_address[1]
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    out = io.StringIO()
    gw = E.Gateway(("127.0.0.1", 0), frozenset({f"127.0.0.1:{oport}"}), E.Recorder(out))
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    yield gw.server_address[1], oport, out
    gw.shutdown()
    origin.shutdown()
    gw.server_close()
    origin.server_close()


def _ask(port: int, raw: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(raw)
        got = b""
        while True:
            d = s.recv(65536)
            if not d:
                return got
            got += d


def _lines(out: io.StringIO, n: int) -> list[dict]:
    for _ in range(200):
        rows = [json.loads(x) for x in out.getvalue().splitlines()]
        if len(rows) >= n:
            return rows
        threading.Event().wait(0.02)
    raise AssertionError(out.getvalue())


def test_connect_to_the_host_is_refused_and_recorded(world):
    gw, oport, out = world
    closed_port = oport + 1 if oport < 65000 else oport - 1
    got = _ask(gw, f"CONNECT 127.0.0.1:{closed_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert got.startswith(b"HTTP/1.1 403")
    row = _lines(out, 1)[0]
    assert row["status"] == "denied" and row["host"] == "127.0.0.1" and row["seq"] == 1 and "public" in row["reason"]


def test_plain_http_to_the_host_is_refused(world):
    gw, _, out = world
    got = _ask(gw, b"GET http://localhost:9/ HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert got.startswith(b"HTTP/1.1 403") and _lines(out, 1)[0]["status"] == "denied"


def test_https_url_in_a_plain_request_is_refused(world):
    gw, _, out = world
    assert _ask(gw, b"GET https://example.com/ HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert _lines(out, 1)[0]["status"] == "denied"


def test_http_is_forwarded_recorded_and_the_query_stays_out(world):
    gw, oport, out = world
    url = f"http://user:pw@127.0.0.1:{oport}/data/file.csv?key=SECRETVALUE"
    got = _ask(gw, f"GET {url} HTTP/1.1\r\nHost: wrong\r\nProxy-Authorization: x\r\nProxy-Connection: keep\r\n\r\n"
               .encode())
    assert got.startswith(b"HTTP/1.0 200") or got.startswith(b"HTTP/1.1 200")
    assert got.endswith(b"origin says hello")
    method, path, _, headers = _Origin.seen[0]
    assert method == "GET" and path == "/data/file.csv?key=SECRETVALUE"
    assert "Proxy-Authorization" not in headers and "Proxy-Connection" not in headers
    assert headers["Host"] == f"127.0.0.1:{oport}"
    row = _lines(out, 1)[0]
    text = json.dumps(row)
    assert "SECRETVALUE" not in text and "pw" not in text
    assert row["url"] == f"http://127.0.0.1:{oport}/data/file.csv"
    assert row["query_sha256"] == hashlib.sha256(b"key=SECRETVALUE").hexdigest()
    assert row["response_sha256"] == hashlib.sha256(got).hexdigest() and row["down"] == len(got)


def test_a_post_body_arrives_whole(world):
    gw, oport, _ = world
    body = b"x" * 200_000
    head = (f"POST http://127.0.0.1:{oport}/up HTTP/1.1\r\nHost: h\r\nContent-Length: {len(body)}\r\n\r\n").encode()
    assert _ask(gw, head + body).endswith(b"posted")
    assert _Origin.seen[0][2] == body


def test_a_tunnel_carries_bytes_both_ways_and_counts_them(world):
    gw, oport, out = world
    with socket.create_connection(("127.0.0.1", gw), timeout=10) as s:
        s.sendall(f"CONNECT 127.0.0.1:{oport} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        assert s.recv(4096).startswith(b"HTTP/1.1 200")
        s.sendall(b"GET /t HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        got = b""
        while True:
            d = s.recv(65536)
            if not d:
                break
            got += d
    assert got.endswith(b"origin says hello")
    row = _lines(out, 1)[0]
    assert row["status"] == "ok" and row["method"] == "CONNECT" and row["down"] == len(got) and row["up"] > 0
    assert "url" not in row and "response_sha256" not in row  # no path, no content hash: stated in docs/lab.md


def test_lines_are_numbered_without_gaps_under_concurrency(world):
    gw, _, out = world
    threads = [threading.Thread(target=_ask, args=(gw, b"CONNECT 127.0.0.1:9 HTTP/1.1\r\n\r\n")) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r["seq"] for r in _lines(out, 20)) == list(range(1, 21))


def test_the_program_uses_the_standard_library_only():
    import ast
    import sys
    from pathlib import Path
    tree = ast.parse(Path(E.__file__).read_text(encoding="utf-8"))
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert names - {"__future__"} <= set(sys.stdlib_module_names), names
