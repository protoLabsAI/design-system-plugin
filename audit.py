"""Design-system adherence audit — a rule engine over source TEXT, plus a repo walker.

``audit_text(text, filename, vocab, …)`` is pure: hand it the text of a CSS / SCSS / TSX /
JSX / TS / JS / HTML / Vue / Svelte file and a ``vocab.Vocab`` and it returns findings. It
never touches the filesystem, so the same engine serves ``ds_check`` (a pasted snippet), the
repo auditor below (``audit_tree`` — the only function here that reads files), and a URL
auditor feeding it fetched stylesheets.

Every finding is a plain dict::

    {rule, severity, lane, file, line, col, snippet, message, suggestion, group}

* ``severity`` — ``error`` (definitely broken: a token that doesn't exist), ``warn`` (a real
  violation with a known fix), ``info`` (worth knowing; judgment call).
* ``lane`` — WHO fixes it. ``consumer``: the audited app should change (use the token,
  compose the component). ``ds``: the design system itself is missing something (no type
  scale, a color the app keeps needing, a DS class everyone overrides). Getting the lane
  right is the point: "DS should add a type scale" and "app should use var(--pl-x)" go to
  different repos.
* ``group`` — the theme a finding belongs to (a token, a DS class, a literal), so a report
  can say "var(--pl-color-border) fallback #2a2a31 — 41 places" instead of 41 lines.

Rules (stable ids; each can be switched off by passing ``rules=``):

consumer lane
  raw-color            literal colors instead of var(--pl-*) — nearest token + ΔE2000
  stale-fallback       var(--pl-x, <fallback>) whose fallback isn't the token's value in any theme
  unknown-token        var(--pl-foo) the DS doesn't define (typo / removed)
  off-scale-length     hardcoded font-size / radius / spacing / box-shadow where a token scale exists
  ds-class-override    app CSS restyling the DS's own .pl-* classes (forking the system)
  legacy-alias         app custom properties that re-declare a DS token's value (--brand-indigo)
  hand-rolled-control  raw <button>/<input>/<select>/<textarea>/<dialog> when the DS ships one
  shadow-component     a local component named like (or as a family of) a DS component
  foreign-ui-lib       imports of competing UI kits (MUI, Chakra, antd, shadcn, raw Radix, Bootstrap…)
ds lane (aggregated across everything scanned)
  missing-scale        hardcoded values for a property the DS has NO token scale for — one finding
  palette-gap          a color with no close token, used repeatedly across files
  override-hotspot     a DS class overridden in many places — the DS likely needs a variant/prop

Suppress with a comment: ``ds-audit-ignore`` (this line; optionally ``ds-audit-ignore
raw-color,stale-fallback``), ``ds-audit-ignore-next-line``, or ``ds-audit-ignore-file``
anywhere in the file.
"""

from __future__ import annotations

import bisect
import fnmatch
import importlib.util as _ilu
import json
import os
import re
from pathlib import Path


def _load(filename: str):
    spec = _ilu.spec_from_file_location(f"design_system_audit_{Path(filename).stem}", Path(__file__).resolve().parent / filename)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vocab_mod = _load("vocab.py")

# ── rule registry ─────────────────────────────────────────────────────────────

RULES: dict[str, dict] = {
    "raw-color": {"lane": "consumer", "summary": "Literal colors instead of var(--pl-*)"},
    "stale-fallback": {"lane": "consumer", "summary": "var(--pl-x, fallback) whose fallback no longer matches the token"},
    "unknown-token": {"lane": "consumer", "summary": "var(--pl-…) names the design system doesn't define"},
    "off-scale-length": {"lane": "consumer", "summary": "Hardcoded font-size / radius / spacing / shadow where a token scale exists"},
    "ds-class-override": {"lane": "consumer", "summary": "App CSS restyling the design system's own classes"},
    "legacy-alias": {"lane": "consumer", "summary": "App custom properties duplicating a design-token value"},
    "hand-rolled-control": {"lane": "consumer", "summary": "Raw form controls where the DS ships a component"},
    "shadow-component": {"lane": "consumer", "summary": "Local components shadowing a DS component"},
    "foreign-ui-lib": {"lane": "consumer", "summary": "Imports of competing UI kits"},
    "missing-scale": {"lane": "ds", "summary": "The DS has no token scale for a property the app sets by hand"},
    "palette-gap": {"lane": "ds", "summary": "A color the app keeps needing that no token is close to"},
    "override-hotspot": {"lane": "ds", "summary": "A DS class overridden so often it needs a variant/prop"},
}
AGGREGATE_RULES = ("missing-scale", "palette-gap", "override-hotspot")

# Default control → DS component map, used when no inventory is supplied. With an inventory,
# the first candidate the DS actually ships wins, and an element with none is not flagged.
DEFAULT_CONTROL_MAP = {"button": "Button", "input": "Input", "select": "DropdownSelect", "textarea": "Textarea", "dialog": "Dialog"}
_CONTROL_CANDIDATES = {
    "button": ("Button", "IconButton"),
    "input": ("Input", "TextField", "TextInput"),
    "select": ("DropdownSelect", "Select", "Combobox"),
    "textarea": ("Textarea", "TextArea"),
    "dialog": ("Dialog", "Modal"),
    "checkbox": ("Checkbox", "Switch"),
    "radio": ("RadioGroup", "Radio"),
}
DS_PACKAGES = ("@protolabsai/ui", "@protolabsai/design", "@pl/ui")

FOREIGN_UI = (
    (re.compile(r"^@mui/|^@material-ui/"), "Material UI"),
    (re.compile(r"^@chakra-ui/"), "Chakra UI"),
    (re.compile(r"^antd(/|$)|^@ant-design/"), "Ant Design"),
    (re.compile(r"^@/components/ui(/|$)|^~/components/ui(/|$)"), "shadcn/ui (generated components)"),
    (re.compile(r"^@radix-ui/"), "Radix primitives (direct)"),
    (re.compile(r"^bootstrap(/|$)|^react-bootstrap(/|$)"), "Bootstrap"),
    (re.compile(r"^@mantine/"), "Mantine"),
    (re.compile(r"^@headlessui/"), "Headless UI"),
    (re.compile(r"^@nextui-org/|^@heroui/"), "NextUI / HeroUI"),
    (re.compile(r"^primereact(/|$)"), "PrimeReact"),
    (re.compile(r"^semantic-ui"), "Semantic UI"),
    (re.compile(r"^daisyui(/|$)"), "daisyUI"),
)

# Files and dirs a DS audit must never read: dependencies, build output, VCS, vendored code,
# snapshots, test fixtures, and the token file itself (it IS the design system).
DEFAULT_EXCLUDE_DIRS = frozenset({
    "node_modules", "dist", "build", "out", ".git", ".hg", ".svn", "vendor", "vendored", "third_party",
    "third-party", "coverage", ".next", ".nuxt", ".svelte-kit", ".turbo", ".cache", "__snapshots__",
    "__tests__", "storybook-static", ".venv", "venv", "__pycache__", "public",
})
DEFAULT_EXCLUDE_GLOBS = ("*.min.*", "*.map", "*.snap", "*.test.*", "*.spec.*", "*.d.ts", "*tokens.css", "*.stories.*")
EXTENSIONS = {
    ".css": "css", ".scss": "scss", ".sass": "scss", ".less": "scss",
    ".ts": "js", ".tsx": "js", ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".html": "html", ".htm": "html", ".vue": "sfc", ".svelte": "sfc", ".astro": "sfc",
}
MAX_FILE_BYTES = 1_500_000

SEVERITY_WEIGHT = {"error": 3.0, "warn": 1.0, "info": 0.25}


