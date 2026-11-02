"""The Lab's egress gateway program (GOAL 18.6 "Lab network rule"; docs/lab.md section 11). Standard library only: this
exact source is what `lab/egress.py` runs in the gateway container, with `python -I -c`.

A lab box has one network interface, on an internal Docker network with no default route. The only other thing on
that network is this proxy. So a program in the box that ignores the proxy settings reaches nothing, and the rule is
enforced by the network, not by this program being asked nicely. This program adds three things:

  - it refuses every destination that resolves to a non-public address (private, loopback, link-local, carrier NAT,
    reserved, multicast), so the LAN and the host stay out of reach. It connects to the exact address it checked, so a
    name that changes its answer between the check and the connection gains nothing;
  - it prints one JSON line per connection on stdout, which the host collects into the discovery trace;
  - exact `host:port` pairs the owner configured (a local model server) are let through by name (CHH_EGRESS_ALLOW).

What a line holds. HTTPS: host, port, address, byte counts, times. The tunnel is end-to-end encrypted and this proxy
does not open it, so no URL path and no content hash exist for HTTPS. Plain HTTP: also the method, the URL without its
query (the query goes by hash: it may hold a key) and a sha256 of the response. Lines carry a sequence number, so a
lost line shows up as a gap.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import select
import socket
import socketserver
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit

PORT = 3128
MAX_HEADER_BYTES = 64 << 10
MAX_TEXT = 1000  # characters of a host or URL path kept in a record
CONNECT_TIMEOUT_S = 20.0
IDLE_TIMEOUT_S = 300.0  # UNCALIBRATED (owner decision, GOAL 13.8): a tunnel silent this long is closed
CHUNK = 1 << 16
ALLOW_ENV = "CHH_EGRESS_ALLOW"


class Refused(Exception):
    """A destination this gateway will not connect to."""


def address_problem(ip: str) -> str | None:
    """Why `ip` may not be connected to, or None for an ordinary public address. Anything unparsable is refused."""
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return "not an IP address"
    if isinstance(a, ipaddress.IPv6Address):
        if a.ipv4_mapped is not None:
            a = a.ipv4_mapped
        elif a.sixtofour is not None or a.teredo is not None or a in ipaddress.ip_network("64:ff9b::/96"):
            return "an IPv6 form that carries an IPv4 address"
    if a.is_multicast or a.is_unspecified or not a.is_global:
        return "not a public internet address"
    return None


def allowed_pairs(text: str | None) -> frozenset[str]:
    return frozenset(p.strip().lower() for p in (text or "").split(",") if p.strip())


def choose_address(host: str, port: int, allow: frozenset[str]) -> str:
    """The one address to connect to. Every address the name resolves to must pass: a name that answers both public
    and private addresses is refused, so which one a client would have picked does not matter."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as e:
        raise Refused("name does not resolve") from e
    ips = sorted({str(i[4][0]) for i in infos})
    if not ips:
        raise Refused("name does not resolve")
    if f"{host}:{port}".lower() not in allow:
        for ip in ips:
            why = address_problem(ip)
            if why:
                raise Refused(f"{ip}: {why}")
    return ips[0]


def split_target(target: str) -> tuple[str, int]:
    """`host:port` of a CONNECT line, `[v6]:port` included."""
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, _, port = target.rpartition(":")
    if not host or not port.isascii() or not port.isdigit() or not 0 < int(port) < 65536:
        raise Refused("bad CONNECT target")
    return host, int(port)


def clean_url(url: str) -> tuple[str, int, str, str, str | None]:
    """(host, port, record form, origin-form path, query hash) of an absolute http:// URL. The record form has neither
    user info nor query."""
    try:
        u = urlsplit(url)
        host, port = u.hostname, u.port or 80
    except ValueError as e:
        raise Refused("bad URL") from e
    if u.scheme.lower() != "http" or not host:
        raise Refused("only absolute http:// URLs are forwarded (https goes through CONNECT)")
    path = u.path or "/"
    query = hashlib.sha256(u.query.encode()).hexdigest() if u.query else None
    origin = path + (f"?{u.query}" if u.query else "")
    shown = f"http://{host}" + (f":{port}" if port != 80 else "") + path
    return host, port, shown[:MAX_TEXT], origin, query


