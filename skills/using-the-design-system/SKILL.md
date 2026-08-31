---
name: using-the-design-system
description: When the user asks about the design system, or asks you to build/style/prototype any UI — which component to use, what a token is, "do we have a…", designing a new component or layout — check the live system with the ds_* tools before answering or writing markup. Never state a component, variant, prop or token you have not read.
---

# Using the design system

This agent has a live window into a real design system. The tools read it **at call time**, so
they reflect the system as it is now — not as it was when the model was trained.

## The one rule

**Never name a component, variant, prop, class or token you have not read from a tool in this
turn.** A `variant="destructive"` that is actually called `"danger"`, or a
`var(--pl-color-danger)` that does not exist, is worse than saying you are not sure: it looks
authoritative and resolves to nothing.

Design systems exist to be used. The expensive failure mode is not getting a detail wrong — it
is quietly reinventing something the system already ships, so **look before you build**.

## Answering a question about the system

1. `ds_search "<keyword>"` — components, variants and tokens in one shot. Start here.
2. `ds_story "<Component>"` for its variants plus a **live preview URL per variant** — link it,
   the user can click it.
3. `ds_component "<Component>"` for the story source when you need real props and usage.
4. `ds_rules` for judgment — when to use what, and what this system deliberately doesn't do.
5. `ds_tokens "<section>"` for values. Pass the section ("Color", "Space", "Typography", …);
   the full set is a few thousand characters and most questions touch one family.

Lead with the answer, then the evidence. Cite the exact token and the exact variant.

**If the system doesn't cover it, say so.** "There is no date picker" is a useful, honest
answer — follow it with the primitives to compose one from, and label any extension you propose
as a *proposal*, not as something that exists.

For an inventory or a token family, render it with `show_component` (a `table` or `keyvalue`)
rather than a prose blob — it is structured data and reads far better inline.

## Building or styling UI

Before writing markup: `ds_search` for what exists, `ds_rules` for the constraints,
`ds_tokens` for values.

- **Compose what exists.** If a component covers the case, use it. Extending the system is a
  deliberate act; forking it by accident is not.
- **Never hardcode a value a token defines.** Run `ds_check` over what you wrote — it flags
  literals that already have a token.
- **Accessibility is part of the component**, not a later pass: semantic elements, keyboard
  operability, visible focus, contrast, labels. ARIA only where it earns its place.

## Prototyping something new

Delegate to `task("ds-designer", "<what you want>")`. It reads the system, builds the prototype
from the real classes and tokens, and renders it with `show_artifact` so the user can look at
it. Then `task("design-critic", …)` reviews it against the live system and WCAG and returns a
verdict with prioritised findings.

Shipping is a **pull request**, and a human merges it. Prototype → critique → PR.

## Keeping up

`ds_drift` reports what changed since the last check and broadcasts `design-system.drift-detected`.
When tokens move or components come and go, the docs and consuming surfaces need reconciling —
that is a PR or a finding, not a shrug.
