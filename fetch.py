"""Hardened outbound GET for operator-supplied URLs — stdlib only, path-loaded like the others.

The agent runs on the operator's machine, inside their tailnet, next to their other services.
A tool that fetches "whatever URL it is handed" is an SSRF primitive unless it is careful, so
this module is the one place that care lives (theme_extract uses it; the url-audit slice can
too):

* **Globally-routable addresses only.** Every address a host resolves to must be
  ``ipaddress.is_global`` — after unwrapping IPv4-mapped, 6to4 and Teredo IPv6 forms, which
  otherwise smuggle a private v4 address past a v6 check. That excludes loopback, RFC1918,
  link-local, CGNAT/Tailscale ``100.64.0.0/10``, ULA, and the rest. An allowlist, not a denylist.
* **The connection is PINNED to the vetted address.** The host is resolved once; the socket
  connects to that IP (TLS still verifies the certificate for the hostname via SNI), and the
  peer address of the connected socket is re-checked. A resolver that answers "public" to the
  check and "127.0.0.1" to the connect (DNS rebinding) gets nothing.
* **Redirects are followed by hand** (≤ ``max_redirects``) and every hop is re-vetted.
* **A wall-clock deadline** bounds the whole call, including a server that drips one byte at a
  time (a per-read timeout alone never fires on that). Callers share one deadline across a
  multi-fetch job.
* **Decompressed bytes are capped as they stream** — a 1 GiB gzip bomb stops at ``max_bytes``.
  Only identity/gzip/deflate are requested; any other ``Content-Encoding`` is refused.

API: ``fetch_text(url, *, max_bytes, deadline, allow_offsite_hosts=True) -> (final_url, text)``.
Raises :class:`FetchError` (a ``RuntimeError``) with a legible reason.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
import zlib
from collections.abc import Callable, Iterable
from urllib.parse import urljoin, urlparse

USER_AGENT = "Mozilla/5.0 (compatible; protoagent-design-system/1.0)"
MAX_REDIRECTS = 4
_CHUNK = 65536


class FetchError(RuntimeError):
    pass


def _unwrap(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        if ip.sixtofour:
            return ip.sixtofour
        if ip.teredo:
            return ip.teredo[1]  # (server, client) — the client is the embedded target
    return ip


def is_public_ip(addr: str) -> bool:
    """True only for a globally-routable address (after unwrapping v4-in-v6 forms)."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    inner = _unwrap(ip)
    return bool(ip.is_global and inner.is_global and not inner.is_multicast)