def rewrite_request(method: str, origin: str, version: str, header_lines: list[bytes], host: str, port: int) -> bytes:
    """The request as the origin server sees it: origin-form, no proxy headers, one exchange per connection."""
    keep = [h for h in header_lines if h.split(b":", 1)[0].strip().lower()
            not in (b"proxy-connection", b"proxy-authorization", b"connection", b"host", b"keep-alive")]
    host_value = host if port == 80 else f"{host}:{port}"
    head = [f"{method} {origin} {version}".encode("latin-1"), b"Host: " + host_value.encode("idna"),
            b"Connection: close", *keep]
    return b"\r\n".join(head) + b"\r\n\r\n"


class Recorder:
    """One JSON line per connection on `out`, numbered; a lock keeps lines whole and numbers gap-free."""

    def __init__(self, out: Any = None):
        self.out: Any = out if out is not None else sys.stdout
        self.seq = 0
        self.lock = threading.Lock()

    def write(self, **fields: object) -> None:
        with self.lock:
            self.seq += 1
            line = json.dumps({"seq": self.seq, **fields}, sort_keys=True)
            self.out.write(line + "\n")
            self.out.flush()


def relay(client: socket.socket, server: socket.socket, on_down: Any = None, idle_s: float = IDLE_TIMEOUT_S
          ) -> tuple[int, int]:
    """Copy both ways until either side closes or both stay silent for `idle_s`. Returns (bytes up, bytes down)."""
    up = down = 0
    socks = [client, server]
    while True:
        ready, _, _ = select.select(socks, [], [], idle_s)
        if not ready:
            return up, down
        for s in ready:
            try:
                data = s.recv(CHUNK)
            except OSError:
                return up, down
            if not data:
                return up, down
            try:
                (server if s is client else client).sendall(data)
            except OSError:
                return up, down
            if s is client:
                up += len(data)
            else:
                down += len(data)
                if on_down is not None:
                    on_down(data)


class Handler(socketserver.StreamRequestHandler):
    server: Gateway
    rbufsize = 0  # no read-ahead: bytes after the headers must stay in the socket for the relay

    def _head(self) -> tuple[list[str], list[bytes]] | None:
        first = self.rfile.readline(MAX_HEADER_BYTES)
        parts = first.decode("latin-1").split()
        if len(parts) != 3:
            return None
        lines: list[bytes] = []
        total = len(first)
        while True:
            line = self.rfile.readline(MAX_HEADER_BYTES)
            total += len(line)
            if line in (b"\r\n", b"\n", b"") or total > MAX_HEADER_BYTES:
                break
            lines.append(line.rstrip(b"\r\n"))
        return parts, lines

    def _say(self, status: str) -> None:
        try:
            self.wfile.write(f"HTTP/1.1 {status}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode())
            self.wfile.flush()
        except OSError:
            pass

    def _open(self, host: str, port: int) -> tuple[socket.socket, str]:
        ip = choose_address(host, port, self.server.allow)
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(CONNECT_TIMEOUT_S)
        try:
            s.connect((ip, port))
        except OSError:
            s.close()
            raise
        s.settimeout(None)
        return s, ip

    def handle(self) -> None:
        t0 = time.time()
        rec = self.server.recorder
        head = self._head()
        if head is None:
            return
        (method, target, version), lines = head
        base: dict[str, object] = {"at": t0, "method": method.upper()[:16]}
        try:
            if method.upper() == "CONNECT":
                host, port = split_target(target)
                base.update(host=host[:MAX_TEXT], port=port)
                server, ip = self._open(host, port)
                base["ip"] = ip
                with server:
                    self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    self.wfile.flush()
                    up, down = relay(self.connection, server)
                rec.write(**base, status="ok", up=up, down=down, ended=time.time())
                return
            host, port, shown, origin, query = clean_url(target)
            base.update(host=host[:MAX_TEXT], port=port, url=shown, query_sha256=query)
            server, ip = self._open(host, port)
            base["ip"] = ip
            digest = hashlib.sha256()
            with server:
                server.sendall(rewrite_request(method, origin, version, lines, host, port))
                up, down = relay(self.connection, server, digest.update)
            rec.write(**base, status="ok", up=up, down=down, response_sha256=digest.hexdigest(), ended=time.time())
        except Refused as e:
            self._say("403 Forbidden")
            rec.write(**base, status="denied", reason=str(e)[:200], ended=time.time())
        except OSError as e:
            self._say("502 Bad Gateway")
            rec.write(**base, status="failed", reason=type(e).__name__, ended=time.time())


class Gateway(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], allow: frozenset[str], recorder: Recorder):
        self.allow = allow
        self.recorder = recorder
        super().__init__(address, Handler)


def main() -> None:
    Gateway(("0.0.0.0", PORT), allowed_pairs(os.environ.get(ALLOW_ENV)), Recorder()).serve_forever()


if __name__ == "__main__":
    main()
