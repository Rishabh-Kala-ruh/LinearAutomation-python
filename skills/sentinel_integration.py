"""
Sentinel Guardian Integration — loads Sentinel testing skills and builds
test-generation prompts for Claude Code.

Sentinel skills are SKILL.md files that contain expert-level instructions
for generating specific types of tests (unit, integration, security, etc.).

This module reads those skills from disk and combines them with ticket context
to produce a test-generation prompt that Claude Code executes BEFORE implementation.

Usage:
    from skills.sentinel_integration import SentinelTestGenerator

    gen = SentinelTestGenerator("/path/to/sentinel-guardian/skills")
    prompt = gen.build_test_prompt(enriched_context, worktree_path, repo_name)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from skills.ticket_enricher import EnrichedContext


# Skills to invoke in order. test-setup runs first (once per repo),
# then unit-tests for the actual test generation.
# More skills can be added here as needed (integration-tests, security-tests, etc.)
DEFAULT_SKILL_CHAIN = ["test-setup", "unit-tests"]


@dataclass
class SentinelSkill:
    """A loaded Sentinel skill with its full prompt content."""
    name: str
    content: str


class SentinelTestGenerator:
    def __init__(self, skills_path: str) -> None:
        """
        Initialize with the path to Sentinel Guardian's skills directory.
        e.g., /home/rishabh/.openclaw/workspace/sentinel-guardian/skills
        """
        self.skills_path = skills_path
        self._cache: dict[str, SentinelSkill] = {}

    def _load_skill(self, skill_name: str) -> SentinelSkill | None:
        """Load a Sentinel skill from disk, with caching."""
        if skill_name in self._cache:
            return self._cache[skill_name]

        skill_file = os.path.join(self.skills_path, skill_name, "SKILL.md")
        if not os.path.exists(skill_file):
            return None

        with open(skill_file) as f:
            content = f.read()

        skill = SentinelSkill(name=skill_name, content=content)
        self._cache[skill_name] = skill
        return skill

    def _load_context(self, context_name: str) -> str | None:
        """Load a stack context file if available."""
        context_file = os.path.join(self.skills_path, "contexts", context_name)
        if os.path.exists(context_file):
            with open(context_file) as f:
                return f.read()
        return None

    def get_available_skills(self) -> list[str]:
        """List all available Sentinel skills."""
        if not os.path.exists(self.skills_path):
            return []
        return [
            d for d in os.listdir(self.skills_path)
            if os.path.isdir(os.path.join(self.skills_path, d))
            and os.path.exists(os.path.join(self.skills_path, d, "SKILL.md"))
        ]

    def build_test_prompt(
        self,
        context: EnrichedContext,
        worktree_path: str,
        repo_name: str,
        skill_chain: list[str] | None = None,
    ) -> str | None:
        """
        Build a test-generation prompt by combining Sentinel skills with ticket context.

        Returns None if no Sentinel skills are available (falls back to inline TDD).
        """
        chain = skill_chain or DEFAULT_SKILL_CHAIN
        loaded_skills: list[SentinelSkill] = []

        for skill_name in chain:
            skill = self._load_skill(skill_name)
            if skill:
                loaded_skills.append(skill)

        if not loaded_skills:
            return None

        sections: list[str] = []

        # ── Header ───────────────────────────────────────────────────
        sections.append(f"# Test Generation for {context.id} — {context.title}")
        sections.append(f"\nYou are generating tests in the repository at `{worktree_path}` (repo: {repo_name}).")
        sections.append(f"Your ONLY job is to write test cases. Do NOT implement the fix/feature.")

        # ── Ticket Context ───────────────────────────────────────────
        sections.append(f"\n## Ticket Context")
        sections.append(f"**ID:** {context.id}")
        sections.append(f"**Title:** {context.title}")
        sections.append(f"**Priority:** {context.priority}")
        sections.append(f"**Description:**\n{context.description}")

        if context.acceptance_criteria:
            sections.append(f"\n### Acceptance Criteria (EACH must have at least one test)")
            for i, ac in enumerate(context.acceptance_criteria, 1):
                sections.append(f"{i}. {ac}")

        if context.comments:
            sections.append(f"\n### Discussion Thread (check for edge cases to test)")
            for c in context.comments:
                date = c.created_at[:10] if c.created_at else "unknown"
                sections.append(f"\n**{c.author}** ({date}):\n{c.body}")

        if context.file_hints:
            sections.append(f"\n### Likely Relevant Files")
            for f in context.file_hints:
                sections.append(f"- `{f}`")

        # ── Sentinel Skills ──────────────────────────────────────────
        sections.append(f"\n---\n## Testing Instructions")
        sections.append(f"Follow the Sentinel Guardian testing methodology below.")

        for skill in loaded_skills:
            sections.append(f"\n{'='*60}")
            sections.append(f"## Sentinel Skill: {skill.name}")
            sections.append(f"{'='*60}")
            sections.append(skill.content)

        # ── Final Instructions ───────────────────────────────────────
        sections.append(f"""
---
## CRITICAL RULES FOR THIS PHASE

1. **ONLY write tests.** Do NOT implement the fix/feature. Do NOT modify any source code.
2. **Test the behavior described in the ticket** — acceptance criteria, edge cases from comments.
3. **Follow the repo's existing test conventions** — same framework, same directory structure.
4. **Tests MUST fail** when you run them (since the fix doesn't exist yet). If they pass, your tests aren't testing the right thing.
5. **Commit all test files** with message: `test({context.id}): add tests for {context.title}`
6. Do NOT push. Do NOT create a PR. Just commit locally.
7. After committing, run the tests to confirm they fail as expected. Print the test output.

**Remember: You are ONLY writing tests. The implementation will be done in a separate phase by another agent.**
""")

        return "\n".join(sections)