def file_kind(filename: str) -> str | None:
    return EXTENSIONS.get(Path(filename or "").suffix.lower())


# ── text utilities ────────────────────────────────────────────────────────────


def mask_comments(text: str, kind: str) -> str:
    """Blank every comment to spaces, keeping length and newlines — so offsets, lines and
    columns in the masked text are the original's. Strings are skipped over (a ``//`` in a
    URL string is not a comment) but left intact: colors live in strings."""
    out = list(text)
    n = len(text)
    i = 0
    line_comments = kind in ("js", "scss", "sfc")
    track_strings = kind in ("js", "scss", "css")
    html = kind in ("html", "sfc")

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = text[i]
        if html and c == "<" and text.startswith("<!--", i):
            j = text.find("-->", i + 4)
            j = n if j < 0 else j + 3
            blank(i, j)
            i = j
            continue
        if c == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "*":
                j = text.find("*/", i + 2)
                j = n if j < 0 else j + 2
                blank(i, j)
                i = j
                continue
            if (nxt == "/" and line_comments and (i == 0 or text[i - 1] not in ":\\")
                    and (kind != "sfc" or i == 0 or text[i - 1] in " \t\n;{}")):
                j = text.find("\n", i)
                j = n if j < 0 else j
                blank(i, j)
                i = j
                continue
        if track_strings and c in "'\"`":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == c:
                    break
                if c != "`" and text[j] == "\n":  # quotes can't span lines — contain the damage
                    break
                j += 1
            i = j + 1
            continue
        i += 1
    return "".join(out)


_IGNORE_RE = re.compile(r"ds-audit-ignore(-next-line)?(?![\w-])(?:[:\s]+([a-z][a-z,\s-]*))?")


def suppressions(text: str) -> tuple[bool, dict[int, set[str] | None]]:
    """(whole file ignored?, {line: rule ids suppressed there — None = all rules})."""
    if "ds-audit-ignore-file" in text:
        return True, {}
    per_line: dict[int, set[str] | None] = {}
    if "ds-audit-ignore" not in text:
        return False, per_line
    for ln, line in enumerate(text.splitlines(), 1):
        for m in _IGNORE_RE.finditer(line):
            target = ln + 1 if m.group(1) else ln
            ids = {t for t in re.split(r"[,\s]+", m.group(2) or "") if t in RULES}
            if not ids:
                per_line[target] = None
            elif per_line.get(target, set()) is not None:
                per_line[target] = (per_line.get(target) or set()) | ids
    return False, per_line


def _balanced(text: str, open_idx: int) -> int:
    """Index just past the ``)`` matching the ``(`` at ``open_idx`` (or len(text))."""
    depth = 0
    for k in range(open_idx, len(text)):
        ch = text[k]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return k + 1
        elif ch in "\n;{}" and depth == 1 and ch != "\n":
            return k
    return len(text)


def _split_top(value: str) -> list[tuple[str, int]]:
    """Split a CSS value on top-level whitespace (not inside parens) → [(token, offset)]."""
    toks: list[tuple[str, int]] = []
    depth, start = 0, None
    for k, ch in enumerate(value + " "):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch.isspace() and depth == 0:
            if start is not None:
                toks.append((value[start:k], start))
                start = None
        elif start is None:
            start = k
    return toks


def _split_selector_list(sel: str) -> list[str]:
    """Split a selector list on top-level commas (not inside :is()/:not()/[attr])."""
    parts, depth, start = [], 0, 0
    for k, ch in enumerate(sel):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append(sel[start:k])
            start = k + 1
    parts.append(sel[start:])
    return [p.strip() for p in parts if p.strip()]


def _subject(selector: str) -> str:
    """The rightmost compound selector, with functional-pseudo arguments (:not(), :has())
    and attribute brackets removed — they describe context, not the element styled."""
    flat, depth = [], 0
    for ch in selector:
        if ch in "([":
            depth += 1
            if depth == 1:
                flat.append(ch)  # keep the brackets: `[x]` is still its own compound
            continue
        if ch in ")]":
            depth = max(0, depth - 1)
            if depth == 0:
                flat.append(ch)
            continue
        if depth == 0:
            flat.append(ch)
    text = "".join(flat)
    return re.split(r"\s*[>+~]\s*|\s+", text.strip())[-1] if text.strip() else ""


def _alias_target(alias: str, hits: list[str]) -> str:
    """Of the DS tokens sharing an alias's value, the one whose NAME it most resembles —
    ``--brand-indigo`` → ``--pl-color-brand-indigo``, not a semantic token that happens to
    share the value in one theme."""
    import difflib

    core = alias.lstrip("-")
    return max(hits, key=lambda h: (core in h, difflib.SequenceMatcher(None, core, h).ratio(), -hits.index(h)))


def _kebab(prop: str) -> str:
    return re.sub(r"([A-Z])", lambda m: "-" + m.group(1).lower(), prop)


class _Lines:
    def __init__(self, text: str):
        self.text = text
        self.starts = [0] + [m.end() for m in re.finditer(r"\n", text)]

    def pos(self, offset: int) -> tuple[int, int]:
        i = bisect.bisect_right(self.starts, offset) - 1
        return i + 1, offset - self.starts[i] + 1

    def line_text(self, line: int) -> str:
        a = self.starts[line - 1]
        b = self.starts[line] - 1 if line < len(self.starts) else len(self.text)
        return self.text[a:b]


# ── context shared across files ───────────────────────────────────────────────


class AuditContext:
    """State that spans files: app-defined custom properties (so a var the app defines
    itself isn't "unknown", and a legacy alias is reported at its definition), the raw
    material for the ds-lane aggregates, and the counters behind the adherence score."""

    def __init__(self, inventory=None, rules=None, ds_packages=DS_PACKAGES, class_prefix: str | None = None):
        self.inventory = _norm_inventory(inventory)
        self.rules = set(rules) if rules else set(RULES)
        self.ds_packages = tuple(ds_packages or ())
        self.class_prefix = class_prefix
        self.local_vars: dict[str, list[str]] = {}  # name → [raw values]
        self.missing: dict[str, list[dict]] = {}    # scale kind → occurrences (no DS scale)
        self.far_colors: dict[str, list[dict]] = {}  # normalized color → occurrences
        self.overrides: dict[str, list[dict]] = {}  # DS class → rule-block occurrences
        self.token_refs = 0
        self.ds_imports = 0
        self.files_scanned = 0
        self.lines_scanned = 0
        self.skipped: list[str] = []

    def collect_definitions(self, text: str, filename: str) -> None:
        """Pass one of a tree audit: record every custom property the app DEFINES."""
        kind = file_kind(filename) or "css"
        masked = mask_comments(text, kind)
        for m in re.finditer(r"(?<![\w-])(--[A-Za-z0-9_-]+)\s*:\s*([^;{}]*)", masked):
            self.local_vars.setdefault(m.group(1), []).append(m.group(2).strip())


_EXPORT_RE = re.compile(r"^export\s+(?:default\s+)?(?:function|const|class|let)\s+([A-Z][A-Za-z0-9]*)", re.MULTILINE)
_EXPORT_LIST_RE = re.compile(r"^export\s*\{([^}]*)\}", re.MULTILINE)


def exported_components(source: str) -> list[str]:
    """PascalCase names a DS source module exports — the real component inventory, as
    opposed to Storybook titles (which are often GROUPS: "Forms", "Overlays")."""
    names = set(_EXPORT_RE.findall(source or ""))
    for m in _EXPORT_LIST_RE.finditer(source or ""):
        for part in m.group(1).split(","):
            name = part.strip().split(" as ")[-1].strip()
            if re.fullmatch(r"[A-Z][A-Za-z0-9]*", name):
                names.add(name)
    # SCREAMING constants (BUILTIN, CONTRAST) aren't components.
    return sorted(n for n in names if not n.isupper())


