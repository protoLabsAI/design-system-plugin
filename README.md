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
| `ds_components` | the component inventory from `packages/ui/src` (the story files) |
| `ds_component <name>` | one component's story SOURCE — its API, props, usage |
| `ds_stories` | the published Storybook inventory: every component and every variant name |
| `ds_story <name>` | one component's variants, each with a **live render URL** you can show the user |
| `ds_rules` | the visual-identity rules (when to use what, what we don't do) |
| `ds_search <keyword>` | components, variants **and** tokens matching a keyword — the fastest "do we have a…" |
| `ds_kit_classes` | the `.pl-*` classes the DS's published kit stylesheet actually defines — the vocabulary a no-build prototype can use |
| `ds_check <code> [filename]` | lints a snippet with the audit engine (every rule below) → the token / component to use instead, with ΔE for near-miss colors |
| `ds_audit_repo <path> [include] [exclude] [rules] [max_findings]` | audits a **local checkout** (or an onboarded `owner/repo`) for design-system adherence → a 0-100 score and a report split into **DS gaps** vs **consumer fixes**, written as `.md` + `.json` to the plugin data dir |
| `ds_drift` | what changed since the last check (tokens + components); updates a snapshot |

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

| Rule | Lane | What it catches |
|---|---|---|
| `raw-color` | consumer | literal colors (hex / rgb / hsl / oklch / named) instead of `var(--pl-*)` — exact token, near token (ΔE), token-at-alpha (`color-mix`), or no close token |
| `stale-fallback` | consumer | `var(--pl-x, <fallback>)` whose fallback matches the token in **no** theme |
| `unknown-token` | consumer | `var(--pl-foo)` the DS doesn't define (typo / removed) |
| `off-scale-length` | consumer | hardcoded font-size / radius / gap-margin-padding / box-shadow where a token scale exists |
| `ds-class-override` | consumer | app CSS whose selector **subject** is a DS class (`.x .pl-dialog__body {}`) — forking the component |
| `legacy-alias` | consumer | app custom properties (`--brand-indigo: #6366f1`, or `var(--brand-indigo, #6366f1)`) duplicating a token's value |
| `hand-rolled-control` | consumer | raw `<button>`/`<input>`/`<select>`/`<textarea>`/`<dialog>` in JSX when the DS ships the component |
| `shadow-component` | consumer | local components named like a DS component (`StatusDot`), or a `*Chip` family that doesn't use the DS one |
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
  - ds_component     # a component's story source (props, usage)
  - ds_tokens        # the --pl-* vocabulary (takes a section)
  - ds_kit_classes   # the no-build class vocabulary
  - ds_rules         # the visual-identity rules
  - ds_check         # lint a snippet against the tokens
  - ds_audit_repo    # audit a local checkout (reads only under the allowed roots)
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
