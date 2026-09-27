"""The delegating-across-boards skill — the cross-board playbook (ownership, brief template,
cross-repo timing gate, follow-up). Docs-as-code: assert the contract the skill has to carry so
a future edit can't quietly drop a required section or the exact gate form. Host-free."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "delegating-across-boards" / "SKILL.md"


def test_frontmatter_and_four_sections():
    body = SKILL.read_text()
    assert body.startswith("---\nname: delegating-across-boards\n")
    # a one-line description in the frontmatter
    fm = body.split("---", 2)[1]
    assert "description:" in fm
    # the four required sections
    for section in (
        "## 1. Who owns what",
        "## 2. The brief template",
        "## 3. Cross-repo timing",
        "## 4. Following up",
    ):
        assert section in body, section


def test_ownership_names_the_repos_and_the_delegate():
    body = SKILL.read_text()
    for token in (
        "packages/design-system",
        "packages/ui",
        "design-system-plugin",
        "design-system-archetype",
        "protoEngineer",
        "delegate_to",
        "background=true",
    ):
        assert token in body, token


def test_ownership_defers_merge_policy_to_the_owning_board():
    body = SKILL.read_text()
    # merge policy belongs to the repo owner; a peer running auto_merge is not told "humans merge"
    assert "auto_merge" in body
    assert "merge hold" in body


def test_brief_template_lists_every_required_field():
    body = SKILL.read_text()
    for token in (
        "#3688",              # issue number with its title echoed beside it
        "file:line",
        "acceptance criteria",
        "source issue",
        "Refs #N",
        "Fixes #N",
        "whole repo",         # grep the whole repo, tests included
        "tests included",
        "test files in scope",
        "grep -rn",
    ):
        assert token in body, token


def test_timing_gate_exact_form_and_after_publish_alternative():
    body = SKILL.read_text()
    assert "waits_for: npm:@protolabsai/<pkg>@contains:protoLabsAI/protoContent@<ds-card-id>" in body
    # never a version floor, never depends_on across boards
    assert "depends_on" in body and "version floor" in body
    # the after-publish exact-version alternative, and the card mechanics pointer
    assert "exact published version" in body or "exact, already-live version" in body
    assert "cross-repo-chain" in body


def test_followup_requires_tool_verification():
    body = SKILL.read_text()
    for token in ("board_get_feature", "github_get_pr", 'Never report "done" on trust'):
        assert token in body, token


def test_using_the_design_system_links_this_skill():
    index = (ROOT / "skills" / "using-the-design-system" / "SKILL.md").read_text()
    assert "delegating-across-boards" in index


def test_no_retired_delegate_names():
    # retired delegates (e.g. Fable) must not appear as delegate names in the playbook
    body = SKILL.read_text().lower()
    assert "fable" not in body
