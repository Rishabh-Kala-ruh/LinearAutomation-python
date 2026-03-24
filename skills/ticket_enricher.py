"""
Ticket Enricher Skill

Extracts deep context from Linear or Jira tickets to build
a rich, structured prompt for Claude Code — resulting in more accurate fixes.
"""

from __future__ import annotations

import re
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import requests

from lib.linear_client import LinearClient


# ── Types ────────────────────────────────────────────────────────────────────


@dataclass
class EnrichedComment:
    body: str
    created_at: str
    author: str


@dataclass
class SubIssue:
    id: str
    title: str
    status: str
    description: str


@dataclass
class ParentContext:
    id: str
    title: str
    description: str


@dataclass
class IssueRelationInfo:
    type: str
    id: str | None
    title: str | None
    description: str


@dataclass
class AttachmentInfo:
    title: str
    url: str
    mime_type: str | None = None


@dataclass
class EnrichedContext:
    source: str  # "linear" | "jira"
    id: str
    title: str
    description: str
    url: str
    priority: str
    status: str | None
    labels: list[str] = field(default_factory=list)
    type: str | None = None
    components: list[str] | None = None
    comments: list[EnrichedComment] = field(default_factory=list)
    sub_issues: list[SubIssue] = field(default_factory=list)
    parent_context: ParentContext | None = None
    relations: list[IssueRelationInfo] = field(default_factory=list)
    attachments: list[AttachmentInfo] = field(default_factory=list)
    linked_branches: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    file_hints: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


# ── Helpers ──────────────────────────────────────────────────────────────────

PRIORITY_MAP: dict[int, str] = {
    0: "No priority",
    1: "Urgent",
    2: "High",
    3: "Medium",
    4: "Low",
}

VALID_EXTENSIONS = (
    "ts", "tsx", "js", "jsx", "py", "rb", "go", "rs", "java",
    "css", "scss", "html", "vue", "svelte", "json", "yaml", "yml",
    "toml", "md", "sql",
)


def parse_acceptance_criteria(description: str) -> list[str]:
    criteria: list[str] = []
    lines = description.split("\n")
    in_ac_section = False

    for line in lines:
        trimmed = line.strip()

        if re.match(
            r"^(acceptance\s+criteria|ac|requirements|expected\s+behavior|definition\s+of\s+done)",
            trimmed,
            re.IGNORECASE,
        ):
            in_ac_section = True
            continue

        checkbox = re.match(r"^-\s*\[[ x]?\]\s*(.+)", trimmed, re.IGNORECASE)
        if checkbox:
            criteria.append(checkbox.group(1))
            continue

        if in_ac_section:
            if trimmed == "" or re.match(r"^(##|---|\*\*\*)", trimmed):
                in_ac_section = False
                continue
            bullet = re.match(r"^[-*•]\s*(.+)", trimmed)
            num = re.match(r"^\d+[.)]\s*(.+)", trimmed)
            if bullet:
                criteria.append(bullet.group(1))
            elif num:
                criteria.append(num.group(1))
            elif len(trimmed) > 5:
                criteria.append(trimmed)

    return criteria


def extract_file_hints(description: str, comments: list[EnrichedComment]) -> list[str]:
    hints: set[str] = set()
    all_text = "\n".join([description] + [c.body for c in comments])

    file_patterns = re.findall(r"[\w./\\-]+\.\w{1,10}", all_text)
    for f in file_patterns:
        if "://" in f or re.match(r"^\d+\.\d+", f) or f.startswith("http") or len(f) < 5:
            continue
        ext = f.rsplit(".", 1)[-1].lower()
        if ext in VALID_EXTENSIONS:
            hints.add(f)

    code_refs = re.findall(r"`([^`]+)`", all_text)
    for ref in code_refs:
        if 2 < len(ref) < 80 and " " not in ref:
            hints.add(ref)

    return list(hints)


# ── Linear Enricher ──────────────────────────────────────────────────────────


