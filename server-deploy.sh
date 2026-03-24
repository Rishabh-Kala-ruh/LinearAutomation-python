#!/bin/bash
set -e

echo "=============================================="
echo "  Linear Automation — Server Deployment"
echo "  OpenClaw (scheduler) + Claude Code (AI coder)"
echo "=============================================="
echo ""

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Check prerequisites ──────────────────────────────────────────────
echo "Checking prerequisites..."
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found. Install Python 3.12+ first."; exit 1; }
command -v gh >/dev/null 2>&1 || { echo "ERROR: gh CLI not found. Install: https://cli.github.com"; exit 1; }
command -v git >/dev/null 2>&1 || { echo "ERROR: git not found."; exit 1; }
command -v claude >/dev/null 2>&1 || { echo "ERROR: claude CLI not found. Install: npm i -g @anthropic-ai/claude-code"; exit 1; }
command -v openclaw >/dev/null 2>&1 || { echo "ERROR: openclaw not found. Install: npm i -g openclaw"; exit 1; }

echo "  python3:  $(python3 --version)"
echo "  gh:       $(gh --version | head -1)"
echo "  claude:   $(claude --version 2>&1 | head -1)"
echo "  openclaw: $(openclaw --version)"
echo ""

# ── Check .env ───────────────────────────────────────────────────────
if grep -q "xxxx" "$PROJECT_DIR/.env" 2>/dev/null; then
  echo "ERROR: Update .env with your Linear API key first!"
  echo "  Edit: $PROJECT_DIR/.env"
  echo "  Get key from: https://linear.app/settings/api"
  exit 1
fi

# ── Install Python deps ──────────────────────────────────────────────
echo "Installing dependencies..."
cd "$PROJECT_DIR"
pip install -r requirements.txt
echo ""

# ── Step 1: Authenticate Claude Code via Google OAuth ────────────────
echo "=============================================="
echo "  Step 1: Claude Code Authentication"
echo "=============================================="
echo ""
echo "Claude Code needs to be authenticated so it can fix code on this server."
echo "It uses your Google account (Claude Pro subscription) — no API key needed."
echo ""

# Check if already authenticated
if claude -p "echo hello" --output-format text >/dev/null 2>&1; then
  echo "  ✓ Claude Code is already authenticated!"
else
  echo "  Claude Code is NOT authenticated yet."
  echo ""
  echo "  To authenticate, run this on YOUR LAPTOP (where you have a browser):"
  echo ""
  echo "    claude setup-token"
  echo ""
  echo "  It will open your browser → log in with Google → copy the token."
  echo "  Then come back here and run:"
  echo ""
  echo "    claude login --setup-token \"PASTE_TOKEN_HERE\""
  echo ""
  echo "  After that, re-run this script."
  exit 1
fi
echo ""

# ── Step 2: Authenticate gh CLI ──────────────────────────────────────
echo "Checking GitHub CLI auth..."
if gh auth status >/dev/null 2>&1; then
  echo "  ✓ GitHub CLI is authenticated"
else
  echo "  GitHub CLI is NOT authenticated. Run: gh auth login"
  exit 1
fi
echo ""

# ── Step 3: Set up OpenClaw daemon (scheduler) ───────────────────────
echo "=============================================="
echo "  Step 2: OpenClaw Daemon (Scheduler)"
echo "=============================================="
echo ""
echo "OpenClaw runs as a background daemon and triggers the scan every hour."
echo ""

# Check if OpenClaw daemon is running
if openclaw gateway status 2>/dev/null | grep -qi "running"; then
  echo "  ✓ OpenClaw daemon is running"
else
  echo "  OpenClaw daemon is NOT running."
  echo "  Run: openclaw onboard --install-daemon"
  echo "  Then re-run this script."
  exit 1
fi
echo ""

# ── Step 4: Register hourly cron job ─────────────────────────────────
echo "=============================================="
echo "  Step 3: Register Hourly Cron Job"
echo "=============================================="
echo ""

# Remove existing job if present
openclaw cron list 2>/dev/null | grep -q "linear-ticket-scan" && \
  openclaw cron remove linear-ticket-scan 2>/dev/null || true

openclaw cron add \
  --name "linear-ticket-scan" \
  --every 3600000 \
  --session isolated \
  --message "Run the Linear ticket automation: execute 'cd $PROJECT_DIR && python3 run_once.py' in the terminal and report results." \
  --model "anthropic/claude-sonnet-4-20250514" \
  --thinking medium

echo "  ✓ Cron job registered: runs every 1 hour"
echo ""

# ── Done ─────────────────────────────────────────────────────────────
echo "=============================================="
echo "  Deployment Complete!"
echo "=============================================="
echo ""
echo "Architecture:"
echo "  ┌─────────────────────────────────────┐"
echo "  │ OpenClaw Daemon (always running)    │"
echo "  │   └─ Cron: every 1 hour             │"
echo "  │       └─ python3 run_once.py         │"
echo "  │           ├─ Fetch Linear tickets    │"
echo "  │           ├─ claude -p (fix code)    │"
echo "  │           ├─ git push                │"
echo "  │           └─ gh pr create            │"
echo "  └─────────────────────────────────────┘"
echo ""
echo "Auth:"
echo "  Claude Code → Google OAuth (your Pro subscription)"
echo "  GitHub CLI  → gh auth"
echo "  Linear      → API key in .env"
echo ""
echo "Commands:"
echo "  openclaw cron list                          - View scheduled jobs"
echo "  openclaw cron run linear-ticket-scan        - Run scan NOW"
echo "  openclaw cron runs --id linear-ticket-scan  - View run history"
echo "  python3 run_once.py                         - Manual one-time scan"
echo "  tail -f logs/automation.log                 - Watch logs"
echo ""
