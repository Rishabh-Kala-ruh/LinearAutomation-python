"""
Developer Skill — Development intelligence layer.

Sits between ticket fetching and Claude Code execution.
Handles scope resolution, repo inheritance, and smart prompt building
for all cases: normal tickets, parent tickets with sub-tasks, and sub-tasks.

Usage:
    from skills.developer_skill import DeveloperSkill

    skill = DeveloperSkill(linear_api_key, viewer_id)
    result = skill.process(issue, team_key)
    # result.repos       — resolved repo entries
    # result.prompt      — scope-aware prompt for Claude
    # result.identifier  — ticket identifier
    # result.scope_type  — "normal" | "parent_with_subtasks" | "subtask"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from lib.linear_client import LinearClient
from skills.ticket_enricher import (
    LinearEnricher, EnrichedContext, EnrichedComment,
    parse_acceptance_criteria, extract_file_hints,
    PRIORITY_MAP,
)


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
    prompt: str
    enriched_context: EnrichedContext


@dataclass
class RepoEntry:
    name: str
    clone_url: str | None = None


# ── Developer Skill ──────────────────────────────────────────────────────────


class DeveloperSkill:
    def __init__(self, api_key: str, viewer_id: str, github_org: str = "ruh-ai") -> None:
        self.client = LinearClient(api_key)
        self.enricher = LinearEnricher(api_key)
        self.viewer_id = viewer_id
        self.github_org = github_org

    def process(
        self, issue: dict[str, Any], team_key: str, worktree_path: str, repo_name: str
    ) -> DeveloperResult:
        """
        Main entry point. Resolves scope, repos, and builds the prompt.

        Returns a DeveloperResult with everything needed to run Claude Code.
        """
        identifier = issue["identifier"]
        issue_id = issue["id"]

        # Step 1: Enrich the ticket with deep context
        enriched = self.enricher.enrich(issue)

        # Step 2: Resolve scope — what kind of ticket is this?
        scope_type, parent_info, sub_tasks = self._resolve_scope(issue)

        # Step 3: Resolve repos (with parent inheritance for sub-tasks)
        labels = [l["name"] for l in (issue.get("labels") or {}).get("nodes", [])]
        project_name = (issue.get("project") or {}).get("name")
        repos = self._resolve_repos(issue, labels, team_key, project_name, parent_info)

        # Step 4: Build scope-aware prompt
        prompt = self._build_prompt(
            enriched, scope_type, parent_info, sub_tasks,
            worktree_path, repo_name,
        )

        return DeveloperResult(
            identifier=identifier,
            title=issue["title"],
            scope_type=scope_type,
            repos=repos,
            prompt=prompt,
            enriched_context=enriched,
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
    ) -> list[RepoEntry]:
        """
        Resolve target repos. If this is a sub-task without repo info,
        inherit from the parent ticket.
        """
        # Try resolving from the issue itself first
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
    ) -> str:
        """Build a scope-aware prompt for Claude Code."""
        sections: list[str] = []

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

        # ── Instructions ─────────────────────────────────────────────
        sections.append("\n---\n## Instructions")

        # Scope-specific instructions
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

        sections.append(f"""1. **Read and analyze** the codebase — start with the files/symbols mentioned above.
2. **Understand the full context** — the description, acceptance criteria, AND the discussion thread all matter.
3. **Implement the fix or feature** described in the ticket. Follow existing code style.
4. **Handle edge cases** mentioned in the comments.
5. **Stage and commit ALL changes** with this commit message format:
   `fix({context.id}): <short summary of what was changed>`
6. Do NOT push. Do NOT create a PR. Just commit locally.
7. If you cannot fix the issue, create `CLAUDE_UNABLE.md` explaining exactly why.

### Quality Checklist
- [ ] All acceptance criteria are met
- [ ] Edge cases from comments are handled
- [ ] No regressions introduced
- [ ] Code follows existing patterns and style
- [ ] Changes are minimal and focused — don't refactor unrelated code
{f'- [ ] Only sub-task {context.id} scope is implemented (no parent scope leakage)' if scope_type == "subtask" else ''}
{f'- [ ] Other developers sub-tasks are NOT touched' if scope_type == "parent_with_subtasks" and any(not s.is_mine for s in sub_tasks) else ''}
**Important: Commit your changes before finishing.**""")

        return "\n".join(sections)
