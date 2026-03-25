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
|  |                           |    |  | Phase 1: COLLECT           | |  |
|  +---------------------------+    |  |   Fetch + sort by priority | |  |
|                                   |  | Phase 2: PREPARE           | |  |
|  +---------------------------+    |  |   Clone/update repos (||)  | |  |
|  |  GitHub Actions           |    |  | Phase 3: EXECUTE           | |  |
|  |  (auto-deploy)            |    |  |   N tickets in parallel    | |  |
|  |                           |    |  |   Claude Code + PR + done  | |  |
|  |  PR merged → main         |    |  +---------------------------+ |  |
|  |    → SSH → git pull        |    |                                |  |
|  |    → docker compose up    |    +--------------------------------+  |
|  +---------------------------+                                       |
+-----------------------------------------------------------------------+
```

## How It Works

### Phase 1: COLLECT — Fetch and Prioritize

**Trigger:** OpenClaw daemon fires every hour, or the Docker container runs continuously.

The script connects to the Linear GraphQL API with a **single batch query** that fetches issues with labels and project inline (fewer API calls). It filters:

- **Assignee = you** (only your tickets)
- **Status = "unstarted" or "started"** (Todo / In Progress)
- Skips tickets already processed (tracked in `processed_issues.json`)
- Skips tickets with labels `claude-processing` or `claude-done`

Then **sorts by priority**: Urgent → High → Medium → Low → None. Urgent tickets always get processed first.

### Phase 2: PREPARE — Clone Repos in Parallel

Each ticket is mapped to one or more GitHub repos using this priority:

| Priority | Source | Example |
|----------|--------|---------|
| 1 | `repo:` label on ticket | `repo:my-backend` |
| 2 | GitHub URL in description | `https://github.com/org/repo` |
| 3 | `Repository: name` in description | `Repository: my-backend` |
| 4 | Linear project name | Project "frontend" |
| 5 | Team key (fallback) | Team "ENG" |

All **unique repos are cloned/updated in parallel** (up to 4 at once). Each repo is only fetched once even if multiple tickets target it.

### Phase 3: EXECUTE — Process Tickets in Parallel

Up to `MAX_CONCURRENT_TICKETS` (default: 2) tickets are processed simultaneously using a thread pool. Each ticket runs independently in its own git worktree.

For each ticket (in parallel):

#### 1. Move Ticket to "In Progress"
The Linear issue state is transitioned to `started`.

#### 2. Enrich Ticket Context
The **Ticket Enricher** (`skills/ticket_enricher.py`) extracts deep context with **all 7 API calls fired in parallel**:

- Full description + all comments (team discussion)
- Sub-issues / child tasks
- Parent issue context (bigger picture)
- Related/blocking tickets
- Labels, priority, attachments
- Acceptance criteria (parsed from description)
- File hints (file paths and code references from description + comments)

#### 3. Create Worktree
An isolated git worktree is created at `.worktrees/claude/<ticket-id>` based on `origin/dev`. Per-repo locks prevent concurrent git operations on the same repo.

#### 4. Claude Code Fixes the Code

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

Claude Code autonomously:
1. Reads the codebase and understands the structure
2. Reads relevant files based on enriched context and file hints
3. Writes/edits code to fix the issue
4. Stages all changes
5. Commits with message: `fix(TICKET-ID): short summary`

If it cannot fix the issue, it creates `CLAUDE_UNABLE.md` explaining why.

#### 5. Push and Create PR

```bash
git push origin "claude/<ticket-id>"
gh pr create --base dev --head "claude/<ticket-id>" --title "fix(TICKET-ID): title"
```

- Detects the actual `owner/repo` from the git remote (not hardcoded)
- Creates PR targeting `dev` branch (falls back to `main`)
- PR body includes ticket title, Linear link, and description

#### 6. Update Linear

On success:
- Moves ticket to **"Done"**
- Comments on the ticket with PR link(s)
- Marks ticket as processed (thread-safe with lock)

On failure:
- Does **NOT** mark as processed -- retries on next hourly run

