# design-system-plugin

The agent's **live window into a design system**. A protoAgent plugin
([ADR 0027](https://github.com/protoLabsAI/protoAgent)) — a first-party domain capability on
top of the [frontend-bundle](https://github.com/protoLabsAI/frontend-bundle).

> **Private** — reads a private repo (protoContent) via the GitHub API with `GH_TOKEN`.

## Why this exists — read live, don't cache

The design system is a **live source of truth**: `@protolabsai/design` owns the brand *values*
(`src/tokens.js` → built `dist/tokens.json` → `--pl-*` CSS vars + a Tailwind preset),
`packages/ui` owns the components, `docs/reference/visual-identity.md` owns the *rules*. Freezing
any of that into an agent's knowledge base guarantees drift. So this plugin reads it **straight
from the repo at call time** — the anti-drift principle, as tools.

## Tools

| Tool | What it returns |
|---|---|
| `ds_tokens [section]` | the live token vocabulary — every `--pl-*` var with its dark and light value, read from the generated `tokens.css`; pass a section (`Color`, `Space`, …) to fetch one family |
| `ds_components` | the component inventory from `packages/ui/src` — every public export grouped by the module you import it from, plus the story files |
| `ds_component <name>` | one component's API by name — JSDoc + signature + props type read from the module that exports it (`MobileNav` → `@protolabsai/ui/app-shell`), a story usage excerpt; a partial name lists the closest exports (`Toast` → `ToastProvider`, `useToast`) |
| `ds_stories` | the published Storybook inventory: every component and every variant name |
| `ds_story <name>` | one component's variants, each with a **live render URL** you can show the user |
| `ds_rules` | the visual-identity rules (when to use what, what we don't do) |
| `ds_search <keyword>` | public exports, Storybook components/variants **and** tokens matching a keyword — the fastest "do we have a…" |
| `ds_kit_classes` | the `.pl-*` classes the DS's published kit stylesheet actually defines — the vocabulary a no-build prototype can use |
| `ds_check <code> [filename]` | lints a snippet with the audit engine (every rule below) → the token / component to use instead, with ΔE for near-miss colors |
| `ds_audit_repo <path> [include] [exclude] [rules] [max_findings]` | audits a **local checkout** (or an onboarded `owner/repo`) for design-system adherence → a 0-100 score and a report split into **DS gaps** vs **consumer fixes**, written as `.md` + `.json` to the plugin data dir |
| `ds_site_probe_script [script_only]` | the in-page **probe** the agent runs with `browser_eval` on a live site (`browser_open` first) — returns ≤ 30 KB of JSON: computed style usage, text/ground pairs, `:root` vars, DS class usage, landmarks, forms and the page's **repeated UI patterns** |
| `ds_audit_url <url_or_probe> [rules]` | audits a **live site** — from the probe (rendered truth: colors → nearest token by ΔE, type/radius/spacing/shadow vs the scales, `--pl-*` adoption + token drift, WCAG contrast, unnamed controls) or, as a **static** fallback, from a URL (HTML + stylesheets through the CSS rules) → the same score / lanes / report as `ds_audit_repo` |
| `ds_component_gaps <probe>` | breaks a probed site into repeated patterns and classifies each **COVERED / VARIANT GAP / MISSING / UNCLASSIFIED** against the DS inventory — proposed name + props API, priority, and ready-to-file gap issues |
| `ds_drift` | what changed since the last check (tokens + components); updates a snapshot |
| `theme_scale` / `theme_contrast` / `theme_palette` / `theme_apply` | the LCH theme-designer engine: an 11-step scale, a WCAG check, a harmony palette, and persisting `{mode, overrides}` as this console's theme |
| `theme_extract <source>` | brand signals from a **URL** (static HTML + same-site/CDN stylesheets, no JS), raw **CSS**, a rendered **probe JSON**, or a **hex list** → ranked brand candidates with evidence, ground/text, fonts, radius, and suggested seeds with a confidence |
| `theme_probe_script` | the in-page probe (`PROBE_JS`) — run it with `browser_eval` on a JS-rendered site and pass its JSON to `theme_extract` |
| `theme_generate <primary> [secondary] [neutral] [name] [scope] [font_family] [radius]` | a full **dark + light** theme for every `--pl-color-*` the LIVE `tokens.css` defines, AA-checked and repaired. Returns a compact preview to hand `show_artifact` verbatim, ready `theme_apply` maps, and the URL of the full preview (`/plugins/design-system/themes/<name>/preview`); writes `<name>.theme.css` (the DS's 4-block shape, scope-able for white-label), `<name>.theme.json` and `<name>.preview.html` to the plugin's instance data dir |

## Theming from a brand

`theme_extract` → `theme_generate` → `show_artifact` the preview → iterate → deliver. Pure logic
lives in `themegen.py` (stdlib + the in-repo `colorcore`; no new dependencies — the frozen desktop
host can't install wheels).

- **The var list is never hardcoded.** `theme_generate` reads the live `tokens.css` and covers
  every `--pl-color-*` it defines. Each var's role comes from its NAME (a role table: ground,
  surface, text tiers, on-accent, accent + states, focus, status, chart series, lines, scrims,
  brand marks) and its lightness structure from the DS's OWN default in each theme — so the
  elevation relation (dark: raised lighter than the ground; light: raised is the lightest) and the
  text tiers carry over by construction. A var the table doesn't know keeps its hue relation to the
  default accent, rotated onto the brand.
- **Accent per theme.** The brand if it clears 3:1 on the ground, takes an AA on-accent and sits
  in the DS's lightness band for that theme; otherwise the nearest `theme.scale` step that does
  (pure yellow → a deep olive on light), preferring the DS's own on-accent polarity.
- **Contrast is a gate, not a report.** Text 4.5:1, tertiary text / accent / focus / status /
  chart series 3:1, against every surface. A failing pair is repaired by nudging LCH lightness and
  every repair is listed (before → after → why). Borders are decorative and not gated. Status
  colors default to 3:1 because the DS intends them as hue marks; `generate(strict_status=True)`
  holds them to 4.5:1.
- **Output.** `<name>.theme.css` mirrors the DS's own build: `:root` (dark default) →
  `@media (prefers-color-scheme: light)` → `:root[data-theme="light"]` → `:root[data-theme="dark"]`,
  themed blocks carrying only what differs. Pass `scope='[data-brand="acme"]'` for a white-label
  scope. `<name>.theme.json`'s `dark`/`light` maps are `theme_apply`-ready. `<name>.preview.html`
  shows both themes side by side using the kit's `.pl-*` classes (zero-specificity fallbacks, so it
  is fully styled without the kit) plus the contrast table.
- **Fetching is hardened** (`fetch.py`, shared by any tool that fetches an operator-supplied URL):
  only globally-routable addresses (an allowlist: loopback, RFC1918, link-local, CGNAT/Tailscale
  `100.64/10`, ULA and v4-in-v6 forms are refused); the connection is pinned to the vetted IP
  (TLS still verifies the hostname), so DNS rebinding gets nothing; every redirect is re-vetted;
  one wall-clock deadline bounds the whole extract; decompressed bytes are capped as they stream.
- **Confidence is earned.** `high` needs a real score, at least two INDEPENDENT kinds of evidence
  (custom property, button fill, theme-color meta, links) and a 2× margin over the runner-up; a
  URL read that got no stylesheet is capped at `low`. Status/text-named props
  (`--brand-color-danger-fg`) never count as brand, and the page's ground and body-text colors
  are removed from the brand candidates.
- **Rendered sites.** A static read can't see CSS-in-JS or runtime themes. `theme_probe_script`
  returns a self-contained expression that samples visible elements' computed colors weighted by
  rendered area; run it with `browser_eval` and hand the JSON to `theme_extract`.

## Auditing a repo — `ds_audit_repo`

The same rule engine that powers `ds_check` walks a local checkout and reports how well it
upholds the design system. Two pure modules do the work, so a URL auditor can reuse them on
fetched CSS and computed styles:

- **`vocab.py`** — the token vocabulary built from the DS's `tokens.css` (themes resolved
  structurally by `tokens.parse_css`): every var name, per-theme values, value → var reverse
  maps, `nearest_color` by **CIEDE2000** (the DS's `oklch()` tokens included), and the
  length scales (space / radius / font-size / shadow) with `nearest_length`. A scale needs
  ≥ 2 distinct tokens; a DS with one radius token has a *value*, not a scale, and the auditor
  says so once instead of flagging every radius.
- **`audit.py`** — the rules, the repo walker and the report renderers.
- **`components.py`** — the component index behind `ds_component` / `ds_search` / the
  shadow-component inventory: the package's `exports` map says which modules are public, each
  module's `export` statements say what it ships (re-exports followed), and the API is read
  from the source.

| Rule | Lane | What it catches |
|---|---|---|
| `raw-color` | consumer | literal colors (hex / rgb / hsl / oklch / named) instead of `var(--pl-*)` — exact token, near token (ΔE), token-at-alpha (`color-mix`), or no close token |
| `stale-fallback` | consumer | `var(--pl-x, <fallback>)` whose fallback matches the token in **no** theme |
| `unknown-token` | consumer | `var(--pl-foo)` the DS doesn't define (typo / removed) |
| `off-scale-length` | consumer | hardcoded font-size / radius / gap-margin-padding / box-shadow where a token scale exists |
| `ds-class-override` | consumer | app CSS whose selector **subject** is a DS class (`.x .pl-dialog__body {}`) — forking the component |
| `legacy-alias` | consumer | app custom properties (`--brand-indigo: #6366f1`, or `var(--brand-indigo, #6366f1)`) duplicating a token's value |
| `hand-rolled-control` | consumer | raw `<button>`/`<input>`/`<select>`/`<textarea>`/`<dialog>` in JSX when the DS ships the component (a composite `<button>` — a class **and** ≥2 element children, e.g. icon + label + meta — is exempt per protoContent#551) |
| `shadow-component` | consumer | a local component that re-implements a DS one: the same name (`StatusDot`, or with a generic affix: `CustomCard`) without importing it, or a `*Surface`/`*Card` that hand-writes the DS root class (`pl-surface`) instead of rendering it. A component that imports or renders the DS component it's named after (`ChatSurface`, `MetricGrid`) is composition, never flagged |
| `foreign-ui-lib` | consumer | imports of MUI, Chakra, antd, shadcn `@/components/ui`, raw `@radix-ui/*`, Bootstrap, … |
| `namespace-squat` | consumer | the app defines `--pl-*` names the DS doesn't ship (silences `unknown-token`, collides later) |
| `missing-scale` | **ds** | the DS has no scale for a property the app sets by hand — ONE finding with the value histogram |
| `scale-gap` | **ds** | a DS scale lacks a step the app uses ≥ 10× or in ≥ 3 files (`gap: 6px` ×141) |
| `palette-gap` | **ds** | a color with no close token, used ≥ 3× across ≥ 2 files |
| `override-hotspot` | **ds** | a DS class overridden in ≥ 3 blocks across ≥ 2 files — the component needs a variant/prop |

**Lanes** are the point: `ds` findings get filed on the design-system repo ("add a type
scale"), `consumer` findings on the audited repo ("use `var(--pl-color-accent)`").

**Adherence score** = `100 × good / (good + penalty)`, where *good* = valid `var(--pl-*)`
references + names imported from the DS packages, and *penalty* = the sum over consumer
finding **groups** of `min(10, Σ weights)` with error 3 · warn 1 · info 0.25 — off-scale info
weighs 0 (it's the DS's `scale-gap`), and 141 copies of one value are one decision, not 141.
DS-lane findings don't lower it — they aren't the consumer's to fix. The markdown report
opens with a **Fix first** block (broken tokens, stale fallbacks, forks) before the rule table.

A color literal is called *exact* only when it equals the token in **every** theme; matching
one theme of a themed token (`#fff` = a light-mode surface) is reported as info, because
swapping it in changes what the other theme renders.

**Path safety.** `ds_audit_repo` reads only under the host's project-onboarding root, a
registered project / work folder (ADR 0095 / 0007), or this plugin's `audit_roots` setting —
the path is `resolve()`d first, so `..` and symlinks can't escape. Given an `owner/repo` that
isn't checked out it tells the agent to run `onboard_project` first; it never clones.

**Low false positives by construction:** comments are masked (positions preserved), `url()` /
`data:` URIs are skipped, a bare `#123`/`#add` in TS is an issue ref or anchor unless it sits
in a color context, `<button>` inside a JS string is text, SVG presentation colors are `info`.
The walker never reads dot-dirs, `node_modules`, `dist`, `build`, `out`, vendored code,
coverage, test dirs (`tests`, `__tests__`, `e2e`, `fixtures`, …), stories, snapshots,
minified files or the token file (`public/` is scanned). It reads **regular files only** —
symlinks and FIFOs/devices are skipped, reads are bounded (1.5 MB) and never trust `st_size` —
and the whole audit has a 60 s budget, after which it stops and says so. Suppress a
deliberate line with a `ds-audit-ignore` comment (optionally `ds-audit-ignore raw-color`),
`ds-audit-ignore-next-line`, or a whole file with `ds-audit-ignore-file`.

The workflow — onboard → audit → triage by lane → verify → file grouped issues — is the
`auditing-a-repo` skill.

## Auditing a live site — `ds_site_probe_script` → `ds_audit_url` / `ds_component_gaps`

A repo audit reads what the code *says*; a site audit reads what the browser *renders*. This
plugin never drives a browser (plugins don't import each other): the **agent** composes core's
agent_browser tools with ours —

```
browser_open(url) → browser_eval(<ds_site_probe_script>) → ds_audit_url(<probe>) + ds_component_gaps(<probe>)
```

- **`siteprobe_js.py`** — `SITE_PROBE_SOURCE` (readable) and `SITE_PROBE_JS`, its minified build
  (`python scripts/build_probe.py <esbuild>` regenerates it; a test fails when the recorded source
  hash is stale). The minified form ships because `browser_eval` passes the script on the
  `agent-browser` command line and Windows caps a whole command line at 32,767 characters: the
  script stays ≤ 24 KiB quoted, leaving ≥ 6,000 characters for the exe path and runtime flags
  (tested). One self-contained expression; it walks ≤ 5,000
  visible elements (≤ 3.5 s), and returns a JSON string hard-capped at 30 KB (it trims itself in
  stages and says what it trimmed): computed colors per property with counts, rendered-area
  weight and example selectors; font families/sizes/weights/line-heights; radii; padding/margin/
  gap; shadows; z-indices; transitions; text-vs-effective-background pairs (the worst-contrast
  ones kept even when small); `:root` custom properties (`--pl-*` first); `.pl-*` class counts;
  landmarks; forms and their controls; and up to 40 **clusters** — visible elements grouped by a
  structural signature (inferred kind + tag + role + normalized class stems — utility classes,
  CSS-module hashes and state classes stripped — + child shape), each with a count, a trimmed
  whitelisted-attribute `outerHTML` (≤ 600 chars), size stats, observed states (`aria-*`,
  `disabled`, `data-state`), variant features (backgrounds, heights, font sizes, radii, BEM
  modifiers) and the accessible name.
- **`siteprobe.py`** — the pure analyzer. The probe runs in the PAGE's realm, so its output is
  untrusted: `parse_probe` rebuilds every probe from a fixed schema (typed containers, finite
  numbers within ranges, length-capped strings, known kinds, ≤ 10 pages, ≤ 60 clusters/page,
  1.5 MB input) and relays a `browser_eval` error instead of guessing. Every report and issue
  draft quotes page content inertly — `@mentions`, `#123` refs and closing keywords are
  neutralized, links/HTML escaped, fences longer than any backtick run inside them, and evidence
  sits under an "untrusted page content" note. Then: `audit_probe` (findings in `audit.py`'s shape, rendered
  through the same `render_markdown`), `component_gaps` (kind → DS component table, variant checks
  against the DS's published Storybook story names, proposed APIs from the observed variation,
  priority = frequency × prominence), `merge_probes` (several pages → one), and `static_audit`.
- **`fetch.py`** — the ONE guarded fetcher (shared with `theme_extract`):
  `fetch_text(url, *, max_bytes, deadline, allow_offsite_hosts=True)` with an ABSOLUTE
  `time.monotonic()` deadline. `is_global` addresses only after unwrapping v4-in-v6 forms
  (mapped, compatible, 6to4, Teredo, NAT64) — no loopback, RFC 1918, link-local or
  CGNAT/Tailscale `100.64/10`; the socket **pinned** to the address that was checked (no
  DNS-rebinding window; TLS still verifies the host name); every redirect re-checked, https→http
  refused; a watchdog that shuts the socket at the deadline, so a byte-drip in the TLS handshake,
  status line, headers, chunk sizes or body can't outlive it; a cap on the *decompressed* body
  (gzip incl. multi-member, zlib and raw deflate); and nothing but `FetchError` escapes.

URL-mode rules: `no-ds-adoption`, `token-value-drift`, `unknown-token`, `off-token-color`,
`off-scale-length`, `low-contrast`, `unnamed-control`, `non-semantic-control`, and the ds-lane
`missing-scale` / `palette-gap`. Static mode runs the repo rules over the fetched CSS; on a site
that doesn't reference the DS's custom properties it skips `legacy-alias`, `ds-class-override`
and the DS-lane aggregates (a shared `.pl-` prefix — GitHub's Primer `.pl-c1` — is not adoption).

**The preferred path is one `execute_code` script** — the ~24 KB probe script and its ≤ 30 KB result
never pass through the model's context:

```python
js = tools.ds_site_probe_script(script_only=True)
probes = []
for u in urls:
    tools.browser_open(url=u)
    probes.append(tools.browser_eval(expression=js))
import json; p = json.dumps(probes)
print(tools.ds_audit_url(url_or_probe=p)[:2500]); print(tools.ds_component_gaps(probe=p)[:3500])
```

`execute_code` ships disabled and its default bridge allowlist is read-only core tools, so the
operator enables it and adds `browser_open, browser_eval, ds_site_probe_script, ds_audit_url,
ds_component_gaps` to its `tools` setting. Without it the agent calls the tools one by one,
passing the probe script to `browser_eval` and its result to ours verbatim (~10 K tokens each way).

The workflow — probe 3-5 representative pages → audit → decompose → screenshot and verify →
report with `show_artifact` → file deduplicated gap issues → hand implementation to the
`protoEngineer` delegate — is the `auditing-a-site` skill.

## The explorer — a browse surface, and only that

The plugin serves one console view (**Design System** in the right rail, also reachable from
⌘K) with three panes:

- **Foundations** — the live token vocabulary as swatches, type specimens, spacing rules and
  motion values. Themed tokens render as a split chip showing the dark and light face together,
  because the *pair* is what you judge; click any token to copy its `var()`.
- **Components** — the gallery, one card per variant.
- **Playground** — one story at full size, with controls generated from its **own `argTypes`**
  (Storybook hands those over its channel), a width picker and a theme override. Editing a
  control drives the live component via `updateStoryArgs` — the message Storybook's own manager
  sends — and the panel shows the resulting JSX to copy.

**The gallery renders the design system's own published Storybook**, not a replica. The
inventory comes from `index.json` and each card is an `<iframe>` onto that Storybook's
`/iframe?id=<story>`. A plugin whose purpose is preventing drift has no business maintaining a
second copy of the components — and the sidebar taxonomy is then the design system's *own*
(Foundations, Primitives, Layout, Navigation…), inherited rather than imposed.

Story frames render in the operator's current console theme, so the gallery never sits in dark
while the console is in light — which is how a contrast regression stays invisible.

Point `storybook_url` at any published Storybook. Leave it blank and the gallery turns off; the
token, rules and lint tools keep working.

### What this view deliberately does NOT do

It has no chat box and no prototype preview frame. protoAgent already ships a chat system and
an **artifact plugin**; a second question box means a second markdown renderer, a second escape
path and a second loading state, and a second preview frame means reimplementing sandboxing,
versioning and render verification that `show_artifact` already does properly.

So the agent's half lives in **tools, subagents and a skill** — you ask in chat, and prototypes
render in the Artifact panel. A view earns its place only for what chat can't do: browsing.
(This is also where Storybook's own MCP server landed — a pure tool surface, with interaction
happening in the agent's existing chat.)

## Subagents

### `ds-explainer`

Answers a question about the design system from the LIVE system rather than from memory —
which component or token to use, what a variant is for, whether the system covers a case at
all. Every claim has to come from a `ds_*` call in the same turn. It is told to prefer what
already exists, and to say "the system has no X yet" rather than invent a plausible token or
variant; a reasonable extension is offered explicitly as a *proposal*.

Ask in chat, or `task("ds-explainer", …)`. Its allowlist is derived from the tool objects, so a
rename can't silently drop one.

> **If you ever drive a subagent from a plugin ROUTE:** it resolves its allowlist against the
> **lead agent's** bound tool map, which a route plays no part in building — the call degrades
> to `No tools available for subagent '<name>'` with nothing explaining why. Pass `extra_tools`
> explicitly (`graph.sdk.run_subagent`). Not needed here: these are chat-driven.

### `ds-designer`

Turns a design intent into a working HTML prototype built from the system's real classes and
tokens — a fragment, not a page. Grounded in `ds_kit_classes` specifically because a plausible
invention (`.pl-datepicker`) renders as an unstyled div: only classes the kit actually ships
will look like anything. Told to compose an existing component when one covers the case, and to name the gap when
none does — a named gap is a design-system finding, and worth more than a silent one-off.

It **renders with `show_artifact`** and self-checks with `check_artifact` — the artifact plugin
already gives sandboxed rendering, versioning, `update_artifact`/`rewrite_artifact` and a render
verdict, so this plugin doesn't reimplement any of it. Those two tools are named in its
allowlist rather than imported: plugins coordinate through the host, never by importing each
other (ADR 0039). An unresolved name is skipped, so with the artifact plugin off the designer
degrades to describing the prototype instead of failing.

`task("ds-designer", …)` in chat, then `task("design-critic", …)` to review it.

### `design-critic` subagent

The plugin also registers a **`design-critic`** subagent (ADR 0018) — an adversarial design +
accessibility reviewer. Hand it a UI prototype or component (JSX/TSX/HTML/CSS) + what it's for
via `task("design-critic", …)`; it reviews against the **live** design system (grounded through
the `ds_*` tools — tokens, rules, existing components) and WCAG a11y, and returns prioritized
**BLOCKER / SHOULD-FIX / NIT** findings + a `ship-ready | revise | blocked` verdict. It reviews,
it doesn't rewrite — the QA half of "prototype → critique → PR" (text, not pixels). Pairs with a
`component-author` delegate (a strong coding model on the gateway) that turns an approved prototype
into a real `packages/ui` PR.

## Skill

`skills/auditing-a-repo/SKILL.md` carries the audit workflow (onboard → `ds_audit_repo` →
triage by lane → verify → file grouped issues, DS gaps in the established gap format).

`skills/theming-from-a-brand/SKILL.md` carries the brand → theme workflow (extract → confirm
seeds → generate → preview → iterate → deliver).

`skills/auditing-a-site/SKILL.md` carries the live-site workflow (probe in the browser →
`ds_audit_url` → `ds_component_gaps` → verify by screenshot → report → file gaps → hand off).

`skills/using-the-design-system/SKILL.md` auto-loads and carries the agent-facing contract:
*never name a component, variant, prop or token you have not read from a tool this turn*; search
before you build; say the system doesn't cover something rather than inventing a token; render
inventories with `show_component`; prototype → critique → PR. This is guidance, so it belongs in
a skill — it reaches the agent in chat, inside a `task()`, and on a scheduled turn alike.

## Events (ADR 0039)

`ds_drift` broadcasts **`design-system.drift-detected`** with the repo/ref, whether tokens moved,
and which components were added or removed. Declared in the manifest, so it's discoverable in
`/api/runtime/status` and a consumer doesn't have to reverse-engineer the payload. Broadcast
rather than wired: this plugin doesn't need to know who cares.

## Using it from Claude Code / Cursor (MCP)

The design system is reachable from any MCP client **without this plugin shipping an MCP
server** — protoAgent's operator MCP surface already exposes plugin tools (ADR 0075). Name the
ones you want in the host config:

```yaml
operator_mcp_tools:
  - ds_search        # components, variants and tokens by keyword
  - ds_stories       # the published inventory
  - ds_story         # one component's variants + live preview URLs
  - ds_component     # a component's API from source (props, usage) — any public export
  - ds_tokens        # the --pl-* vocabulary (takes a section)
  - ds_kit_classes   # the no-build class vocabulary
  - ds_rules         # the visual-identity rules
  - ds_check         # lint a snippet against the tokens
  - ds_audit_repo    # audit a local checkout (reads only under the allowed roots)
  - ds_audit_url     # audit a site (a probe JSON, or a static fetch of a public URL)
  - ds_component_gaps  # decompose a probed site into covered / variant-gap / missing components
```

The allowlist is **deny-by-default**, so a foreign client gets only what you name (`"*"` exposes
everything). All of the read tools are MCP-safe — none are on the HITL incompatible list. The
tool docstrings are what the client sees as tool descriptions, which is why they read as
instructions rather than summaries.

This is deliberately not a `register_mcp_server` call: **that seam is for a server the agent
*connects to*, not one it exposes.** The outward direction is already solved by the host, so
shipping one here would be both the wrong seam and a duplicate.

## Drift watch

`register()` arms a **native recurring watch** (protoAgent scheduler — no external cron/service):
on the `watch_cron` cadence it fires a turn that calls `ds_drift` and, if the design system moved
(tokens changed, components added/removed), has the agent sync the docs + consuming surfaces (a PR
on protoContent) or hand the lead a tight finding. Blank `watch_cron` turns it off for that agent.
The watch is plugin-owned, so a disable/uninstall cancels it; it re-arms idempotently on reload.

## Config (ADR 0019 — editable in the console)

```yaml
design-system:
  repo: protoLabsAI/protoContent
  ref: main
  tokens_path: packages/design-system/dist/tokens.json   # committed built JSON (fallback)
  tokens_css_path: packages/design-system/dist/tokens.css  # generated --pl-* vars (the contract)
  components_path: packages/ui/src
  rules_path: docs/reference/visual-identity.md
  storybook_url: https://protocontent-storybook.pages.dev   # "" = gallery off
  watch_cron: "0 14 * * *"   # "" = watch off
  audit_roots: []            # extra dirs ds_audit_repo may read (onboarding root + projects always allowed)
```

`tokens_css_path` is read in preference to `tokens_path` because the **generated CSS is the
contract**: consumers write `var(--pl-color-brand-lavender)`, and that name is produced by the
DS's own build. Re-deriving the camelCase→kebab rule here would mean maintaining a second copy of
it, and a token name this plugin invents but the design system doesn't publish is worse than no
name at all. The JSON holds the values; the CSS holds the names. A repo with no built CSS falls
back to the JSON automatically.

Auth: the GitHub contents API is read with `GITHUB_TOKEN` / `GH_TOKEN` from the env (the same
token the `github` plugin uses; protoContent is private). No separate plugin secret.

## Install

Ships via **[design-system-stack](https://github.com/protoLabsAI/design-system-stack)** (the Design System Engineer archetype) — `enabled: [delegates, artifact, design-system, github]`.
Or install standalone:

```bash
python -m server plugin install https://github.com/protoLabsAI/design-system-plugin
```

(Private-repo runtime installs need protoAgent ≥ the fix in
[#1805](https://github.com/protoLabsAI/protoAgent/pull/1805), or `PROTOAGENT_PLUGIN_FETCH=archive`.)

## Roadmap

- Audit a live URL on top of `vocab.py` + `audit.py` (fetched stylesheets + computed styles).
- Tailwind arbitrary lengths (`p-[13px]`, `text-[11px]`) in `off-scale-length`.
- component-level a11y hints from the stories; a `doc-sync` companion that opens the docs PR the
  drift watch describes.
