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
                       (a composite <button>: class + >=2 element children is exempt — protoContent#551)
  shadow-component     a local component that re-implements a DS one (same name, or its root class) without using it
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
import stat
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
    "namespace-squat": {"lane": "consumer", "summary": "App-defined custom properties inside the DS's --pl-* namespace"},
    "missing-scale": {"lane": "ds", "summary": "The DS has no token scale for a property the app sets by hand"},
    "scale-gap": {"lane": "ds", "summary": "A DS scale missing a step the app uses over and over"},
    "palette-gap": {"lane": "ds", "summary": "A color the app keeps needing that no token is close to"},
    "override-hotspot": {"lane": "ds", "summary": "A DS class overridden so often it needs a variant/prop"},
}
AGGREGATE_RULES = ("missing-scale", "scale-gap", "palette-gap", "override-hotspot")
# The order a reader should work the consumer lane in: broken first, forks next, polish last.
CONSUMER_PRIORITY = ("unknown-token", "stale-fallback", "ds-class-override", "legacy-alias", "namespace-squat",
                     "foreign-ui-lib", "shadow-component", "raw-color", "hand-rolled-control", "off-scale-length")

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
    "__tests__", "__mocks__", "__fixtures__", "tests", "test", "fixtures", "e2e", "storybook-static",
    ".venv", "venv", "__pycache__",
})  # plus every dot-directory. `public/` IS scanned: hand-written static CSS/HTML lives there.
DEFAULT_EXCLUDE_GLOBS = ("*.min.*", "*.map", "*.snap", "*.test.*", "*.spec.*", "*.d.ts", "*tokens.css", "*.stories.*")
EXTENSIONS = {
    ".css": "css", ".scss": "scss", ".sass": "scss", ".less": "scss",
    ".ts": "js", ".tsx": "js", ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".html": "html", ".htm": "html", ".vue": "sfc", ".svelte": "sfc", ".astro": "sfc",
}
MAX_FILE_BYTES = 1_500_000
DEFAULT_TIME_BUDGET = 60.0  # seconds for a whole audit_tree; past it the audit stops and says so
# A group of identical findings (one token, one literal) stops costing score past this weight:
# 141 copies of `gap: 6px` are one decision, not 141.
GROUP_WEIGHT_CAP = 10.0

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


