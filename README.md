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
| `ds_tokens` | the live token vocabulary — every `--pl-*` var with its dark and light value, read from the generated `tokens.css` |
| `ds_components` | the component inventory from `packages/ui/src` (the story files) |
| `ds_component <name>` | one component's story SOURCE — its API, props, usage |
| `ds_stories` | the published Storybook inventory: every component and every variant name |
| `ds_story <name>` | one component's variants, each with a **live render URL** you can show the user |
| `ds_rules` | the visual-identity rules (when to use what, what we don't do) |
| `ds_kit_classes` | the `.pl-*` classes the DS's published kit stylesheet actually defines — the vocabulary a no-build prototype can use |
| `ds_check <css\|jsx>` | flags a hardcoded hex a token already defines → the token to use instead |
| `ds_drift` | what changed since the last check (tokens + components); updates a snapshot |

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
will look like anything. Told to compose an existing component when one covers the case, and to
name the gap in a leading comment when none does.

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

- `ds_check` beyond hex: spacing/radius/type literals, Tailwind arbitrary values (`[#…]`, `[13px]`).
- component-level a11y hints from the stories; a `doc-sync` companion that opens the docs PR the
  drift watch describes.