def _norm_inventory(inventory) -> list[str]:
    """Inventory entries → bare component names. Storybook titles ("Components/Layout/Grid")
    keep their last segment; spaces are dropped ("Status Dot" → "StatusDot")."""
    out = []
    for item in inventory or ():
        name = item.get("title") if isinstance(item, dict) else str(item)
        name = re.sub(r"[^A-Za-z0-9]", "", str(name or "").split("/")[-1])
        if name and name[0].isalpha():
            out.append(name[0].upper() + name[1:])
    return sorted(set(out))


# ── the per-file engine ───────────────────────────────────────────────────────

_COLOR_FUNC_RE = re.compile(r"(?<![\w-])(rgba?|hsla?|oklch|oklab|lab|lch)\(", re.IGNORECASE)
_HEX_RE = re.compile(r"(?<![\w&#/.%-])#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![\w-])")
_VAR_RE = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)\s*(,)?")
_COLOR_PROPS = re.compile(r"(^|-)(color|background|fill|stroke|border|outline|shadow|caret|accent|stop|flood|decoration|column-rule|scrollbar)", re.IGNORECASE)
_COLOR_CONTEXT = re.compile(r"(color|colou?r|background|bg|fill|stroke|border|shadow|outline|stop|tint|swatch|palette|hex)\w*['\"]?\s*[:=(,\[]\s*[\w\s.'\"`(]*$", re.IGNORECASE)
_SVG_ATTR = re.compile(r"(fill|stroke|stop-color|flood-color|lighting-color|color)\s*=\s*[{'\"]*\s*$", re.IGNORECASE)
_URL_RE = re.compile(r"url\(|data:[a-z]+/[\w.+-]+[;,]", re.IGNORECASE)

_SPACE_PROPS = re.compile(r"^(gap|row-gap|column-gap|margin|margin-(top|right|bottom|left|block|inline|block-start|block-end|inline-start|inline-end)|padding|padding-(top|right|bottom|left|block|inline|block-start|block-end|inline-start|inline-end))$")
_RADIUS_PROPS = re.compile(r"^border(-(top|bottom|start|end)-(left|right|start|end))?-radius$")
_JS_STYLE_PROPS = re.compile(
    r"(?<![\w$.-])(fontSize|borderRadius|border(?:Top|Bottom)(?:Left|Right)Radius|gap|rowGap|columnGap|margin(?:Top|Right|Bottom|Left|Block|Inline)?|padding(?:Top|Right|Bottom|Left|Block|Inline)?|boxShadow|color|backgroundColor|background|borderColor|fill|stroke|outlineColor)\s*:\s*"
)
_KEBAB_DECL = re.compile(r"(?<![\w-])(--[A-Za-z0-9_-]+|[a-z][a-z-]*)\s*:\s*")

_DEF_FUNC_RE = re.compile(r"(?:^|[\s;(){}])(?:export\s+(?:default\s+)?)?function\s+([A-Z][A-Za-z0-9_]*)\s*[(<]")
_DEF_CONST_RE = re.compile(r"(?:^|[\s;{}])(?:export\s+)?(?:const|let)\s+([A-Z][A-Za-z0-9_]*)\s*(?::[^=\n]+)?=\s*(?:React\.)?(?:memo|forwardRef|styled|\(|function|async\b|<)")
_DEF_CLASS_RE = re.compile(r"class\s+([A-Z][A-Za-z0-9_]*)\s+extends\s+(?:React\.)?(?:Pure)?Component\b")
_IMPORT_RE = re.compile(r"""(?:\bfrom\s+|\bimport\s*\(?\s*|\brequire\s*\(\s*|@import\s+(?:url\(\s*)?)(['"])([^'"\n]+)\1""")
_NAMED_IMPORT_RE = re.compile(r"""import\s+(?:type\s+)?(?:[\w$]+\s*,\s*)?\{([^}]*)\}\s*from\s*(['"])([^'"\n]+)\2""")
_CONTROL_RE = re.compile(r"(?<![\w.$])<(button|input|select|textarea|dialog)(?=[\s/>])")
_AFFIXES = re.compile(r"^(App|Local|Custom|My|Base|Simple|Basic|Ui|UI|Styled|Old|Legacy|Our)|(Local|Custom|Base|Old|Legacy|Component|Impl|Ui|UI)$")


def _mk(rule, severity, filename, lines: _Lines, offset, message, suggestion="", group="", **extra) -> dict:
    line, col = lines.pos(offset)
    snippet = lines.line_text(line).strip()
    if len(snippet) > 160:
        snippet = snippet[:157] + "…"
    f = {
        "rule": rule, "severity": severity, "lane": RULES[rule]["lane"], "file": filename, "line": line,
        "col": col, "snippet": snippet, "message": message, "suggestion": suggestion, "group": group or rule,
    }
    f.update(extra)
    return f


