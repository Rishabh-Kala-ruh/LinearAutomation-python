# Linear Automation (Python) - AI-Powered Ticket Resolution

Automatically picks up Linear tickets assigned to you, uses **Claude Code AI** to analyze and fix them, and creates **Pull Requests** on GitHub -- every 1 hour, with zero manual intervention.

## Architecture

```
+-----------------------------------------------------------------------+
|                  SERVER (VPS / Cloud VM)                               |
|                                                                       |
|  +---------------------------+    +--------------------------------+  |
|  |  OpenClaw Daemon          |    |  Docker: linear-automation     |  |
|  |  (systemd service)        |    |                                |  |
|  |                           |    |  +---------------------------+ |  |
|  |  Cron Job: every 1 hour --+--->|  | python3 run_once.py       | |  |
|  |  "linear-ticket-scan"     |    |  |                           | |  |
|  |                           |    |  | 1. Linear API (fetch)     | |  |
|  +---------------------------+    |  | 2. Ticket Enricher        | |  |
|                                   |  | 3. Git (clone/worktree)   | |  |
|                                   |  | 4. Claude Code (fix code) | |  |
|                                   |  | 5. Git push + gh pr       | |  |
|                                   |  | 6. Linear API (update)    | |  |
|                                   |  +---------------------------+ |  |
|                                   +--------------------------------+  |
+-----------------------------------------------------------------------+
```

## How It Works

### Step 1: Scheduled Trigger (Every 1 Hour)

OpenClaw daemon runs as a systemd service on the server with a cron job that fires every hour. It executes:

```bash
docker exec linear-automation python3 run_once.py
```

### Step 2: Fetch Tickets from Linear

The script connects to the Linear GraphQL API and fetches tickets where:
- **Assignee = you** (only your tickets)
- **Status = "unstarted" or "started"** (Todo / In Progress)
- Skips tickets already processed (tracked in `processed_issues.json`)
- Skips tickets with labels `claude-processing` or `claude-done`

### Step 3: Detect Target Repositories

Each ticket is mapped to one or more GitHub repos using this priority:

| Priority | Source | Example |
|----------|--------|---------|
| 1 | `repo:` label on ticket | `repo:my-backend` |
| 2 | GitHub URL in description | `https://github.com/org/repo` |
| 3 | `Repository: name` in description | `Repository: my-backend` |
| 4 | Linear project name | Project "frontend" |
| 5 | Team key (fallback) | Team "ENG" |

One ticket can target **multiple repos** via multiple `repo:` labels.

### Step 4: Move Ticket to "In Progress"

The Linear issue state is transitioned to `started` so your team sees it's being worked on.

### Step 5: Developer Skill — Scope Resolution & Context Enrichment

The **Developer Skill** (`skills/developer_skill.py`) is the intelligence layer that handles all ticket cases:

| Case | What Happens |
|------|-------------|
| **Normal ticket** (no sub-tasks) | Fix everything in the ticket |
| **Parent ticket** with sub-tasks on other devs | Fix only what's NOT covered by those sub-tasks |
| **Sub-task** assigned to you | Fix only the sub-task scope, inherit repo from parent |
| **Parent + some sub-tasks** assigned to you | Fix parent scope + your sub-tasks, skip others' |

The skill:
1. **Resolves scope** — detects if it's a normal ticket, parent, or sub-task
2. **Inherits repo info** — sub-tasks without `repo:` labels inherit from parent
3. **Builds scope-aware prompt** — tells Claude exactly what to implement and what NOT to touch
4. **Enriches context** via the Ticket Enricher (7 API calls fired in parallel):
   - Full description + all comments
   - Sub-issues with assignee info (for scope exclusion)
   - Parent issue context (for sub-task inheritance)
   - Related/blocking tickets, labels, priority, attachments
   - Acceptance criteria and file hints (from ticket + parent)

### Step 6: Prepare the Repository

For each target repo:

1. **Check REPO_MAP** -- use pre-configured local path if available
2. **Auto-clone** -- if not in REPO_MAP, clone from GitHub using SSH
3. **Update** -- `git fetch` + checkout `dev` (or `main`/`master`)
4. **Create worktree** -- isolated branch `claude/<ticket-id>` based on `origin/dev`

Worktrees provide full isolation -- each ticket gets its own directory and branch. The main checkout stays clean.

### Step 7: Claude Code Fixes the Code

This is the core AI coding step:

```bash
claude -p "$(cat prompt.txt)" \
  --allowedTools "Bash" "Read" "Edit" "Write" "Glob" "Grep" \
  --max-turns 30 \
  --output-format text
```

