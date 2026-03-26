"""
Pathfinder Comment Parser — extracts structured data from Pathfinder analysis comments.

Pathfinder is an external automation that adds RCA (bug) or TRD (feature) comments
to Linear tickets before they reach "Ready for Development". This parser extracts:
  - Classification (BUG/FEATURE/TASK)
  - Complexity (S/M/L/XL)
  - Repos affected (with primary/secondary distinction)
  - Code changes table (file, function, change type, description)
  - File hints (extracted from code changes + trace)
  - Full RCA/TRD content (used as primary context for Claude)

Usage:
    from skills.pathfinder_parser import parse_pathfinder_comment, PathfinderAnalysis

    analysis = parse_pathfinder_comment(comments)
    if analysis:
        print(analysis.classification)  # "BUG" or "FEATURE"
        print(analysis.repos)           # ["agent-platform-v2", "ai-gateway"]
        print(analysis.file_changes)    # [{"file": "...", "function": "...", ...}]
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


PATHFINDER_MARKER = "Pathfinder Analysis"


@dataclass
class FileChange:
    """A single row from Pathfinder's Code Changes table."""
    repo: str
    file: str
    function: str
    change_type: str  # MODIFY, ADD, NEW FILE, VERIFY
    description: str


@dataclass
class PathfinderAnalysis:
    """Parsed output from a Pathfinder comment."""
    classification: str  # BUG, FEATURE, TASK
    complexity: str  # S, M, L, XL
    repos: list[str]  # repo names in order (first = primary)
    primary_repo: str | None  # explicitly marked primary, or first in list
    full_comment: str  # raw Pathfinder comment body (used as context for Claude)
    file_changes: list[FileChange] = field(default_factory=list)
    file_hints: list[str] = field(default_factory=list)
    implementation_order: list[str] = field(default_factory=list)


def find_pathfinder_comment(comments: list[dict]) -> dict | None:
    """Find the Pathfinder analysis comment from a list of comments."""
    for comment in comments:
        body = comment.get("body") or ""
        if PATHFINDER_MARKER in body:
            return comment
    return None


