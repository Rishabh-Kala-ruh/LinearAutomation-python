"""
Shared core logic for both main.py (continuous loop) and run_once.py (single scan).

Architecture: 3-phase processing
  Phase 1 — COLLECT: fetch all issues, filter, sort by priority
  Phase 2 — PREPARE: clone/update all unique repos in parallel
  Phase 3 — EXECUTE: process tickets in parallel (max N workers)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from lib.config import (
    LINEAR_API_KEY, GITHUB_ORG, TARGET_BRANCH, LOGS_DIR,
    CLAUDE_CMD, REPOS_DIR, REPO_MAP, PROCESSING_LABEL, DONE_LABEL,
    MAX_CONCURRENT_TICKETS, SENTINEL_SKILLS_PATH,
)
from lib.linear_client import LinearClient
from skills.developer_skill import DeveloperSkill, DeveloperResult


# ── Types ────────────────────────────────────────────────────────────────────

class RepoEntry:
    def __init__(self, name: str, clone_url: str | None = None) -> None:
        self.name = name
        self.clone_url = clone_url


# ── Init ─────────────────────────────────────────────────────────────────────

linear = LinearClient(LINEAR_API_KEY)
# Developer skill is initialized after we know the viewer_id (in process_tickets)
dev_skill: DeveloperSkill | None = None

os.makedirs(LOGS_DIR, exist_ok=True)

PROCESSED_FILE = os.path.join(LOGS_DIR, "processed_issues.json")
processed_issues: set[str] = set()
if os.path.exists(PROCESSED_FILE):
    try:
        with open(PROCESSED_FILE) as f:
            processed_issues = set(json.load(f))
    except Exception:
        pass

# Thread-safety locks
_processed_lock = threading.Lock()
_repo_locks: dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()


def _get_repo_lock(repo_name: str) -> threading.Lock:
    """Get or create a per-repo lock to prevent concurrent git operations on the same repo."""
    with _repo_locks_guard:
        if repo_name not in _repo_locks:
            _repo_locks[repo_name] = threading.Lock()
        return _repo_locks[repo_name]


# ── Helpers ──────────────────────────────────────────────────────────────────

def save_processed() -> None:
    with _processed_lock:
        with open(PROCESSED_FILE, "w") as f:
            json.dump(list(processed_issues), f, indent=2)


def log(msg: str) -> None:
    ts = datetime.utcnow().isoformat() + "Z"
    line = f"[{ts}] {msg}"
    print(line, flush=True)
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


# ── Priority ─────────────────────────────────────────────────────────────────

# Linear priority: 0=none, 1=urgent, 2=high, 3=medium, 4=low
# Sort key: urgent first (1), then high (2), medium (3), low (4), none last (5)
PRIORITY_SORT_KEY = {1: 1, 2: 2, 3: 3, 4: 4, 0: 5}
PRIORITY_NAMES = {0: "None", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}


def _priority_sort_key(issue: dict[str, Any]) -> int:
    return PRIORITY_SORT_KEY.get(issue.get("priority", 0), 5)


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

def _run_claude(identifier: str, prompt_file: str, log_file: str, worktree_path: str, phase: str) -> bool:
    """Execute a single Claude Code invocation."""
    log(f"Running Claude Code ({CLAUDE_CMD}) — {phase} for {identifier}...")
    try:
        output = shell(
            f"""{CLAUDE_CMD} -p "$(cat '{prompt_file}')" """
            f"""--allowedTools "Bash" "Read" "Edit" "Write" "Glob" "Grep" """
            f"""--max-turns 30 --output-format text""",
            cwd=worktree_path,
            timeout=900,
        )
        with open(log_file, "a") as f:
            f.write(f"\n{'='*60}\n{phase}\n{'='*60}\n{output}\n")
        log(f"Claude Code {phase} finished for {identifier}")
        return True
    except subprocess.CalledProcessError as err:
        log(f"Claude Code {phase} failed for {identifier}: {err}")
        stdout = err.output or ""
        with open(log_file, "a") as f:
            f.write(f"\n{'='*60}\n{phase} — ERROR\n{'='*60}\n{err}\n{stdout}\n")
        return False


def run_claude_code(
    worktree_path: str, issue: dict[str, Any], repo_name: str, team_key: str
) -> bool:
    identifier = issue["identifier"]
    log_file = os.path.join(LOGS_DIR, f"claude_{identifier}.log")

    # Clear log file for fresh run
    with open(log_file, "w") as f:
        f.write(f"# Claude Code log for {identifier}\n")

    if not dev_skill:
        log(f"ERROR: Developer Skill not initialized for {identifier}")
        return False

    # Use developer skill for scope-aware prompt building
    log(f"Developer Skill processing {identifier}...")
    try:
        result = dev_skill.process(issue, team_key, worktree_path, repo_name)
    except RuntimeError as err:
        log(f"FAILED: {identifier} — {err}")
        with open(log_file, "a") as f:
            f.write(f"\nFAILED: {err}\n")
        return False

    ctx = result.enriched_context
    pf = result.pathfinder
    pf_info = f"Pathfinder: {pf.classification}/{pf.complexity}, repos={pf.repos}" if pf else "Pathfinder: not found"
    log(
        f"Scope: {result.scope_type} | Stack: {result.stack_type} | "
        f"Test phases: {len(result.test_phases)} | {pf_info} | "
        f"Enriched: {len(ctx.comments)} comments, "
        f"{len(ctx.sub_issues)} sub-issues, "
        f"{len(ctx.relations)} relations, "
        f"{len(ctx.file_hints)} file hints"
    )

    # ── Sentinel Test Phases (sequential) ─────────────────────────────
    total_phases = len(result.test_phases)
    for i, (skill_name, prompt) in enumerate(result.test_phases, 1):
        phase_label = f"Phase {i}/{total_phases}: {skill_name}"
        prompt_file = os.path.join(LOGS_DIR, f"prompt_{skill_name}_{identifier}.txt")
        with open(prompt_file, "w") as f:
            f.write(prompt)

        log(f"[{identifier}] Starting {phase_label}...")
        ok = _run_claude(identifier, prompt_file, log_file, worktree_path, phase_label)
        if not ok:
            log(f"[{identifier}] {phase_label} failed — stopping test generation")
            return False
        log(f"[{identifier}] {phase_label} complete.")

    # ── Final Phase: Implementation ───────────────────────────────────
    log(f"[{identifier}] All test phases complete. Starting implementation...")
    impl_prompt_file = os.path.join(LOGS_DIR, f"prompt_impl_{identifier}.txt")
    with open(impl_prompt_file, "w") as f:
        f.write(result.impl_prompt)

    return _run_claude(identifier, impl_prompt_file, log_file, worktree_path, "Final Phase: Implementation")




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

def transition_issue(issue: dict[str, Any], state_type: str, state_name: str | None = None) -> None:
    """Transition issue to a target state. Matches by name first (if provided), then by type."""
    try:
        team_id = (issue.get("team") or {}).get("id") or linear.get_issue_team_id(issue["id"])
        if not team_id:
            return
        states = linear.get_team_states(team_id)
        target_state = None
        if state_name:
            target_state = next((s for s in states if s["name"].lower() == state_name.lower()), None)
        if not target_state:
            target_state = next((s for s in states if s["type"] == state_type), None)
        if target_state:
            linear.update_issue(issue["id"], target_state["id"])
            log(f'Moved {issue["identifier"]} to "{target_state["name"]}"')
        else:
            log(f'No state matching name="{state_name}" or type="{state_type}" found for team')
    except Exception as err:
        log(f'Failed to transition {issue["identifier"]}: {err}')


def comment_on_issue(issue_id: str, body: str) -> None:
    try:
        linear.create_comment(issue_id, body)
    except Exception as err:
        log(f"Comment failed: {err}")


# ── Single Issue Processor (runs in thread) ──────────────────────────────────

def _process_single_issue(issue: dict[str, Any], team_key: str) -> None:
    """Process one ticket end-to-end. Thread-safe — uses per-repo locks."""
    identifier = issue["identifier"]
    priority_name = PRIORITY_NAMES.get(issue.get("priority", 0), "None")

    # Extract labels and project from the batch query result
    labels = [l["name"] for l in (issue.get("labels") or {}).get("nodes", [])]
    project_name = (issue.get("project") or {}).get("name")

    log(f"\n[{priority_name}] Processing: {identifier} - {issue['title']}")
    log(f"Labels: {', '.join(labels) or 'none'}")

    repo_entries = detect_repos(issue, labels, team_key, project_name)
    log(f"Detected repos: {', '.join(r.clone_url or f'{GITHUB_ORG}/{r.name}' for r in repo_entries)}")

    try:
        transition_issue(issue, "started")
        pr_urls: list[str] = []

        for entry in repo_entries:
            log(f"  [{identifier}] Working on repo: {entry.name}")
            try:
                # Per-repo lock: prevents concurrent clone/fetch on the same repo
                with _get_repo_lock(entry.name):
                    repo_path = get_repo_path(entry.name, entry.clone_url)
                    branch_name = f"claude/{identifier.lower()}"
                    worktree_path = create_worktree(repo_path, branch_name)

                # Claude Code and PR creation run outside the repo lock
                # (worktrees are fully isolated)
                success = run_claude_code(worktree_path, issue, entry.name, team_key)

                if success:
                    pr_url = push_and_create_pr(worktree_path, entry.name, branch_name, issue)
                    if pr_url:
                        pr_urls.append(pr_url)

                cleanup_worktree(repo_path, worktree_path)
            except Exception as err:
                log(f"  [{identifier}] Error on repo {entry.name}: {err}")

        if pr_urls:
            transition_issue(issue, "started", state_name="Code Review")
            pr_list = "\n".join(f"- {url}" for url in pr_urls)
            comment_on_issue(
                issue["id"],
                f"🤖 **Claude Code** created {len(pr_urls)} PR(s):\n\n{pr_list}\n\nPlease review.",
            )
            log(f"Done: {identifier} -> {', '.join(pr_urls)}")
            with _processed_lock:
                processed_issues.add(issue["id"])
            save_processed()
        else:
            log(f"No PRs created for {identifier} — will retry next run")
    except Exception as err:
        log(f"Error: {identifier}: {err} — will retry next run")


# ── Main Processing Loop (3-phase) ──────────────────────────────────────────

def process_tickets() -> None:
    global dev_skill
    log("=== Starting ticket scan ===")
    log(f"Max concurrent tickets: {MAX_CONCURRENT_TICKETS}")
    try:
        me = linear.get_viewer()
        log(f'Authenticated as: {me["name"]} ({me["email"]})')

        # Initialize developer skill with viewer ID and Sentinel path
        dev_skill = DeveloperSkill(
            LINEAR_API_KEY, me["id"], GITHUB_ORG,
            sentinel_skills_path=SENTINEL_SKILLS_PATH,
        )
        if not dev_skill.sentinel_available:
            log("ERROR: Sentinel Guardian skills not found! Tickets will NOT be processed.")
            log(f"  Expected path: {SENTINEL_SKILLS_PATH}")
            log("  Ensure skills are mounted in Docker or exist at the configured path.")
        else:
            available_skills = dev_skill.sentinel.get_available_skills()
            log(f"Sentinel Guardian: enabled ({len(available_skills)} skills available)")

        teams = linear.get_teams()

        # ── Phase 1: COLLECT — fetch all eligible issues, sorted by priority ──

        eligible: list[tuple[dict[str, Any], str]] = []  # (issue, team_key)

        for team in teams:
            log(f'Scanning team: {team["name"]} ({team["key"]})')
            issues = linear.get_issues_with_labels(team["id"], me["id"], first=20)

            for issue in issues:
                if issue["id"] in processed_issues:
                    continue

                labels_lower = [l["name"].lower() for l in (issue.get("labels") or {}).get("nodes", [])]
                if PROCESSING_LABEL in labels_lower or DONE_LABEL in labels_lower:
                    continue

                eligible.append((issue, team["key"]))

        if not eligible:
            log("No eligible tickets found.")
            log("=== Scan complete ===")
            return

        # Sort by priority: Urgent (1) > High (2) > Medium (3) > Low (4) > None (0)
        eligible.sort(key=lambda x: _priority_sort_key(x[0]))

        priority_summary = ", ".join(
            f"{issue['identifier']}({PRIORITY_NAMES.get(issue.get('priority', 0), 'None')})"
            for issue, _ in eligible
        )
        log(f"Eligible tickets ({len(eligible)}), priority order: {priority_summary}")

        # ── Phase 2: PREPARE — clone/update all unique repos in parallel ──

        all_repo_entries: dict[str, RepoEntry] = {}  # repo_name -> entry (deduplicated)
        issue_repos: list[tuple[dict[str, Any], str, list[RepoEntry]]] = []

        for issue, team_key in eligible:
            labels = [l["name"] for l in (issue.get("labels") or {}).get("nodes", [])]
            project_name = (issue.get("project") or {}).get("name")
            entries = detect_repos(issue, labels, team_key, project_name)
            issue_repos.append((issue, team_key, entries))
            for entry in entries:
                if entry.name.lower() not in all_repo_entries:
                    all_repo_entries[entry.name.lower()] = entry

        if all_repo_entries:
            unique_repos = list(all_repo_entries.values())
            log(f"Preparing {len(unique_repos)} unique repo(s): {', '.join(r.name for r in unique_repos)}")

            def _prepare_repo(entry: RepoEntry) -> None:
                try:
                    with _get_repo_lock(entry.name):
                        get_repo_path(entry.name, entry.clone_url)
                except Exception as err:
                    log(f"Failed to prepare repo {entry.name}: {err}")

            with ThreadPoolExecutor(max_workers=min(len(unique_repos), 4)) as pool:
                list(pool.map(_prepare_repo, unique_repos))

            log("Repos ready.")

        # ── Phase 3: EXECUTE — process tickets in parallel ──

        log(f"Processing {len(eligible)} ticket(s) with {MAX_CONCURRENT_TICKETS} worker(s)...")

        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TICKETS) as pool:
            futures = {
                pool.submit(_process_single_issue, issue, team_key): issue["identifier"]
                for issue, team_key, _ in issue_repos
            }
            for future in as_completed(futures):
                identifier = futures[future]
                try:
                    future.result()
                except Exception as err:
                    log(f"Unhandled error processing {identifier}: {err}")

    except Exception as err:
        log(f"Scan error: {err}")
    log("=== Scan complete ===")
