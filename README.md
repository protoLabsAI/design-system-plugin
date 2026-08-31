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
| `ds_check <css\|jsx>` | flags a hardcoded hex a token already defines → the token to use instead |
| `ds_drift` | what changed since the last check (tokens + components); updates a snapshot |

## The explorer — a Storybook that the agent can read

The plugin serves a console view (**Design System** in the right rail) with two panes:

- **Foundations** — the live token vocabulary as swatches, type specimens, spacing rules and
  motion values. Themed tokens render as a split chip showing the dark and light face together,
  because the *pair* is what you actually judge; click any token to copy its `var()`.
- **Components** — the gallery, one card per variant, filterable by component *or* variant name.
- **Playground** — one story filling the viewport (stage and controls are full-height
  panels; nothing scrolls to see the preview) with a controls panel, a width picker
  (fill / 1280 / 768 / 390) and a theme override. The controls are generated from the
  story's **own `argTypes`**, which Storybook hands over its channel, so the panel always
  matches the real component API instead of a schema we'd have to keep in step by hand.
  Editing a control drives the live component through `updateStoryArgs` — the same message
  Storybook's own manager sends — and the panel shows the resulting JSX to copy.

**The gallery renders the design system's own published Storybook**, not a replica. The inventory
comes from `index.json` and each card is an `<iframe>` onto that Storybook's `/iframe?id=<story>`.
So what an operator browses is the real library at its current deploy — a plugin whose whole
purpose is preventing drift has no business maintaining a second copy of the components. It also
means the taxonomy in the sidebar is the design system's *own* (Foundations, Primitives, Layout,
Navigation…), inherited rather than imposed.

Story frames are rendered in the operator's current console theme (passed through as Storybook's
`theme` global), so a gallery never sits in dark while the console is in light — which is how a
contrast regression stays invisible until someone ships it.

Point `storybook_url` at any published Storybook. Leave it blank and the gallery turns off; the
token, rules and lint tools keep working, so a design system without a Storybook is still usable.

## `design-critic` subagent

The plugin also registers a **`design-critic`** subagent (ADR 0018) — an adversarial design +
accessibility reviewer. Hand it a UI prototype or component (JSX/TSX/HTML/CSS) + what it's for
via `task("design-critic", …)`; it reviews against the **live** design system (grounded through
the `ds_*` tools — tokens, rules, existing components) and WCAG a11y, and returns prioritized
**BLOCKER / SHOULD-FIX / NIT** findings + a `ship-ready | revise | blocked` verdict. It reviews,
it doesn't rewrite — the QA half of "prototype → critique → PR" (text, not pixels). Pairs with a
`component-author` delegate (a strong coding model on the gateway) that turns an approved prototype
into a real `packages/ui` PR.

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
