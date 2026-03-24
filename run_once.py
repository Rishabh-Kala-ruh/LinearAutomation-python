#!/usr/bin/env python3
"""
Single-run mode: process tickets once and exit.
Scheduling: OpenClaw daemon (server) or launchd/cron (laptop).
AI Coder: Claude Code CLI (`claude -p`) — authenticated via Google OAuth.
"""

import sys

from lib.config import LINEAR_API_KEY
from lib.core import process_tickets, log

if not LINEAR_API_KEY or "xxxx" in LINEAR_API_KEY:
    log("ERROR: Set LINEAR_API_KEY in .env")
    sys.exit(1)

process_tickets()
log("Run-once complete. Exiting.")