def parse_pathfinder_comment(comments: list[dict]) -> PathfinderAnalysis | None:
    """
    Parse the Pathfinder analysis comment from a list of issue comments.
    Returns None if no Pathfinder comment is found.
    """
    comment = find_pathfinder_comment(comments)
    if not comment:
        return None

    body = comment.get("body") or ""

    # ── Classification ────────────────────────────────────────────────
    classification = "UNKNOWN"
    m = re.search(r"\*\*Classification:\*\*\s*(\w+)", body)
    if m:
        classification = m.group(1).upper()

    # ── Complexity ────────────────────────────────────────────────────
    complexity = "M"
    m = re.search(r"\*\*Complexity:\*\*\s*(\w+)", body)
    if m:
        complexity = m.group(1).upper()

    # ── Repos Affected ────────────────────────────────────────────────
    repos: list[str] = []
    primary_repo: str | None = None

    # Format 1: "**Repos Affected:** agent-platform-v2 (primary), ai-gateway (possible)"
    m = re.search(r"\*\*Repos Affected:\*\*\s*(.+)", body)
    if m:
        repos_line = m.group(1).strip()
        for part in repos_line.split(","):
            part = part.strip()
            repo_match = re.match(r"([\w.-]+)", part)
            if repo_match:
                repo_name = repo_match.group(1)
                repos.append(repo_name)
                if "primary" in part.lower():
                    primary_repo = repo_name

    # Format 2: "#### Repo 1: `agent-platform-v2` (Primary Changes)"
    if not repos:
        for m in re.finditer(r"#{1,4}\s+Repo\s+\d+:\s+`?([\w.-]+)`?", body):
            repo_name = m.group(1)
            if repo_name not in repos:
                repos.append(repo_name)

    # Format 3: "### agent-platform-v2" or "### agent-platform-v2 (Primary)"
    # (already handled in code changes table section below)

    # Format 4: Extract from "Affected Files Summary" table — "| agent-platform-v2 |"
    if not repos:
        for m in re.finditer(r"\|\s*([\w][\w.-]+)\s*\|.*\|.*\|", body):
            repo_name = m.group(1).strip()
            # Skip table headers and common non-repo words
            if repo_name.lower() not in ("repo", "file", "risk", "change", "function", "description", "type"):
                if repo_name not in repos:
                    repos.append(repo_name)

    if repos and not primary_repo:
        primary_repo = repos[0]

    # ── Code Changes Table ────────────────────────────────────────────
    file_changes: list[FileChange] = []
    file_hints: list[str] = []

    # Find all markdown table rows with file paths
    # Pattern: | `file/path.py` | `function()` | **TYPE** | description |
    # Also handles: | file/path.py | function | TYPE | description |
    current_repo = ""
    for line in body.split("\n"):
        # Detect repo header formats:
        #   "### agent-platform-v2" / "### agent-platform-v2 (Primary)"
        #   "#### Repo 1: `agent-platform-v2` (Primary Changes)"
        repo_header = re.match(r"#{2,4}\s+(?:Repo\s+\d+:\s+)?`?([\w.-]+)`?", line)
        if repo_header and "/" not in repo_header.group(1):
            candidate = repo_header.group(1)
            # Skip generic headers like "Requirements", "Risks", etc.
            if not candidate[0].isupper() or candidate in repos or candidate.count("-") >= 1:
                current_repo = candidate
            continue

        # Parse table rows
        if "|" in line and not line.strip().startswith("|---"):
            cells = [c.strip().strip("`").strip("*") for c in line.split("|")]
            cells = [c for c in cells if c]  # remove empty

            if len(cells) >= 4:
                file_path = cells[0]
                function_name = cells[1]
                change_type = cells[2].upper()
                description = cells[3] if len(cells) > 3 else ""

                # Check if it looks like a real file path
                if "/" in file_path and "File" not in file_path and "---" not in file_path:
                    repo = current_repo or (primary_repo or "")
                    file_changes.append(FileChange(
                        repo=repo,
                        file=file_path,
                        function=function_name,
                        change_type=change_type,
                        description=description,
                    ))
                    file_hints.append(file_path)
                    # Also add function as hint if it looks meaningful
                    if function_name and function_name not in ("—", "-", "N/A", ""):
                        clean_func = function_name.rstrip("()")
                        if clean_func:
                            file_hints.append(clean_func)

    # ── Extract file paths from Root Cause Trace ──────────────────────
    # Pattern: `repo/path/file.py:function()` or backtick code refs
    for m in re.finditer(r"`([\w./\\-]+\.(?:py|ts|tsx|js|jsx|go|rs|java)(?::[\w()]+)?)`", body):
        hint = m.group(1)
        if hint not in file_hints:
            file_hints.append(hint)

    # Pattern: file.py:123 (line numbers in trace)
    for m in re.finditer(r"`?([\w./\\-]+\.(?:py|ts|js)):(\d+)`?", body):
        hint = f"{m.group(1)}:{m.group(2)}"
        if hint not in file_hints:
            file_hints.append(hint)

    # ── Implementation Order ──────────────────────────────────────────
    implementation_order: list[str] = []
    in_impl_section = False
    for line in body.split("\n"):
        if re.match(r"##\s+Implementation Order", line, re.IGNORECASE):
            in_impl_section = True
            continue
        if in_impl_section:
            if line.startswith("##") or line.startswith("---"):
                in_impl_section = False
                continue
            step_match = re.match(r"\d+\.\s+\*\*([\w.-]+)\*\*", line)
            if step_match:
                implementation_order.append(step_match.group(1))

    # Deduplicate file hints while preserving order
    seen: set[str] = set()
    unique_hints: list[str] = []
    for h in file_hints:
        if h.lower() not in seen:
            seen.add(h.lower())
            unique_hints.append(h)

    return PathfinderAnalysis(
        classification=classification,
        complexity=complexity,
        repos=repos,
        primary_repo=primary_repo,
        full_comment=body,
        file_changes=file_changes,
        file_hints=unique_hints,
        implementation_order=implementation_order,
    )
