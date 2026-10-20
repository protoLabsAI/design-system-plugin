"""Design previews in chat — saved snapshots rendered by a frame component (ADR 0118 D5).

A preview is a palette, a contrast pair or a component's Storybook variants, shown INLINE in the
conversation. The chat transcript carries only a pointer, ``{preview_id, title}`` (component-v1
props persist in chat history, so they must stay small). The plugin owns the snapshot here, and
the frame page (``preview_card.html``) reads it back through the host-mediated read channel
(``reads=["previews/*"]``). The frame holds no bearer.

A snapshot is IMMUTABLE: an old palette in the transcript never silently adopts new token
values. It is also BOUNDED. Retention keeps the newest ``MAX_ROWS`` and drops anything older than
``MAX_AGE_DAYS``, so the frame shows "no longer available" for an expired card instead of the
store growing forever.

Every builder here sanitises PER ITEM. A swatch whose color can't be parsed drops that swatch
(and is counted), not the whole palette. Colors are canonicalised to lowercase ``#rrggbb`` /
``#rrggbbaa`` with the plugin's own CSS color parser. The frame's validator accepts exactly
``COLOR_PATTERN``, so client and server agree by construction: the server only ever writes
what the client accepts. ``var()`` references are resolved against the token set being shown.
Anything still unresolvable (``color-mix()``, ``currentColor``, a dangling var) is dropped,
because a saved specimen must not change with the viewer's theme.

Pure logic plus SQLite only: no protoAgent imports, so the suite runs host-free.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

COMPONENT = "design-preview"
KINDS = frozenset({"palette", "contrast", "component"})
VERSION = 1

# Bounds. Shared with the frame's validator (it re-checks them on read: saved data is untrusted too).
MAX_BYTES = 128 * 1024
MAX_GROUPS = 16
MAX_SWATCHES = 512
MAX_STORIES = 100
MAX_LABEL = 200
MAX_COPY = 240

# Retention: whichever bound bites first.
MAX_ROWS = 500
MAX_AGE_DAYS = 90

# The ONE color shape a snapshot may carry, as the frame checks it. Keep this string identical
# to COLOR_RE in preview_card.html (a test pins the two together).
COLOR_PATTERN = r"^#(?:[0-9a-f]{6}|[0-9a-f]{8})$"
_COLOR_RE = re.compile(COLOR_PATTERN)
_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_VAR_RE = re.compile(r"^var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,\s*(.+))?\)$", re.S)

# One process-wide write lock: busy_timeout is a retry loop, not a queue.
_WRITE_LOCK = threading.Lock()


# ── props validator (the component-v1 kind) ─────────────────────────────────────


def validate_ref(props: dict) -> str | None:
    """The host forwards a ``design-preview`` payload only when this returns None."""
    if not isinstance(props, dict) or set(props) != {"preview_id", "title"}:
        return "props must be exactly {preview_id, title}"
    if not isinstance(props["preview_id"], str) or not _ID_RE.match(props["preview_id"]):
        return "invalid preview_id"
    title = props["title"]
    if not isinstance(title, str) or not 1 <= len(title) <= MAX_LABEL:
        return "invalid title"
    return None


# ── color canonicalisation ──────────────────────────────────────────────────────


def canonical_color(value, parse_rgba: Callable[[str], tuple], lookup: dict | None = None, _depth: int = 0) -> str | None:
    """Any parseable CSS color → ``#rrggbb`` (opaque) or ``#rrggbbaa``. ``None`` when it can't be.

    ``parse_rgba`` is the plugin's color parser (``theme.parse_rgba``), injected so this module
    stays dependency-free. ``var(--x[, fallback])`` resolves through ``lookup`` (up to 5 hops),
    then through its fallback."""
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or _depth > 5:
        return None
    v = value.strip()
    m = _VAR_RE.match(v)
    if m:
        ref = (lookup or {}).get(m.group(1))
        out = canonical_color(ref, parse_rgba, lookup, _depth + 1) if ref is not None else None
        if out is None and m.group(2):
            out = canonical_color(m.group(2), parse_rgba, lookup, _depth + 1)
        return out
    try:
        r, g, b, a = parse_rgba(v)
    except Exception:  # noqa: BLE001 — unparseable → the caller drops this one item
        return None
    vals = [r, g, b] + ([a] if a < 1.0 else [])
    hexed = "#" + "".join(f"{round(max(0.0, min(1.0, float(c))) * 255):02x}" for c in vals)
    return hexed if _COLOR_RE.match(hexed) else None


def _label(value, limit: int = MAX_LABEL) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s[:limit] if s else None


# ── payload builders (per-item sanitising) ───────────────────────────────────────


def palette_payload(
    title: str, groups: list[dict], parse_rgba, *, mode: str = "dark", lookup: dict | None = None, light_lookup: dict | None = None
) -> tuple[dict | None, int]:
    """A palette snapshot from ``[{name, swatches: [{label, value, light?, copy?}]}]``.

    Returns ``(payload, dropped)``. A bad swatch is dropped and counted; a group left empty
    disappears; ``payload`` is None only when NOTHING survives. ``lookup`` / ``light_lookup``
    map a var name to its dark / light value, for resolving ``var()`` references."""
    lookup = lookup or {}
    light_lookup = light_lookup or lookup
    dropped = 0
    total = 0
    out_groups: list[dict] = []
    for g in groups or []:
        if len(out_groups) >= MAX_GROUPS:
            dropped += len(g.get("swatches") or [])
            continue
        name = _label(g.get("name")) or "Colors"
        swatches = []
        for s in g.get("swatches") or []:
            if total >= MAX_SWATCHES:
                dropped += 1
                continue
            label = _label(s.get("label"))
            value = canonical_color(s.get("value"), parse_rgba, lookup)
            if not label or value is None:
                dropped += 1
                continue
            sw = {"label": label, "value": value}
            if s.get("light") is not None:
                light = canonical_color(s.get("light"), parse_rgba, light_lookup)
                if light is not None:  # an unresolvable light face drops only the pair, not the swatch
                    sw["light"] = light
            copy = _label(s.get("copy"), MAX_COPY)
            if copy:
                sw["copy"] = copy
            swatches.append(sw)
            total += 1
        if swatches:
            out_groups.append({"name": name, "swatches": swatches})
    if not out_groups:
        return None, dropped
    payload = {"kind": "palette", "title": _label(title) or "Palette", "mode": "light" if mode == "light" else "dark", "groups": out_groups}
    if dropped:
        payload["dropped"] = dropped
    return payload, dropped


def contrast_payload(foreground: str, background: str, result: dict, parse_rgba) -> dict | None:
    """A contrast snapshot. The specimen paints what WCAG measured: a translucent foreground
    is shown as its composited color (``result["composited_foreground"]``)."""
    fg = canonical_color(result.get("composited_foreground") or foreground, parse_rgba)
    bg = canonical_color(background, parse_rgba)
    ratio = result.get("ratio")
    if fg is None or bg is None or not isinstance(ratio, (int, float)) or not 1 <= ratio <= 21:
        return None
    verdicts = {k: result.get(k) for k in ("aa_normal", "aa_large", "aaa_normal")}
    if not all(isinstance(v, bool) for v in verdicts.values()):
        return None
    out = {
        "kind": "contrast",
        "title": f"Contrast · {ratio}:1",
        "foreground": fg,
        "background": bg,
        "foreground_input": _label(foreground, 120),
        "background_input": _label(background, 120),
        "ratio": float(ratio),
        **verdicts,
    }
    if isinstance(result.get("foreground_alpha"), (int, float)):
        out["foreground_alpha"] = float(result["foreground_alpha"])
    return out


def safe_storybook(base: str) -> str:
    """A Storybook root the frame may embed: http(s), a host, no credentials/query/fragment."""
    url = urlsplit((base or "").strip())
    if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("storybook_url must be an http(s) root URL without credentials, query or fragment")
    return url.geturl().rstrip("/")


def story_url_ok(base: str, url: str, story_id: str) -> bool:
    """The exact check the frame makes: same origin as the root, ``<root>/iframe[.html]``,
    and an ``id`` query matching the story."""
    try:
        root, u = urlsplit(base), urlsplit(url)
    except ValueError:
        return False
    path = root.path.rstrip("/")
    return (
        u.scheme in {"http", "https"}
        and (u.scheme, u.netloc) == (root.scheme, root.netloc)
        and not u.username
        and not u.password
        and u.path in {f"{path}/iframe", f"{path}/iframe.html"}
        and parse_qs(u.query).get("id") == [story_id]
    )


def component_payload(comp: dict, base: str, preview_url, docs_url) -> tuple[dict | None, int]:
    """A component snapshot: its variants with their live render + Storybook URLs. A story
    whose URL fails the frame's check is dropped (counted), never the whole card."""
    base = safe_storybook(base)
    stories, dropped = [], 0
    for s in comp.get("stories") or []:
        if len(stories) >= MAX_STORIES:
            dropped += 1
            continue
        sid, name = _label(s.get("id")), _label(s.get("name"))
        if not sid or not name:
            dropped += 1
            continue
        prev, docs = preview_url(base, sid), docs_url(base, sid)
        if not story_url_ok(base, prev, sid):
            dropped += 1
            continue
        stories.append({"id": sid, "name": name, "preview": prev, "docs": docs})
    if not stories:
        return None, dropped
    return {
        "kind": "component",
        "title": _label(comp.get("label") or comp.get("title")) or "Component",
        "component_title": _label(comp.get("title")) or "Component",
        "storybook_url": base,
        "stories": stories,
    }, dropped


