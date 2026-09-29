"""Component index — every PUBLIC export of the DS component package, by name.

Story files are named for Storybook GROUPS (``Overlays.stories.tsx``), and source modules for
their family (``app-shell.tsx`` ships ``AppShell`` AND ``MobileNav``), so neither file name is a
lookup key for a component. This module reads the package's own contract instead: the
``package.json`` ``exports`` map says which modules are public, and each module's ``export``
statements say which names it ships. A lookup by name then lands on the right module, and the
API is read from the source itself (JSDoc + signature + the props type it references).

Pure logic (no network, no protoAgent imports); ``__init__`` owns the fetch and the tools.
"""

from __future__ import annotations

import json
import posixpath
import re

SOURCE_EXT = (".tsx", ".ts", ".jsx", ".js", ".mjs")
_NOT_PUBLIC = re.compile(r"\.(stories|test|spec|d)\.|(^|/)(internal|index\.test)\b")

# `export function X` / `export const X` / `export class X` / `export default function X`
_DECL_RE = re.compile(r"^export\s+(?:default\s+)?(?:async\s+)?(function\*?|const|let|class)\s+([A-Za-z_$][\w$]*)", re.MULTILINE)
_TYPE_DECL_RE = re.compile(r"^export\s+(?:declare\s+)?(type|interface|enum)\s+([A-Za-z_$][\w$]*)", re.MULTILINE)
# `export { a, b as c }` / `export type { T }` — optionally `from "./x"`
_LIST_RE = re.compile(r"^export\s+(type\s+)?\{([^}]*)\}\s*(?:from\s*['\"]([^'\"]+)['\"])?", re.MULTILINE)
_STAR_RE = re.compile(r"^export\s+\*\s+(?:as\s+([A-Za-z_$][\w$]*)\s+)?from\s*['\"]([^'\"]+)['\"]", re.MULTILINE)


def kind_of(name: str, decl: str = "") -> str:
    """component · hook · type · value — what the agent is looking at."""
    if decl in ("type", "interface", "enum"):
        return "type"
    if re.fullmatch(r"use[A-Z0-9]\w*", name):
        return "hook"
    if name[:1].isupper() and not name.isupper():
        return "component"
    return "value"


def public_modules(package_json: str, components_path: str, listing: list[str]) -> list[tuple[str, str]]:
    """``[(module file relative to components_path, import specifier)]`` for every public module.

    Reads ``exports`` (and ``main``/``module`` as a root entry) from ``package.json``; a target
    outside ``components_path`` (a built dist file, a stylesheet) isn't a source module and is
    skipped. With no usable ``package.json`` every non-story/test source file in the listing is
    treated as public (the best available guess — better than an empty index)."""
    try:
        pkg = json.loads(package_json) if package_json else {}
    except (ValueError, TypeError):
        pkg = {}
    name = str(pkg.get("name") or "") if isinstance(pkg, dict) else ""
    comp_rel = posixpath.basename(components_path.rstrip("/"))  # "src" of "packages/ui/src"
    targets: list[tuple[str, str]] = []
    exports = pkg.get("exports") if isinstance(pkg, dict) else None
    if isinstance(exports, str):
        exports = {".": exports}
    if isinstance(exports, dict):
        for key, target in exports.items():
            if isinstance(target, dict):  # conditional exports: {"import": ..., "types": ...}
                target = next((v for k, v in target.items() if k in ("source", "import", "default", "require") and isinstance(v, str)), None)
            if isinstance(key, str) and isinstance(target, str):
                targets.append((key, target))
    elif isinstance(pkg, dict):
        for fld in ("source", "module", "main"):
            if isinstance(pkg.get(fld), str):
                targets.append((".", pkg[fld]))
                break
    have = set(listing)
    out: list[tuple[str, str]] = []
    for key, target in targets:
        t = posixpath.normpath(target.lstrip("./")) if not target.startswith("../") else ""
        if not t.startswith(comp_rel + "/"):
            continue
        rel = t[len(comp_rel) + 1:]
        if "/" in rel or not rel.endswith(SOURCE_EXT) or "*" in rel or (have and rel not in have):
            continue
        spec = name + ("" if key == "." else "/" + key.lstrip("./")) if name else key
        out.append((rel, spec))
    if out:
        return out
    return [(f, "") for f in sorted(listing) if f.endswith(SOURCE_EXT) and not _NOT_PUBLIC.search(f)]


