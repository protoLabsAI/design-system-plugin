"""Storybook bridge — the component gallery's spine.

A design system that publishes a Storybook already maintains the thing this plugin
would otherwise have to reinvent: an inventory of every component AND every variant,
with a live render for each. ``index.json`` is that inventory (machine-readable, one
entry per story), and ``/iframe?id=<story-id>`` renders any single story standalone.

So the gallery renders the REAL components, not a hand-authored replica that drifts
from them — which is the whole point of a plugin whose job is to prevent drift.

Pure logic lives here (no protoAgent imports, no network); ``__init__`` owns the fetch
and the tool/route surface.

TRAP: a static export served by Cloudflare Pages 308-redirects ``/iframe.html`` to
``/iframe`` (extension stripping), while a local ``storybook dev`` serves ONLY
``/iframe.html``. ``preview_url`` emits the extensionless form and lets the redirect
resolve it; ``preview_url(..., legacy=True)`` forces the dev-server shape.
"""

from __future__ import annotations

# Story entries Storybook generates for the docs page rather than a rendered variant.
# They have no visual to show, so the gallery skips them (the agent can still read them).
_DOCS_TYPES = frozenset({"docs"})


def normalize_base(url: str) -> str:
    """Trim a Storybook root URL to a bare origin+path with no trailing slash."""
    return (url or "").strip().rstrip("/")


def preview_url(base: str, story_id: str, *, legacy: bool = False) -> str:
    """Live standalone render URL for one story — what the gallery iframes.

    ``viewMode=story`` drops the docs chrome; ``shortcuts``/``nav`` off keeps the
    embedded frame from stealing keystrokes from the console around it.
    """
    leaf = "iframe.html" if legacy else "iframe"
    return (
        f"{normalize_base(base)}/{leaf}?id={story_id}"
        "&viewMode=story&shortcuts=false&singleStory=true"
    )


def docs_url(base: str, story_id: str) -> str:
    """The full Storybook UI, deep-linked to one story — the 'open in Storybook' escape."""
    return f"{normalize_base(base)}/?path=/story/{story_id}"


def parse_index(index: dict) -> list[dict]:
    """Fold a Storybook ``index.json`` into one record per COMPONENT.

    Storybook indexes by story; a gallery browses by component. Entries share a
    ``title`` ("Components/Primitives/Button"), which is also the design system's own
    taxonomy — so the last segment is the component and the rest is its group path.
    Returns components sorted by group then label, each with its stories in index order
    (which is source order — the DS author's intended reading, not alphabetical).
    """
    entries = (index or {}).get("entries") or {}
    by_title: dict[str, dict] = {}
    for entry in entries.values():
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        rec = by_title.setdefault(
            title,
            {
                "title": title,
                "label": title.rsplit("/", 1)[-1],
                "group": title.rsplit("/", 1)[0] if "/" in title else "",
                "import_path": entry.get("importPath") or "",
                "stories": [],
                "docs": [],
            },
        )
        story = {
            "id": entry.get("id") or "",
            "name": entry.get("name") or "",
            "tags": list(entry.get("tags") or ()),
        }
        target = "docs" if str(entry.get("type") or "") in _DOCS_TYPES else "stories"
        rec[target].append(story)
    return sorted(by_title.values(), key=lambda c: (c["group"], c["label"]))


def group_tree(components: list[dict]) -> list[dict]:
    """Group components under their group path, preserving the DS's own ordering.

    The group path IS the taxonomy the design system already curates (Foundations,
    Primitives/Atoms, Layout, Navigation, …) — an atomic-design hierarchy we inherit
    rather than impose. Ungrouped components collect under "" so callers can render
    them at the top without a heading.
    """
    out: list[dict] = []
    index: dict[str, dict] = {}
    for comp in components:
        group = comp["group"]
        if group not in index:
            index[group] = {"group": group, "components": []}
            out.append(index[group])
        index[group]["components"].append(comp)
    return out


def find_component(components: list[dict], name: str) -> dict | None:
    """Resolve a component by exact title, by label, or case-insensitively by label."""
    needle = (name or "").strip()
    if not needle:
        return None
    for comp in components:
        if comp["title"] == needle or comp["label"] == needle:
            return comp
    lowered = needle.lower()
    for comp in components:
        if comp["label"].lower() == lowered or comp["title"].lower() == lowered:
            return comp
    return None


def summarize(components: list[dict]) -> str:
    """Compact grouped inventory for the agent — components with their variant names.

    Deliberately terse: the agent reads this to decide what already exists before
    proposing something new, so it needs breadth (every component, every variant name)
    without the token cost of the story SOURCE, which ``ds_story`` fetches on demand.
    """
    if not components:
        return "No stories found."
    lines: list[str] = []
    total = 0
    for node in group_tree(components):
        lines.append(f"\n{node['group'] or '(ungrouped)'}")
        for comp in node["components"]:
            names = ", ".join(s["name"] for s in comp["stories"]) or "—"
            total += len(comp["stories"])
            lines.append(f"  - {comp['label']}: {names}")
    header = f"{len(components)} components / {total} stories"
    return header + "\n" + "\n".join(lines)