#### 7. Cleanup
- Removes the worktree (frees disk space)
- Logs everything to `logs/automation.log`

## CI/CD — Auto-Deploy on Merge

A GitHub Actions workflow (`.github/workflows/deploy.yml`) automatically deploys to the server when a PR is merged to `main`:

```
PR merged → main → GitHub Actions → SSH into server → git pull → docker compose up --build -d
```

### Required GitHub Secrets

| Secret | Value |
|--------|-------|
| `SERVER_HOST` | Server IP address |
| `SERVER_USER` | SSH username |
| `SERVER_SSH_KEY` | Private SSH key (contents of `~/.ssh/id_ed25519`) |

## Project Structure

```
linear-automation-python/
|-- main.py                        # Continuous loop mode (time.sleep)
|-- run_once.py                    # Single scan mode (used by cron)
|-- lib/
|   |-- config.py                  # Environment config loader
|   |-- core.py                    # 3-phase processing engine
|   +-- linear_client.py           # Linear GraphQL API client
|-- skills/
|   +-- ticket_enricher.py         # Deep context extraction from Linear/Jira
|-- openclaw-skill/
|   +-- SKILL.md                   # OpenClaw skill definition
|-- .github/
|   +-- workflows/
|       +-- deploy.yml             # Auto-deploy on merge to main
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
| **Linear** | API Key | `.env` → `LINEAR_API_KEY` |
| **Claude Code** | OAuth Token (Pro subscription) | `.env` → `CLAUDE_CODE_OAUTH_TOKEN` |
| **GitHub (push)** | SSH Key | Server `~/.ssh/id_ed25519` mounted in Docker |
| **GitHub (PR)** | Personal Access Token | `.env` → `GH_TOKEN` |

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
MAX_CONCURRENT_TICKETS=2
```

### 2. Get Your Tokens

**Linear API Key:**
Go to [linear.app/settings/api](https://linear.app/settings/api) → Create key

**GitHub Personal Access Token:**
Go to [github.com/settings/tokens](https://github.com/settings/tokens) → Generate new token (with `repo` scope)

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

After initial setup, all future deployments are automatic via GitHub Actions (merge PR → auto-deploy).

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
| `MAX_CONCURRENT_TICKETS` | Max tickets processed in parallel | `2` |

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
3. **Set priority** -- Urgent/High tickets are processed first
4. **Link to a repo** using one of:
   - Label: `repo:exact-repo-name`
   - GitHub URL in description: `https://github.com/owner/repo`
   - Text in description: `Repository: repo-name`

## Retry Logic

```
Ticket found → sorted by priority → processed in parallel
    |
    |-- Success (PR created)
    |     → Mark as processed (won't re-scan)
    |     → Move ticket to "Done"
    |     → Comment PR link on ticket
    |
    +-- Failure (clone error, Claude error, no changes)
          → Do NOT mark as processed
          → Will retry on next hourly run
          → Stale worktrees/branches auto-cleaned
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

# Rebuild after code changes (auto — just merge a PR)
# Manual: docker compose up -d --build
```

## Tech Stack

| Component | Technology | Role |
|-----------|-----------|------|
| Orchestrator | Python 3.12 | 3-phase parallel processing engine |
| AI Coder | Claude Code CLI | Reads codebase, writes fixes, commits |
| Scheduler | OpenClaw | Cron daemon, triggers hourly scans |
| Container | Docker | Isolates the runtime environment |
| CI/CD | GitHub Actions | Auto-deploy on merge to main |
| Ticket Mgmt | Linear GraphQL API | Batch queries via `requests` |
| Git Hosting | GitHub | Hosts repos, PRs via `gh` CLI |
| Enrichment | Ticket Enricher | Parallel context extraction from tickets |

## Cost

| Component | Cost |
|-----------|------|
| OpenClaw | Free (open source) |
| Claude Code | Existing Pro subscription ($20/mo) |
| Linear API | Free |
| GitHub API | Free |
| GitHub Actions | Free (2,000 min/month) |
| Server | Your existing VM |

## License

ISC
