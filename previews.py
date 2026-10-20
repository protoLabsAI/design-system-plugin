"""Immutable design previews. Chat carries only an id; the plugin owns the snapshot."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

COMPONENT = "design-preview-ref"
KINDS = {"palette", "contrast", "component"}
MAX_BYTES = 128 * 1024


def validate_ref(props: dict) -> str | None:
    if set(props) != {"preview_id", "title"}:
        return "preview_id and title required"
    if not isinstance(props["preview_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", props["preview_id"]):
        return "invalid preview_id"
    if not isinstance(props["title"], str) or not 1 <= len(props["title"]) <= 200:
        return "invalid title"
    return None


def safe_storybook(base: str) -> str:
    url = urlsplit(base)
    if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Storybook must be an http(s) root URL without credentials, query or fragment")
    return base.rstrip("/")


def save(root: Path, payload: dict) -> dict:
    if payload.get("kind") not in KINDS:
        raise ValueError("unknown preview kind")
    title = str(payload.get("title") or "Design preview")[:200]
    snapshot = {**payload, "title": title, "version": 1, "created_at": datetime.now(timezone.utc).isoformat()}
    raw = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise ValueError("preview is too large; select a smaller token family")
    root.mkdir(parents=True, exist_ok=True)
    preview_id = uuid.uuid4().hex
    with closing(sqlite3.connect(root / "previews.db")) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS previews (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("INSERT INTO previews VALUES (?, ?)", (preview_id, raw))
    return {"preview_id": preview_id, "title": title}


def read(root: Path, preview_id: str) -> dict | None:
    if not re.fullmatch(r"[a-f0-9]{32}", preview_id) or not (root / "previews.db").is_file():
        return None
    with closing(sqlite3.connect(root / "previews.db")) as db:
        row = db.execute("SELECT payload FROM previews WHERE id = ?", (preview_id,)).fetchone()
    return json.loads(row[0]) if row else None
