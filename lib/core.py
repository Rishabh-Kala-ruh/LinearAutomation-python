"""
Shared core logic for both main.py (continuous loop) and run_once.py (single scan).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from lib.config import (
    LINEAR_API_KEY, GITHUB_ORG, TARGET_BRANCH, LOGS_DIR,
    CLAUDE_CMD, REPOS_DIR, REPO_MAP, PROCESSING_LABEL, DONE_LABEL,
)
from lib.linear_client import LinearClient
from skills.ticket_enricher import LinearEnricher, EnrichedContext, build_enriched_prompt


# ── Types ────────────────────────────────────────────────────────────────────

class RepoEntry:
    def __init__(self, name: str, clone_url: str | None = None) -> None:
        self.name = name
        self.clone_url = clone_url


# ── Init ─────────────────────────────────────────────────────────────────────

linear = LinearClient(LINEAR_API_KEY)
enricher = LinearEnricher(LINEAR_API_KEY)

os.makedirs(LOGS_DIR, exist_ok=True)

PROCESSED_FILE = os.path.join(LOGS_DIR, "processed_issues.json")
processed_issues: set[str] = set()
if os.path.exists(PROCESSED_FILE):
    try:
        with open(PROCESSED_FILE) as f:
            processed_issues = set(json.load(f))
    except Exception:
        pass


# ── Helpers ──────────────────────────────────────────────────────────────────

def save_processed() -> None:
    with open(PROCESSED_FILE, "w") as f:
        json.dump(list(processed_issues), f, indent=2)


def log(msg: str) -> None:
    ts = datetime.utcnow().isoformat() + "Z"
    line = f"[{ts}] {msg}"
    print(line)
    with open(os.path.join(LOGS_DIR, "automation.log"), "a") as f:
        f.write(line + "\n")


EXTRA_PATHS = ":".join([
    os.path.join(Path.home(), ".npm-global/bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
])


def shell(cmd: str, cwd: str | None = None, timeout: int = 600) -> str:
    env = {**os.environ, "PATH": f"{EXTRA_PATHS}:{os.environ.get('PATH', '')}"}
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        cwd=cwd, timeout=timeout, env=env,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=result.stderr,
        )
    return result.stdout.strip()


# ── Repo Detection ───────────────────────────────────────────────────────────

def detect_repos(
    issue: dict[str, Any], labels: list[str], team_key: str, project_name: str | None
) -> list[RepoEntry]:
    seen: set[str] = set()
    repos: list[RepoEntry] = []

    def add_repo(name: str, clone_url: str | None = None) -> None:
        if name.lower() not in seen:
            seen.add(name.lower())
            repos.append(RepoEntry(name, clone_url))

    # 1. repo: labels
    for label in labels:
        if label.lower().startswith("repo:"):
            add_repo(label.split(":", 1)[1].strip())
    if repos:
        return repos

    # 2. GitHub URLs in description
    desc = issue.get("description") or ""
    for m in re.finditer(r"github\.com/([\w.-]+)/([\w.-]+)", desc):
        owner, repo = m.group(1), m.group(2).removesuffix(".git")
        add_repo(repo, f"git@github.com:{owner}/{repo}.git")
    if repos:
        return repos

    # 3. Text patterns: Repository: name / Repo: name
    for m in re.finditer(r"(?:repository|repo)\s*:\s*([\w.-]+)", desc, re.IGNORECASE):
        add_repo(m.group(1).strip())
    if repos:
        return repos

    # 4. Project name
    if project_name:
        return [RepoEntry(project_name.lower().replace(" ", "-"))]

    # 5. Team key fallback
    return [RepoEntry(team_key.lower())]


# ── Repo Management ─────────────────────────────────────────────────────────

def get_repo_path(repo_name: str, clone_url: str | None) -> str:
    repo_path = REPO_MAP.get(repo_name) or REPO_MAP.get(repo_name.lower())

    if not repo_path:
        os.makedirs(REPOS_DIR, exist_ok=True)
        repo_path = os.path.join(REPOS_DIR, repo_name)

        if not os.path.exists(repo_path):
            url = clone_url or f"git@github.com:{GITHUB_ORG}/{repo_name}.git"
            log(f'Repo "{repo_name}" not in REPO_MAP — auto-cloning from {url}...')
            shell(f'git clone {url} "{repo_path}"')
            log(f"Cloned to {repo_path}")

    if not os.path.exists(repo_path):
        raise RuntimeError(f"Repo path does not exist: {repo_path}")
    if not os.path.exists(os.path.join(repo_path, ".git")):
        raise RuntimeError(f"Not a git repo: {repo_path}")

    log(f"Updating {repo_name} at {repo_path}...")
    shell("git fetch origin", cwd=repo_path)
    try:
        shell(f"git rev-parse --verify origin/{TARGET_BRANCH}", cwd=repo_path)
        shell(f"git checkout {TARGET_BRANCH}", cwd=repo_path)
        shell(f"git pull origin {TARGET_BRANCH}", cwd=repo_path)
    except Exception:
        try:
            shell("git checkout main && git pull origin main", cwd=repo_path)
        except Exception:
            shell("git checkout master && git pull origin master", cwd=repo_path)

    return repo_path


def create_worktree(repo_path: str, branch_name: str) -> str:
    worktree_path = os.path.join(repo_path, ".worktrees", branch_name)
    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)

    try:
        shell(f'git worktree remove "{worktree_path}" --force', cwd=repo_path)
    except Exception:
        pass
    try:
        shell(f'git branch -D "{branch_name}"', cwd=repo_path)
    except Exception:
        pass

    # Detect base branch
    base_branch = "origin/master"
    try:
        shell(f"git rev-parse --verify origin/{TARGET_BRANCH}", cwd=repo_path)
        base_branch = f"origin/{TARGET_BRANCH}"
    except Exception:
        try:
            shell("git rev-parse --verify origin/main", cwd=repo_path)
            base_branch = "origin/main"
        except Exception:
            pass

    shell(f'git worktree add -b "{branch_name}" "{worktree_path}" {base_branch}', cwd=repo_path)
    log(f"Created worktree at {worktree_path} (branch: {branch_name})")
    return worktree_path


def cleanup_worktree(repo_path: str, worktree_path: str) -> None:
    try:
        shell(f'git worktree remove "{worktree_path}" --force', cwd=repo_path)
    except Exception:
        pass


# ── Claude Code ──────────────────────────────────────────────────────────────

def run_claude_code(worktree_path: str, issue: dict[str, Any], repo_name: str) -> bool:
    identifier = issue["identifier"]
    log(f"Enriching ticket {identifier} with deep context...")

    try:
        enriched_context = enricher.enrich(issue)
        log(
            f"Enriched: {len(enriched_context.comments)} comments, "
            f"{len(enriched_context.sub_issues)} sub-issues, "
            f"{len(enriched_context.relations)} relations, "
            f"{len(enriched_context.file_hints)} file hints"
        )
    except Exception as err:
        log(f"Enrichment failed, falling back to basic context: {err}")
        enriched_context = EnrichedContext(
            source="linear",
            id=identifier,
            title=issue["title"],
            description=issue.get("description") or "No description.",
            url=issue["url"],
            priority="Unknown",
            status="Unknown",
            created_at=issue.get("createdAt", ""),
            updated_at=issue.get("updatedAt", ""),
        )

    prompt = build_enriched_prompt(enriched_context, worktree_path, repo_name)
    prompt_file = os.path.join(LOGS_DIR, f"prompt_{identifier}.txt")
    log_file = os.path.join(LOGS_DIR, f"claude_{identifier}.log")

    with open(prompt_file, "w") as f:
        f.write(prompt)

    log(f"Running Claude Code ({CLAUDE_CMD}) for {identifier} in {worktree_path}...")
    try:
        output = shell(
            f"""{CLAUDE_CMD} -p "$(cat '{prompt_file}')" """
            f"""--allowedTools "Bash" "Read" "Edit" "Write" "Glob" "Grep" """
            f"""--max-turns 30 --output-format text""",
            cwd=worktree_path,
            timeout=900,
        )
        with open(log_file, "w") as f:
            f.write(output)
        log(f"Claude Code finished for {identifier}")
        return True
    except subprocess.CalledProcessError as err:
        log(f"Claude Code failed for {identifier}: {err}")
        stdout = err.output or ""
        with open(log_file, "w") as f:
            f.write(f"ERROR: {err}\n{stdout}")
        return False


# ── PR Creation ──────────────────────────────────────────────────────────────

def push_and_create_pr(
    worktree_path: str, repo_name: str, branch_name: str, issue: dict[str, Any]
) -> str | None:
    identifier = issue["identifier"]

    try:
        diff = shell("git diff HEAD~1 --stat", cwd=worktree_path)
        if not diff:
            log(f"No changes for {identifier}")
            return None
    except Exception:
        log(f"No commits for {identifier}")
        return None

    if os.path.exists(os.path.join(worktree_path, "CLAUDE_UNABLE.md")):
        log(f"Claude Code unable to fix {identifier}")
        return None

    log(f"Pushing {branch_name}...")
    shell(f'git push origin "{branch_name}"', cwd=worktree_path)

    remote_url = shell("git remote get-url origin", cwd=worktree_path)
    repo_match = re.search(r"[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?$", remote_url)
    gh_repo = f"{repo_match.group(1)}/{repo_match.group(2)}" if repo_match else f"{GITHUB_ORG}/{repo_name}"
    log(f"Detected GitHub repo: {gh_repo}")

    title = issue["title"]
    description = issue.get("description") or "N/A"
    url = issue["url"]

    pr_body = (
        f"## {identifier}: {title}\n\n"
        f"### Linear Ticket\n{url}\n\n"
        f"### Description\n{description}\n\n"
        f"---\n*Automated by Linear-Claude Automation*"
    )
    pr_body_file = os.path.join(LOGS_DIR, f"pr_body_{identifier}.txt")
    with open(pr_body_file, "w") as f:
        f.write(pr_body)

    try:
        return shell(
            f'gh pr create --repo "{gh_repo}" --base "{TARGET_BRANCH}" '
            f'--head "{branch_name}" --title "fix({identifier}): {title}" '
            f'--body-file "{pr_body_file}"',
            cwd=worktree_path,
        )
    except Exception:
        try:
            return shell(
                f'gh pr create --repo "{gh_repo}" --base main '
                f'--head "{branch_name}" --title "fix({identifier}): {title}" '
                f'--body-file "{pr_body_file}"',
                cwd=worktree_path,
            )
        except Exception as err:
            log(f"PR creation failed: {err}")
            return None


# ── Linear Updates ───────────────────────────────────────────────────────────

def transition_issue(issue: dict[str, Any], state_type: str) -> None:
    try:
        team_id = linear.get_issue_team_id(issue["id"])
        if not team_id:
            return
        states = linear.get_team_states(team_id)
        target_state = next((s for s in states if s["type"] == state_type), None)
        if target_state:
            linear.update_issue(issue["id"], target_state["id"])
            log(f'Moved {issue["identifier"]} to "{target_state["name"]}"')
        else:
            log(f'No state of type "{state_type}" found for team')
    except Exception as err:
        log(f'Failed to transition {issue["identifier"]}: {err}')


def comment_on_issue(issue_id: str, body: str) -> None:
    try:
        linear.create_comment(issue_id, body)
    except Exception as err:
        log(f"Comment failed: {err}")


# ── Main Processing Loop ────────────────────────────────────────────────────

def process_tickets() -> None:
    log("=== Starting ticket scan ===")
    try:
        me = linear.get_viewer()
        log(f'Authenticated as: {me["name"]} ({me["email"]})')

        teams = linear.get_teams()
        for team in teams:
            log(f'Scanning team: {team["name"]} ({team["key"]})')
            issues = linear.get_issues(team["id"], me["id"], first=20)

            for issue in issues:
                if issue["id"] in processed_issues:
                    continue

                labels = linear.get_issue_labels(issue["id"])
                labels_lower = [l.lower() for l in labels]
                if PROCESSING_LABEL in labels_lower or DONE_LABEL in labels_lower:
                    continue

                identifier = issue["identifier"]
                title = issue["title"]
                log(f"\nProcessing: {identifier} - {title}")
                log(f"Labels: {', '.join(labels)}")

                project_name: str | None = None
                try:
                    project_name = linear.get_issue_project_name(issue["id"])
                except Exception:
                    pass

                repo_entries = detect_repos(issue, labels, team["key"], project_name)
                log(f"Detected repos: {', '.join(r.clone_url or f'{GITHUB_ORG}/{r.name}' for r in repo_entries)}")

                try:
                    transition_issue(issue, "started")
                    pr_urls: list[str] = []

                    for entry in repo_entries:
                        log(f"  Working on repo: {entry.name}")
                        try:
                            repo_path = get_repo_path(entry.name, entry.clone_url)
                            branch_name = f"claude/{identifier.lower()}"
                            worktree_path = create_worktree(repo_path, branch_name)
                            success = run_claude_code(worktree_path, issue, entry.name)

                            if success:
                                pr_url = push_and_create_pr(worktree_path, entry.name, branch_name, issue)
                                if pr_url:
                                    pr_urls.append(pr_url)

                            cleanup_worktree(repo_path, worktree_path)
                        except Exception as err:
                            log(f"  Error on repo {entry.name}: {err}")

                    if pr_urls:
                        transition_issue(issue, "completed")
                        pr_list = "\n".join(f"- {url}" for url in pr_urls)
                        comment_on_issue(
                            issue["id"],
                            f"🤖 **Claude Code** created {len(pr_urls)} PR(s):\n\n{pr_list}\n\nPlease review.",
                        )
                        log(f"Done: {identifier} -> {', '.join(pr_urls)}")
                        processed_issues.add(issue["id"])
                        save_processed()
                    else:
                        log(f"No PRs created for {identifier} — will retry next run")
                except Exception as err:
                    log(f"Error: {identifier}: {err} — will retry next run")
    except Exception as err:
        log(f"Scan error: {err}")
    log("=== Scan complete ===")