def mask_strings(text: str) -> str:
    """Blank the CONTENTS of JS string literals ('…', "…", `…`), keeping the quotes, length
    and newlines. Quoted strings stop at a newline, which contains the damage from an
    apostrophe in JSX text."""
    out = list(text)
    n, i = len(text), 0
    while i < n:
        c = text[i]
        if c in "'\"`":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == c or (c != "`" and text[j] == "\n"):
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
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
    # Split on "\n" ONLY — the same line model as findings (_Lines). splitlines() also breaks
    # on \f, \x85, U+2028 and lone \r, which would shift every suppression below them.
    for ln, line in enumerate(text.split("\n"), 1):
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
        self.off_scale: dict[str, list[dict]] = {}   # scale kind → off-scale occurrences (scale-gap)
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
# A DECIDED (fine-grained) spacing scale ships half-steps, so a value bracketed by two steps this
# close apart is a snap the DS already ruled on (#547), not a scale question. A coarse scale whose
# natural step IS this wide keeps today's off-scale/scale-gap behaviour (see ``_judge_length``).
_RULED_OUT_GAP = 4.0
# A literal border-radius at or above this is a pill: snap it to the pill token when the scale ships
# one, rather than snapping it to the largest finite step (#525).
_PILL_RADIUS_PX = 100.0
_JS_STYLE_PROPS = re.compile(
    r"(?<![\w$.-])(fontSize|borderRadius|border(?:Top|Bottom)(?:Left|Right)Radius|gap|rowGap|columnGap|margin(?:Top|Right|Bottom|Left|Block|Inline)?|padding(?:Top|Right|Bottom|Left|Block|Inline)?|boxShadow|color|backgroundColor|background|borderColor|fill|stroke|outlineColor)\s*:\s*"
)
_KEBAB_VALUE = re.compile(r"[^;`'\"{}\n]+")
_JS_STYLE_VALUE = re.compile(r"(['\"`])([^'\"`\n]*)\1|(-?\d+(?:\.\d+)?)(?![\w.])")
_URI_END = re.compile(r"['\"`)\s]")
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

    def __init__(self, text: str, filename: str, vocab, ctx: AuditContext, loose: bool = False):
        self.loose = loose
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
                end = _URI_END.search(self.masked, m.end())
                self.excluded.append((m.start(), end.start() if end else len(self.masked)))

        # var() references: (start, end, name, fallback_start|None, fallback_end)
        self.vars: list[tuple[int, int, str, int | None, int]] = []
        for m in _VAR_RE.finditer(self.masked):
            end = _balanced(self.masked, m.start() + 3)
            fb = m.end() if m.group(2) else None
            self.vars.append((m.start(), end, m.group(1), fb, max(end - 1, m.end())))

        self.decls = self._declarations()
        self.selectors = self._selectors() if self.kind in ("css", "scss") else self._style_block_selectors()

        # Offset indexes — every "is this offset inside X?" question is a bisect, not a scan
        # (a linear scan per candidate made a 16k-line stylesheet take minutes).
        self.excluded.sort()
        self._excl_starts = [a for a, _ in self.excluded]
        self.decls.sort(key=lambda d: d[2])
        self._decl_starts = [d[2] for d in self.decls]
        self._fb = sorted((v for v in self.vars if v[3] is not None), key=lambda v: v[3])
        self._fb_starts = [v[3] for v in self._fb]

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
                vm = _KEBAB_VALUE.match(self.masked, vs)
                if not vm or not vm.group(0).strip():
                    continue
                # Only when it reads like CSS: terminated by `;` (a JS object uses `,`).
                after = self.masked[vm.end(): vm.end() + 1]
                if after != ";":
                    continue
                val = vm.group(0).strip()
                vs += len(vm.group(0)) - len(vm.group(0).lstrip())
                out.append((prop.lower() if not prop.startswith("--") else prop, val, vs, vs + len(val)))
                seen.add(vs)
            # React style objects: camelCase props.
            for m in _JS_STYLE_PROPS.finditer(self.masked):
                vs = m.end()
                vm = _JS_STYLE_VALUE.match(self.masked, vs)
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

    def _in_excluded(self, offset: int) -> bool:
        i = bisect.bisect_right(self._excl_starts, offset) - 1
        # Excluded spans don't nest, but check a few back in case two touch.
        return any(self.excluded[k][0] <= offset < self.excluded[k][1] for k in range(i, max(-1, i - 3), -1))

    def _enclosing_var(self, offset: int):
        """The innermost var() whose FALLBACK contains ``offset`` (or None). Fallback spans
        nest (balanced parens), so walking back from the bisect point, the first span that
        contains the offset is the innermost one; the walk is capped — real nesting is shallow."""
        i = bisect.bisect_right(self._fb_starts, offset) - 1
        for k in range(i, max(-1, i - 64), -1):
            v = self._fb[k]
            if v[3] <= offset < v[4]:
                return v
        return None

    def _decl_at(self, offset: int):
        i = bisect.bisect_right(self._decl_starts, offset) - 1
        for k in range(i, max(-1, i - 4), -1):
            d = self.decls[k]
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
        if "namespace-squat" in r:
            self.rule_namespace_squat()
        if self.kind in ("html", "sfc") and "hand-rolled-control" in r:
            self.rule_controls()
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
        # Loose: a pasted fragment with no CSS structure at all ("#ff0000", "bg-[#9b87f2]").
        # Every hex counts except a bare all-digit one (an issue reference).
        loose = self.loose and not self.decls
        out = []
        for m in _HEX_RE.finditer(text):
            off = m.start()
            if css_like and not loose and self._decl_at(off) is None:
                continue  # `#add` in a selector is an id, not a color
            if loose and not css_like and not m.group(1).isdigit():
                out.append((off, m.group(0)))
                continue
            if css_like and loose and m.group(1).isdigit():
                continue
            if not css_like:
                hexd = m.group(1)
                ctx_before = text[max(0, off - 48):off].split("\n")[-1]
                in_decl = self._decl_at(off) is not None
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
            if css_like and not loose and self._decl_at(m.start()) is None:
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
            if off in seen or self._in_excluded(off):
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
        # "Exact" only when the literal IS the token in every theme. Matching one theme of a
        # themed token (#fff = bg-raised's LIGHT value) is a different claim: swapping it in
        # changes what renders in the other theme — fatal for a QR code on white.
        full = [n for n in exact if len(v.themes_matching(n, lit)) == len(v.values(n))]
        partial = [n for n in exact if n not in full]
        severity = "info" if svg else "warn"
        if full:
            name = full[0]
            also = [v.ref(x) for x in full[1:3]]
            msg = f"hardcoded `{lit}` is exactly {v.ref(name)}"
            if partial:
                p0 = partial[0]
                msg += f" (also {v.ref(p0)}'s {'/'.join(v.themes_matching(p0, lit))} value — use that if it's the role meant)"
            sug = f"use {v.ref(name)}" + (f" (same value: {', '.join(also)})" if also else "")
        elif partial:
            name = partial[0]
            which = "/".join(v.themes_matching(name, lit))
            vals = v.values(name)
            others = ", ".join(f"{t} {vals[t]}" for t in vals if t not in v.themes_matching(name, lit))
            msg = f"hardcoded `{lit}` equals {v.ref(name)}'s {which} value only ({others} elsewhere)"
            sug = (f"use {v.ref(name)} if this should follow the theme; if it must stay `{lit}` in every theme "
                   "(a QR code, a brand mark), keep it and add a ds-audit-ignore comment")
            severity = "info"
            exact = partial
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
        f = _mk("raw-color", severity, self.filename, self.lines, off, msg, sug,
                group=(exact[0] if exact else best["var"] if best["distance"] < 5 else norm),
                literal=lit, nearest=best["var"], delta_e=best["delta_e"], exact=bool(exact))
        self.add(f)

    def _opaque_tint(self, lit: str) -> str | None:
        """An OPAQUE token whose RGB matches a translucent literal (ΔE < 2.5) — the literal is
        that token at some alpha, and ``color-mix()`` expresses it without a new literal."""
        rgba = vocab_mod.parse_color(lit)
        best = None
        for m in self.vocab.color_matches(lit, k=len(self.vocab.colors)):
            faces = self.vocab.colors[m["var"]]
            # Every theme's face must be this opaque color — tinting a themed token that only
            # matches in one theme would change the other theme's rendering.
            if m["delta_e"] < 2.5 and all(t[3] >= 0.999 and vocab_mod.color_distance((*rgba[:3], 1.0), t)[0] < 2.5 for t in faces.values()) \
                    and (best is None or m["delta_e"] < best[1]):
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
                if kind == "radius":
                    if tok.endswith("%"):
                        continue  # circle
                    if abs(px) >= 999 and not self.vocab.has_scale("radius"):
                        continue  # a lone radius token has no pill step to snap 999px onto — leave it
                off = vs + toff
                self._judge_length(kind, prop, tok, px, off)

    def _judge_length(self, kind: str, prop: str, tok: str, px: float, off: int) -> None:
        v = self.vocab
        if kind == "radius" and v.has_scale("radius"):
            # The radius scale (#525) is a DECIDED scale, pill and all: an off-token radius is a
            # consumer snap-to-nearest, never a DS scale-gap. (A lone radius token has no scale and
            # still falls to the missing-scale gap below.)
            self._judge_radius(prop, tok, px, off)
            return
        near = v.nearest_length(kind, px)
        if not v.has_scale(kind):
            # One token is a value, not a scale: "font-size: 14px → use --pl-font-base-size" is
            # odd advice when 12/13/11px have nowhere to go. It all belongs to the DS gap.
            self.ctx.missing.setdefault(kind, []).append({"file": self.filename, "line": self.lines.pos(off)[0], "value": tok, "prop": prop, "px": px})
            return
        entries = sorted({(p, n) for n, p in v.scale(kind)})
        gaps = [b[0] - a[0] for a, b in zip(entries, entries[1:])]
        # A DECIDED spacing scale ships a step finer than its base (a half-step). In it an on-token
        # literal is simply on-scale (nothing to fix) and an off-token value bracketed by two steps
        # ≤ 4px apart is a snap the DS ruled on (#547), not a scale question. A coarse scale whose
        # natural step is that wide keeps today's exact-nudge / off-scale / scale-gap behaviour.
        decided = kind == "space" and bool(gaps) and min(gaps) < _RULED_OUT_GAP
        if near and near[2] == 0:
            if decided:
                return  # on-scale in a decided scale — a real step, no finding
            self.add(_mk("off-scale-length", "warn", self.filename, self.lines, off,
                         f"hardcoded {prop}: {tok} is exactly {v.ref(near[0])}",
                         f"use {v.ref(near[0])}", group=f"{kind}: {tok} → {near[0]}", value_px=px, scale=kind))
            return
        below = [e for e in entries if e[0] <= abs(px)]
        above = [e for e in entries if e[0] >= abs(px)]
        if decided and below and above and (above[0][0] - below[-1][0]) <= _RULED_OUT_GAP:
            lo_p, lo_n = below[-1]
            hi_p, hi_n = above[0]
            target = lo_n if (abs(px) - lo_p) <= (hi_p - abs(px)) else hi_n   # ties → the smaller step
            self.add(_mk("off-scale-length", "warn", self.filename, self.lines, off,
                         f"{prop}: {tok} is ruled out by the spacing scale; snap to {v.ref(lo_n)} or {v.ref(hi_n)} (nearest {v.ref(target)})",
                         f"use {v.ref(target)}", group=f"{kind}: {tok}", value_px=px, scale=kind))
            return
        self.ctx.off_scale.setdefault(kind, []).append({"file": self.filename, "line": self.lines.pos(off)[0], "value": tok, "prop": prop, "px": px})
        around = " / ".join(f"{n}={p:g}px" for p, n in ([below[-1]] if below else []) + ([above[0]] if above else []))
        self.add(_mk("off-scale-length", "info", self.filename, self.lines, off,
                     f"{prop}: {tok} is off the {vocab_mod.SCALE_LABEL[kind]} scale (between {around})",
                     f"snap to {v.ref(near[0])}" if near else "use a scale token",
                     group=f"{kind}: {tok}", value_px=px, scale=kind))

    def _judge_radius(self, prop: str, tok: str, px: float, off: int) -> None:
        """The radius scale (#525) is decided, so every off-token radius is a consumer
        snap-to-nearest at ``warn`` and never feeds ``ctx.off_scale`` (no scale-gap). A literal that
        equals a token is an exact match; one ≥ 100px is a pill; every other value names its nearest
        step, breaking ties toward the smaller step (5px → --pl-radius, 7px names both md and lg)."""
        v = self.vocab
        apx = abs(px)
        scale = v.scale("radius")
        pill = next((n for n, p in scale if p >= 999), None)
        exact = next((n for n, p in scale if p == apx), None)
        if exact is not None:
            self.add(_mk("off-scale-length", "warn", self.filename, self.lines, off,
                         f"hardcoded {prop}: {tok} is exactly {v.ref(exact)}",
                         f"use {v.ref(exact)}", group=f"radius: {tok} → {exact}", value_px=px, scale="radius"))
            return
        if pill and apx >= _PILL_RADIUS_PX:
            self.add(_mk("off-scale-length", "warn", self.filename, self.lines, off,
                         f"{prop}: {tok} is a pill radius — snap to {v.ref(pill)}",
                         f"use {v.ref(pill)}", group=f"radius: {tok} → {pill}", value_px=px, scale="radius"))
            return
        steps = sorted({(p, n) for n, p in scale if p < 999})   # ascending px → the smaller step first
        best = min(abs(apx - p) for p, _ in steps)
        near = [n for p, n in steps if abs(abs(apx - p) - best) < 1e-6]
        named = " or ".join(v.ref(n) for n in near)
        self.add(_mk("off-scale-length", "warn", self.filename, self.lines, off,
                     f"{prop}: {tok} is off the border-radius scale — snap to the nearest step {named}",
                     f"use {v.ref(near[0])}", group=f"radius: {tok}", value_px=px, scale="radius"))

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
                         f"hand-rolled box-shadow `{val}` — the DS ships elevation tokens ({', '.join(dict.fromkeys(n for n, _ in v.shadows))})",
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

    def _composite_button(self, tag: str, tag_end: int) -> bool:
        """A raw <button> is SANCTIONED (protoContent#551) when it carries a class/className AND
        wraps a composite of >=2 top-level ELEMENT children — a whole-row drawer item (icon +
        label + trailing meta), an agent-switcher trigger, a history-row toggle. ACTION buttons
        (text-only, a lone icon, or an icon + a text label) stay flagged. Self-closing tags and
        an unmatched </button> fail toward the finding (return False)."""
        if tag_end < 0 or tag.rstrip().endswith("/"):
            return False  # no closing '>' on the opening tag, or self-closing <button/>: no children
        if not re.search(r"\bclass(Name)?\s*=", tag):
            return False  # no class attribute → an action button, keep flagging
        close = self.masked.find("</button", tag_end + 1)
        if close < 0:
            return False  # unmatched </button> → fail toward the finding
        inner = mask_strings(self.masked[tag_end + 1: close])  # blank string bodies so a '>' in an attr can't mis-split a tag
        depth = count = 0
        for tm in re.finditer(r"<(/?)[A-Za-z][\w.\-]*[^>]*?(/?)>", inner):
            if tm.group(1) == "/":
                depth = max(0, depth - 1)
                continue
            if depth == 0:
                count += 1
                if count >= 2:
                    return True
            if tm.group(2) != "/":
                depth += 1
        return count >= 2

    def rule_controls(self) -> None:
        # In JS, a "<button>" inside a string literal is text, not markup: scan with string
        # contents blanked. Templates (HTML/Vue/Svelte) have no JS strings to worry about.
        scan = mask_strings(self.masked) if self.kind == "js" else self.masked
        for m in _CONTROL_RE.finditer(scan):
            el = m.group(1)
            tag_end = self.masked.find(">", m.end())
            tag = self.masked[m.end(): tag_end if tag_end > 0 else m.end() + 200]
            if re.search(r"""\bclass(Name)?\s*=\s*[{'"`]*[^>]*?(?<![\w-])""" + re.escape(self.class_prefix[1:]), tag):
                continue  # styled with the DS's own kit class — on-system (the no-build path)
            if el == "button" and self._composite_button(tag, tag_end):
                continue  # sanctioned composite row/trigger (class + >=2 element children) — protoContent#551
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

    def _uses_ds(self, ds: str, imported: set[str]) -> bool:
        """Does this file COMPOSE the DS component ``ds``? It imports it (or a DS relative
        that embeds its name: ConfirmDialog for Dialog) from the DS package, or renders it
        (``<Surface``, ``<UI.Surface``) — a DS component reached through a local barrel is still
        the DS component."""
        if any(ds in name for name in imported):
            return True
        return re.search(r"<(?:[A-Za-z_$][\w$]*\.)?" + re.escape(ds) + r"(?![\w$])", self.masked) is not None

    def _root_class(self, ds: str, body: str) -> str | None:
        """The DS component's ROOT class (``pl-surface`` / ``pl-mobilenav`` /
        ``pl-mobile-nav``) written by hand in ``body`` — re-implementing its markup. A BEM
        element or modifier (``pl-surface__head``, ``pl-surface--raised``) alone doesn't count:
        restyling a part is ds-class-override's business, not a fork of the whole."""
        pre = self.class_prefix[1:]
        kebab = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", ds).lower()
        for cls in dict.fromkeys((pre + kebab, pre + ds.lower())):
            if re.search(r"(?<![\w-])" + re.escape(cls) + r"(?![\w-])", body):
                return cls
        return None

    def rule_shadow_components(self) -> None:
        """A local component that re-implements a DS component instead of using it. The
        evidence has to be real, because the common case — an app feature that COMPOSES DS
        parts (ChatSurface, MetricGrid, CodeRefChip) — is exactly what the DS is for:

        - its name IS the DS component's (StatusDot), or that name plus a generic affix
          (CustomButton, BaseCard), and the file neither imports nor renders the DS one; or
        - it is named for a DS family (*Surface, *Card) AND hand-writes that component's root
          class (``pl-surface``) without importing or rendering the DS component.

        A name that merely ends in a DS name is never flagged on its own, and a file that
        imports/renders the DS component a local is named after is composing it — not flagged,
        whatever the name."""
        inv = self.ctx.inventory
        if not inv:
            return
        by_lower = {n.lower(): n for n in inv}
        imported = self._ds_imported_names()
        defs: list[tuple[int, str]] = []
        seen: set[str] = set()
        for rx in (_DEF_FUNC_RE, _DEF_CONST_RE, _DEF_CLASS_RE):
            for m in rx.finditer(self.masked):
                if m.group(1) not in seen:
                    seen.add(m.group(1))
                    defs.append((m.start(1), m.group(1)))
        defs.sort()
        for i, (off, local) in enumerate(defs):
            body = self.masked[off: defs[i + 1][0] if i + 1 < len(defs) else len(self.masked)]
            if local.lower() in by_lower:
                ds = by_lower[local.lower()]
                # `import { Markdown as DSMarkdown }` + `function Markdown` is a wrapper, and so is
                # a lazy import of a local module of the same name — neither is a fork here.
                wraps_local = re.search(r"""(?:import\(\s*|\bfrom\s+)['"][^'"]*/""" + re.escape(local) + r"""['"]""", self.masked)
                if ds in imported or wraps_local:
                    continue
                self.add(_mk("shadow-component", "warn", self.filename, self.lines, off,
                             f"local component {local} duplicates the design system's {ds}",
                             f"import {ds} from the DS; if it lacks something, extend the DS component instead of forking it",
                             group=ds, component=ds, local=local, evidence="same name"))
                continue
            stripped = _AFFIXES.sub("", local)
            if stripped != local and stripped.lower() in by_lower:
                ds = by_lower[stripped.lower()]
                if not self._uses_ds(ds, imported):
                    self.add(_mk("shadow-component", "warn", self.filename, self.lines, off,
                                 f"local component {local} looks like a re-implementation of the DS's {ds}",
                                 f"use {ds} (compose or extend it) rather than a parallel copy",
                                 group=ds, component=ds, local=local, evidence="same name + generic affix"))
                continue
            for ds in sorted((n for n in inv if len(n) >= 4 and local.endswith(n)), key=len, reverse=True):
                if self._uses_ds(ds, imported):
                    break  # composes the DS component — the right move
                cls = self._root_class(ds, body)
                if cls:
                    self.add(_mk("shadow-component", "warn", self.filename, self.lines, off,
                                 f"{local} re-implements the DS {ds} — it hand-writes {ds}'s root class `{cls}` instead of rendering <{ds}>",
                                 f"render the DS {ds} (add a variant/prop to it if {local} needs one) rather than copying its markup",
                                 group=ds, component=ds, local=local, evidence=f"root class {cls}"))
                    break

    def rule_namespace_squat(self) -> None:
        """App definitions of ``--pl-*`` names the DS doesn't ship. They silence unknown-token
        for every reference (the app "defines" them), and the DS can ship the same name later
        with a different meaning. Redefining a REAL token (theming) is not squatting."""
        if self.vocab.source != "css":
            return
        for prop, val, vs, ve in self.decls:
            if prop.startswith(self.prefix) and not self.vocab.is_known(prop):
                close = self.vocab.close_names(prop)
                self.add(_mk("namespace-squat", "warn", self.filename, self.lines, vs,
                             f"{prop} is defined by the app inside the design system's {self.prefix}* namespace, but the DS doesn't ship it",
                             (f"did you mean {', '.join(close)}? " if close else "")
                             + "rename it into the app's own namespace (e.g. --app-…), or ask the DS for the token",
                             group=prop))

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