class _FileScan:
    """One file's audit. Split out so the rules can share the parsed structure
    (declarations, var() spans, excluded spans) instead of re-scanning."""

    def __init__(self, text: str, filename: str, vocab, ctx: AuditContext):
        self.text = text
        self.filename = filename
        self.kind = file_kind(filename) or "css"
        self.vocab = vocab
        self.ctx = ctx
        self.prefix = vocab.prefix or "--pl-"
        self.class_prefix = ctx.class_prefix or ("." + self.prefix[2:])
        self.masked = mask_comments(text, self.kind)
        self.lines = _Lines(text)
        self.findings: list[dict] = []
        self.jsx = Path(filename).suffix.lower() in (".tsx", ".jsx") or (self.kind == "js" and re.search(r"<[A-Za-z][\w.]*[\s/>]", self.masked) and "React" in self.masked)

        self.excluded: list[tuple[int, int]] = []  # url(), data: URIs — never color-scanned
        for m in _URL_RE.finditer(self.masked):
            if m.group(0).lower().startswith("url("):
                self.excluded.append((m.start(), _balanced(self.masked, m.end() - 1)))
            else:
                end = re.search(r"['\"`)\s]", self.masked[m.end():])
                self.excluded.append((m.start(), m.end() + (end.start() if end else 0)))

        # var() references: (start, end, name, fallback_start|None, fallback_end)
        self.vars: list[tuple[int, int, str, int | None, int]] = []
        for m in _VAR_RE.finditer(self.masked):
            end = _balanced(self.masked, m.start() + 3)
            fb = m.end() if m.group(2) else None
            self.vars.append((m.start(), end, m.group(1), fb, max(end - 1, m.end())))

        self.decls = self._declarations()
        self.selectors = self._selectors() if self.kind in ("css", "scss") else self._style_block_selectors()

    # ── structure ────────────────────────────────────────────────────────────
    def _css_regions(self) -> list[tuple[int, int]]:
        """Where CSS syntax lives: the whole file for CSS; <style> blocks and style=""
        attributes for HTML/SFC; CSS-in-JS template literals are handled by the kebab
        declaration pass in JS files."""
        if self.kind in ("css", "scss"):
            return [(0, len(self.masked))]
        regions = []
        for m in re.finditer(r"<style[^>]*>(.*?)</style>", self.masked, re.DOTALL | re.IGNORECASE):
            regions.append((m.start(1), m.end(1)))
        return regions

    def _declarations(self) -> list[tuple[str, str, int, int]]:
        """[(prop (kebab-case), value, value_start, value_end)]."""
        out: list[tuple[str, str, int, int]] = []
        seen: set[int] = set()
        seg_text = self.masked
        # Blank url(...) bodies so a data: URI's `;base64,` can't split a declaration.
        chars = list(seg_text)
        for a, b in self.excluded:
            for k in range(a, min(b, len(chars))):
                if chars[k] not in "\n()":
                    chars[k] = "x"
        seg_text = "".join(chars)

        for a, b in self._css_regions():
            pos = a
            for m in re.finditer(r"[{};]", seg_text[a:b] + ";"):
                end = a + m.start()
                seg = seg_text[pos:end]
                delim = m.group(0)
                if delim != "{" and ":" in seg:
                    pm = re.match(r"\s*(--[A-Za-z0-9_-]+|[A-Za-z-]+)\s*:", seg)
                    if pm:
                        vs = pos + pm.end()
                        raw = self.masked[vs:end]
                        lead = len(raw) - len(raw.lstrip())
                        out.append((pm.group(1).lower() if not pm.group(1).startswith("--") else pm.group(1), raw.strip(), vs + lead, vs + lead + len(raw.strip())))
                        seen.add(vs + lead)
                pos = end + 1
        if self.kind in ("js", "html", "sfc"):
            # style="…" attributes (HTML/SFC).
            for m in re.finditer(r"\bstyle\s*=\s*(['\"])(.*?)\1", self.masked, re.DOTALL):
                for d in re.finditer(r"([A-Za-z-]+)\s*:\s*([^;]+)", m.group(2)):
                    vs = m.start(2) + d.start(2)
                    if vs not in seen:
                        out.append((d.group(1).lower(), d.group(2).strip(), vs, vs + len(d.group(2).strip())))
                        seen.add(vs)
        if self.kind in ("js", "sfc", "html"):
            # CSS-in-JS / template literals: kebab-case `prop: value;`.
            for m in _KEBAB_DECL.finditer(self.masked):
                prop = m.group(1)
                if not (prop.startswith("--") or "-" in prop or prop in ("gap", "padding", "margin", "color", "background", "fill", "stroke")):
                    continue
                vs = m.end()
                if vs in seen:
                    continue
                vm = re.match(r"[^;`'\"{}\n]+", self.masked[vs:])
                if not vm or not vm.group(0).strip():
                    continue
                # Only when it reads like CSS: terminated by `;` (a JS object uses `,`).
                after = self.masked[vs + vm.end(): vs + vm.end() + 1]
                if after != ";":
                    continue
                val = vm.group(0).strip()
                out.append((prop.lower() if not prop.startswith("--") else prop, val, vs, vs + len(val)))
                seen.add(vs)
            # React style objects: camelCase props.
            for m in _JS_STYLE_PROPS.finditer(self.masked):
                vs = m.end()
                vm = re.match(r"(['\"`])([^'\"`\n]*)\1|(-?\d+(?:\.\d+)?)(?![\w.])", self.masked[vs:])
                if not vm or vs in seen:
                    continue
                if vm.group(3) is not None:
                    val, start = vm.group(3) + ("px" if vm.group(3) not in ("0",) else ""), vs
                    if m.group(1) not in ("fontSize", "gap", "rowGap", "columnGap") and not m.group(1).startswith(("margin", "padding", "border")):
                        continue
                    out.append((_kebab(m.group(1)), val, start, start + len(vm.group(3))))
                else:
                    val, start = vm.group(2), vs + 1
                    out.append((_kebab(m.group(1)), val, start, start + len(val)))
                seen.add(vs)
        return out

    def _selectors(self, a: int = 0, b: int | None = None) -> list[tuple[str, int]]:
        """[(selector text, offset)] for every rule block (not @-rules) in [a, b)."""
        b = len(self.masked) if b is None else b
        out = []
        pos = a
        for m in re.finditer(r"[{};]", self.masked[a:b]):
            end = a + m.start()
            if m.group(0) == "{":
                seg = self.masked[pos:end]
                sel = seg.strip()
                if sel and not sel.startswith("@") and not re.match(r"^(from|to|\d+(\.\d+)?%)(\s*,|$)", sel):
                    out.append((" ".join(sel.split()), pos + (len(seg) - len(seg.lstrip()))))
            pos = end + 1
        return out

    def _style_block_selectors(self) -> list[tuple[str, int]]:
        out = []
        for a, b in self._css_regions():
            out.extend(self._selectors(a, b))
        return out

    def _in(self, spans, offset: int) -> bool:
        return any(a <= offset < b for a, b in spans)

    def _enclosing_var(self, offset: int):
        """The innermost var() whose FALLBACK contains ``offset`` (or None)."""
        best = None
        for v in self.vars:
            if v[3] is not None and v[3] <= offset < v[4] and (best is None or v[3] > best[3]):
                best = v
        return best

    def _decl_at(self, offset: int):
        for d in self.decls:
            if d[2] <= offset < d[3] + 1:
                return d
        return None

    def add(self, f: dict) -> None:
        if f["rule"] in self.ctx.rules:
            self.findings.append(f)

    # ── rules ────────────────────────────────────────────────────────────────
    def run(self) -> list[dict]:
        r = self.ctx.rules
        self.ctx.token_refs += sum(1 for v in self.vars if self.vocab.is_known(v[2]))
        if r & {"stale-fallback", "unknown-token", "legacy-alias"}:
            self.rule_vars()
        if r & {"raw-color", "legacy-alias", "palette-gap"}:
            self.rule_colors()
        if r & {"off-scale-length", "missing-scale"}:
            self.rule_lengths()
        if r & {"ds-class-override", "override-hotspot"}:
            self.rule_class_overrides()
        if r & {"legacy-alias"}:
            self.rule_alias_definitions()
        if self.kind in ("js", "sfc"):
            self.count_ds_imports()
            if self.jsx and "hand-rolled-control" in r:
                self.rule_controls()
            if self.jsx and "shadow-component" in r:
                self.rule_shadow_components()
            if "foreign-ui-lib" in r:
                self.rule_foreign_imports()
        elif "foreign-ui-lib" in r:
            self.rule_foreign_imports()
        return self.findings

    def rule_vars(self) -> None:
        v = self.vocab
        for start, end, name, fb, fb_end in self.vars:
            fallback = self.masked[fb:fb_end].strip() if fb is not None else ""
            if name.startswith(self.prefix):
                if v.source != "css":
                    continue  # a JSON-only vocabulary has no published var names to judge against
                if not v.is_known(name):
                    if name in self.ctx.local_vars:
                        continue  # the app defines it itself — its own namespace choice
                    close = v.close_names(name)
                    self.add(_mk("unknown-token", "error", self.filename, self.lines, start,
                                 f"{name} is not defined by the design system — it resolves to its fallback (or nothing)",
                                 f"did you mean {', '.join(close)}?" if close else "use a token from ds_tokens, or ask the DS to add one",
                                 group=name))
                    continue
                if not fallback or fallback.startswith("var(") or not v.values(name):
                    continue
                if v.value_matches(name, fallback):
                    continue
                vals = v.values(name)
                shown = " / ".join(f"{t} `{vals[t]}`" for t in vals) if len({vocab_mod.normalize_value(x) for x in vals.values()}) > 1 else f"`{next(iter(vals.values()))}`"
                typed = vocab_mod.is_color(fallback) or vocab_mod.parse_length(fallback) is not None
                self.add(_mk("stale-fallback", "warn" if typed else "info", self.filename, self.lines, start,
                             f"fallback `{fallback}` for {name} doesn't match the token (DS value: {shown})",
                             f"drop the fallback — var({name}) — the DS always defines it; or update it to `{v.value(name, 'dark') or next(iter(vals.values()))}`",
                             group=f"{name} ← {vocab_mod.normalize_value(fallback)}"))
            elif fallback and not fallback.startswith("var(") and "legacy-alias" in self.ctx.rules:
                hits = v.exact_colors(fallback) if vocab_mod.is_color(fallback) else v.vars_for_value(fallback) if vocab_mod.parse_length(fallback) is None else []
                if hits:
                    tgt = _alias_target(name, hits)
                    self.add(_mk("legacy-alias", "warn", self.filename, self.lines, start,
                                 f"{name} is a legacy alias: its fallback `{fallback}` is exactly {v.ref(tgt)}'s value",
                                 f"replace var({name}, {fallback}) with {v.ref(tgt)}",
                                 group=name))

    def _color_candidates(self):
        """(offset, literal, from_svg_attr) for every color literal worth judging."""
        text = self.masked
        css_like = self.kind in ("css", "scss")
        out = []
        decl_spans = [(d[2], d[3] + 1) for d in self.decls]
        for m in _HEX_RE.finditer(text):
            off = m.start()
            if css_like and not self._in(decl_spans, off):
                continue  # `#add` in a selector is an id, not a color
            if not css_like:
                hexd = m.group(1)
                ctx_before = text[max(0, off - 48):off].split("\n")[-1]
                in_decl = self._in(decl_spans, off)
                quoted_whole = text[off - 1:off] in "'\"`" and text[m.end():m.end() + 1] in "'\"`"
                arbitrary = text[off - 1:off] == "["  # tailwind bg-[#123456]
                has_ctx = bool(_COLOR_CONTEXT.search(ctx_before)) or bool(_SVG_ATTR.search(ctx_before))
                if hexd.isdigit() and not (in_decl or has_ctx):
                    continue  # "#123" is an issue reference far more often than a color
                if len(hexd) in (3, 4) and not (in_decl or has_ctx or arbitrary):
                    continue
                if len(hexd) in (6, 8) and not (in_decl or has_ctx or arbitrary or quoted_whole):
                    continue
            out.append((off, m.group(0)))
        for m in _COLOR_FUNC_RE.finditer(text):
            if css_like and not self._in(decl_spans, m.start()):
                continue
            end = _balanced(text, m.end() - 1)
            lit = text[m.start():end]
            if "var(" in lit or "calc(" in lit or "${" in lit:
                continue
            out.append((m.start(), lit))
        # Named colors: only as a whole declaration token of a color property.
        for prop, val, vs, ve in self.decls:
            if prop.startswith("--") or not _COLOR_PROPS.search(prop):
                continue
            for tok, toff in _split_top(val):
                low = tok.lower().replace("!important", "").strip()
                if low in vocab_mod.NAMED_COLORS and low != "transparent":
                    out.append((vs + toff, tok))
        return out

    def rule_colors(self) -> None:
        v = self.vocab
        if not v.colors:
            return
        seen: set[int] = set()
        for off, lit in sorted(self._color_candidates()):
            if off in seen or self._in(self.excluded, off):
                continue
            seen.add(off)
            enc = self._enclosing_var(off)
            if enc is not None:
                if enc[2].startswith(self.prefix):
                    continue  # a DS token's fallback — stale-fallback owns it
                if v.exact_colors(lit) and "legacy-alias" in self.ctx.rules:
                    continue  # reported as legacy-alias at the var()
            decl = self._decl_at(off)
            if decl is not None:
                prop = decl[0]
                if prop.startswith(self.prefix):
                    continue  # (re)defining a DS token — theming, not usage
                if prop.startswith("--") and v.exact_colors(decl[1]):
                    continue  # the whole definition is an alias — legacy-alias reports it
                if prop == "box-shadow" and "off-scale-length" in self.ctx.rules and v.shadows:
                    continue  # the hand-rolled shadow is one finding, not one per color stop
            self._judge_color(off, lit)

    def _judge_color(self, off: int, lit: str) -> None:
        v = self.vocab
        matches = v.color_matches(lit, 3)
        if not matches:
            return
        before = self.masked[max(0, off - 32):off].split("\n")[-1]
        svg = bool(_SVG_ATTR.search(before))
        best = matches[0]
        norm = vocab_mod.normalize_color(lit) or lit
        exact = v.exact_colors(lit)
        rgba = vocab_mod.parse_color(lit)
        if exact:
            name = exact[0]
            also = [v.ref(x) for x in exact[1:3]]
            themed = ""
            if v.is_themed(name):
                vals = v.values(name)
                hit = [m["theme"] for m in matches if m["var"] == name]
                which = f"its {hit[0]} value" if hit else "its value"
                themed = f" ({which}; themed: " + ", ".join(f"{t} {vals[t]}" for t in vals) + ") — the literal won't follow the theme"
            msg = f"hardcoded `{lit}` is exactly {v.ref(name)}{themed}"
            sug = f"use {v.ref(name)}" + (f" (same value: {', '.join(also)})" if also else "")
        elif rgba is not None and rgba[3] < 0.999 and (tint := self._opaque_tint(lit)):
            pct = round(rgba[3] * 100)
            msg = f"hardcoded `{lit}` is {v.ref(tint)} at {pct}% alpha"
            sug = f"color-mix(in srgb, {v.ref(tint)} {pct}%, transparent)"
            best = {**best, "var": tint}
        elif best["distance"] < 5:
            msg = f"hardcoded `{lit}` ≈ {v.ref(best['var'])} (ΔE {best['delta_e']:g}) — drifted copy of a token"
            sug = f"use {v.ref(best['var'])}"
        else:
            msg = f"hardcoded `{lit}` — not a design token (nearest {v.ref(best['var'])}, ΔE {best['delta_e']:g}{', α differs' if best['alpha_delta'] >= 0.01 else ''})"
            sug = f"use the closest role token ({v.ref(best['var'])}) if it fits, or propose a new token to the DS"
            self.ctx.far_colors.setdefault(norm, []).append({"file": self.filename, "line": self.lines.pos(off)[0], "literal": lit, "nearest": best["var"], "delta_e": best["delta_e"]})
        f = _mk("raw-color", "info" if svg else "warn", self.filename, self.lines, off, msg, sug,
                group=(exact[0] if exact else best["var"] if best["distance"] < 5 else norm),
                literal=lit, nearest=best["var"], delta_e=best["delta_e"], exact=bool(exact))
        self.add(f)

    def _opaque_tint(self, lit: str) -> str | None:
        """An OPAQUE token whose RGB matches a translucent literal (ΔE < 2.5) — the literal is
        that token at some alpha, and ``color-mix()`` expresses it without a new literal."""
        best = None
        for m in self.vocab.color_matches(lit, k=len(self.vocab.colors)):
            tok = self.vocab.colors[m["var"]].get(m["theme"])
            if m["delta_e"] < 2.5 and tok is not None and tok[3] >= 0.999 and (best is None or m["delta_e"] < best[1]):
                best = (m["var"], m["delta_e"])
        return best[0] if best else None

    def rule_lengths(self) -> None:
        for prop, val, vs, ve in self.decls:
            if prop.startswith("--"):
                continue
            if prop == "font-size":
                kind = "font-size"
            elif _RADIUS_PROPS.match(prop):
                kind = "radius"
            elif _SPACE_PROPS.match(prop):
                kind = "space"
            elif prop == "box-shadow":
                kind = "shadow"
            else:
                continue
            if kind == "shadow":
                self._judge_shadow(val, vs)
                continue
            for tok, toff in _split_top(val):
                tok = tok.replace("!important", "").strip()
                if not tok or "(" in tok:
                    continue  # var()/calc()/clamp() — not a hardcoded literal
                px = vocab_mod.parse_length(tok)
                if px is None or px == 0:
                    continue
                if kind == "space" and abs(px) <= 1:
                    continue  # hairline nudges aren't spacing decisions
                if kind == "radius" and (abs(px) >= 999 or tok.endswith("%")):
                    continue  # pill / circle
                off = vs + toff
                self._judge_length(kind, prop, tok, px, off)

    def _judge_length(self, kind: str, prop: str, tok: str, px: float, off: int) -> None:
        v = self.vocab
        near = v.nearest_length(kind, px)
        if near and near[2] == 0:
            self.add(_mk("off-scale-length", "warn", self.filename, self.lines, off,
                         f"hardcoded {prop}: {tok} is exactly {v.ref(near[0])}",
                         f"use {v.ref(near[0])}", group=f"{kind}: {tok} → {near[0]}", value_px=px, scale=kind))
            return
        if not v.has_scale(kind):
            self.ctx.missing.setdefault(kind, []).append({"file": self.filename, "line": self.lines.pos(off)[0], "value": tok, "prop": prop, "px": px})
            return
        entries = sorted({(p, n) for n, p in v.scale(kind)})
        below = [e for e in entries if e[0] <= abs(px)]
        above = [e for e in entries if e[0] >= abs(px)]
        around = " / ".join(f"{n}={p:g}px" for p, n in ([below[-1]] if below else []) + ([above[0]] if above else []))
        self.add(_mk("off-scale-length", "info", self.filename, self.lines, off,
                     f"{prop}: {tok} is off the {vocab_mod.SCALE_LABEL[kind]} scale (between {around})",
                     f"snap to {v.ref(near[0])}" if near else "use a scale token",
                     group=f"{kind}: {tok}", value_px=px, scale=kind))

    def _judge_shadow(self, val: str, off: int) -> None:
        v = self.vocab
        low = val.strip().lower()
        if not low or low in ("none", "inherit", "initial", "unset") or low.startswith("var(") or "var(" in low:
            return
        hit = v.shadow_for(val)
        if hit:
            self.add(_mk("off-scale-length", "warn", self.filename, self.lines, off,
                         f"hand-written box-shadow is exactly {v.ref(hit)}", f"use {v.ref(hit)}", group=f"shadow → {hit}", scale="shadow"))
        elif v.has_scale("shadow"):
            self.add(_mk("off-scale-length", "info", self.filename, self.lines, off,
                         f"hand-rolled box-shadow `{val}` — the DS ships elevation tokens ({', '.join(n for n, _ in v.shadows)})",
                         "use the closest elevation token", group="shadow: hand-rolled", scale="shadow"))
        else:
            self.ctx.missing.setdefault("shadow", []).append({"file": self.filename, "line": self.lines.pos(off)[0], "value": val, "prop": "box-shadow", "px": None})

    def rule_class_overrides(self) -> None:
        """Flag rule blocks whose SUBJECT (the rightmost compound of any selector in the list)
        is a DS class. ``.x .pl-dialog__body {}`` restyles the DS; ``.pl-markdown pre {}``
        styles content rendered inside a DS container and is left alone — judging by any
        mention of ``.pl-`` would bury the real forks under contextual styling."""
        pat = re.compile(re.escape(self.class_prefix) + r"[A-Za-z0-9_-]+")
        for sel, off in self.selectors:
            classes = sorted({c for part in _split_selector_list(sel) for c in pat.findall(_subject(part))})
            if not classes:
                continue
            for c in classes:
                self.ctx.overrides.setdefault(c, []).append({"file": self.filename, "line": self.lines.pos(off)[0], "selector": sel})
            self.add(_mk("ds-class-override", "warn", self.filename, self.lines, off,
                         f"app CSS restyles the design system's own class{'es' if len(classes) > 1 else ''} {', '.join(classes)} — a local fork of the component",
                         "use the component's props/variants or tokens; if none fits, that's a DS gap to file (not a local override)",
                         group=classes[0], selector=sel, classes=classes))

    def rule_alias_definitions(self) -> None:
        v = self.vocab
        for prop, val, vs, ve in self.decls:
            if not prop.startswith("--") or prop.startswith(self.prefix) or "var(" in val:
                continue
            if vocab_mod.is_color(val):
                hits = v.exact_colors(val)
            elif vocab_mod.parse_length(val) is None and val.strip():
                hits = v.vars_for_value(val)
            else:
                hits = []  # `--gap: 8px` equalling a space token is coincidence as often as not
            if hits:
                tgt = _alias_target(prop, hits)
                self.add(_mk("legacy-alias", "warn", self.filename, self.lines, vs,
                             f"{prop}: {val} re-declares {v.ref(tgt)}'s value under a local name",
                             f"delete {prop} and use {v.ref(tgt)} directly (or alias it: {prop}: {v.ref(tgt)})",
                             group=prop))

    def _ds_imported_names(self) -> set[str]:
        names: set[str] = set()
        for m in _NAMED_IMPORT_RE.finditer(self.masked):
            if any(m.group(3) == p or m.group(3).startswith(p + "/") for p in self.ctx.ds_packages):
                for part in m.group(1).split(","):
                    part = part.strip().split(" as ")[0].replace("type ", "").strip()
                    if part:
                        names.add(part)
        return names

    def count_ds_imports(self) -> None:
        self.ctx.ds_imports += len(self._ds_imported_names())

    def _control_component(self, element: str) -> str | None:
        inv = self.ctx.inventory
        if not inv:
            return DEFAULT_CONTROL_MAP.get(element)
        lower = {n.lower(): n for n in inv}
        for cand in _CONTROL_CANDIDATES.get(element, ()):
            if cand.lower() in lower:
                return lower[cand.lower()]
        return None

    def rule_controls(self) -> None:
        for m in _CONTROL_RE.finditer(self.masked):
            el = m.group(1)
            tag_end = self.masked.find(">", m.end())
            tag = self.masked[m.end(): tag_end if tag_end > 0 else m.end() + 200]
            if el == "input":
                tm = re.search(r"""\btype\s*=\s*[{'"]*\s*['"]?([a-z]+)""", tag)
                itype = tm.group(1) if tm else "text"
                if itype in ("hidden", "file", "submit", "reset", "image", "range", "color"):
                    continue
                if itype in ("checkbox", "radio"):
                    el = itype
            comp = self._control_component(el)
            if not comp:
                continue
            self.add(_mk("hand-rolled-control", "info", self.filename, self.lines, m.start(),
                         f"raw <{m.group(1)}{' type=' + el if el in ('checkbox', 'radio') else ''}> — the design system ships <{comp}>",
                         f"use <{comp}> from the DS so focus, states and theming come for free",
                         group=f"<{m.group(1)}> → {comp}", component=comp))

    def rule_shadow_components(self) -> None:
        inv = self.ctx.inventory
        if not inv:
            return
        by_lower = {n.lower(): n for n in inv}
        imported = self._ds_imported_names()
        seen: set[str] = set()
        for rx in (_DEF_FUNC_RE, _DEF_CONST_RE, _DEF_CLASS_RE):
            for m in rx.finditer(self.masked):
                local = m.group(1)
                if local in seen:
                    continue
                seen.add(local)
                off = m.start(1)
                if local.lower() in by_lower:
                    ds = by_lower[local.lower()]
                    if ds in imported:
                        self.add(_mk("shadow-component", "info", self.filename, self.lines, off,
                                     f"local {local} wraps the DS {ds} under the same name",
                                     f"fine if it only adds app wiring — but a same-named wrapper hides which {ds} a reader is looking at; consider a distinct name",
                                     group=ds, component=ds, local=local))
                        continue
                    self.add(_mk("shadow-component", "warn", self.filename, self.lines, off,
                                 f"local component {local} duplicates the design system's {ds}",
                                 f"import {ds} from the DS; if it lacks something, extend the DS component instead of forking it",
                                 group=ds, component=ds, local=local))
                    continue
                stripped = _AFFIXES.sub("", local)
                if stripped != local and stripped.lower() in by_lower and by_lower[stripped.lower()] not in imported:
                    ds = by_lower[stripped.lower()]
                    self.add(_mk("shadow-component", "warn", self.filename, self.lines, off,
                                 f"local component {local} looks like a re-implementation of the DS's {ds}",
                                 f"use {ds} (compose or extend it) rather than a parallel copy",
                                 group=ds, component=ds, local=local))
                    continue
                fam = [n for n in inv if len(n) >= 4 and local.endswith(n) and local != n]
                if fam:
                    ds = max(fam, key=len)
                    if any(ds in name for name in imported):
                        continue  # it composes the DS component (or a DS relative: ConfirmDialog) — the right move
                    self.add(_mk("shadow-component", "info", self.filename, self.lines, off,
                                 f"{local} is a local *{ds} variant that doesn't use the DS {ds}",
                                 f"build it on the DS {ds} (a variant/prop), or propose the variant to the DS",
                                 group=f"*{ds} family", component=ds, local=local))

    def rule_foreign_imports(self) -> None:
        for m in _IMPORT_RE.finditer(self.masked):
            spec = m.group(2)
            for rx, label in FOREIGN_UI:
                if rx.search(spec):
                    self.add(_mk("foreign-ui-lib", "warn", self.filename, self.lines, m.start(2),
                                 f"imports {spec} ({label}) — a second component system alongside the DS",
                                 "use the DS equivalent; if it has none, file the gap rather than mixing kits",
                                 group=label, package=spec))
                    break


