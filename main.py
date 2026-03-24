#!/usr/bin/env python3
"""
Continuous loop mode: polls Linear every POLL_INTERVAL minutes.
Scheduling: OpenClaw daemon (server) or run directly on laptop.
AI Coder: Claude Code CLI (`claude -p`) — authenticated via Google OAuth.
"""

import sys
import time

from lib.config import LINEAR_API_KEY, GITHUB_ORG, TARGET_BRANCH, POLL_INTERVAL
from lib.core import process_tickets, log


def main() -> None:
    log("Linear-Claude Automation started")
    log(f"Config: org={GITHUB_ORG}, target={TARGET_BRANCH}, interval={POLL_INTERVAL // 60}min")

    if not LINEAR_API_KEY or "xxxx" in LINEAR_API_KEY:
        log("ERROR: Please set LINEAR_API_KEY in .env file")
        log("Get your API key from: https://linear.app/settings/api")
        sys.exit(1)

    process_tickets()

    log(f"Next scan in {POLL_INTERVAL // 60} minutes...")
    while True:
        time.sleep(POLL_INTERVAL)
        process_tickets()
        log(f"Next scan in {POLL_INTERVAL // 60} minutes...")


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        log(f"Fatal error: {err}")
        sys.exit(1)