def resolve_public(host: str, port: int) -> str:
    """Resolve ``host`` ONCE and return one vetted address to connect to. Refuses if ANY
    resolved address is non-public (a mixed answer is how a rebinding attack hedges)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise FetchError(f"could not resolve {host} ({e.__class__.__name__})") from e
    addrs = [info[4][0] for info in infos]
    if not addrs:
        raise FetchError(f"{host} resolved to nothing")
    bad = [a for a in addrs if not is_public_ip(a)]
    if bad:
        raise FetchError(f"refusing to fetch {host} — it resolves to a non-public address ({bad[0]})")
    return addrs[0]


class _Pinned:
    """Mixin: connect to the vetted IP, never re-resolve the hostname."""

    _pin_ip: str

    def _open_socket(self) -> socket.socket:
        sock = socket.create_connection((self._pin_ip, self.port), self.timeout)  # type: ignore[attr-defined]
        peer = sock.getpeername()[0]
        if peer != self._pin_ip or not is_public_ip(peer):
            sock.close()
            raise FetchError(f"connected peer {peer} is not the vetted address")
        return sock


class _PinnedHTTP(_Pinned, http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pin_ip = ip

    def connect(self) -> None:
        self.sock = self._open_socket()


class _PinnedHTTPS(_Pinned, http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._pin_ip = ip

    def connect(self) -> None:
        raw = self._open_socket()
        # Certificate + SNI are for the HOSTNAME; the TCP peer is the vetted IP.
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)  # type: ignore[attr-defined]


def _site(host: str) -> str:
    """Registrable-ish site of a hostname (``cdn.acme.com`` → ``acme.com``); an IP literal is
    its own site (``127.0.0.1`` and ``10.0.0.1`` must never look "same-site")."""
    try:
        ipaddress.ip_address((host or "").strip("[]"))
        return (host or "").lower()
    except ValueError:
        pass
    parts = (host or "").lower().split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _offsite_ok(allow: bool | Callable[[str], bool] | Iterable[str], host: str) -> bool:
    if allow is True:
        return True
    if allow is False or allow is None:
        return False
    if callable(allow):
        return bool(allow(host))
    return host in set(allow)


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise FetchError("deadline exceeded")
    return left


def _charset(ctype: str) -> str:
    m = re.search(r"charset=([\w.-]+)", ctype or "", re.IGNORECASE)
    enc = m.group(1) if m else "utf-8"
    try:
        "".encode(enc)
    except LookupError:
        enc = "utf-8"
    return enc


def fetch_text(
    url: str,
    *,
    max_bytes: int,
    deadline: float,
    allow_offsite_hosts: bool | Callable[[str], bool] | Iterable[str] = True,
    max_redirects: int = MAX_REDIRECTS,
) -> tuple[str, str]:
    """GET ``url`` → ``(final_url, text)``.

    ``max_bytes`` caps the DECODED body (it is truncated there, not an error). ``deadline`` is
    an absolute ``time.monotonic()`` value bounding the whole call, redirects included — pass
    the same one to every fetch of a multi-fetch job. ``allow_offsite_hosts`` governs where a
    REDIRECT may go relative to the first URL's site: ``True`` (any public host), ``False``
    (same site only), a predicate ``host -> bool``, or a collection of hostnames.
    """
    first_site = _site(urlparse(url).hostname or "")
    cur = url
    for _hop in range(max_redirects + 1):
        u = urlparse(cur)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise FetchError(f"not an http(s) URL: {cur}")
        host = u.hostname.lower()
        if _hop and _site(host) != first_site and not _offsite_ok(allow_offsite_hosts, host):
            raise FetchError(f"redirect to off-site host {host} not allowed")
        port = u.port or (443 if u.scheme == "https" else 80)
        ip = resolve_public(host, port)
        timeout = min(10.0, _remaining(deadline))
        cls = _PinnedHTTPS if u.scheme == "https" else _PinnedHTTP
        conn = cls(host, port, ip, timeout)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        try:
            conn.request("GET", path, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,text/css,application/json;q=0.9,*/*;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "close",
            })
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
                cur = urljoin(cur, resp.getheader("Location"))
                continue
            if resp.status >= 400:
                raise FetchError(f"{cur} answered HTTP {resp.status}")
            enc = (resp.getheader("Content-Encoding") or "identity").strip().lower()
            if enc in ("gzip", "x-gzip"):
                dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
            elif enc == "deflate":
                dec = zlib.decompressobj()
            elif enc in ("identity", ""):
                dec = None
            else:
                raise FetchError(f"{cur} used unsupported Content-Encoding {enc!r}")
            out = bytearray()
            while len(out) < max_bytes:
                if conn.sock is not None:
                    conn.sock.settimeout(min(10.0, _remaining(deadline)))
                chunk = resp.read1(_CHUNK) if hasattr(resp, "read1") else resp.read(_CHUNK)
                if not chunk:
                    break
                if dec is None:
                    out.extend(chunk)
                else:
                    # max_length bounds what ONE compressed chunk may expand into.
                    out.extend(dec.decompress(dec.unconsumed_tail + chunk, max_bytes - len(out) + 1))
                    while dec.unconsumed_tail and len(out) < max_bytes:
                        out.extend(dec.decompress(dec.unconsumed_tail, max_bytes - len(out) + 1))
                _remaining(deadline)
            return cur, bytes(out[:max_bytes]).decode(_charset(resp.getheader("Content-Type") or ""), errors="replace")
        except FetchError:
            raise
        except (OSError, http.client.HTTPException, zlib.error) as e:
            if time.monotonic() >= deadline:
                raise FetchError("deadline exceeded") from e
            raise FetchError(f"could not fetch {cur} ({e.__class__.__name__})") from e
        finally:
            conn.close()
    raise FetchError(f"too many redirects from {url}")
