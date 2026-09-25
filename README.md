# Comments Autofixer

Fetches active review comments from an **Azure DevOps Pull Request** and sends each one to the selected AI coding CLI to apply fixes automatically. Supported agents include **GitHub Copilot CLI**, **Codex CLI**, **Claude Code**, **Gemini CLI**, and **Antigravity CLI**. After each successful fix the corresponding PR thread is marked as *fixed* via the Azure DevOps REST API.

## How it works

1. Connects to Azure DevOps and fetches all active (non-resolved) PR threads.
2. Presents each comment interactively with a side-by-side diff of the affected code.
3. You approve or skip each comment. Skipped comments can include a reply and are marked *won't fix*.
4. Approved comments are sent to the selected AI coding CLI, which edits the files in place.
5. On success, the PR thread is resolved automatically.

Three processing modes are available:

- **Interactive** — fix each comment immediately after you approve it (sequential).
- **Batch** — review all comments first, then fix them all in one pass.
- **Grouped** — like batch, but all approved comments on the same file are sent to the agent in a single prompt (one agent call per file). All threads in a group are resolved together.

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | Uses union types and `match` syntax |
| `requests`, `python-dotenv`, `questionary` | Installed via `pip install -r requirements.txt` |
| `git` | Must be on `PATH` |
| GitHub CLI (`gh`) | Required only for `AGENT=copilot` when no Copilot token env var is set. Must be on `PATH` and authenticated (`gh auth login`). The script validates auth with `gh auth status` before processing comments. |
| GitHub Copilot CLI | Required only for `AGENT=copilot`. See [github/copilot-cli](https://github.com/github/copilot-cli). Install via `curl -fsSL https://gh.io/copilot-install \| bash` (macOS/Linux), `winget install GitHub.Copilot` (Windows), or `npm install -g @github/copilot`. Requires an active Copilot subscription. |
| Codex CLI | Required only for `AGENT=codex`. Must be on `PATH` and authenticated with `codex login`. The script validates auth with `codex login status` before processing comments. |
| Claude Code | Required only for `AGENT=claude`. Must be on `PATH` and authenticated with `claude auth login`. The script validates auth with `claude auth status` before processing comments. |
| Gemini CLI | Required only for `AGENT=gemini`. Must be on `PATH` and authenticated. The script validates the CLI with `gemini --version` before processing comments. |
| Antigravity CLI | Required only for `AGENT=antigravity`. Must expose the `agy` command on `PATH` and be authenticated through Antigravity's keyring/browser sign-in flow. The script validates the CLI with `agy --version` before processing comments. |
| Azure DevOps PAT | Requires **Code (Read & Write)** and **Pull Request Threads (Read & Write)** scopes |

All agents are invoked in autonomous mode. Copilot runs with `--autopilot --allow-all --no-ask-user`; Codex runs with `--dangerously-bypass-approvals-and-sandbox`; Claude Code runs with `--dangerously-skip-permissions --print`; Gemini runs with `--yolo --skip-trust`; Antigravity runs with `--dangerously-skip-permissions --print` (and supports forwarding `--model` with automatic mapping to the exact model names used by `agy`).

## Installation

```
pip install -r requirements.txt
```

## Configuration

### Value resolution order

For every setting the script resolves a value in this priority order — highest wins:

```
CLI flag  >  .env file  >  auto-detection  >  interactive prompt
```

- **CLI flag** — passed directly when invoking the script (`--pr-id 42`).
- **`.env` file** — a `KEY=value` file placed in the directory where you run the script. Loaded automatically at startup via `python-dotenv`. Ideal for per-repo defaults.
- **Auto-detection** — org, project, repo and work directory are inferred from the `origin` git remote; PR ID is inferred from the current branch if exactly one active PR matches it.
- **Interactive prompt** — any value still missing after the above steps is asked for at runtime.

### `.env` file

Create a `.env` file in the directory where you run the script. **Never commit it** — it is already in `.gitignore`.

```dotenv
# Personal Access Token — needs Code (Read & Write) and Pull Request Threads (Read & Write) scopes.
AZURE_DEVOPS_PAT=your-pat-here

# Pull Request ID. Auto-detected from the current branch when exactly one active PR matches.
PR_ID=42

# Path to the repository whose PR comments you are fixing.
WORK_DIR=/path/to/repo

# AI coding CLI to use: copilot (default), codex, claude, gemini, or antigravity.
AGENT=copilot
# Model name passed to the selected AI coding CLI (defaults to agent-specific default if unset).
MODEL=

# Optional Copilot CLI auth for headless use. If unset, `gh auth status` is used.
COPILOT_GITHUB_TOKEN=
GH_TOKEN=
GITHUB_TOKEN=

# Processing mode: interactive (fix each comment immediately), batch (review all, then fix all),
# or grouped (like batch, but one prompt per file containing all of that file's comments).
MODE=batch

# Order comments are presented: asc (oldest first), desc (newest first), file (by file path).
ORDER=desc
# Only include comments on or after / on or before this date (YYYY-MM-DD or ISO 8601).
SINCE=
UNTIL=
# Also process threads that are already resolved or closed (default: active only).
INCLUDE_RESOLVED=false

# Optional: only process comments whose text contains one of these substrings.
# Comma-separated list; matching is case-sensitive and literal (include brackets).
COMMENT_PREFIXES=[PERFORMANCE],[ARCHITECTURE],[REFACTOR],[SECURITY]

# Print comments without calling the selected AI coding CLI or updating threads; saves raw API response to JSON.
DRY_RUN=false
# Directory for the timestamped session log file (default: current directory).
LOG_DIR=
```

### CLI flags

All flags are optional — see [Value resolution order](#value-resolution-order).

| Flag | `.env` variable |
|---|---|
| `--pat` | `AZURE_DEVOPS_PAT` |
| `--pr-id` | `PR_ID` |
| `--agent` | `AGENT` |
| `--work-dir` | `WORK_DIR` |
| `--model` | `MODEL` |
| `--mode` | `MODE` |
| `--order` | `ORDER` |
| `--dry-run` | `DRY_RUN` |
| `--include-resolved` | `INCLUDE_RESOLVED` |
| `--since DATE` | `SINCE` |
| `--until DATE` | `UNTIL` |
| `--log-dir DIR` | `LOG_DIR` |
| *(read by Copilot CLI)* | `COPILOT_GITHUB_TOKEN` |
| *(read by Copilot CLI)* | `GH_TOKEN` |
| *(read by Copilot CLI)* | `GITHUB_TOKEN` |
| *(no CLI flag)* | `COMMENT_PREFIXES` |

## Usage

```bash
# Run from inside the repository — org, project, repo and PR ID are auto-detected
export AZURE_DEVOPS_PAT=<your-pat>
python pr_comments_to_agent.py

# Override PR ID explicitly
python pr_comments_to_agent.py --pr-id 42

# Use a specific model
python pr_comments_to_agent.py --model gpt-4o

# Use Codex CLI instead of Copilot CLI
python pr_comments_to_agent.py --agent codex --model gpt-5

# Use Claude Code instead of Copilot CLI
python pr_comments_to_agent.py --agent claude --model claude-sonnet-4-6

# Use Gemini CLI instead of Copilot CLI
python pr_comments_to_agent.py --agent gemini --model gemini-2.5-pro

# Use Antigravity CLI instead of Copilot CLI
python pr_comments_to_agent.py --agent antigravity

# Preview comments without calling an AI coding CLI (saves raw API response to JSON)
python pr_comments_to_agent.py --dry-run
```

When the PAT is set in `.env` everything is auto-detected and you can simply run:

```bash
python pr_comments_to_agent.py
```

## Session logging

Every run writes a timestamped log file (`pr_<id>_<timestamp>.log`) to the current directory (or `--log-dir`). All console output is mirrored to this file.
