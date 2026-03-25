"""
Sentinel Guardian Integration — loads Sentinel testing skills and builds
sequential per-skill prompts for Claude Code.

Each Sentinel skill runs as a separate Claude Code invocation (phase).
Stack detection determines which skills apply to the repo.

Usage:
    from skills.sentinel_integration import SentinelTestGenerator

    gen = SentinelTestGenerator("/path/to/sentinel-guardian/skills")
    phases = gen.build_test_phases(enriched_context, worktree_path, repo_name)
    # phases = [("test-setup", "prompt..."), ("unit-tests", "prompt..."), ...]
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from skills.ticket_enricher import EnrichedContext


# ── Skill chains by stack type ───────────────────────────────────────────────

BACKEND_SKILLS = [
    "test-setup",
    "unit-tests",
    "integration-tests",
    "contract-tests",
    "security-tests",
    "resilience-tests",
    "smoke-tests",
    "e2e-api-tests",
    "test-review",
]

FRONTEND_SKILLS = [
    "test-setup",
    "unit-tests",
    "e2e-browser-tests",
    "test-review",
]

FULLSTACK_SKILLS = [
    "test-setup",
    "unit-tests",
    "integration-tests",
    "contract-tests",
    "security-tests",
    "resilience-tests",
    "smoke-tests",
    "e2e-api-tests",
    "e2e-browser-tests",
    "test-review",
]


# ── Stack detection signals ──────────────────────────────────────────────────

# Files that indicate a backend repo
BACKEND_SIGNALS = [
    "requirements.txt", "pyproject.toml", "setup.py", "Pipfile",  # Python
    "go.mod",  # Go
    "Cargo.toml",  # Rust
    "pom.xml", "build.gradle",  # Java
    "Gemfile",  # Ruby
]

# Files that indicate a frontend repo
FRONTEND_SIGNALS = [
    "next.config.js", "next.config.ts", "next.config.mjs",  # Next.js
    "vite.config.ts", "vite.config.js",  # Vite
    "angular.json",  # Angular
    "svelte.config.js",  # Svelte
    "nuxt.config.ts",  # Nuxt
]

# package.json dependencies that indicate frontend
FRONTEND_DEPS = {"react", "next", "vue", "angular", "svelte", "nuxt"}


@dataclass
class SentinelSkill:
    """A loaded Sentinel skill with its full prompt content."""
    name: str
    content: str


class SentinelTestGenerator:
    def __init__(self, skills_path: str) -> None:
        self.skills_path = skills_path
        self._cache: dict[str, SentinelSkill] = {}

    @property
    def available(self) -> bool:
        """Check if Sentinel skills are available on disk."""
        return os.path.isdir(self.skills_path) and bool(self.get_available_skills())

    def _load_skill(self, skill_name: str) -> SentinelSkill | None:
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

    def get_available_skills(self) -> list[str]:
        if not os.path.exists(self.skills_path):
            return []
        return [
            d for d in os.listdir(self.skills_path)
            if os.path.isdir(os.path.join(self.skills_path, d))
            and os.path.exists(os.path.join(self.skills_path, d, "SKILL.md"))
        ]

    # ── Stack Detection ──────────────────────────────────────────────────

    def detect_stack(self, worktree_path: str) -> str:
        """
        Detect repo stack type by checking for signal files.
        Returns: "backend", "frontend", or "fullstack"
        """
        has_backend = False
        has_frontend = False

        for signal in BACKEND_SIGNALS:
            if os.path.exists(os.path.join(worktree_path, signal)):
                has_backend = True
                break

        for signal in FRONTEND_SIGNALS:
            if os.path.exists(os.path.join(worktree_path, signal)):
                has_frontend = True
                break

        # Check package.json for frontend deps if not already detected
        if not has_frontend:
            pkg_json = os.path.join(worktree_path, "package.json")
            if os.path.exists(pkg_json):
                try:
                    import json
                    with open(pkg_json) as f:
                        pkg = json.load(f)
                    all_deps = set()
                    all_deps.update((pkg.get("dependencies") or {}).keys())
                    all_deps.update((pkg.get("devDependencies") or {}).keys())
                    if all_deps & FRONTEND_DEPS:
                        has_frontend = True
                    # A package.json without frontend deps is likely a Node.js backend
                    if not has_frontend and not has_backend:
                        has_backend = True
                except Exception:
                    pass

        if has_backend and has_frontend:
            return "fullstack"
        if has_frontend:
            return "frontend"
        if has_backend:
            return "backend"

        # Default to backend if nothing detected
        return "backend"

    def get_skill_chain(self, worktree_path: str) -> list[str]:
        """Get the appropriate skill chain for the repo's stack."""
        stack = self.detect_stack(worktree_path)
        if stack == "fullstack":
            return FULLSTACK_SKILLS
        if stack == "frontend":
            return FRONTEND_SKILLS
        return BACKEND_SKILLS

    # ── Phase Builder ────────────────────────────────────────────────────

    def build_test_phases(
        self,
        context: EnrichedContext,
        worktree_path: str,
        repo_name: str,
    ) -> list[tuple[str, str]]:
        """
        Build sequential per-skill prompts for Claude Code.

        Returns list of (skill_name, prompt) tuples.
        Each tuple is a separate Claude Code invocation.
        Returns empty list if no skills could be loaded.
        """
        chain = self.get_skill_chain(worktree_path)
        available = set(self.get_available_skills())

        phases: list[tuple[str, str]] = []
        for skill_name in chain:
            if skill_name not in available:
                continue
            skill = self._load_skill(skill_name)
            if not skill:
                continue

            prompt = self._build_phase_prompt(context, skill, worktree_path, repo_name)
            phases.append((skill_name, prompt))

        return phases

    def _build_ticket_context(self, context: EnrichedContext) -> str:
        """Build the shared ticket context section (reused across phases)."""
        sections: list[str] = []

        sections.append("## Ticket Context")
        sections.append(f"**ID:** {context.id}")
        sections.append(f"**Title:** {context.title}")
        sections.append(f"**Priority:** {context.priority}")
        sections.append(f"**Description:**\n{context.description}")

        if context.acceptance_criteria:
            sections.append("\n### Acceptance Criteria (EACH must have at least one test)")
            for i, ac in enumerate(context.acceptance_criteria, 1):
                sections.append(f"{i}. {ac}")

        if context.comments:
            sections.append(f"\n### Discussion Thread (check for edge cases to test)")
            for c in context.comments:
                date = c.created_at[:10] if c.created_at else "unknown"
                sections.append(f"\n**{c.author}** ({date}):\n{c.body}")

        if context.file_hints:
            sections.append("\n### Likely Relevant Files")
            for f in context.file_hints:
                sections.append(f"- `{f}`")

        return "\n".join(sections)

    def _build_phase_prompt(
        self,
        context: EnrichedContext,
        skill: SentinelSkill,
        worktree_path: str,
        repo_name: str,
    ) -> str:
        """Build prompt for a single Sentinel skill phase."""
        is_review = skill.name == "test-review"
        ticket_ctx = self._build_ticket_context(context)

        sections: list[str] = []

        # ── Header
        sections.append(f"# Sentinel Phase: {skill.name}")
        sections.append(f"**Ticket:** {context.id} — {context.title}")
        sections.append(f"**Repo:** `{repo_name}` at `{worktree_path}`")

        # ── Ticket context
        sections.append(f"\n{ticket_ctx}")

        # ── Skill instructions
        sections.append(f"\n---\n## Sentinel Skill: {skill.name}")
        sections.append(skill.content)

        # ── Phase-specific rules
        if is_review:
            sections.append(f"""
---
## RULES FOR THIS PHASE

1. **Review all test files** written in previous phases.
2. Follow the test-review skill checklist exactly.
3. If issues are found, **fix them** in the test files.
4. **Commit fixes** with message: `test({context.id}): review fixes for {context.title}`
5. Do NOT implement the fix/feature. Do NOT modify source code.
6. Do NOT push. Just commit locally.
""")
        else:
            sections.append(f"""
---
## RULES FOR THIS PHASE

1. **ONLY write tests** for the `{skill.name}` category. Do NOT implement the fix/feature. Do NOT modify source code.
2. **Test the behavior described in the ticket** — acceptance criteria, edge cases from comments.
3. **Follow the repo's existing test conventions** — same framework, same directory structure.
4. **Check what tests already exist** from previous phases. Do NOT duplicate them. Build on top of them.
5. **Commit all test files** with message: `test({context.id}): add {skill.name} for {context.title}`
6. Do NOT push. Do NOT create a PR. Just commit locally.
7. After committing, run the tests. Print the output.

**You are ONLY writing {skill.name}. Implementation happens in a later phase.**
""")

        return "\n".join(sections)