class LinearEnricher:
    def __init__(self, api_key: str) -> None:
        self.client = LinearClient(api_key)

    def enrich(self, issue: dict[str, Any]) -> EnrichedContext:
        issue_id = issue["id"]

        context = EnrichedContext(
            source="linear",
            id=issue["identifier"],
            title=issue["title"],
            description=issue.get("description") or "No description provided.",
            url=issue["url"],
            priority=PRIORITY_MAP.get(issue.get("priority", 0), "Unknown"),
            status=None,
            created_at=issue.get("createdAt", ""),
            updated_at=issue.get("updatedAt", ""),
        )

        # Fire all 7 API calls in parallel (like Promise.allSettled in TS version)
        results: dict[str, Any] = {}

        def _fetch(key: str, fn: Any, *args: Any) -> tuple[str, Any]:
            try:
                return (key, fn(*args))
            except Exception:
                return (key, None)

        with ThreadPoolExecutor(max_workers=7) as pool:
            futures = [
                pool.submit(_fetch, "state", self.client.get_issue_state, issue_id),
                pool.submit(_fetch, "labels", self.client.get_issue_labels, issue_id),
                pool.submit(_fetch, "comments", self.client.get_issue_comments, issue_id, 50),
                pool.submit(_fetch, "children", self.client.get_issue_children, issue_id, 20),
                pool.submit(_fetch, "parent", self.client.get_issue_parent, issue_id),
                pool.submit(_fetch, "relations", self.client.get_issue_relations, issue_id, 20),
                pool.submit(_fetch, "attachments", self.client.get_issue_attachments, issue_id, 10),
            ]
            for future in as_completed(futures):
                key, value = future.result()
                results[key] = value

        # State
        if results.get("state"):
            context.status = results["state"].get("name")

        # Labels
        if results.get("labels"):
            context.labels = results["labels"]

        # Comments
        if results.get("comments"):
            raw_comments = sorted(results["comments"], key=lambda c: c.get("createdAt", ""))
            context.comments = [
                EnrichedComment(
                    body=c.get("body", ""),
                    created_at=c.get("createdAt", ""),
                    author=(c.get("user") or {}).get("name", "Unknown"),
                )
                for c in raw_comments
            ]

        # Children / sub-issues
        if results.get("children"):
            context.sub_issues = [
                SubIssue(
                    id=c["identifier"],
                    title=c["title"],
                    status=(c.get("state") or {}).get("name", "unknown"),
                    description=c.get("description") or "",
                )
                for c in results["children"]
            ]

        # Parent
        if results.get("parent"):
            parent = results["parent"]
            context.parent_context = ParentContext(
                id=parent["identifier"],
                title=parent["title"],
                description=parent.get("description") or "",
            )

        # Relations
        if results.get("relations"):
            context.relations = [
                IssueRelationInfo(
                    type=r.get("type", ""),
                    id=(r.get("relatedIssue") or {}).get("identifier"),
                    title=(r.get("relatedIssue") or {}).get("title"),
                    description=((r.get("relatedIssue") or {}).get("description") or "")[:200],
                )
                for r in results["relations"]
            ]

        # Attachments
        if results.get("attachments"):
            context.attachments = [
                AttachmentInfo(
                    title=a.get("title") or a.get("url", ""),
                    url=a.get("url", ""),
                )
                for a in results["attachments"]
            ]

        context.acceptance_criteria = parse_acceptance_criteria(context.description)
        context.file_hints = extract_file_hints(context.description, context.comments)

        return context


# ── Jira Enricher ────────────────────────────────────────────────────────────