- Runs in the **worktree directory** (isolated branch)
- **`-p` (print mode)**: Non-interactive, no user prompts
- **`--allowedTools`**: Restricted to safe coding tools only
- **`--max-turns 30`**: Prevents runaway loops
- **15 minute timeout** per ticket
- **Auth**: Claude Pro subscription via OAuth token

Claude Code follows **Test-Driven Development (TDD)** in two phases:

**Phase 1 — Sentinel Test Generation** (if Sentinel Guardian is available):
- Loads Sentinel Guardian testing skills (`test-setup`, `unit-tests`)
- Generates comprehensive test cases based on acceptance criteria, edge cases, and the ticket description
- Commits tests separately: `test(TICKET-ID): add tests for ...`
- Verifies tests FAIL (since the fix doesn't exist yet)

**Phase 2 — Implementation**:
- Reads the codebase and the tests from Phase 1
- Implements the fix/feature to make all tests pass
- **Never edits test files** — if tests fail, the code is fixed, NOT the tests
- Commits implementation: `fix(TICKET-ID): ...`

If Sentinel is not available, falls back to single-phase TDD where Claude writes both tests and implementation.
3. Writes/edits code to fix the issue
4. Stages all changes
5. Commits with message: `fix(TICKET-ID): short summary`

If it cannot fix the issue, it creates `CLAUDE_UNABLE.md` explaining why.

### Step 8: Push and Create PR

```bash
git push origin "claude/<ticket-id>"
gh pr create --base dev --head "claude/<ticket-id>" --title "fix(TICKET-ID): title"
```

- Detects the actual `owner/repo` from the git remote (not hardcoded)
- Creates PR targeting `dev` branch (falls back to `main`)
- PR body includes ticket title, Linear link, and description

### Step 9: Update Linear

On success:
- Moves ticket to **"Done"**
- Comments on the ticket with PR link(s)
- Marks ticket as processed

On failure:
- Does **NOT** mark as processed -- retries on next hourly run

### Step 10: Cleanup

- Removes the worktree (frees disk space)
- Logs everything to `logs/automation.log`

## Project Structure

```
linear-automation-python/
|-- main.py                        # Continuous loop mode (time.sleep)
|-- run_once.py                    # Single scan mode (used by cron)
|-- lib/
|   |-- config.py                  # Environment config loader
|   |-- core.py                    # Shared core logic
|   +-- linear_client.py           # Linear GraphQL API client
|-- skills/
|   |-- developer-skill/
|   |   +-- SKILL.md               # TDD instructions, quality checklist, critical rules
|   |-- developer_skill.py         # Scope resolution, repo inheritance, dynamic prompt assembly
|   |-- sentinel_integration.py    # Sentinel Guardian test generation integration
|   +-- ticket_enricher.py         # Deep context extraction from Linear/Jira
|-- openclaw-skill/
|   +-- SKILL.md                   # OpenClaw skill definition
|-- requirements.txt               # Python dependencies
|-- .env.example                   # Config template
|-- Dockerfile                     # Docker image definition
|-- docker-compose.yml             # Container orchestration
|-- entrypoint.sh                  # Container startup script
|-- setup.sh                       # Laptop setup (macOS launchd)
|-- server-deploy.sh               # Server deployment script
+-- .gitignore
```

### Runtime directories (inside Docker, not committed):

```
logs/
|-- automation.log              # Main run log
|-- processed_issues.json       # Tracks completed tickets
|-- claude_RUH-6.log            # Claude output per ticket
+-- prompt_RUH-6.txt            # Prompt sent to Claude per ticket

repos/
+-- Test-openClaw-automation/   # Auto-cloned repo
    |-- .git/
    +-- .worktrees/
        +-- claude/ruh-6/       # Isolated worktree per ticket
```

## Authentication

| Service | Auth Method | Config Location |
|---------|-------------|-----------------|
| **Linear** | API Key | `.env` -> `LINEAR_API_KEY` |
| **Claude Code** | OAuth Token (Pro subscription) | `.env` -> `CLAUDE_CODE_OAUTH_TOKEN` |
| **GitHub (push)** | SSH Key | Server `~/.ssh/id_ed25519` mounted in Docker |
| **GitHub (PR)** | Personal Access Token | `.env` -> `GH_TOKEN` |

## Setup

### Prerequisites

- Python 3.12+
- Docker and Docker Compose
- Git
- GitHub CLI (`gh`)
- Claude Code CLI (`npm i -g @anthropic-ai/claude-code`)
- A Linear account with API key
- A Claude Pro subscription

### 1. Clone and Configure

```bash
git clone https://github.com/Rishabh-Kala-ruh/LinearAutomation-python.git
cd LinearAutomation-python
cp .env.example .env
```

Edit `.env` with your credentials:

```env
LINEAR_API_KEY=lin_api_your_key_here
GITHUB_ORG=your-github-org
GH_TOKEN=github_pat_your_token_here
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-your_token_here
TARGET_BRANCH=dev
POLL_INTERVAL_MINUTES=60
```

### 2. Get Your Tokens

**Linear API Key:**
Go to [linear.app/settings/api](https://linear.app/settings/api) -> Create key

**GitHub Personal Access Token:**
Go to [github.com/settings/tokens](https://github.com/settings/tokens) -> Generate new token (with `repo` scope)

**Claude Code OAuth Token:**
```bash
claude setup-token
# Follow the browser OAuth flow
# Copy the generated token
```

**SSH Key (for server):**
```bash
ssh-keygen -t ed25519
# Add public key to GitHub: github.com/settings/keys
```

### 3. Deploy to Server

```bash
# SSH into your server
ssh user@your-server-ip

# Clone the repo
git clone https://github.com/Rishabh-Kala-ruh/LinearAutomation-python.git
cd LinearAutomation-python

# Configure
cp .env.example .env
nano .env  # Add your tokens

# Build and start
docker compose up -d --build

# Authenticate Claude Code (one-time, interactive)
docker exec -it linear-automation claude setup-token

# Verify
docker exec linear-automation claude auth status
```

### 4. Setup OpenClaw Scheduler (Optional)

```bash
# Install OpenClaw
curl -fsSL https://openclaw.ai/install.sh | bash

# Onboard
openclaw onboard --install-daemon

# Run the server deploy script
bash server-deploy.sh
```

### 5. Run on Laptop (Alternative)

```bash
pip install -r requirements.txt
python main.py          # Continuous loop (every 1 hour)
# OR
python run_once.py      # Single scan
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `LINEAR_API_KEY` | Linear API key | Required |
| `GITHUB_ORG` | GitHub organization name | `ruh-ai` |
| `TARGET_BRANCH` | Base branch for PRs | `dev` |
| `GH_TOKEN` | GitHub Personal Access Token | Required |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code OAuth token | Required |
| `CLAUDE_CMD` | Claude CLI binary path | `claude` |
| `REPOS_DIR` | Directory for auto-cloned repos | `./repos` |
| `LOGS_DIR` | Directory for logs | `./logs` |
| `REPO_MAP` | JSON mapping repo names to local paths | `{}` |
| `POLL_INTERVAL_MINUTES` | Scan interval in minutes | `60` |

### REPO_MAP

For laptop use, map repo names to local paths:

```env
REPO_MAP={"my-backend": "/Users/you/projects/backend", "frontend": "/Users/you/projects/frontend"}
```

On the server, leave empty (`{}`) -- repos are auto-cloned.

## Ticket Setup in Linear

For the automation to pick up a ticket:

1. **Assign it to yourself**
2. **Set status** to Todo or In Progress
3. **Link to a repo** using one of:
   - Label: `repo:exact-repo-name`
   - GitHub URL in description: `https://github.com/owner/repo`
   - Text in description: `Repository: repo-name`

## Retry Logic

```
Ticket found -> attempt fix
    |
    |-- Success (PR created)
    |     -> Mark as processed (won't re-scan)
    |     -> Move ticket to "Done"
    |     -> Comment PR link on ticket
    |
    +-- Failure (clone error, Claude error, no changes)
          -> Do NOT mark as processed
          -> Will retry on next hourly run
          -> Stale worktrees/branches auto-cleaned
```

## Useful Commands

```bash
# Check status
docker ps
docker exec linear-automation claude auth status

# Run manually
docker exec linear-automation python3 run_once.py

# View logs
docker exec linear-automation tail -50 /app/logs/automation.log
docker exec linear-automation cat /app/logs/claude_RUH-6.log

# Clear processed tickets (re-scan all)
docker exec linear-automation rm /app/logs/processed_issues.json

# Rebuild after code changes
docker compose up -d --build
```

## Tech Stack

| Component | Technology | Role |
|-----------|-----------|------|
| Orchestrator | Python 3.12 | Fetches tickets, manages git, creates PRs |
| AI Coder | Claude Code CLI | Reads codebase, writes fixes, commits |
| Scheduler | OpenClaw | Cron daemon, triggers hourly scans |
| Container | Docker | Isolates the runtime environment |
| Ticket Mgmt | Linear GraphQL API | Fetches/updates tickets via `requests` |
| Git Hosting | GitHub | Hosts repos, PRs via `gh` CLI |
| Enrichment | Ticket Enricher | Extracts deep context from tickets |

## Cost

| Component | Cost |
|-----------|------|
| OpenClaw | Free (open source) |
| Claude Code | Existing Pro subscription ($20/mo) |
| Linear API | Free |
| GitHub API | Free |
| Server | Your existing VM |

## License

ISC
