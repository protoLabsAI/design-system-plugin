"""fetch.py — the hardened fetcher. Local sockets only (no network): a throwaway HTTP server on
127.0.0.1, with ``is_public_ip`` / ``socket.getaddrinfo`` monkeypatched where a test needs the
loopback server to stand in for a "public" host."""

from __future__ import annotations

import gzip
import http.server
import importlib.util
import io
import socket
import threading
import time
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("design_system_fetch_t", Path(__file__).resolve().parent.parent / "fetch.py")
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)

_BOMB = None


def _bomb() -> bytes:
    global _BOMB
    if _BOMB is None:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9) as g:
            z = b"\0" * (1 << 22)
            for _ in range(16):  # 64 MiB of zeros → ~64 KB on the wire
                g.write(z)
        _BOMB = buf.getvalue()
    return _BOMB


class _H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/slow":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            for _ in range(40):
                try:
                    self.wfile.write(b"<p>x</p>")
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.25)
            return
        if self.path == "/bomb":
            body = _bomb()
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/br":
            self.send_response(200)
            self.send_header("Content-Encoding", "br")
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"xyz")
            return
        if self.path.startswith("/redirect-private"):
            self.send_response(302)
            self.send_header("Location", "http://10.0.0.1/admin")
            self.end_headers()
            return
        body = b"<style>:root{--brand:#e11d48}</style>INTERNAL"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def loopback_is_public(monkeypatch):
    """Let 127.0.0.1 stand in for a public host (and ONLY it)."""
    real = fx.is_public_ip
    monkeypatch.setattr(fx, "is_public_ip", lambda a: a == "127.0.0.1" or real(a))


def _dl(s: float = 5.0) -> float:
    return time.monotonic() + s


@pytest.mark.parametrize("addr, public", [
    ("8.8.8.8", True), ("1.1.1.1", True), ("2606:4700:4700::1111", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("172.16.0.1", False), ("192.168.1.1", False),
    ("169.254.169.254", False),            # cloud metadata
    ("100.119.239.8", False), ("100.64.0.1", False),  # CGNAT / Tailscale
    ("0.0.0.0", False), ("::1", False), ("fc00::1", False), ("fe80::1", False),
    ("::ffff:127.0.0.1", False), ("::ffff:10.0.0.1", False),  # IPv4-mapped
    ("2002:7f00:1::1", False), ("2002:a00:1::1", False),       # 6to4 wrapping 127/8 and 10/8
    ("2001:0:4136:e378:8000:63bf:80ff:fffe", False),           # Teredo (client 127.0.0.1)
    ("not-an-ip", False),
])
def test_is_public_ip_is_an_allowlist(addr, public):
    assert fx.is_public_ip(addr) is public


def test_refuses_loopback_and_tailnet_urls():
    for url in ("http://127.0.0.1:7870/", "http://100.119.239.8:8765/", "http://[::1]/", "http://169.254.169.254/latest/meta-data/"):
        with pytest.raises(fx.FetchError, match="non-public"):
            fx.fetch_text(url, max_bytes=1000, deadline=_dl())


def test_refuses_a_host_with_any_private_answer(monkeypatch):
    monkeypatch.setattr(fx.socket, "getaddrinfo", lambda host, port, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.119.239.8", port)),
    ])
    with pytest.raises(fx.FetchError, match="non-public"):
        fx.fetch_text("http://mixed.test/", max_bytes=1000, deadline=_dl())


def test_connection_is_pinned_to_the_vetted_address(monkeypatch, server, loopback_is_public):
    """DNS rebinding: the resolver answers the vetted IP first, then a private one. The fetch
    must resolve ONCE and connect to what it vetted."""
    calls = []

    def flip(host, port, *a, **k):
        if host != "rebind.test":  # socket.create_connection((ip, port)) re-parses the literal
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]
        calls.append(host)
        ip = "127.0.0.1" if len(calls) == 1 else "10.255.255.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(fx.socket, "getaddrinfo", flip)
    _, text = fx.fetch_text(f"http://rebind.test:{server}/", max_bytes=10_000, deadline=_dl())
    assert "INTERNAL" in text and calls == ["rebind.test"]


def test_redirects_are_revetted(server, loopback_is_public):
    with pytest.raises(fx.FetchError, match="non-public"):
        fx.fetch_text(f"http://127.0.0.1:{server}/redirect-private", max_bytes=1000, deadline=_dl())


def test_offsite_redirects_can_be_disallowed(server, loopback_is_public):
    with pytest.raises(fx.FetchError, match="off-site"):
        fx.fetch_text(f"http://127.0.0.1:{server}/redirect-private", max_bytes=1000, deadline=_dl(), allow_offsite_hosts=False)