# ── the store ─────────────────────────────────────────────────────────────────


def _connect(root: Path) -> sqlite3.Connection:
    """One connection PER CALL (never shared across threads). busy_timeout BEFORE WAL: the WAL
    transition itself takes locks."""
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "previews.db", timeout=5.0)
    db.execute("PRAGMA busy_timeout = 5000")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS previews (id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL)"
    )
    # Additive migration for a pre-release (Codex-draft) table that lacked `created`.
    cols = {r[1] for r in db.execute("PRAGMA table_info(previews)")}
    if "created" not in cols:
        db.execute("ALTER TABLE previews ADD COLUMN created REAL NOT NULL DEFAULT 0")
    db.execute("CREATE INDEX IF NOT EXISTS previews_created ON previews(created)")
    return db


def prune(db: sqlite3.Connection, now: float, max_rows: int = MAX_ROWS, max_age_days: float = MAX_AGE_DAYS) -> None:
    db.execute("DELETE FROM previews WHERE created < ?", (now - max_age_days * 86400,))
    db.execute(
        "DELETE FROM previews WHERE id NOT IN (SELECT id FROM previews ORDER BY created DESC, rowid DESC LIMIT ?)",
        (max_rows,),
    )


def save(root: Path, payload: dict, *, now: float | None = None, max_rows: int = MAX_ROWS, max_age_days: float = MAX_AGE_DAYS) -> dict:
    """Persist one snapshot and return the chat props ``{preview_id, title}``."""
    if payload.get("kind") not in KINDS:
        raise ValueError("unknown preview kind")
    now = time.time() if now is None else now
    title = _label(payload.get("title")) or "Design preview"
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    snapshot = {**payload, "title": title, "version": VERSION, "created_at": created}
    raw = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise ValueError("preview is too large; show a smaller token family")
    preview_id = uuid.uuid4().hex
    with _WRITE_LOCK, closing(_connect(root)) as db, db:
        db.execute("INSERT INTO previews (id, created, payload) VALUES (?, ?, ?)", (preview_id, now, raw))
        prune(db, now, max_rows, max_age_days)
    return {"preview_id": preview_id, "title": title}


def read(root: Path, preview_id: str) -> dict | None:
    if not isinstance(preview_id, str) or not _ID_RE.match(preview_id) or not (root / "previews.db").is_file():
        return None
    with closing(_connect(root)) as db:
        row = db.execute("SELECT payload FROM previews WHERE id = ?", (preview_id,)).fetchone()
    return json.loads(row[0]) if row else None
