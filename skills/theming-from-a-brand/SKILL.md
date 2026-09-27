---
name: theming-from-a-brand
description: When the user asks to theme something from a brand — "pull the theme from acme.com", "make a dark/light theme from our brand colors", "white-label the DS for a client", "match this site's look" — extract the brand with theme_extract, confirm the seeds, generate a full dark + light theme against the LIVE token contract with theme_generate, show the preview, iterate, then deliver it. Never hand-pick --pl-* values for a whole theme.
---

# Theming from a brand

A theme here is a value for **every `--pl-color-*` var the design system's live `tokens.css`
defines**, in both dark and light, with every text/ground pair clearing WCAG AA. That is
`theme_generate`'s job. Your job is to get the right seeds in, show the result, and deliver it.
Don't hand-write a theme var by var: you will miss vars, break the elevation relation, and ship a
contrast failure.

## 1. Extract — `theme_extract`

Hand it whichever of these you have:

- **A URL** (`https://acme.com` or `acme.com`): a static read of the HTML and its same-site and
  CDN stylesheets. No JavaScript runs.
- **A JS-rendered site** (SPA, CSS-in-JS, Tailwind), or a static read that comes back thin or
  monochrome: use a rendered probe instead. Call `theme_probe_script`, open the page with
  `browser_open`, run the script with `browser_eval`, then pass the JSON string it returns to
  `theme_extract`.
- **Brand colors the operator gave you** (`#0f766e, #f59e0b`): pass them as a comma list. The
  first one is the primary.
- **Raw CSS** they pasted.

It returns ranked brand candidates, each with the evidence it came from (a `--brand-primary`
custom property, a button background, `<meta name="theme-color">`…), the site's ground (dark or
light), fonts, radius, and **suggested seeds with a confidence**.

## 2. Confirm the seeds when it's ambiguous

If the confidence is `low` or `medium`, or the top two candidates are close, **ask before you
generate**. Keep the question short and concrete: "Acme's primary looks like teal `#0f766e` (on
their buttons and `--brand-primary`), with amber `#f59e0b` as a secondary accent. Use those?"
A theme built on the wrong primary wastes the operator's review.

When the confidence is `high`, or the operator named the colors, go straight to generating.

## 3. Generate — `theme_generate`

`theme_generate(primary, secondary="", neutral="", name, scope=":root", font_family="", radius="")`

- `name` is the file stem. Use the brand, e.g. `acme`.
- `font_family` must be a plain font stack (names, commas, quotes). A stack copied from a site
  that carries anything else is rejected, not escaped.
- `scope`: use `:root` to replace the default theme (a consumer app, or this console). Use
  `[data-brand="acme"]` for a white-label scope that sits beside the default.
- Pass `font_family` / `radius` only when the brand clearly has them (the extraction reports
  both) and the operator wants them carried over.

Read the summary before you show anything:

- **Adjustments**: e.g. the brand color was stepped deeper on light so it clears 3:1. Say so in
  plain words: "pure yellow can't hold white text or sit on white, so the light theme uses a
  deep olive step of it".
- **Repairs**: the colors that were nudged to pass contrast. You don't need to list them all,
  but mention any the operator will notice.
- **FAIL**: should never happen. If it does, say so. Don't hide it.

## 4. Show it — `show_artifact`

`theme_generate`'s result carries a compact preview between `<<<ARTIFACT_HTML` and
`ARTIFACT_HTML>>>`. Pass **exactly that text** as
`show_artifact(kind="html", title="<name> theme", code=…)`. Don't try to read the preview file
from disk: it lives in the plugin's data dir, outside any project your file tools can reach.
The preview shows dark and light side by side: surfaces, text tiers, the accent button, the
kit's buttons/alerts/badges/card/field (the artifact sandbox loads the DS kit), the focus ring,
chart series and brand marks.

The full page, with the per-token contrast table, is served at the console URL the result names
(`/plugins/design-system/themes/<name>/preview`). Point the operator at it when they want the
numbers.

## 5. Iterate

Operators react to what they see: "too muddy", "the light accent is too dark", "warmer greys".
Change the **seeds** and regenerate, then show the new preview. A different primary step, a
`neutral` whose hue tints the greys, or a `secondary` are the levers. Don't patch individual vars
by hand afterwards: the next regeneration drops the patch, and the patch was never
contrast-checked.

## 6. Deliver

- **This console:** the result's `theme_apply dark:` / `theme_apply light:` lines are
  ready-made maps (colors + font/radius). Pass one as `overrides_json` to `theme_apply` with the
  matching `mode`.
- **A product or consumer repo:** the `.theme.css` (its path is in the result) is an override layer. Load it **after** the
  DS's `tokens.css`. Open a PR adding it, through the builder delegate or the github tools.
  Shipping is a PR, and a human merges it.
- **The DS itself** (a new first-party theme): propose it as a PR to the design-system repo and
  label it as a proposal.

## Don'ts

- Don't invent a `--pl-*` name. The generator covers exactly the vars the live contract defines.
- Don't claim a theme "passes AA" unless you read the contrast summary in this turn.
- Don't generate from a guessed primary when the extraction was ambiguous. Ask first.