def audit_text(text: str, filename: str, vocab, inventory=None, rules=None, ctx: AuditContext | None = None, loose: bool = False) -> list[dict]:
    """Audit one file's text → findings (consumer lane only; the ds-lane aggregates come from
    ``aggregate``/``finalize``). ``filename`` picks the language by extension (default CSS)
    and is echoed into each finding. ``rules``: iterable of rule ids to run (None = all).
    Pass a shared ``ctx`` across files; one is made for you otherwise. ``loose=True`` is for
    a pasted FRAGMENT of unknown shape: when the text has no CSS declarations at all, every
    hex literal counts (not only ones in a color context)."""
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
    findings = _FileScan(text, filename, vocab, ctx, loose=loose).run()
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
            have = vocab.scale(kind) if kind != "shadow" else [(n, 0) for n in dict.fromkeys(n for n, _ in vocab.shadows)]
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
    if "scale-gap" in ctx.rules:
        for kind, occ in sorted(ctx.off_scale.items(), key=lambda kv: -len(kv[1])):
            by_value: dict[str, list[dict]] = {}
            for o in occ:
                by_value.setdefault(f"{abs(o['px']):g}px", []).append(o)
            recurring = sorted(((val, items) for val, items in by_value.items()
                                if len(items) >= SCALE_GAP_MIN_USES or len({o["file"] for o in items}) >= SCALE_GAP_MIN_FILES),
                               key=lambda kv: -len(kv[1]))
            if not recurring:
                continue
            n = sum(len(items) for _, items in recurring)
            steps = ", ".join(f"{n_}={px:g}px" for n_, px in vocab.scale(kind))
            files = sorted({o["file"] for _, items in recurring for o in items})
            out.append({
                "rule": "scale-gap", "severity": "info", "lane": "ds", "file": "", "line": 0, "col": 0, "snippet": "",
                "group": f"scale-gap: {kind}",
                "message": (f"the {vocab_mod.SCALE_LABEL[kind]} scale ({steps}) has no step for values the app uses "
                            f"over and over: " + ", ".join(f"{val} ×{len(items)}" for val, items in recurring[:8])
                            + f" — {n} uses across {len(files)} files"),
                "suggestion": "DS: decide per value — add the step (it's a real rhythm the product needs) or rule it "
                              "out (then consumers snap to the nearest step); either way the consumer can't settle it alone",
                "count": n, "values": {val: len(items) for val, items in recurring}, "files": files[:50],
                "evidence": [{"file": o["file"], "line": o["line"], "snippet": f"{o['prop']}: {o['value']}"} for _, items in recurring for o in items[:5]],
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


SCALE_GAP_MIN_USES = 10
SCALE_GAP_MIN_FILES = 3

SCORE_FORMULA = (
    "score = 100 × good / (good + penalty), where good = valid var(--pl-*) references + names "
    "imported from the DS packages, and penalty = Σ over consumer finding groups of "
    "min(10, Σ weights) with error 3, warn 1, info 0.25. Only an UNDECIDED off-scale value weighs "
    "0 — the off-scale-length info that's really a DS scale-gap; a value the DS already ruled on "
    "(a snap to a decided step) is a warn and counts. DS-lane findings don't lower the score."
)


def _weight(f: dict) -> float:
    if f.get("rule") == "off-scale-length" and f.get("severity") == "info":
        return 0.0  # only an UNDECIDED off-scale value weighs 0 — it's a question for the DS scale (scale-gap); a decided snap is a warn and counts against the consumer
    return SEVERITY_WEIGHT.get(f.get("severity", "warn"), 1.0)


def score(findings: list[dict], ctx: AuditContext) -> int:
    groups: dict[tuple, float] = {}
    for f in findings:
        if f.get("lane") == "consumer":
            key = (f.get("rule"), f.get("group"))
            groups[key] = groups.get(key, 0.0) + _weight(f)
    penalty = sum(min(GROUP_WEIGHT_CAP, w) for w in groups.values())
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


# ── markdown safety ───────────────────────────────────────────────────────────
# Reports quote untrusted text — a consumer repo's source, or a live page's markup, selectors and
# copy — and they end up pasted into GitHub issues. Quoted text must stay DATA: it can't close a
# fence, open a heading, @mention someone, reference/close an issue (#12, org/repo#12, "Fixes
# #12"), or smuggle a link/image/HTML tag.

_ZWJ = "\u200d"
_SPAN_RE = re.compile(r"(`+)(.+?)\1", re.DOTALL)


def md_neutralize(text: str) -> str:
    """Defuse GitHub autolinks in plain text: ``@name`` and ``#123`` get a zero-width joiner."""
    t = str(text).replace("@", "@" + _ZWJ)
    return re.sub(r"#(?=\d+(?![\w-]))", "#" + _ZWJ, t)  # #123 is an issue ref; #7c8cff is a color


def md_text(text, limit: int = 600) -> str:
    """One line of report prose that may embed untrusted values. Whitespace collapses (no
    newline can start a heading or a fence); outside our own `code` spans, mentions and issue
    refs are neutralized and ``<``, ``[``/``]`` and ``!`` link/image/HTML syntax is escaped."""
    t = " ".join(str(text or "").split())
    if len(t) > limit:
        t = t[: limit - 1] + "…"
    out, pos = [], 0
    for m in _SPAN_RE.finditer(t):
        out.append(_md_plain(t[pos:m.start()]))
        out.append(md_neutralize(m.group(0)))
        pos = m.end()
    out.append(_md_plain(t[pos:]))
    return "".join(out)


def _md_plain(t: str) -> str:
    t = md_neutralize(t)
    return t.replace("<", "&lt;").replace("[", "\\[").replace("]", "\\]")


def md_code(text, limit: int = 300, table: bool = False) -> str:
    """An inline code span that can hold anything: the backtick run is longer than any run in
    the content, newlines collapse, and (in a table cell) ``|`` is escaped."""
    t = " ".join(str(text or "").split())
    if len(t) > limit:
        t = t[: limit - 1] + "…"
    t = md_neutralize(t)
    if table:
        t = t.replace("|", "\\|")
    run = max((len(r) for r in re.findall(r"`+", t)), default=0) + 1
    pad = " " if t.startswith("`") or t.endswith("`") or not t else ""
    return f"{'`' * run}{pad}{t}{pad}{'`' * run}"


def md_fence(content: str, lang: str = "") -> str:
    """A fenced block whose fence is longer than any backtick run inside it (min 3)."""
    run = max(3, max((len(r) for r in re.findall(r"`+", content or "")), default=0) + 1)
    return f"{'`' * run}{lang}\n{content}\n{'`' * run}"


def _fix_first(findings: list[dict], limit: int = 12, per_rule: int = 3) -> list[str]:
    """The groups that are broken or forked, most urgent first — so 18 unknown tokens aren't
    buried under a thousand spacing nits."""
    urgent = {"unknown-token": ("error",), "stale-fallback": ("warn",), "ds-class-override": ("warn",),
              "legacy-alias": ("warn",), "namespace-squat": ("warn",), "foreign-ui-lib": ("warn",),
              "shadow-component": ("warn",), "raw-color": ("warn",)}
    grouped = _group([f for f in findings if f.get("lane") == "consumer" and f["severity"] in urgent.get(f["rule"], ())])
    out = []
    for rule in CONSUMER_PRIORITY:
        groups = sorted(grouped.get(rule, {}).items(), key=lambda kv: -len(kv[1]))
        for key, items in groups[:per_rule]:
            first = items[0]
            where = ", ".join(md_code(_loc(f)) for f in items[:3]) + (f" +{len(items) - 3}" if len(items) > 3 else "")
            out.append(f"- **{first['severity']}** `{rule}` **{md_text(key, 160)}** ×{len(items)} — {md_text(first['message'])} ({where})")
            if len(out) >= limit:
                return out
        if len(groups) > per_rule:
            rest = sum(len(g) for _, g in groups[per_rule:])
            out.append(f"  - +{len(groups) - per_rule} more `{rule}` group(s), {rest} finding(s) — below")
    return out


def _loc(item: dict) -> str:
    """`file:line`, or just the location when there is no line (a URL, a rendered page)."""
    return f"{item['file']}:{item['line']}" if item.get("line") else str(item.get("file") or "")


def render_markdown(findings: list[dict], summary: dict, title: str = "Design-system audit", per_group: int = 5, groups_per_rule: int = 12, vocab=None,
                    registry: dict | None = None, stats_line: str | None = None) -> str:
    """Human/agent report: DS lane first (what the design system must fix), then the consumer
    lane, each rule grouped by theme with file:line evidence, capped with "+N more".

    ``registry`` (default ``RULES``) is the rule table sections are drawn from — the URL
    auditor passes its own, a superset — and ``stats_line`` replaces the repo-shaped header
    line ("N files, N token references…") when the audit wasn't over files."""
    s = summary
    rules_reg = registry or RULES
    L = [f"# {md_text(title, 300)}", ""]
    L.append(stats_line or (f"**Adherence score: {s['score']}/100** — {s['files_scanned']} files, {s['token_refs']} token references, "
             f"{s['ds_imports']} DS component imports. Findings: {s['by_lane'].get('consumer', 0)} consumer · {s['by_lane'].get('ds', 0)} design-system gaps "
             f"({s['by_severity'].get('error', 0)} error, {s['by_severity'].get('warn', 0)} warn, {s['by_severity'].get('info', 0)} info)."))
    L.append("")
    L.append(f"<sub>{s['score_formula']}</sub>")
    if vocab is not None and vocab.missing_scales():
        L.append("")
        L.append("DS has no token scale for: " + ", ".join(vocab_mod.SCALE_LABEL[k] for k in vocab.missing_scales()) + ".")
    fix_first = _fix_first(findings)
    if fix_first:
        L += ["", "## Fix first"]
        L += fix_first
    L.append("")
    L.append("| rule | lane | count |")
    L.append("|---|---|---|")
    # Priority order, not count order: a thousand spacing nits mustn't sit above 18 broken tokens.
    order = [r for r in CONSUMER_PRIORITY if r in s["by_rule"]] + [r for r in s["by_rule"] if r not in CONSUMER_PRIORITY]
    for rule in order:
        L.append(f"| `{rule}` | {rules_reg.get(rule, {}).get('lane', '?')} | {s['by_rule'][rule]} |")
    if s["top_files"]:
        L.append("")
        L.append("Top offending files: " + ", ".join(f"{md_code(p)} ({n})" for p, n in s["top_files"][:8]))
    grouped = _group(findings)
    for lane, heading in (("ds", "Design-system lane — gaps the DS should fix (file in the DS repo)"),
                          ("consumer", "Consumer lane — violations this codebase should fix")):
        order = list(CONSUMER_PRIORITY) + [r for r in rules_reg if r not in CONSUMER_PRIORITY]
        rules = [r for r in order if rules_reg.get(r, {}).get("lane") == lane and r in grouped]
        if not rules:
            continue
        L += ["", f"## {heading}"]
        for rule in rules:
            groups = grouped[rule]
            total = sum(len(g) for g in groups.values())
            # The count table is over EVERY finding; a capped reply (ds_audit_repo's max_findings)
            # lists fewer. Say so, rather than printing two different numbers for one rule.
            full = s["by_rule"].get(rule, total) if lane == "consumer" else total
            count = f"{total}" if full == total else f"{total} of {full} shown — the report files hold all {full}"
            L += ["", f"### `{rule}` — {rules_reg[rule]['summary']} ({count})"]
            ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
            for key, items in ordered[:groups_per_rule]:
                first = items[0]
                if lane == "ds":
                    L.append(f"- **{md_text(first['message'])}**")
                    L.append(f"  - {md_text(first['suggestion'])}")
                    ev = first.get("evidence") or []
                    for e in ev[:per_group]:
                        L.append(f"  - {md_code(_loc(e))} {md_code(e['snippet'])}")
                    if len(ev) > per_group:
                        L.append(f"  - +{len(ev) - per_group} more")
                    continue
                L.append(f"- **{md_text(key, 160)}** ×{len(items)} — {md_text(first['message'])}" + (f" → {md_text(first['suggestion'])}" if first.get("suggestion") else ""))
                for f in items[:per_group]:
                    L.append(f"  - {md_code(_loc(f))} {md_code(f['snippet'])}")
                if len(items) > per_group:
                    L.append(f"  - +{len(items) - per_group} more")
            if len(ordered) > groups_per_rule:
                rest = sum(len(g) for _, g in ordered[groups_per_rule:])
                L.append(f"- +{len(ordered) - groups_per_rule} more groups ({rest} findings) — see the JSON report")
    if not findings:
        L += ["", "No findings — this code is on-system. ✓"]
    return "\n".join(L) + "\n"


def render_json(findings: list[dict], summary: dict, root: str = "", vocab=None, registry: dict | None = None) -> str:
    return json.dumps({
        "root": root,
        "summary": summary,
        "vocab": vocab.summary() if vocab is not None else None,
        "rules": registry or RULES,
        "findings": findings,
    }, indent=1, default=str)


# ── repo walker (the only file I/O in this module) ────────────────────────────


def _split_globs(globs) -> list[str]:
    if not globs:
        return []
    if isinstance(globs, str):
        globs = re.split(r"[,\n]", globs)
    return [g.strip() for g in globs if g and g.strip()]


def walk(root, include_globs=None, exclude_globs=None, max_files: int = 5000, skipped: list | None = None) -> tuple[list[Path], bool]:
    """Auditable files under ``root`` (sorted), and whether ``max_files`` truncated the list.
    Globs match the POSIX path relative to ``root`` with fnmatch (``*`` crosses ``/``).

    Only REGULAR files are returned: symlinks (which could point outside ``root`` — at a
    secret, a FIFO, /dev/zero) and every other special file are skipped, with the reason
    appended to ``skipped`` when given. Directory symlinks are never descended into."""
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
            try:
                st = os.lstat(os.path.join(dirpath, fn))
            except OSError as e:
                if skipped is not None:
                    skipped.append(f"{rel}: {e.strerror or e}")
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                if skipped is not None:
                    skipped.append(f"{rel}: {'symlink' if stat.S_ISLNK(st.st_mode) else 'not a regular file'} — not followed")
                continue
            if len(files) >= max_files:
                truncated = True
                break
            files.append(Path(dirpath) / fn)
        if truncated:
            break
    return files, truncated


def read_bounded(path, limit: int = MAX_FILE_BYTES) -> str | None:
    """A regular file's text, or ``None`` when it is larger than ``limit``. Never follows a
    symlink (O_NOFOLLOW), never blocks on a FIFO (O_NONBLOCK + an fstat S_ISREG check), and
    never trusts ``st_size`` (/dev/zero reports 0): it reads at most ``limit + 1`` bytes.
    Raises ``OSError`` for anything that isn't a readable regular file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(str(path), flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path}: not a regular file")
        chunks, total = [], 0
        while total <= limit:
            chunk = os.read(fd, min(1 << 16, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    if total > limit:
        return None
    return b"".join(chunks).decode("utf-8", errors="replace")


def audit_tree(root, vocab, inventory=None, include_globs=None, exclude_globs=None, max_files: int = 5000, rules=None, ds_packages=DS_PACKAGES, time_budget: float = DEFAULT_TIME_BUDGET) -> dict:
    """Audit every source file under ``root``. Two passes: first every custom property the
    app defines (so its own vars aren't "unknown" and aliases are judged tree-wide), then the
    rules. Returns ``{root, findings, summary, truncated}`` — findings include the ds lane.
    ``time_budget`` (seconds) bounds the whole audit: past it, remaining files are skipped,
    ``truncated`` is set and ``summary["timed_out"]`` says so."""
    import time

    root = Path(root)
    ctx = AuditContext(inventory=inventory, rules=rules, ds_packages=ds_packages)
    files, truncated = walk(root, include_globs, exclude_globs, max_files, skipped=ctx.skipped)
    deadline = time.monotonic() + max(0.0, float(time_budget or 0)) if time_budget else None
    timed_out = False
    texts: list[tuple[str, str]] = []
    for p in files:
        rel = p.relative_to(root).as_posix()
        if deadline is not None and time.monotonic() > deadline:
            timed_out = True
            break
        try:
            text = read_bounded(p)
        except OSError as e:
            ctx.skipped.append(f"{rel}: {e.strerror or e}")
            continue
        if text is None:
            ctx.skipped.append(f"{rel}: larger than {MAX_FILE_BYTES} bytes")
            continue
        texts.append((rel, text))
        ctx.collect_definitions(text, rel)
    findings: list[dict] = []
    for i, (rel, text) in enumerate(texts):
        if deadline is not None and time.monotonic() > deadline:
            timed_out = True
            ctx.skipped.append(f"time budget of {time_budget:g}s reached — {len(texts) - i} read file(s) not audited")
            break
        findings.extend(audit_text(text, rel, vocab, ctx=ctx))
    allf, summary = finalize(findings, ctx, vocab)
    truncated = truncated or timed_out
    summary["truncated"] = truncated
    summary["timed_out"] = timed_out
    summary["max_files"] = max_files
    return {"root": str(root), "findings": allf, "summary": summary, "truncated": truncated}