def test_a_slow_drip_server_is_cut_off_by_the_deadline(server, loopback_is_public):
    t0 = time.monotonic()
    with pytest.raises(fx.FetchError, match="deadline"):
        fx.fetch_text(f"http://127.0.0.1:{server}/slow", max_bytes=1_000_000, deadline=_dl(1.2))
    assert time.monotonic() - t0 < 3.0  # the drip would run 10s


def test_gzip_bomb_is_capped_while_streaming(server, loopback_is_public):
    t0 = time.monotonic()
    _, text = fx.fetch_text(f"http://127.0.0.1:{server}/bomb", max_bytes=200_000, deadline=_dl(10))
    assert len(text) == 200_000  # 64 MiB inflated would have been the alternative
    assert time.monotonic() - t0 < 5


def test_unsupported_content_encoding_is_refused(server, loopback_is_public):
    with pytest.raises(fx.FetchError, match="Content-Encoding"):
        fx.fetch_text(f"http://127.0.0.1:{server}/br", max_bytes=1000, deadline=_dl())


def test_non_http_schemes_are_refused():
    for url in ("file:///etc/passwd", "ftp://example.com/", "gopher://x/"):
        with pytest.raises(fx.FetchError, match="http"):
            fx.fetch_text(url, max_bytes=100, deadline=_dl())


# ── merged-canonical regressions (review of #19: deadline phases, decoders, hostile input) ────


