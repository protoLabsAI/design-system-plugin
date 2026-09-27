"""Hardened outbound GET for operator/agent-supplied URLs — stdlib only, path-loaded like the others.

The ONE fetcher every URL-reading tool uses — theme_extract (themegen) and ds_audit_url's
static mode. Don't add a second: extend this one.

The agent runs on the operator's machine, inside their tailnet, next to their other services.
A tool that fetches "whatever URL it is handed" is an SSRF primitive unless it is careful, so
this module is the one place that care lives:

* **Globally-routable addresses only.** Every address a host resolves to must be
  ``ipaddress.is_global`` — after unwrapping IPv4-mapped, IPv4-compatible, 6to4, Teredo and
  NAT64 (``64:ff9b::/96``) IPv6 forms, which otherwise smuggle a private v4 address past a v6
  check. That excludes loopback, RFC 1918, link-local, CGNAT/Tailscale ``100.64.0.0/10``, ULA and
  the rest. An allowlist, not a denylist.
* **The connection is PINNED to the vetted address.** The host is resolved once; the socket
  connects to that IP (TLS still verifies the certificate for the hostname via SNI), and the
  peer address of the connected socket is re-checked. A resolver that answers "public" to the
  check and "127.0.0.1" to the connect (DNS rebinding) gets nothing.
* **Redirects are followed by hand** (≤ ``max_redirects``), every hop is re-vetted, an
  https → http downgrade is refused, and a 3xx without a ``Location`` is an error.
* **An absolute wall-clock deadline** (a ``time.monotonic()`` value) bounds the whole call —
  connect, TLS handshake, status line, headers, chunk sizes and body, across every redirect. A
  watchdog timer ``shutdown()``s the socket when it expires (shutdown only — never ``close()``
  from the timer thread: on macOS shutdown-then-close from another thread can lose the wake-up
  of the blocked reader), so a server that drips one byte at a time can't outlive it. Callers
  share one deadline across a multi-fetch job.
* **Decompressed bytes are capped as they stream** — a 1 GiB gzip bomb stops at ``max_bytes``.
  gzip (multi-member too), zlib-wrapped and raw deflate are decoded; any other
  ``Content-Encoding`` is refused.
* **Nothing but FetchError escapes.** Resolver errors on hostile labels (``UnicodeError`` /
  ``ValueError``), corrupt compressed bodies (``zlib.error``), protocol errors — all become a
  :class:`FetchError` with a legible reason.

API::

    fetch_text(url, *, max_bytes, deadline, allow_offsite_hosts=True, max_redirects=4)
        -> (final_url, text)
    is_public_ip(addr) -> bool
    resolve_public(host, port) -> vetted ip          # raises FetchError
    site_of(host) -> str                             # "cdn.acme.co.uk" -> "acme.co.uk"; IP literals are their own site
    same_site(a, b) -> bool                          # hosts or URLs

Design choices settled when the two earlier copies were merged (#17's won unless noted):

* ``deadline`` is ABSOLUTE (monotonic) and required, as are ``max_bytes`` — #17. A relative
  number of seconds is a different meaning under the same name; callers pass
  ``time.monotonic() + budget``.
* ``allow_offsite_hosts`` defaults to ``True`` — #17. It only governs REDIRECTS leaving the first
  URL's site; the vetting is the security boundary, the site rule is a scoping choice the caller
  makes (ds_audit_url passes ``False`` for the page itself). It accepts a bool, a predicate
  ``host -> bool`` (#17) or a collection of hostnames, compared case-insensitively (#19).
* ``Accept-Encoding: gzip, deflate`` — #17: smaller transfers, and the decoder is capped anyway.
* Exports ``is_public_ip`` / ``resolve_public`` — #17 (the tests' seams); ``site_of`` /
  ``same_site`` — #19, now the ONLY site helper (the four private ``_site`` copies are gone).
* Error strings are #17's ("… non-public address …", "deadline exceeded", "off-site").
* Explicit 6to4/Teredo unwrap — #17; NAT64 and IPv4-compatible unwrap added in review.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import zlib
from collections.abc import Callable, Iterable
from urllib.parse import urljoin, urlsplit

USER_AGENT = "Mozilla/5.0 (compatible; protoagent-design-system/1.0)"
MAX_REDIRECTS = 4
_CHUNK = 65536
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class FetchError(RuntimeError):
    """A refused or failed fetch. The message is safe to show the model."""


# ── addresses ─────────────────────────────────────────────────────────────────


def _unwrap(ip):
    """The IPv4 address an IPv6 form carries, if any (else ``ip`` itself)."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        if ip.sixtofour:
            return ip.sixtofour
        if ip.teredo:
            return ip.teredo[1]  # (server, client) — the client is the embedded target
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if 1 < int(ip) < (1 << 32):  # deprecated IPv4-compatible ::a.b.c.d
            return ipaddress.IPv4Address(int(ip))
    return ip


