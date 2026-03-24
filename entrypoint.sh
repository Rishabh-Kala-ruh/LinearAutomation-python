#!/bin/bash
set -e

echo "=============================================="
echo "  Linear Automation Container Starting"
echo "=============================================="

# ── SSH Key Setup ────────────────────────────────────────────────
if [ -f /root/.ssh/id_ed25519 ]; then
  # Copy from read-only mount to writable location, then fix perms
  cp /root/.ssh/id_ed25519 /tmp/ssh_key
  mkdir -p /root/.ssh_rw
  mv /tmp/ssh_key /root/.ssh_rw/id_ed25519
  chmod 600 /root/.ssh_rw/id_ed25519
  # Point git/ssh to use this key (exported so child processes inherit it)
  export GIT_SSH_COMMAND="ssh -i /root/.ssh_rw/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
  # Also configure git globally as fallback
  git config --global core.sshCommand "$GIT_SSH_COMMAND"
  echo "  ✓ SSH key found and configured"
else
  echo "  ⚠ No SSH key mounted at /root/.ssh/id_ed25519"
  echo "    Repos will be cloned via HTTPS instead"
fi

# Add GitHub to known hosts
ssh-keyscan -H github.com >> /root/.ssh/known_hosts 2>/dev/null || true

# ── Claude Code Auth ─────────────────────────────────────────────
if [ -n "$CLAUDE_SETUP_TOKEN" ]; then
  echo "  Setting up Claude Code auth via setup-token..."
  claude login --setup-token "$CLAUDE_SETUP_TOKEN" 2>/dev/null || true
  echo "  ✓ Claude Code authenticated"
elif [ -d /root/.claude ]; then
  echo "  ✓ Claude Code credentials mounted"
else
  echo "  ⚠ No Claude Code auth found"
  echo "    Set CLAUDE_SETUP_TOKEN env var or mount ~/.claude"
fi

# ── GitHub CLI Auth ──────────────────────────────────────────────
if [ -n "$GH_TOKEN" ]; then
  echo "  ✓ GitHub CLI auth via GH_TOKEN"
elif gh auth status >/dev/null 2>&1; then
  echo "  ✓ GitHub CLI already authenticated"
else
  echo "  ⚠ GitHub CLI not authenticated"
  echo "    Set GH_TOKEN env var or mount ~/.config/gh"
fi

echo ""
echo "  Config:"
echo "    GITHUB_ORG=$GITHUB_ORG"
echo "    TARGET_BRANCH=$TARGET_BRANCH"
echo "    POLL_INTERVAL=${POLL_INTERVAL_MINUTES}min"
echo "    CLAUDE_CMD=$CLAUDE_CMD"
echo ""
echo "  Starting automation loop..."
echo "=============================================="
echo ""

# Run the continuous loop
exec python main.py