def resolve_relative(module: str, spec: str, listing: list[str]) -> str | None:
    """``./command-palette.views`` from ``command-palette.tsx`` → ``command-palette.views.tsx``
    (only same-directory relatives; anything else isn't in the listing we can read)."""
    if not spec.startswith("./"):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(module), spec))
    if base in listing:
        return base
    for ext in SOURCE_EXT:
        if base + ext in listing:
            return base + ext
    return None


def module_exports(source: str) -> list[dict]:
    """Every name a module exports: ``[{name, kind, from}]`` where ``from`` is the relative
    specifier of a re-export (``export { a } from "./x"``), else ``""``. ``export * from``
    comes back as ``{name: "*", from: "./x"}`` for the caller to expand."""
    src = source or ""
    out: dict[str, dict] = {}
    for m in _DECL_RE.finditer(src):
        out.setdefault(m.group(2), {"name": m.group(2), "kind": kind_of(m.group(2)), "from": ""})
    for m in _TYPE_DECL_RE.finditer(src):
        out.setdefault(m.group(2), {"name": m.group(2), "kind": "type", "from": ""})
    for m in _LIST_RE.finditer(src):
        all_types = bool(m.group(1))
        for part in m.group(2).split(","):
            part = part.strip()
            if not part:
                continue
            is_type = all_types or part.startswith("type ")
            part = part.removeprefix("type ").strip()
            orig, _, alias = part.partition(" as ")
            name = (alias or orig).strip()
            if not re.fullmatch(r"[A-Za-z_$][\w$]*", name) or name == "default":
                continue
            out.setdefault(name, {"name": name, "kind": "type" if is_type else kind_of(name),
                                  "from": m.group(3) or "", "orig": orig.strip()})
    stars = [{"name": "*", "kind": "", "from": m.group(2)} for m in _STAR_RE.finditer(src) if not m.group(1)]
    return sorted(out.values(), key=lambda e: e["name"]) + stars


def _match_close(text: str, i: int) -> int:
    """Index just past the bracket closing ``text[i]`` (one of ``( { [``), skipping strings
    and comments. Angle brackets are NOT counted: ``=>`` in a type would unbalance them."""
    pairs = {"(": ")", "{": "}", "[": "]"}
    stack = [pairs[text[i]]]
    j = i + 1
    n = len(text)
    while j < n and stack:
        c = text[j]
        if c in "\"'`":
            j += 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
        elif text.startswith("//", j):
            j = text.find("\n", j)
            j = n if j < 0 else j
        elif text.startswith("/*", j):
            j = text.find("*/", j + 2)
            j = n if j < 0 else j + 1
        elif c in pairs:
            stack.append(pairs[c])
        elif c == stack[-1]:
            stack.pop()
        j += 1
    return j


def _leading_doc(text: str, start: int) -> int:
    """Start offset of the JSDoc block directly above ``start`` (or ``start`` itself)."""
    head = text[:start].rstrip()
    if head.endswith("*/"):
        k = head.rfind("/**")
        if k >= 0:
            return k
    return start


def _type_decl(source: str, name: str) -> str:
    """The full ``type X = …`` / ``interface X {…}`` declaration in ``source`` (or "")."""
    m = re.search(r"^(?:export\s+)?(?:declare\s+)?(type|interface)\s+" + re.escape(name) + r"\b[^=\{\n]*", source, re.MULTILINE)
    if not m:
        return ""
    j = m.end()
    if m.group(1) == "interface":
        k = source.find("{", j)
        end = _match_close(source, k) if k >= 0 else j
    else:
        # A type alias ends at the first `;` (or blank line) outside brackets.
        k = source.find("=", j)
        end = k + 1 if k >= 0 else j
        while end < len(source):
            c = source[end]
            if c in "({[":
                end = _match_close(source, end)
                continue
            if c == ";" or source.startswith("\n\n", end) or source.startswith("\nexport", end):
                end += 1 if c == ";" else 0
                break
            end += 1
    return source[_leading_doc(source, m.start()):end].strip()