def is_public_ip(addr: str) -> bool:
    """True only for a globally-routable unicast address (after unwrapping v4-in-v6 forms)."""
    try:
        ip = ipaddress.ip_address(str(addr).split("%", 1)[0])
    except ValueError:
        return False
    inner = _unwrap(ip)
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64:
        # NAT64 (DNS64 on an IPv6-only network): the embedded v4 address is the real target.
        return bool(inner.is_global and not inner.is_multicast)
    # Every other wrapper must be global itself AND carry a global address (#17's rule).
    return bool(ip.is_global and inner.is_global and not inner.is_multicast)


def resolve_public(host: str, port: int) -> str:
    """Resolve ``host`` ONCE and return one vetted address to connect to. Refuses if ANY
    resolved address is non-public (a mixed answer is how a rebinding attack hedges)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError) as e:  # "a"*70 / "a..com" raise UnicodeError from the idna codec
        raise FetchError(f"could not resolve {host[:120]} ({e.__class__.__name__})") from e
    addrs = [info[4][0] for info in infos]
    if not addrs:
        raise FetchError(f"{host[:120]} resolved to nothing")
    bad = [a for a in addrs if not is_public_ip(a)]
    if bad:
        raise FetchError(f"refusing to fetch {host[:120]} — it resolves to a non-public address ({bad[0]})")
    return addrs[0]


# ── sites ─────────────────────────────────────────────────────────────────────


def site_of(host: str) -> str:
    """Registrable-ish site of a hostname (``cdn.acme.com`` → ``acme.com``, ``www.acme.co.uk``
    → ``acme.co.uk``). A heuristic, not the public-suffix list. An IP literal is its own site
    (``127.0.0.1`` and ``10.0.0.1`` must never look "same-site")."""
    h = (host or "").strip().lower().rstrip(".")
    try:
        return str(ipaddress.ip_address(h.strip("[]").split("%", 1)[0]))
    except ValueError:
        pass
    parts = h.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov", "edu"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_site(a: str, b: str) -> bool:
    """Do two hosts (or URLs) belong to the same site (by :func:`site_of`)?"""
    def host(x: str) -> str:
        try:
            return (urlsplit(x).hostname or "") if "://" in x else x
        except ValueError:
            return ""
    ha, hb = host(a or ""), host(b or "")
    return bool(ha and hb) and site_of(ha) == site_of(hb)


def _offsite_ok(allow, host: str) -> bool:
    if allow is True:
        return True
    if allow is False or allow is None:
        return False
    if callable(allow):
        try:
            return bool(allow(host))
        except Exception:  # noqa: BLE001 — a broken predicate refuses
            return False
    return host.lower() in {str(h).lower() for h in allow}


# ── connections ───────────────────────────────────────────────────────────────


class _Pinned:
    """Mixin: connect to the vetted IP, never re-resolve the hostname. ``_raw`` is the TCP
    socket (under TLS too) so the deadline watchdog can shut it down mid-handshake."""

    _pin_ip: str
    _raw: socket.socket | None = None
    _expired: threading.Event

    def _open_socket(self) -> socket.socket:
        if self._expired.is_set():
            raise FetchError("deadline exceeded")
        sock = socket.create_connection((self._pin_ip, self.port), self.timeout)  # type: ignore[attr-defined]
        self._raw = sock
        if self._expired.is_set():  # the watchdog fired while we were connecting
            sock.close()
            raise FetchError("deadline exceeded")
        peer = sock.getpeername()[0]
        if peer != self._pin_ip or not is_public_ip(peer):
            sock.close()
            raise FetchError(f"connected peer {peer} is not the vetted address")
        return sock

    def kill(self) -> None:
        """Watchdog callback: wake any blocked read. SHUTDOWN ONLY — see the module doc."""
        self._expired.set()
        raw = self._raw
        if raw is not None:
            try:
                raw.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _PinnedHTTP(_Pinned, http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pin_ip = ip
        self._expired = threading.Event()

    def connect(self) -> None:
        self.sock = self._open_socket()


class _PinnedHTTPS(_Pinned, http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._pin_ip = ip
        self._expired = threading.Event()

    def connect(self) -> None:
        raw = self._open_socket()
        # Certificate + SNI are for the HOSTNAME; the TCP peer is the vetted IP.
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)  # type: ignore[attr-defined]


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise FetchError("deadline exceeded")
    return left


def _charset(ctype: str) -> str:
    m = re.search(r"charset=\"?([\w.-]+)", ctype or "", re.IGNORECASE)
    enc = m.group(1) if m else "utf-8"
    try:
        "".encode(enc)
    except LookupError:
        enc = "utf-8"
    return enc


class _Decoder:
    """Streaming, size-capped decoder for identity / gzip (multi-member) / deflate (zlib-wrapped
    OR raw — servers send both under ``deflate``)."""

    def __init__(self, enc: str, url: str):
        self.enc = enc
        if enc in ("gzip", "x-gzip"):
            self.dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif enc == "deflate":
            self.dec = zlib.decompressobj()
        elif enc in ("identity", ""):
            self.dec = None
        else:
            raise FetchError(f"{url} used unsupported Content-Encoding {enc!r}")
        self.started = False

    def feed(self, chunk: bytes, room: int) -> bytes:
        """Decode ``chunk``, producing at most ``room`` bytes (+1 so the caller sees overflow)."""
        if self.dec is None:
            return chunk
        out = bytearray()
        data = chunk
        while data and len(out) <= room:
            try:
                got = self.dec.decompress(data, room - len(out) + 1)
            except zlib.error:
                if self.enc == "deflate" and not self.started:
                    self.dec = zlib.decompressobj(-zlib.MAX_WBITS)  # raw deflate
                    self.started = True
                    continue
                raise
            self.started = True
            out.extend(got)
            if self.dec.eof and self.dec.unused_data and self.enc != "deflate":
                data = self.dec.unused_data  # the next gzip member
                self.dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
                continue
            data = self.dec.unconsumed_tail
        return bytes(out)


def fetch_text(
    url: str,
    *,
    max_bytes: int,
    deadline: float,
    allow_offsite_hosts: bool | Callable[[str], bool] | Iterable[str] = True,
    max_redirects: int = MAX_REDIRECTS,
) -> tuple[str, str]:
    """GET ``url`` → ``(final_url, text)``. Raises :class:`FetchError` and nothing else.

    ``max_bytes`` caps the DECODED body (it is truncated there, not an error). ``deadline`` is
    an ABSOLUTE ``time.monotonic()`` value bounding the whole call, redirects included — pass
    the same one to every fetch of a multi-fetch job. ``allow_offsite_hosts`` governs where a
    REDIRECT may go relative to the first URL's site: ``True`` (any public host), ``False``
    (same site only), a predicate ``host -> bool``, or a collection of hostnames.
    """
    try:
        return _fetch(url, max_bytes, deadline, allow_offsite_hosts, max_redirects)
    except FetchError:
        raise
    except Exception as e:  # the contract: only FetchError escapes
        if time.monotonic() >= deadline:
            raise FetchError("deadline exceeded") from e
        raise FetchError(f"could not fetch {str(url)[:200]} ({e.__class__.__name__})") from e


def _fetch(url, max_bytes, deadline, allow_offsite_hosts, max_redirects):
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        raise FetchError("deadline must be an absolute time.monotonic() value")
    try:
        first = urlsplit(url)
        first_site = site_of(first.hostname or "")
    except ValueError as e:
        raise FetchError(f"malformed URL: {str(url)[:200]}") from e
    cur = url
    for hop in range(max_redirects + 1):
        try:
            u = urlsplit(cur)
            port = u.port
        except ValueError as e:
            raise FetchError(f"malformed URL: {cur[:200]}") from e
        if u.scheme not in ("http", "https") or not u.hostname:
            raise FetchError(f"not an http(s) URL: {cur[:200]}")
        if u.username or u.password:
            raise FetchError("refusing a URL with embedded credentials")
        host = u.hostname.lower()
        if hop and site_of(host) != first_site and not _offsite_ok(allow_offsite_hosts, host):
            raise FetchError(f"redirect to off-site host {host[:120]} not allowed")
        port = port or (443 if u.scheme == "https" else 80)
        ip = resolve_public(host, port)
        left = _remaining(deadline)
        cls = _PinnedHTTPS if u.scheme == "https" else _PinnedHTTP
        conn = cls(host, port, ip, min(10.0, left))
        watchdog = threading.Timer(left, conn.kill)
        watchdog.daemon = True
        watchdog.start()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        try:
            # http.client derives Host from (host, port) and brackets an IPv6 literal itself.
            conn.request("GET", path, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,text/css,application/json;q=0.9,*/*;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "close",
            })
            resp = conn.getresponse()
            if 300 <= resp.status < 400 and resp.status != 304:
                loc = resp.getheader("Location")
                if not loc:
                    raise FetchError(f"{cur[:200]} answered HTTP {resp.status} with no Location")
                nxt = urljoin(cur, loc.strip())
                if u.scheme == "https" and urlsplit(nxt).scheme == "http":
                    raise FetchError(f"refusing an https → http downgrade redirect to {nxt[:200]}")
                cur = nxt
                continue
            if resp.status >= 400 or resp.status == 304:
                raise FetchError(f"{cur[:200]} answered HTTP {resp.status}")
            decoder = _Decoder((resp.getheader("Content-Encoding") or "identity").strip().lower(), cur[:200])
            out = bytearray()
            while len(out) < max_bytes:
                if conn.sock is not None:
                    conn.sock.settimeout(min(10.0, _remaining(deadline)))
                chunk = resp.read1(_CHUNK)
                if not chunk:
                    break
                out.extend(decoder.feed(chunk, max_bytes - len(out)))
                _remaining(deadline)
            if conn._expired.is_set():  # the watchdog cut the body short: that's not a clean EOF
                raise FetchError("deadline exceeded")
            return cur, bytes(out[:max_bytes]).decode(_charset(resp.getheader("Content-Type") or ""), errors="replace")
        except FetchError:
            raise
        except (OSError, http.client.HTTPException, zlib.error, ValueError) as e:
            if conn._expired.is_set() or time.monotonic() >= deadline:
                raise FetchError("deadline exceeded") from e
            raise FetchError(f"could not fetch {cur[:200]} ({e.__class__.__name__})") from e
        finally:
            watchdog.cancel()
            conn.close()
    raise FetchError(f"too many redirects from {str(url)[:200]}")
