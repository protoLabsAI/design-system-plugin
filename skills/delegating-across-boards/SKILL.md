---
name: delegating-across-boards
description: When a piece of work spans the designSystem board and protoEngineer's board — a DS token/component change that a protoAgent surface must then adopt, or a protoAgent fix that has to land before a DS card can close — use this to decide who owns which repo, write the brief you hand the peer, gate the cross-repo timing, and follow up before claiming anything shipped.
---

# Delegating across boards

Some work touches two boards at once: a token renames here and a protoAgent view has to adopt
it, or a protoAgent bug blocks a DS card. Split it by repo ownership, hand the other half to the
peer with a precise brief, gate the timing, and verify — never assume the far side landed.

## 1. Who owns what

The **designSystem board** owns the DS repos, and only those:

- **protoContent** — the design-system half only: `packages/design-system` and `packages/ui`.
- **design-system-plugin** (this repo).
- **design-system-archetype**.

**protoEngineer** — the a2a delegate and fleet PM — owns **protoAgent** and dispatches its
coders. protoAgent work is never boarded here. When a DS change needs a protoAgent surface to
follow, send protoEngineer a brief:

```
delegate_to("protoEngineer", "<brief>", background=true)
```

**Merge policy belongs to the board that owns the repo.** protoEngineer's board may run
`auto_merge`; a brief you send it must not demand "a human merges this" — that is not yours to
require on their board. If a change genuinely needs a human in the loop before merge (a
migration, a breaking rename), don't assert it — **ask the peer for a merge hold on that card**
and say why. On DS repos, the DS board's own policy applies.

## 2. The brief template

A brief is only as good as the peer can act on without a round-trip. For **each item** include:

- **The issue number with its title echoed beside it** — `#3688 (<the issue's title>)`. A wrong
  number is caught the moment the title doesn't match; a bare `#3688` is not.
- **file:line evidence** — the exact site to change, not "somewhere in the header".
- **The replacement** — the token or component to use (`var(--pl-color-danger)`, `<Button
  variant="danger">`), read from the live system, not remembered.
- **Testable acceptance criteria** — what the peer's coder can check to know it's done.
- **The source issue** — where this came from (the audit, the drift report, the DS card), linked.
- **Refs vs Fixes, explicit.** `Refs #N` when the PR is one partial step and the issue stays
  open; `Fixes #N` only when merging that PR should close it. Say which, per item.

**Before any rename or removal**, grep the **whole repo, tests included**, for the old names and
**list the test files in scope in the brief**:

```
grep -rn "old-token-name\|OldComponent" .
```

A rename that green-lights the app but leaves `tests/foo.spec.ts` asserting the old name is a
broken card. Name those test files so the peer's coder fixes them in the same PR.

## 3. Cross-repo timing

A DS change reaches a consumer through a chain, and each link has to complete before the next:

```
DS card merges → changesets Version PR merges → npm publish → consumer card can start
```

Boarding the consumer card too early means its coder builds against a version that isn't on npm
yet. Gate it one of two ways — **never `depends_on` across boards** (it doesn't span boards):

1. **Gate on the published artifact containing the merge:**

   ```
   waits_for: npm:@protolabsai/<pkg>@contains:protoLabsAI/protoContent@<ds-card-id>
   ```

   This clears **only** when a published version is proven to contain *that card's* merge — not a
   version floor (`>=1.4.0` clears on any bump, including one cut before your merge landed).

2. **Wait for the publish yourself, then brief with the exact published version** — e.g. "adopt
   `@protolabsai/design-system@1.7.2`", as was done for protoAgent#3688. Use this when you're
   watching the release anyway and want to hand over a concrete, already-live version.

For how these gate tokens are authored on a card, see the **cross-repo-chain** skill.

## 4. Following up

A brief handed off is not a thing shipped. Before you tell anyone a cross-board change landed,
verify every link with a tool, not with trust:

- **Card state** — `board_get_feature` on the DS card, and the peer's own report for the
  protoAgent card. "In progress" is not "merged".
- **PR merged** — `github_get_pr` shows the PR actually merged, not just opened or approved.
- **Version published** — the npm version that contains the merge is live, and it's the version
  the consumer card was gated on.

Only when all three hold do you say it shipped. **Never report "done" on trust** — a Version PR
that's still open, a publish that failed, or a peer card that stalled all look like "done" from
a distance, and reporting them as shipped is worse than reporting them as pending.
