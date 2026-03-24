---
name: linear_ticket_scanner
description: Triggers the Linear ticket automation script (run_once.py) which fetches open tickets, uses Claude Code CLI to fix them, and creates PRs.
---

# Linear Ticket Scanner

This skill triggers the Linear automation pipeline.

## What It Does

When triggered (via cron or manually), it runs `python3 run_once.py` which:

1. Fetches open Linear tickets assigned to you
2. Enriches each ticket with comments, sub-issues, file hints
3. Runs **Claude Code CLI** (`claude -p`) to fix the code in a git worktree
4. Pushes the branch and creates a PR to dev
5. Comments on the Linear ticket with the PR link

## Important

- OpenClaw is the **scheduler** — it triggers this script on a cron schedule
- Claude Code is the **AI coder** — it reads code, writes fixes, and commits
- Both run on the same server, authenticated separately