def _minified(text: str) -> bool:
    lines = text.count("\n") + 1
    return len(text) > 2000 and len(text) / lines > 400


def audit_text(text: str, filename: str, vocab, inventory=None, rules=None, ctx: AuditContext | None = None) -> list[dict]:
    """Audit one file's text → findings (consumer lane only; the ds-lane aggregates come from
    ``aggregate``/``finalize``). ``filename`` picks the language by extension (default CSS)
    and is echoed into each finding. ``rules``: iterable of rule ids to run (None = all).
    Pass a shared ``ctx`` across files; one is made for you otherwise."""
    ctx = ctx or AuditContext(inventory=inventory, rules=rules)
    base = Path(filename or "").name.lower()
    if base.endswith("tokens.css"):
        ctx.skipped.append(f"{filename}: the token file itself")
        return []
    if _minified(text):
        ctx.skipped.append(f"{filename}: minified")
        return []
    ignored, per_line = suppressions(text)
    if ignored:
        ctx.skipped.append(f"{filename}: ds-audit-ignore-file")
        return []
    if not ctx.local_vars:
        ctx.collect_definitions(text, filename)
    ctx.files_scanned += 1
    ctx.lines_scanned += text.count("\n") + 1
    findings = _FileScan(text, filename, vocab, ctx).run()
    if per_line:
        findings = [f for f in findings if not (f["line"] in per_line and (per_line[f["line"]] is None or f["rule"] in per_line[f["line"]]))]
    return findings