def _raw_server(handler):
    """A bare TCP server: ``handler(conn)`` writes whatever bytes it likes after the request."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(8)

    def serve():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return

            def one(c=c):
                try:
                    c.recv(65536)
                    handler(c)
                except OSError:
                    pass
                finally:
                    try:
                        c.close()
                    except OSError:
                        pass

            threading.Thread(target=one, daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return s, s.getsockname()[1]


@pytest.fixture
def raw(loopback_is_public):
    socks = []

    def make(handler):
        s, port = _raw_server(handler)
        socks.append(s)
        return f"http://127.0.0.1:{port}/"

    yield make
    for s in socks:
        s.close()


def _drip(prefix: bytes, byte: bytes, n: int = 40, gap: float = 0.2, suffix: bytes = b""):
    def h(c):
        c.sendall(prefix)
        for _ in range(n):
            c.sendall(byte)
            time.sleep(gap)
        c.sendall(suffix)
    return h


@pytest.mark.parametrize("name, handler", [
    ("status line", lambda c: [c.sendall(bytes([b])) or time.sleep(0.2) for b in b"HTTP/1.1 200 OK\r\n" * 3]),
    ("headers", _drip(b"HTTP/1.1 200 OK\r\nX-Slow: ", b"a", suffix=b"\r\nContent-Length: 2\r\n\r\nok")),
    ("chunk size", _drip(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n1", b"0")),
    ("body", _drip(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n", b"x")),
])
def test_the_deadline_holds_in_every_phase_of_the_response(raw, name, handler):
    """A 1-byte drip every 0.2 s used to survive 5 s+ against a 1 s deadline outside the body
    loop (status line, headers, chunk sizes): per-read timeouts never fire on a drip."""
    url = raw(handler)
    t0 = time.monotonic()
    with pytest.raises(fx.FetchError, match="deadline"):
        fx.fetch_text(url, max_bytes=1_000_000, deadline=_dl(1.0))
    assert time.monotonic() - t0 < 2.5, name


def test_the_deadline_holds_during_a_stalled_tls_handshake(monkeypatch, raw):
    """The server accepts and then says nothing: the TLS handshake blocks until the watchdog."""
    url = raw(lambda c: time.sleep(6)).replace("http://", "https://")
    t0 = time.monotonic()
    with pytest.raises(fx.FetchError, match="deadline"):
        fx.fetch_text(url, max_bytes=1000, deadline=_dl(1.0))
    assert time.monotonic() - t0 < 2.5


def _respond(headers: bytes, body: bytes):
    return lambda c: c.sendall(b"HTTP/1.1 " + headers + b"Content-Length: %d\r\n\r\n" % len(body) + body)


def test_corrupt_gzip_is_a_fetch_error(raw):
    url = raw(_respond(b"200 OK\r\nContent-Encoding: gzip\r\n", b"NOT GZIP AT ALL!!!!!"))
    with pytest.raises(fx.FetchError):
        fx.fetch_text(url, max_bytes=1000, deadline=_dl())


def test_raw_and_zlib_deflate_both_decode(raw):
    import zlib

    co = zlib.compressobj(6, zlib.DEFLATED, -15)
    raw_deflate = co.compress(b"<html>raw</html>") + co.flush()
    assert fx.fetch_text(raw(_respond(b"200 OK\r\nContent-Encoding: deflate\r\n", raw_deflate)), max_bytes=1000, deadline=_dl())[1] == "<html>raw</html>"
    wrapped = zlib.compress(b"<html>zlib</html>")
    assert fx.fetch_text(raw(_respond(b"200 OK\r\nContent-Encoding: deflate\r\n", wrapped)), max_bytes=1000, deadline=_dl())[1] == "<html>zlib</html>"


def test_multi_member_gzip_decodes_every_member(raw):
    body = gzip.compress(b"part1-") + gzip.compress(b"part2")
    assert fx.fetch_text(raw(_respond(b"200 OK\r\nContent-Encoding: gzip\r\n", body)), max_bytes=1000, deadline=_dl())[1] == "part1-part2"


def test_a_redirect_without_location_is_an_error(raw):
    with pytest.raises(fx.FetchError, match="no Location"):
        fx.fetch_text(raw(_respond(b"302 Found\r\n", b"redirect?")), max_bytes=1000, deadline=_dl())


def test_https_to_http_downgrade_is_refused(monkeypatch):
    class R:
        status = 301

        def getheader(self, k, d=None):
            return "http://example.com/" if k == "Location" else d

    monkeypatch.setattr(fx, "resolve_public", lambda h, p: "93.184.216.34")
    monkeypatch.setattr(fx._PinnedHTTPS, "request", lambda self, *a, **k: None)
    monkeypatch.setattr(fx._PinnedHTTPS, "getresponse", lambda self: R())
    with pytest.raises(fx.FetchError, match="downgrade"):
        fx.fetch_text("https://example.com/", max_bytes=100, deadline=_dl())


@pytest.mark.parametrize("url", ["https://" + "a" * 70 + ".com/", "https://a..com/", "http://[::1/", "http://x.com:99999/", "http://u:p@x.com/"])
def test_hostile_urls_raise_only_fetch_error(url):
    with pytest.raises(fx.FetchError):
        fx.fetch_text(url, max_bytes=100, deadline=_dl())


@pytest.mark.parametrize("addr, public", [
    ("64:ff9b::7f00:1", False), ("64:ff9b::a00:1", False), ("64:ff9b::6477:ef08", False),  # NAT64 of 127/8, 10/8, tailnet
    ("64:ff9b::808:808", True),                                                              # NAT64 of 8.8.8.8
    ("::7f00:1", False), ("::a00:1", False),                                                 # IPv4-compatible
    ("2002:6477:ef08::1", False), ("fe80::1%lo0", False), ("224.0.0.1", False),
])
def test_more_v4_in_v6_forms(addr, public):
    assert fx.is_public_ip(addr) is public


def test_ip_literals_are_their_own_site():
    assert not fx.same_site("http://127.0.0.1/", "http://10.0.0.1/")
    assert not fx.same_site("1.2.3.4", "9.9.3.4")
    assert fx.site_of("[::1]") == "::1" and fx.site_of("www.acme.co.uk") == "acme.co.uk"
    assert fx.same_site("https://WWW.Acme.com/x", "cdn.acme.com")


def test_offsite_allow_collection_is_case_insensitive():
    assert fx._offsite_ok(["CDN.Example.com"], "cdn.example.com")
    assert fx._offsite_ok(lambda h: h.endswith(".net"), "a.net") and not fx._offsite_ok(lambda h: 1 / 0, "a.net")


def test_ipv6_literal_host_header_is_bracketed(monkeypatch):
    sent = {}

    def fake_request(self, method, path, headers=None):
        self.putrequest(method, path)  # http.client derives Host from (host, port)
        sent["host"] = next(h for h in self._buffer if h.startswith(b"Host:"))
        self._buffer.clear()

    monkeypatch.setattr(fx, "resolve_public", lambda h, p: "2606:4700::1111")
    monkeypatch.setattr(fx._PinnedHTTP, "request", fake_request)
    monkeypatch.setattr(fx._PinnedHTTP, "getresponse", lambda self: (_ for _ in ()).throw(fx.FetchError("stop")))
    with pytest.raises(fx.FetchError, match="stop"):
        fx.fetch_text("http://[2606:4700::1111]:8080/", max_bytes=100, deadline=_dl())
    assert sent["host"] == b"Host: [2606:4700::1111]:8080"
