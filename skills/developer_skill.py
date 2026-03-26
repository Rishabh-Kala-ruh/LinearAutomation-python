"""
Developer Skill — Development intelligence layer.

Sits between ticket fetching and Claude Code execution.
Handles scope resolution, repo inheritance, and smart prompt building
for all cases: normal tickets, parent tickets with sub-tasks, and sub-tasks.

Multi-phase TDD flow (Sentinel is REQUIRED):
  Phase 1..N: Sentinel Guardian generates tests sequentially (one skill per phase)
  Final Phase: Developer implements the fix until ALL tests pass

Usage:
    from skills.developer_skill import DeveloperSkill

    skill = DeveloperSkill(linear_api_key, viewer_id, sentinel_skills_path="/app/sentinel-skills")
    result = skill.process(issue, team_key, worktree_path, repo_name)
    # result.test_phases  — list of (skill_name, prompt) for sequential test generation
    # result.impl_prompt  — final phase: implementation prompt
    # result.repos        — resolved repo entries
    # result.scope_type   — "normal" | "parent_with_subtasks" | "subtask"
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from lib.linear_client import LinearClient
from skills.ticket_enricher import (
    LinearEnricher, EnrichedContext, EnrichedComment,
    parse_acceptance_criteria, extract_file_hints,
    PRIORITY_MAP,
)
from skills.sentinel_integration import SentinelTestGenerator
from skills.pathfinder_parser import parse_pathfinder_comment, PathfinderAnalysis


# ── Types ────────────────────────────────────────────────────────────────────


@dataclass
class SubTaskScope:
    """A sub-task with its assignee info, used for scope exclusion."""
    identifier: str
    title: str
    description: str
    status: str
    assignee_id: str | None
    assignee_name: str | None
    labels: list[str]
    is_mine: bool  # assigned to current viewer


@dataclass
class DeveloperResult:
    """Output of the developer skill — everything core.py needs."""
    identifier: str
    title: str
    scope_type: str  # "normal" | "parent_with_subtasks" | "subtask"
    repos: list[RepoEntry]
    test_prompt: str   # Test Agent prompt (single session, all Sentinel skills)
    impl_prompt: str   # Dev Agent prompt (single session, implementation)
    enriched_context: EnrichedContext
    stack_type: str = "backend"          # "backend" | "frontend" | "fullstack"
    pathfinder: PathfinderAnalysis | None = None  # Parsed Pathfinder analysis (if found)


@dataclass
class RepoEntry:
    name: str
    clone_url: str | None = None


# ── Developer Skill ──────────────────────────────────────────────────────────


# ── Skill file loading ───────────────────────────────────────────────────────

_SKILL_DIR = os.path.join(os.path.dirname(__file__), "developer-skill")
_SKILL_CACHE: str | None = None


def _load_skill_md() -> str:
    """Load the developer SKILL.md instructions. Cached after first read."""
    global _SKILL_CACHE
    if _SKILL_CACHE is not None:
        return _SKILL_CACHE

    skill_file = os.path.join(_SKILL_DIR, "SKILL.md")
    if os.path.exists(skill_file):
        with open(skill_file) as f:
            content = f.read()
        # Strip YAML frontmatter
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                content = content[end + 3:].strip()
        _SKILL_CACHE = content
    else:
        _SKILL_CACHE = ""

    return _SKILL_CACHE


_DEV_AGENT_DIR = os.path.join(os.path.dirname(__file__), "agents")
_DEV_AGENT_CACHE: str | None = None


def _load_dev_agent_md() -> str:
    """Load the dev-agent.md instructions. Cached after first read."""
    global _DEV_AGENT_CACHE
    if _DEV_AGENT_CACHE is not None:
        return _DEV_AGENT_CACHE

    agent_file = os.path.join(_DEV_AGENT_DIR, "dev-agent.md")
    if os.path.exists(agent_file):
        with open(agent_file) as f:
            content = f.read()
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                content = content[end + 3:].strip()
        _DEV_AGENT_CACHE = content
    else:
        _DEV_AGENT_CACHE = ""

    return _DEV_AGENT_CACHE


class DeveloperSkill:
    def __init__(
        self, api_key: str, viewer_id: str,
        github_org: str = "ruh-ai",
        sentinel_skills_path: str = "",
    ) -> None:
        self.client = LinearClient(api_key)
        self.enricher = LinearEnricher(api_key)
        self.viewer_id = viewer_id
        self.github_org = github_org

        # Sentinel is REQUIRED — test generation must always run
        if sentinel_skills_path and os.path.isdir(sentinel_skills_path):
            self.sentinel = SentinelTestGenerator(sentinel_skills_path)
        else:
            self.sentinel = None

    @property
    def sentinel_available(self) -> bool:
        return self.sentinel is not None and self.sentinel.available

    def process(
        self, issue: dict[str, Any], team_key: str, worktree_path: str, repo_name: str
    ) -> DeveloperResult:
        """
        Main entry point. Resolves scope, repos, and builds sequential test phases + implementation prompt.

        Raises RuntimeError if Sentinel skills are not available.
        """
        # Sentinel is mandatory — fail early if not available
        if not self.sentinel_available:
            raise RuntimeError(
                "Sentinel Guardian skills not found. "
                "Test generation is required before implementation. "
                "Ensure SENTINEL_SKILLS_PATH is set and skills are mounted."
            )

        identifier = issue["identifier"]
        issue_id = issue["id"]

        # Step 1: Enrich the ticket with deep context
        enriched = self.enricher.enrich(issue)

        # Step 2: Parse Pathfinder analysis from comments (RCA/TRD)
        pathfinder = parse_pathfinder_comment(
            [{"body": c.body} for c in enriched.comments]
        )
        if pathfinder:
            # Merge Pathfinder file hints into enriched context
            for hint in pathfinder.file_hints:
                if hint not in enriched.file_hints:
                    enriched.file_hints.append(hint)

        # Step 3: Resolve scope — what kind of ticket is this?
        scope_type, parent_info, sub_tasks = self._resolve_scope(issue)

        # Step 4: Resolve repos — Pathfinder takes priority, then labels/description
        labels = [l["name"] for l in (issue.get("labels") or {}).get("nodes", [])]
        project_name = (issue.get("project") or {}).get("name")
        repos = self._resolve_repos(
            issue, labels, team_key, project_name, parent_info, pathfinder
        )

        # Step 5: Detect stack and build Test Agent prompt (single session, all skills)
        stack_type = self.sentinel.detect_stack(worktree_path)
        test_prompt = self.sentinel.build_single_test_prompt(enriched, worktree_path, repo_name)

        if not test_prompt:
            raise RuntimeError(
                f"No Sentinel test skills could be loaded for stack '{stack_type}'. "
                "Check that SKILL.md files exist in the Sentinel skills directory."
            )

        # Step 6: Build Dev Agent prompt (with Pathfinder context)
        impl_prompt = self._build_prompt(
            enriched, scope_type, parent_info, sub_tasks,
            worktree_path, repo_name, pathfinder,
        )

        return DeveloperResult(
            identifier=identifier,
            title=issue["title"],
            scope_type=scope_type,
            repos=repos,
            test_prompt=test_prompt,
            impl_prompt=impl_prompt,
            enriched_context=enriched,
            stack_type=stack_type,
            pathfinder=pathfinder,
        )

    # ── Scope Resolution ─────────────────────────────────────────────────

    def _resolve_scope(
        self, issue: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None, list[SubTaskScope]]:
        """
        Determine the ticket type and fetch relevant context.

        Returns:
            scope_type: "normal" | "parent_with_subtasks" | "subtask"
            parent_info: Parent issue data (if this is a sub-task), else None
            sub_tasks: List of sub-tasks with assignee info (if this is a parent)
        """
        issue_id = issue["id"]

        # Check if this issue has a parent (making it a sub-task)
        parent_info = None
        try:
            parent_info = self.client.get_issue_parent_full(issue_id)
        except Exception:
            pass

        if parent_info:
            # This is a sub-task
            return "subtask", parent_info, []

        # Check if this issue has children (making it a parent)
        sub_tasks: list[SubTaskScope] = []
        try:
            children = self.client.get_issue_children_with_assignees(issue_id)
            for child in children:
                assignee = child.get("assignee")
                assignee_id = assignee["id"] if assignee else None
                sub_tasks.append(SubTaskScope(
                    identifier=child["identifier"],
                    title=child["title"],
                    description=child.get("description") or "",
                    status=(child.get("state") or {}).get("name", "unknown"),
                    assignee_id=assignee_id,
                    assignee_name=assignee["name"] if assignee else None,
                    labels=[l["name"] for l in (child.get("labels") or {}).get("nodes", [])],
                    is_mine=assignee_id == self.viewer_id,
                ))
        except Exception:
            pass

        if sub_tasks:
            return "parent_with_subtasks", None, sub_tasks

        return "normal", None, []

    # ── Repo Resolution ──────────────────────────────────────────────────

    def _resolve_repos(
        self,
        issue: dict[str, Any],
        labels: list[str],
        team_key: str,
        project_name: str | None,
        parent_info: dict[str, Any] | None,
        pathfinder: PathfinderAnalysis | None = None,
    ) -> list[RepoEntry]:
        """
        Resolve target repos. Priority:
          1. Pathfinder 'Repos Affected' (most reliable — from analysis comment)
          2. repo: labels on ticket
          3. GitHub URLs in description
          4. Parent ticket (for sub-tasks)
          5. Project name / team key fallback
        """
        # Priority 1: Pathfinder analysis has explicit repo list
        if pathfinder and pathfinder.repos:
            return [
                RepoEntry(name=r, clone_url=f"git@github.com:{self.github_org}/{r}.git")
                for r in pathfinder.repos
            ]

        # Priority 2-5: Standard detection
        repos = self._detect_repos(issue, labels, team_key, project_name)

        # If we only got a fallback (team key) and this is a sub-task, try the parent
        if parent_info and len(repos) == 1 and repos[0].name == team_key.lower():
            parent_labels = [l["name"] for l in (parent_info.get("labels") or {}).get("nodes", [])]
            parent_project = (parent_info.get("project") or {}).get("name")
            parent_repos = self._detect_repos(parent_info, parent_labels, team_key, parent_project)
            if parent_repos and parent_repos[0].name != team_key.lower():
                return parent_repos

        return repos

    def _detect_repos(
        self, issue: dict[str, Any], labels: list[str], team_key: str, project_name: str | None
    ) -> list[RepoEntry]:
        """Standard repo detection — same logic as core.py but returns RepoEntry."""
        seen: set[str] = set()
        repos: list[RepoEntry] = []

        def add(name: str, clone_url: str | None = None) -> None:
            if name.lower() not in seen:
                seen.add(name.lower())
                repos.append(RepoEntry(name, clone_url))

        # 1. repo: labels
        for label in labels:
            if label.lower().startswith("repo:"):
                add(label.split(":", 1)[1].strip())
        if repos:
            return repos

        # 2. GitHub URLs in description
        desc = issue.get("description") or ""
        for m in re.finditer(r"github\.com/([\w.-]+)/([\w.-]+)", desc):
            owner, repo = m.group(1), m.group(2).removesuffix(".git")
            add(repo, f"git@github.com:{owner}/{repo}.git")
        if repos:
            return repos

        # 3. Text patterns
        for m in re.finditer(r"(?:repository|repo)\s*:\s*([\w.-]+)", desc, re.IGNORECASE):
            add(m.group(1).strip())
        if repos:
            return repos

        # 4. Project name
        if project_name:
            return [RepoEntry(project_name.lower().replace(" ", "-"))]

        # 5. Team key fallback
        return [RepoEntry(team_key.lower())]

    # ── Prompt Builder ───────────────────────────────────────────────────

    def _build_prompt(
        self,
        context: EnrichedContext,
        scope_type: str,
        parent_info: dict[str, Any] | None,
        sub_tasks: list[SubTaskScope],
        worktree_path: str,
        repo_name: str,
        pathfinder: PathfinderAnalysis | None = None,
    ) -> str:
        """Build a scope-aware prompt for Claude Code (Dev Agent)."""
        sections: list[str] = []

        # Load Dev Agent definition
        dev_agent_md = _load_dev_agent_md()
        if dev_agent_md:
            sections.append(dev_agent_md)
            sections.append("")

        source_label = "Jira" if context.source == "jira" else "Linear"

        # ── Header ───────────────────────────────────────────────────
        sections.append(f"You are fixing a {source_label} ticket in the repository at {worktree_path}.")

        if scope_type == "subtask":
            sections.append(f"\n## ⚠ SCOPE: Sub-Task Only")
            sections.append(f"You are working on **sub-task {context.id}**, NOT the full parent ticket.")
            sections.append(f"Only implement what this sub-task describes. Do NOT touch scope belonging to other sub-tasks or the parent's general scope.")

        elif scope_type == "parent_with_subtasks":
            others_subtasks = [s for s in sub_tasks if not s.is_mine]
            if others_subtasks:
                sections.append(f"\n## ⚠ SCOPE: Parent Ticket (With Sub-Task Exclusions)")
                sections.append(f"This ticket has sub-tasks assigned to other developers. **DO NOT** implement their scope:")
            else:
                sections.append(f"\n## Scope: Parent Ticket (All Sub-Tasks Are Yours)")

        # ── Ticket Info ──────────────────────────────────────────────
        sections.append(f"\n## Ticket: {context.id} — {context.title}")
        sections.append(f"**Priority:** {context.priority} | **Status:** {context.status} | **Source:** {context.source}")
        if context.url:
            sections.append(f"**URL:** {context.url}")
        if context.labels:
            sections.append(f"**Labels:** {', '.join(context.labels)}")

        sections.append(f"\n### Description\n{context.description}")

        # ── Pathfinder Analysis (RCA/TRD — primary context) ──────────
        if pathfinder:
            label = "RCA (Root Cause Analysis)" if pathfinder.classification == "BUG" else "TRD (Technical Requirements Document)"
            sections.append(f"\n## Pathfinder {label}")
            sections.append(f"**Classification:** {pathfinder.classification} | **Complexity:** {pathfinder.complexity}")
            sections.append(f"\n> **This is your primary source of truth.** The Pathfinder analysis below contains the exact root cause, fix approach, and code changes table. Follow it precisely.")
            sections.append(f"\n{pathfinder.full_comment}")

            if pathfinder.file_changes:
                sections.append(f"\n### Code Changes Summary (from Pathfinder)")
                sections.append(f"> These are the exact files and functions you need to modify:")
                for fc in pathfinder.file_changes:
                    sections.append(f"- **{fc.change_type}** `{fc.file}` → `{fc.function}` — {fc.description}")

        # ── Parent Context (for sub-tasks) ───────────────────────────
        if scope_type == "subtask" and parent_info:
            parent_id = parent_info.get("identifier", "?")
            parent_title = parent_info.get("title", "?")
            parent_desc = parent_info.get("description") or ""
            sections.append(f"\n### Parent Issue: {parent_id} — {parent_title}")
            sections.append(f"> This is the parent ticket. Read it for context, but only implement YOUR sub-task ({context.id}).")
            if parent_desc:
                sections.append(parent_desc[:800])

        # ── Sub-Task Exclusions (for parent tickets) ─────────────────
        if scope_type == "parent_with_subtasks" and sub_tasks:
            others = [s for s in sub_tasks if not s.is_mine]
            mine = [s for s in sub_tasks if s.is_mine]

            if others:
                sections.append(f"\n### 🚫 Sub-Tasks Assigned to Other Developers (DO NOT IMPLEMENT)")
                sections.append("> These are being handled by other team members. Do NOT touch their scope.")
                for s in others:
                    status_icon = "✅" if s.status.lower() == "done" else "⬜"
                    assignee = s.assignee_name or "Unassigned"
                    sections.append(f"- {status_icon} **{s.identifier}**: {s.title} — *assigned to {assignee}*")
                    if s.description:
                        sections.append(f"  > {s.description[:200]}")

            if mine:
                sections.append(f"\n### ✅ Sub-Tasks Assigned to You (DO implement)")
                for s in mine:
                    status_icon = "✅" if s.status.lower() == "done" else "⬜"
                    sections.append(f"- {status_icon} **{s.identifier}**: {s.title}")
                    if s.description:
                        sections.append(f"  > {s.description[:200]}")

        # ── Acceptance Criteria ──────────────────────────────────────
        if context.acceptance_criteria:
            sections.append("\n### Acceptance Criteria")
            for i, ac in enumerate(context.acceptance_criteria, 1):
                sections.append(f"{i}. {ac}")
            sections.append("\n> **You MUST satisfy ALL acceptance criteria above.**")

        # ── Discussion Thread ────────────────────────────────────────
        if context.comments:
            sections.append(f"\n### Discussion Thread ({len(context.comments)} comments)")
            sections.append("> Read these carefully — they contain clarifications, edge cases, and decisions.")
            for c in context.comments:
                date = c.created_at[:10] if c.created_at else "unknown"
                sections.append(f"\n**{c.author}** ({date}):\n{c.body}")

        # ── Sub-Issues (enriched context, for reference) ─────────────
        if context.sub_issues and scope_type != "parent_with_subtasks":
            # Only show if we haven't already shown detailed sub-task info above
            sections.append("\n### Sub-issues")
            for sub in context.sub_issues:
                check = "x" if sub.status.lower() == "done" else " "
                sections.append(f"- [{check}] **{sub.id}** {sub.title} ({sub.status})")

        # ── Related Issues ───────────────────────────────────────────
        if context.relations:
            sections.append("\n### Related Issues")
            for rel in context.relations:
                sections.append(f"- **{rel.type}**: {rel.id} — {rel.title}")
                if rel.description:
                    sections.append(f"  {rel.description}")

        # ── File Hints ───────────────────────────────────────────────
        all_hints = list(context.file_hints)

        # For sub-tasks, also extract hints from parent description
        if scope_type == "subtask" and parent_info:
            parent_desc = parent_info.get("description") or ""
            parent_hints = extract_file_hints(parent_desc, [])
            for h in parent_hints:
                if h not in all_hints:
                    all_hints.append(h)

        if all_hints:
            sections.append("\n### Likely Relevant Files & Symbols")
            sections.append("These files/symbols were mentioned in the ticket or comments. Start your investigation here:")
            for f in all_hints:
                sections.append(f"- `{f}`")

        # ── Attachments ──────────────────────────────────────────────
        if context.attachments:
            sections.append("\n### Attachments")
            for a in context.attachments:
                sections.append(f"- {a.title}: {a.url}")

        # ── Instructions (loaded from SKILL.md) ─────────────────────
        sections.append("\n---\n## Instructions")

        # Scope-specific instructions (dynamic, stays in Python)
        if scope_type == "subtask":
            sections.append(f"""
