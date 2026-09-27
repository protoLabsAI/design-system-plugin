---
name: auditing-a-repo
description: When asked to audit a repo, codebase or app for design-system adherence — "is X using the design system", "how on-system is this repo", "find DS violations / gaps", "audit the console's styling" — run ds_audit_repo on a local checkout, triage the findings by lane (design-system gap vs consumer fix), verify them in the source, and file grouped, deduplicated issues in the right repo.
---

# Auditing a repo against the design system

`ds_audit_repo` runs the design-system rule engine over a local checkout and returns a 0-100
adherence score plus findings split into two **lanes**. The lane decides where a fix goes, so
read it before anything else:

- **`ds` lane** — the design system is missing something the app needed: no type scale
  (`missing-scale`), a color the app keeps reaching for with no token near it (`palette-gap`),
  a DS class everyone overrides because the component lacks a variant (`override-hotspot`).
  These are filed on the **design-system repo**. The consumer can't fix them properly.
- **`consumer` lane** — the audited app is off-system: raw colors, stale `var()` fallbacks,
  unknown tokens, hardcoded lengths on an existing scale, `.pl-*` overrides, legacy aliases,
  hand-rolled controls, shadow components, foreign UI kits. These are filed on the **audited
  repo**.

## 1. Get the code on disk

`ds_audit_repo` reads local checkouts only, under the onboarding root, a registered project,
or the plugin's `audit_roots`. It never clones.

- A repo not on this host yet → `onboard_project("owner/repo")` first. It clones under the
  onboarding root and registers the project; then `ds_audit_repo("owner/repo")` resolves it.
- Already a registered project or work folder → pass its path (or its `owner/repo`).
- Refused? The message names the remedy. Don't work around the fence. Onboard or register instead.

## 2. Run the audit, scoped

`ds_audit_repo(path, include="apps/web/src/*")`. Scope a monorepo to the UI package; a
whole-repo audit mixes in docs sites and fixtures. Narrow further with `rules=` when you're
chasing one theme (`rules="stale-fallback,unknown-token"`). The reply is a summary, and the full
`.md` and `.json` reports (every finding, file:line) are written to the data dir. Their paths
are at the bottom of the reply.

## 3. Triage by lane, then by theme

Findings arrive **grouped by theme**: one token, one DS class, one literal, with a count and
file:line evidence. Work in groups, never line by line:

- `unknown-token` (**error**) first. These are broken today: the var resolves to its fallback
  or to nothing. Usually a typo or a token the DS removed.
- `stale-fallback` next. The fallback is what renders when the DS stylesheet fails to load,
  and it has drifted from the token. The fix is almost always to *drop* the fallback.
- `raw-color` exact/near matches are mechanical swaps. "No close token" ones are
  judgment calls, and if the same color shows up across files it's also a `palette-gap` (ds lane).
- `off-scale-length` warns are exact token values written by hand (a mechanical swap). Infos
  are off-scale values, which need a design decision: snap to the scale, or it's a DS gap.
- `ds-class-override` is a local fork of a DS component. Read the override. If it's
  reaching for a missing variant, that's the DS-lane `override-hotspot`, not a consumer fix.

## 4. Verify before filing

The engine is heuristic. **Read the cited lines for the top groups** (`read_file` on the
file:line evidence) before claiming anything. Check that the literal really is a color,
that the override really restyles the DS component, that the "shadow" component isn't a thin
wrapper. Drop what doesn't hold up and say you dropped it. A filed false positive costs more
than a missed true one.

The score is a trend line for this repo over time. It isn't a grade to compare across repos.

## 5. File grouped, deduplicated issues

**Search open issues first** in the target repo (both lanes) and comment on an existing one
rather than opening a duplicate. Then file **one issue per theme**, not one per line:
"Drop stale `var()` fallbacks (41 sites)" beats 41 issues.

**Design-system gaps** go to the DS repo, in the established gap format:

```markdown
## Gap
What the design system lacks, in one or two sentences (e.g. "No type scale: only
--pl-font-base-size exists").

## Evidence
The audit numbers and representative file:line citations from the consuming repo
(e.g. "font-size hardcoded 321× across 33 files; 24 distinct values; most common 12px ×111,
13px ×57, 11px ×49"), with a link to the consumer repo.

## Proposed API
The tokens / variant / prop you'd add, named in the DS's own conventions
(e.g. --pl-font-size-xs … -lg mapped to the values in use). Mark it a proposal.

## Priority
How much it hurts and how widely (count, number of files, user-visible or not).

## Context
Which audit (repo, path, date, report path) produced it, and what the consumer does today
as a workaround.
```

**Consumer findings** go to the audited repo: one issue per theme with the count, the
mechanical fix (`var(--pl-x, #old)` → `var(--pl-x)`), and the file:line list (or the path to
the JSON report when the list is long). Link the DS-gap issue when a consumer fix is blocked on it.

Hand the user a short summary: score, the top 3 themes per lane, what you filed (links), and
what you verified away as false positives.
