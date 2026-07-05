# design-system-plugin

The agent's **live window into the protoLabs.studio design system**. A protoAgent plugin
([ADR 0027](https://github.com/protoLabsAI/protoAgent)) — Matt's first domain capability on
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
| `ds_tokens` | the live token vocabulary (colors, spacing, radius, type…) — read before writing any styling |
| `ds_components` | the component inventory from `packages/ui/src` (the Storybook stories) |
| `ds_component <name>` | one component's story — its variants, API, usage |
| `ds_rules` | the visual-identity rules (when to use what, what we don't do) |
| `ds_check <css\|jsx>` | flags a hardcoded hex a token already defines → the token to use instead |
| `ds_drift` | what changed since the last check (tokens + components); updates a snapshot |

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
  tokens_path: packages/design-system/dist/tokens.json   # committed built JSON
  components_path: packages/ui/src
  rules_path: docs/reference/visual-identity.md
  watch_cron: "0 14 * * *"   # "" = watch off
```

Auth: the GitHub contents API is read with `GITHUB_TOKEN` / `GH_TOKEN` from the env (the same
token the `github` plugin uses; protoContent is private). No separate plugin secret.

## Install

Ships via the **frontend-bundle** (Matt's archetype) — `enabled: [delegates, github, design-system]`.
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