**IMPORTANT: You are working on sub-task {context.id} ONLY.**
Do NOT implement anything from the parent ticket that is outside this sub-task's scope.
""")
        elif scope_type == "parent_with_subtasks":
            others = [s for s in sub_tasks if not s.is_mine]
            if others:
                excluded = ", ".join(s.identifier for s in others)
                sections.append(f"""
**IMPORTANT: Do NOT implement scope covered by these sub-tasks: {excluded}**
Those are assigned to other developers. Only implement what is NOT covered by any sub-task,
plus any sub-tasks that are assigned to you.
""")

        # Load static instructions from SKILL.md and inject ticket ID
        skill_instructions = _load_skill_md()
        if skill_instructions:
            sections.append(skill_instructions.replace("{{TICKET_ID}}", context.id))
        else:
            # Fallback if SKILL.md is missing
            sections.append(f"""1. Read and analyze the codebase.
2. Implement the fix or feature described in the ticket.
3. Stage and commit with: `fix({context.id}): <short summary>`
4. Do NOT push. Do NOT create a PR. Just commit locally.
5. If you cannot fix the issue, create `CLAUDE_UNABLE.md` explaining why.""")

        # Dynamic checklist additions based on scope
        if scope_type == "subtask":
            sections.append(f"- [ ] Only sub-task {context.id} scope is implemented (no parent scope leakage)")
        if scope_type == "parent_with_subtasks" and any(not s.is_mine for s in sub_tasks):
            sections.append("- [ ] Other developers sub-tasks are NOT touched")

        return "\n".join(sections)
