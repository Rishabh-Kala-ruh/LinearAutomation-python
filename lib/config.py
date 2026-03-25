"""
Configuration module — loads .env and exposes all settings.
"""

import os
import sys
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

LINEAR_API_KEY: str = os.getenv("LINEAR_API_KEY", "")
GITHUB_ORG: str = os.getenv("GITHUB_ORG", "ruh-ai")
TARGET_BRANCH: str = os.getenv("TARGET_BRANCH", "dev")
LOGS_DIR: str = str(Path(os.getenv("LOGS_DIR", "./logs")).resolve())
CLAUDE_CMD: str = os.getenv("CLAUDE_CMD", "claude")
REPOS_DIR: str = str(Path(os.getenv("REPOS_DIR", "./repos")).resolve())
POLL_INTERVAL: int = (int(os.getenv("POLL_INTERVAL_MINUTES", "60")) or 60) * 60  # seconds

REPO_MAP: dict[str, str] = {}
try:
    REPO_MAP = json.loads(os.getenv("REPO_MAP", "{}"))
except json.JSONDecodeError:
    print("ERROR: REPO_MAP in .env is not valid JSON")
    sys.exit(1)

MAX_CONCURRENT_TICKETS: int = int(os.getenv("MAX_CONCURRENT_TICKETS", "2"))

# Path to Sentinel Guardian skills directory (for test generation)
# Set to empty string or omit to disable Sentinel integration
SENTINEL_SKILLS_PATH: str = os.getenv(
    "SENTINEL_SKILLS_PATH",
    os.path.expanduser("~/.openclaw/workspace/sentinel-guardian/skills"),
)

PROCESSING_LABEL = "claude-processing"
DONE_LABEL = "claude-done"