# ── ds-lane aggregates, score, reports ────────────────────────────────────────


def aggregate(ctx: AuditContext, vocab) -> list[dict]:
    """The design-system-lane findings, built from what every scanned file contributed."""
    out: list[dict] = []
    if "missing-scale" in ctx.rules:
        for kind, occ in sorted(ctx.missing.items(), key=lambda kv: -len(kv[1])):
            values: dict[str, int] = {}
            for o in occ:
                values[o["value"]] = values.get(o["value"], 0) + 1
            top = sorted(values.items(), key=lambda kv: -kv[1])
            have = vocab.scale(kind) if kind != "shadow" else [(n, 0) for n, _ in vocab.shadows]
            files = sorted({o["file"] for o in occ})
            out.append({
                "rule": "missing-scale", "severity": "warn", "lane": "ds", "file": "", "line": 0, "col": 0, "snippet": "",
                "group": f"missing-scale: {kind}",
                "message": (f"the design system has no {vocab_mod.SCALE_LABEL[kind]} scale "
                            f"({'only ' + ', '.join(n for n, _ in have) if have else 'no tokens at all'}), "
                            f"so the app hardcodes it {len(occ)}× across {len(files)} file(s) with {len(values)} distinct values"),
                "suggestion": "DS: add a token scale covering the values in use (most common: "
                              + ", ".join(f"{v} ×{c}" for v, c in top[:8]) + "); then migrate consumers to it",
                "count": len(occ), "values": dict(top), "files": files[:50],
                "evidence": [{"file": o["file"], "line": o["line"], "snippet": f"{o['prop']}: {o['value']}"} for o in occ],
            })
    if "palette-gap" in ctx.rules:
        for norm, occ in sorted(ctx.far_colors.items(), key=lambda kv: -len(kv[1])):
            files = sorted({o["file"] for o in occ})
            if len(occ) < 3 or len(files) < 2:
                continue
            o0 = occ[0]
            out.append({
                "rule": "palette-gap", "severity": "info", "lane": "ds", "file": "", "line": 0, "col": 0, "snippet": "",
                "group": f"palette-gap: {norm}",
                "message": f"`{norm}` is used {len(occ)}× in {len(files)} files and no token is close (nearest {vocab.ref(o0['nearest'])}, ΔE {o0['delta_e']:g})",
                "suggestion": "DS: decide whether this is a missing role token (add it) or an off-brand color (consumers should move to the nearest role)",
                "count": len(occ), "files": files[:50],
                "evidence": [{"file": o["file"], "line": o["line"], "snippet": o["literal"]} for o in occ],
            })
    if "override-hotspot" in ctx.rules:
        for cls, occ in sorted(ctx.overrides.items(), key=lambda kv: -len(kv[1])):
            files = sorted({o["file"] for o in occ})
            if len(occ) < 3 or len(files) < 2:
                continue
            out.append({
                "rule": "override-hotspot", "severity": "info", "lane": "ds", "file": "", "line": 0, "col": 0, "snippet": "",
                "group": f"override-hotspot: {cls}",
                "message": f"{cls} is restyled in {len(occ)} rule blocks across {len(files)} files — consumers keep needing something the component doesn't offer",
                "suggestion": f"DS: read the overrides and add the variant/prop/token they're reaching for on the component behind {cls}",
                "count": len(occ), "files": files[:50],
                "evidence": [{"file": o["file"], "line": o["line"], "snippet": o["selector"][:140]} for o in occ],
            })
    return out


