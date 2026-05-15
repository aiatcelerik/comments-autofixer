# Comments Autofixer

Fetches active review comments from an **Azure DevOps Pull Request** and sends each one to the **GitHub Copilot CLI** to apply fixes automatically. After each successful fix the corresponding PR thread is marked as *fixed* via the Azure DevOps REST API.

## How it works

1. Connects to Azure DevOps and fetches all active (non-resolved) PR threads.
2. Presents each comment interactively with a side-by-side diff of the affected code.
3. You approve or skip each comment. Skipped comments can include a reply and are marked *won't fix*.
4. Approved comments are sent to the Copilot CLI (`copilot --autopilot --yolo`) which edits the files in place.
5. On success, the PR thread is resolved automatically.

Two processing modes are available:

- **Interactive** — fix each comment immediately after you approve it (sequential).
- **Batch** — review all comments first, then fix them all in one pass.

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | Uses union types and `match` syntax |
| `requests`, `python-dotenv`, `questionary` | Installed via `pip install -r requirements.txt` |
| `git` | Must be on `PATH` |
| GitHub CLI (`gh`) | Must be on `PATH` and authenticated (`gh auth login`). The script validates auth with `gh auth status` before processing comments. |
| GitHub Copilot CLI | See [github/copilot-cli](https://github.com/github/copilot-cli). Install via `curl -fsSL https://gh.io/copilot-install \| bash` (macOS/Linux), `winget install GitHub.Copilot` (Windows), or `npm install -g @github/copilot`. Requires an active Copilot subscription. |
| Azure DevOps PAT | Requires **Code (Read & Write)** and **Pull Request Threads (Read & Write)** scopes |

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
# Model name passed to the Copilot CLI.
MODEL=claude-sonnet-4.6
# Processing mode: interactive (fix each comment immediately) or batch (review all, then fix all).
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

# Print comments without calling Copilot or updating threads; saves raw API response to JSON.
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
| `--work-dir` | `WORK_DIR` |
| `--model` | `MODEL` |
| `--mode` | `MODE` |
| `--order` | `ORDER` |
| `--dry-run` | `DRY_RUN` |
| `--include-resolved` | `INCLUDE_RESOLVED` |
| `--since DATE` | `SINCE` |
| `--until DATE` | `UNTIL` |
| `--log-dir DIR` | `LOG_DIR` |
| *(no CLI flag)* | `COMMENT_PREFIXES` |

## Usage

```bash
# Run from inside the repository — org, project, repo and PR ID are auto-detected
export AZURE_DEVOPS_PAT=<your-pat>
python pr_comments_to_copilot.py

# Override PR ID explicitly
python pr_comments_to_copilot.py --pr-id 42

# Use a specific model
python pr_comments_to_copilot.py --model gpt-4o

# Preview comments without calling Copilot (saves raw API response to JSON)
python pr_comments_to_copilot.py --dry-run
```

When the PAT is set in `.env` everything is auto-detected and you can simply run:

```bash
python pr_comments_to_copilot.py
```

## Session logging

Every run writes a timestamped log file (`pr_<id>_<timestamp>.log`) to the current directory (or `--log-dir`). All console output is mirrored to this file.
