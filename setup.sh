#!/bin/bash
set -e

echo "=== Linear-Claude Automation Setup (Laptop) ==="
echo ""

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.linear-claude.automation.plist"
PLIST_SRC="$PROJECT_DIR/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"

# Check prerequisites
echo "Checking prerequisites..."
command -v python3 >/dev/null 2>&1 || { echo "Error: python3 not found"; exit 1; }
command -v gh >/dev/null 2>&1 || { echo "Error: gh (GitHub CLI) not found"; exit 1; }
command -v claude >/dev/null 2>&1 || { echo "Error: claude CLI not found. Install: npm i -g @anthropic-ai/claude-code"; exit 1; }

echo "  python3: $(python3 --version)"
echo "  gh:      $(gh --version | head -1)"
echo "  claude:  found"
echo ""

# Check .env
if grep -q "xxxx" "$PROJECT_DIR/.env" 2>/dev/null; then
  echo "⚠  Please update .env with your Linear API key first!"
  echo "   Edit: $PROJECT_DIR/.env"
  echo "   Get key from: https://linear.app/settings/api"
  exit 1
fi

# Install dependencies
echo "Installing dependencies..."
cd "$PROJECT_DIR"
pip install -r requirements.txt

# Install launchd plist
echo ""
echo "Installing launchd agent (runs every 1 hour)..."
mkdir -p "$HOME/Library/LaunchAgents"

# Unload existing if present
launchctl unload "$PLIST_DST" 2>/dev/null || true

cp "$PLIST_SRC" "$PLIST_DST"
launchctl load "$PLIST_DST"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "The automation will:"
echo "  1. Run immediately on load"
echo "  2. Then every 1 hour"
echo "  3. Scan Linear for open tickets assigned to you"
echo "  4. Use Claude Code (claude -p) to fix issues"
echo "  5. Create PRs to 'dev' branch"
echo ""
echo "Useful commands:"
echo "  python main.py        - Run the continuous loop (alternative to launchd)"
echo "  python run_once.py    - Run once and exit"
echo "  launchctl unload ~/Library/LaunchAgents/$PLIST_NAME  - Stop scheduled runs"
echo "  tail -f logs/automation.log  - Watch logs"
echo ""