SCORE_FORMULA = (
    "score = 100 × good / (good + penalty), where good = valid var(--pl-*) references + names "
    "imported from the DS packages, and penalty = Σ consumer findings weighted error 3, warn 1, "
    "info 0.25. DS-lane findings don't lower the score — they're the design system's to fix."
)


def score(findings: list[dict], ctx: AuditContext) -> int:
    penalty = sum(SEVERITY_WEIGHT.get(f["severity"], 1.0) for f in findings if f.get("lane") == "consumer")
    good = ctx.token_refs + ctx.ds_imports
    if good + penalty == 0:
        return 100
    return round(100 * good / (good + penalty))


def summarize(findings: list[dict], ctx: AuditContext, top_n: int = 10) -> dict:
    by_rule: dict[str, int] = {}
    by_sev: dict[str, int] = {"error": 0, "warn": 0, "info": 0}
    by_lane: dict[str, int] = {"consumer": 0, "ds": 0}
    by_file: dict[str, int] = {}
    for f in findings:
        n = int(f.get("count", 1)) if f.get("lane") == "ds" else 1
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + n
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
        by_lane[f["lane"]] = by_lane.get(f["lane"], 0) + 1
        if f.get("file"):
            by_file[f["file"]] = by_file.get(f["file"], 0) + 1
    return {
        "score": score(findings, ctx),
        "score_formula": SCORE_FORMULA,
        "files_scanned": ctx.files_scanned,
        "lines_scanned": ctx.lines_scanned,
        "token_refs": ctx.token_refs,
        "ds_imports": ctx.ds_imports,
        "findings": len(findings),
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "by_severity": by_sev,
        "by_lane": by_lane,
        "top_files": sorted(by_file.items(), key=lambda kv: -kv[1])[:top_n],
        "skipped": ctx.skipped[:50],
    }