class JiraEnricher:
    def __init__(self, base_url: str, email: str, api_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()

    def _fetch(self, endpoint: str) -> Any:
        url = f"{self.base_url}/rest/api/3/{endpoint}"
        resp = requests.get(url, headers={
            "Authorization": f"Basic {self.auth}",
            "Accept": "application/json",
        })
        resp.raise_for_status()
        return resp.json()

    def enrich(self, issue_key: str) -> EnrichedContext:
        issue = self._fetch(f"issue/{issue_key}?expand=changelog,renderedFields&fields=*all")
        fields = issue["fields"]

        context = EnrichedContext(
            source="jira",
            id=issue_key,
            title=fields.get("summary", ""),
            description=self._extract_text(fields.get("description")) or "No description provided.",
            url=f"{self.base_url}/browse/{issue_key}",
            priority=(fields.get("priority") or {}).get("name", "Unknown"),
            status=(fields.get("status") or {}).get("name", "Unknown"),
            type=(fields.get("issuetype") or {}).get("name", "Unknown"),
            labels=fields.get("labels", []),
            components=[c["name"] for c in (fields.get("components") or [])],
            created_at=fields.get("created", ""),
            updated_at=fields.get("updated", ""),
        )

        # Comments
        comment_data = (fields.get("comment") or {}).get("comments", [])
        context.comments = [
            EnrichedComment(
                author=(c.get("author") or {}).get("displayName", "Unknown"),
                body=self._extract_text(c.get("body")),
                created_at=c.get("created", ""),
            )
            for c in comment_data
        ]

        # Subtasks
        for s in (fields.get("subtasks") or []):
            context.sub_issues.append(SubIssue(
                id=s["key"],
                title=(s.get("fields") or {}).get("summary", ""),
                status=((s.get("fields") or {}).get("status") or {}).get("name", "unknown"),
                description="",
            ))

        # Parent
        parent = fields.get("parent")
        if parent:
            context.parent_context = ParentContext(
                id=parent["key"],
                title=(parent.get("fields") or {}).get("summary", ""),
                description="",
            )
            try:
                parent_issue = self._fetch(f"issue/{parent['key']}?fields=description")
                context.parent_context.description = self._extract_text(
                    parent_issue["fields"].get("description")
                ) or ""
            except Exception:
                pass

        # Issue links / relations
        for link in (fields.get("issuelinks") or []):
            inward = link.get("inwardIssue")
            outward = link.get("outwardIssue")
            related = inward or outward
            rel_type = (link.get("type") or {}).get("inward" if inward else "outward", "")
            context.relations.append(IssueRelationInfo(
                type=rel_type,
                id=(related or {}).get("key"),
                title=((related or {}).get("fields") or {}).get("summary", ""),
                description="",
            ))

        # Attachments
        for a in (fields.get("attachment") or []):
            context.attachments.append(AttachmentInfo(
                title=a.get("filename", ""),
                url=a.get("content", ""),
                mime_type=a.get("mimeType"),
            ))

        context.acceptance_criteria = parse_acceptance_criteria(context.description)
        context.file_hints = extract_file_hints(context.description, context.comments)

        return context

    def _extract_text(self, adf_node: Any) -> str:
        if not adf_node:
            return ""
        if isinstance(adf_node, str):
            return adf_node

        text = ""
        if adf_node.get("text"):
            return adf_node["text"]
        for child in (adf_node.get("content") or []):
            text += self._extract_text(child)
        node_type = adf_node.get("type", "")
        if node_type == "paragraph":
            text += "\n"
        elif node_type == "hardBreak":
            text += "\n"
        elif node_type == "listItem":
            text = "- " + text
        elif node_type == "heading":
            text = "\n" + text
        return text


# ── Prompt Builder ───────────────────────────────────────────────────────────


def build_enriched_prompt(context: EnrichedContext, worktree_path: str, repo_name: str) -> str:
    sections: list[str] = []

    source_label = "Jira" if context.source == "jira" else "Linear"
    sections.append(f"You are fixing a {source_label} ticket in the repository at {worktree_path}.")
    sections.append(f"## Ticket: {context.id} — {context.title}")
    sections.append(f"**Priority:** {context.priority} | **Status:** {context.status} | **Source:** {context.source}")
    if context.url:
        sections.append(f"**URL:** {context.url}")

    if context.labels:
        sections.append(f"**Labels:** {', '.join(context.labels)}")

    sections.append(f"\n### Description\n{context.description}")

    if context.parent_context:
        sections.append(f"\n### Parent Issue: {context.parent_context.id} — {context.parent_context.title}")
        if context.parent_context.description:
            sections.append(context.parent_context.description[:500])
        sections.append("> This ticket is a sub-task. Keep the parent's goal in mind.")

    if context.acceptance_criteria:
        sections.append("\n### Acceptance Criteria")
        for i, ac in enumerate(context.acceptance_criteria, 1):
            sections.append(f"{i}. {ac}")
        sections.append("\n> **You MUST satisfy ALL acceptance criteria above.**")

    if context.comments:
        sections.append(f"\n### Discussion Thread ({len(context.comments)} comments)")
        sections.append("> Read these carefully — they contain clarifications, edge cases, and decisions.")
        for c in context.comments:
            date = c.created_at[:10] if c.created_at else "unknown"
            sections.append(f"\n**{c.author}** ({date}):\n{c.body}")

    if context.sub_issues:
        sections.append("\n### Sub-issues")
        for sub in context.sub_issues:
            check = "x" if sub.status.lower() == "done" else " "
            sections.append(f"- [{check}] **{sub.id}** {sub.title} ({sub.status})")

    if context.relations:
        sections.append("\n### Related Issues")
        for rel in context.relations:
            sections.append(f"- **{rel.type}**: {rel.id} — {rel.title}")
            if rel.description:
                sections.append(f"  {rel.description}")

    if context.file_hints:
        sections.append("\n### Likely Relevant Files & Symbols")
        sections.append("These files/symbols were mentioned in the ticket or comments. Start your investigation here:")
        for f in context.file_hints:
            sections.append(f"- `{f}`")

    if context.attachments:
        sections.append("\n### Attachments")
        for a in context.attachments:
            sections.append(f"- {a.title}: {a.url}")

    sections.append("\n---\n## Instructions")
    sections.append(f"""
1. **Read and analyze** the codebase — start with the files/symbols mentioned above.
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

**Important: Commit your changes before finishing.**""")

    return "\n".join(sections)