def extract_api(source: str, name: str, limit: int = 6000) -> str:
    """One export's API, read from its source: the JSDoc above it, its signature (the full
    destructured-props parameter list and its inline type), and any props type it names that
    the same module declares (``ButtonProps``). Returns "" when ``name`` isn't declared here."""
    src = source or ""
    m = re.search(r"^export\s+(?:default\s+)?(?:async\s+)?(function\*?|const|let|class)\s+" + re.escape(name) + r"\b", src, re.MULTILINE)
    if not m:
        decl = _type_decl(src, name)
        if decl:
            return decl[:limit]
        # `function X` declared privately and exported via `export { X }`
        m = re.search(r"^(?:async\s+)?(function\*?|const|let|class)\s+" + re.escape(name) + r"\b", src, re.MULTILINE)
        if not m:
            return ""
    start = _leading_doc(src, m.start())
    kw = m.group(1)
    if kw.startswith("function"):
        k = src.find("(", m.end())
        end = _match_close(src, k) if k >= 0 else m.end()
        # return-type annotation, up to the body's opening brace
        tail = re.match(r"\s*:\s*[^{;\n]+", src[end:])
        if tail:
            end += tail.end()
        sig = src[start:end].rstrip() + " { … }"
    elif kw == "class":
        k = src.find("{", m.end())
        sig = src[start:k if k >= 0 else m.end()].rstrip() + " { … }"
    else:
        # const X = forwardRef(function X(props: P, ref) {…}) / (props: P) => … — keep the head
        # through the first parameter list, not the whole body.
        line_end = src.find("\n", m.end())
        head_end = line_end if line_end >= 0 else len(src)
        k = src.find("(", m.end(), head_end + 200)
        if k >= 0:
            end = _match_close(src, k)
            inner = src.find("(", k + 1, end)
            if src[m.end():k].strip().endswith(("forwardRef", "memo", "forwardRef<", "React.forwardRef", "React.memo")) and inner >= 0:
                end = _match_close(src, inner)
            head_end = min(end, k + 1500)
        sig = src[start:head_end].rstrip() + " …"
    parts = [sig]
    # Props types the signature references and this module declares.
    for t in dict.fromkeys(re.findall(r"\b([A-Z][A-Za-z0-9]*(?:Props|Options|Config|Item))\b", sig)):
        decl = _type_decl(src, t)
        if decl and decl not in sig:
            parts.append(decl)
    return "\n\n".join(parts)[:limit]


def pascal(stem: str) -> str:
    """``app-shell`` → ``AppShell``; ``command-palette`` → ``CommandPalette``."""
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[-_.]", stem) if p)


def story_candidates(name: str, module: str, story_files: list[str]) -> list[str]:
    """Story files likely to exercise ``name``: its own ``<Name>.stories.*`` first, then the
    ones named for its module's family (``app-shell.tsx`` → ``AppShell*.stories.tsx``)."""
    fam = pascal(posixpath.splitext(posixpath.basename(module))[0]).lower() if module else ""
    own = [f for f in story_files if f.split(".", 1)[0].lower() == name.lower()]
    related = [f for f in story_files if fam and f.split(".", 1)[0].lower() == fam and f not in own]
    return own + related


def usage_excerpt(story_source: str, name: str, lines: int = 14) -> str:
    """The first place a story renders ``<Name`` (or calls ``name(``), with a few lines of
    context — how the DS author actually uses it."""
    m = re.search(r"<" + re.escape(name) + r"[\s/>]", story_source or "") or re.search(r"\b" + re.escape(name) + r"\(", story_source or "")
    if not m:
        return ""
    src = story_source
    first = src.rfind("\n", 0, m.start()) + 1
    first = max(0, src.rfind("\n", 0, max(0, first - 1)) + 1)  # one line of lead-in
    out = src[first:].split("\n")[:lines]
    return "\n".join(out).rstrip()


def search(index: list[dict], query: str, limit: int = 15) -> list[dict]:
    """Exports whose name CONTAINS the query — components and hooks before values and types,
    shorter (closer) names first."""
    q = (query or "").strip().lower()
    if not q:
        return []
    rank = {"component": 0, "hook": 1, "value": 2, "type": 3}
    part = [e for e in index if q in e["name"].lower()]
    part.sort(key=lambda e: (e["name"].lower() != q, rank.get(e["kind"], 4), len(e["name"]), e["name"]))
    return part[:limit]


def lookup(index: list[dict], query: str, limit: int = 4) -> list[dict]:
    """Exact (case-insensitive) name matches; failing that, the closest exports that CONTAIN
    the query (``Toast`` → ToastProvider, useToast)."""
    q = (query or "").strip().lower()
    exact = [e for e in index if e["name"].lower() == q]
    return exact[:limit] if exact else search(index, q, limit)