def finalize(findings: list[dict], ctx: AuditContext, vocab) -> tuple[list[dict], dict]:
    """Consumer findings + the ds-lane aggregates, and the summary over both."""
    allf = findings + aggregate(ctx, vocab)
    return allf, summarize(allf, ctx)


def _group(findings: list[dict]) -> dict[str, dict[str, list[dict]]]:
    out: dict[str, dict[str, list[dict]]] = {}
    for f in findings:
        out.setdefault(f["rule"], {}).setdefault(f.get("group") or f["rule"], []).append(f)
    return out


def render_markdown(findings: list[dict], summary: dict, title: str = "Design-system audit", per_group: int = 5, groups_per_rule: int = 12, vocab=None) -> str:
    """Human/agent report: DS lane first (what the design system must fix), then the consumer
    lane, each rule grouped by theme with file:line evidence, capped with "+N more"."""
    s = summary
    L = [f"# {title}", ""]
    L.append(f"**Adherence score: {s['score']}/100** — {s['files_scanned']} files, {s['token_refs']} token references, "
             f"{s['ds_imports']} DS component imports. Findings: {s['by_lane'].get('consumer', 0)} consumer · {s['by_lane'].get('ds', 0)} design-system gaps "
             f"({s['by_severity'].get('error', 0)} error, {s['by_severity'].get('warn', 0)} warn, {s['by_severity'].get('info', 0)} info).")
    L.append("")
    L.append(f"<sub>{s['score_formula']}</sub>")
    if vocab is not None and vocab.missing_scales():
        L.append("")
        L.append("DS has no token scale for: " + ", ".join(vocab_mod.SCALE_LABEL[k] for k in vocab.missing_scales()) + ".")
    L.append("")
    L.append("| rule | lane | count |")
    L.append("|---|---|---|")
    for rule, n in s["by_rule"].items():
        L.append(f"| `{rule}` | {RULES.get(rule, {}).get('lane', '?')} | {n} |")
    if s["top_files"]:
        L.append("")
        L.append("Top offending files: " + ", ".join(f"`{p}` ({n})" for p, n in s["top_files"][:8]))
    grouped = _group(findings)
    for lane, heading in (("ds", "Design-system lane — gaps the DS should fix (file in the DS repo)"),
                          ("consumer", "Consumer lane — violations this codebase should fix")):
        rules = [r for r in RULES if RULES[r]["lane"] == lane and r in grouped]
        if not rules:
            continue
        L += ["", f"## {heading}"]
        for rule in rules:
            groups = grouped[rule]
            total = sum(len(g) for g in groups.values())
            L += ["", f"### `{rule}` — {RULES[rule]['summary']} ({total})"]
            ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
            for key, items in ordered[:groups_per_rule]:
                first = items[0]
                if lane == "ds":
                    L.append(f"- **{first['message']}**")
                    L.append(f"  - {first['suggestion']}")
                    ev = first.get("evidence") or []
                    for e in ev[:per_group]:
                        L.append(f"  - `{e['file']}:{e['line']}` `{e['snippet']}`")
                    if len(ev) > per_group:
                        L.append(f"  - +{len(ev) - per_group} more")
                    continue
                L.append(f"- **{key}** ×{len(items)} — {first['message']}" + (f" → {first['suggestion']}" if first.get("suggestion") else ""))
                for f in items[:per_group]:
                    L.append(f"  - `{f['file']}:{f['line']}` `{f['snippet']}`")
                if len(items) > per_group:
                    L.append(f"  - +{len(items) - per_group} more")
            if len(ordered) > groups_per_rule:
                rest = sum(len(g) for _, g in ordered[groups_per_rule:])
                L.append(f"- +{len(ordered) - groups_per_rule} more groups ({rest} findings) — see the JSON report")
    if not findings:
        L += ["", "No findings — this code is on-system. ✓"]
    return "\n".join(L) + "\n"


def render_json(findings: list[dict], summary: dict, root: str = "", vocab=None) -> str:
    return json.dumps({
        "root": root,
        "summary": summary,
        "vocab": vocab.summary() if vocab is not None else None,
        "rules": RULES,
        "findings": findings,
    }, indent=1, default=str)


# ── repo walker (the only file I/O in this module) ────────────────────────────


def _split_globs(globs) -> list[str]:
    if not globs:
        return []
    if isinstance(globs, str):
        globs = re.split(r"[,\n]", globs)
    return [g.strip() for g in globs if g and g.strip()]


def walk(root, include_globs=None, exclude_globs=None, max_files: int = 5000) -> tuple[list[Path], bool]:
    """Auditable files under ``root`` (sorted), and whether ``max_files`` truncated the list.
    Globs match the POSIX path relative to ``root`` with fnmatch (``*`` crosses ``/``)."""
    root = Path(root)
    inc = _split_globs(include_globs)
    exc = list(DEFAULT_EXCLUDE_GLOBS) + _split_globs(exclude_globs)
    files: list[Path] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in DEFAULT_EXCLUDE_DIRS and not d.startswith("."))
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        if rel_dir != "." and any(fnmatch.fnmatch(rel_dir, g.rstrip("/*")) or fnmatch.fnmatch(rel_dir + "/", g) for g in _split_globs(exclude_globs)):
            dirnames[:] = []
            continue
        for fn in sorted(filenames):
            if Path(fn).suffix.lower() not in EXTENSIONS:
                continue
            rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
            if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(fn, g) for g in exc):
                continue
            if inc and not any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(fn, g) for g in inc):
                continue
            if len(files) >= max_files:
                truncated = True
                break
            files.append(Path(dirpath) / fn)
        if truncated:
            break
    return files, truncated


def audit_tree(root, vocab, inventory=None, include_globs=None, exclude_globs=None, max_files: int = 5000, rules=None, ds_packages=DS_PACKAGES) -> dict:
    """Audit every source file under ``root``. Two passes: first every custom property the
    app defines (so its own vars aren't "unknown" and aliases are judged tree-wide), then the
    rules. Returns ``{root, findings, summary, truncated}`` — findings include the ds lane."""
    root = Path(root)
    files, truncated = walk(root, include_globs, exclude_globs, max_files)
    ctx = AuditContext(inventory=inventory, rules=rules, ds_packages=ds_packages)
    texts: list[tuple[str, str]] = []
    for p in files:
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                ctx.skipped.append(f"{p.relative_to(root).as_posix()}: larger than {MAX_FILE_BYTES} bytes")
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            ctx.skipped.append(f"{p}: {e}")
            continue
        rel = p.relative_to(root).as_posix()
        texts.append((rel, text))
        ctx.collect_definitions(text, rel)
    findings: list[dict] = []
    for rel, text in texts:
        findings.extend(audit_text(text, rel, vocab, ctx=ctx))
    allf, summary = finalize(findings, ctx, vocab)
    summary["truncated"] = truncated
    summary["max_files"] = max_files
    return {"root": str(root), "findings": allf, "summary": summary, "truncated": truncated}
