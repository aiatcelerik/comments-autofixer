#!/usr/bin/env python3
"""
Fetch comments from an Azure DevOps Pull Request and send each one
to the Copilot CLI.

Requirements:
  pip install requests python-dotenv
  copilot CLI available on PATH
  .env file in the working directory (optional)

Usage examples:
  # Basic usage (PAT from environment variable)
  set AZURE_DEVOPS_PAT=<your-pat>
  python pr_comments_to_copilot.py --org myorg --project myproject --repo myrepo --pr-id 42

  # Specify work directory and PAT directly
  python pr_comments_to_copilot.py --org myorg --project myproject --repo myrepo --pr-id 42 \
      --pat <your-pat> --work-dir C:\Projects\myrepo

  # Use a specific model
  python pr_comments_to_copilot.py ... --model claude-3.5-sonnet

  # Preview comments without calling Copilot
  python pr_comments_to_copilot.py ... --dry-run
"""

import argparse
import base64
import concurrent.futures
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone

import questionary
import requests
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Dependency validation
# ---------------------------------------------------------------------------

def check_dependencies() -> None:
    """Verify all required tools and Python packages are available.

    Exits with a descriptive error message if anything is missing.
    """
    errors: list[str] = []

    # Python packages
    try:
        import requests as _  # noqa: F401
    except ImportError:
        errors.append(
            "Python package 'requests' is not installed.\n"
            "  Fix: pip install requests"
        )

    # External CLI tools
    cli_tools = {
        "copilot": (
            "GitHub Copilot CLI ('copilot') is not found on PATH.\n"
            "  Fix: install via 'gh extension install github/gh-copilot' "
            "and ensure 'gh copilot' (or a 'copilot' wrapper) is on your PATH."
        ),
        "git": (
            "Git ('git') is not found on PATH.\n"
            "  Fix: install Git from https://git-scm.com/downloads"
        ),
    }
    for tool, message in cli_tools.items():
        if shutil.which(tool) is None:
            errors.append(message)

    if errors:
        print("ERROR: Missing required dependencies:\n", file=sys.stderr)
        for i, msg in enumerate(errors, start=1):
            print(f"  {i}. {msg}\n", file=sys.stderr)
        sys.exit(1)


_COPILOT_AUTH_ERROR_PATTERNS = (
    "no authentication information found",
    "not logged in",
    "not authenticated",
    "unauthenticated",
    "unauthorized",
    "please log in",
    "login required",
    "authentication required",
    "sign in",
)


def check_copilot_login() -> None:
    """Verify the Copilot CLI is authenticated before processing any comments.

    Runs a minimal probe invocation (``copilot --no-ask-user -p ...``) and
    inspects the exit code and output for authentication errors.  Exits with a
    descriptive message so that comments are never marked as resolved without
    Copilot having actually run.
    """
    probe_prompt = "Reply with exactly: ok"
    try:
        result = subprocess.run(
            ["copilot", "--autopilot", "--yolo", "--no-ask-user", "-p", probe_prompt],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        print(
            "Warning: Copilot CLI did not respond within 60 s during the auth probe.\n"
            "  Proceeding, but Copilot commands may fail if you are not logged in.",
            file=sys.stderr,
        )
        return
    except OSError as exc:
        # shutil.which already confirmed copilot is on PATH, so this is unusual.
        print(f"Warning: could not run Copilot CLI for auth probe: {exc}", file=sys.stderr)
        return

    output = (result.stdout + result.stderr).lower()
    auth_failure = any(p in output for p in _COPILOT_AUTH_ERROR_PATTERNS)

    if auth_failure:
        print(
            "ERROR: Copilot CLI is not authenticated.\n\n"
            + (result.stdout + result.stderr).strip() + "\n",
            file=sys.stderr,
        )
        sys.exit(1)

    if result.returncode != 0:
        print(
            f"ERROR: Copilot CLI probe exited with code {result.returncode}.\n"
            "  Ensure the Copilot CLI is installed and working correctly.\n"
            f"  Output:\n{(result.stdout + result.stderr).strip()}",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Tee: mirror stdout to a log file
# ---------------------------------------------------------------------------

class _Tee:
    """Proxy for sys.stdout that mirrors all writes to a secondary stream."""

    def __init__(self, primary, secondary):
        self._p = primary
        self._s = secondary

    def write(self, data: str) -> int:
        n = self._p.write(data)
        self._s.write(data)
        self._s.flush()
        return n

    def flush(self) -> None:
        self._p.flush()
        self._s.flush()

    def __getattr__(self, name: str):
        return getattr(self._p, name)


# ---------------------------------------------------------------------------
# Interactive prompt helper
# ---------------------------------------------------------------------------

def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    """Prompt the user for a value; use default if they press Enter."""
    hint = f" [{default}]" if default else ""
    prompt_str = f"  {label}{hint}: "
    if secret:
        value = getpass.getpass(prompt_str)
    else:
        value = input(prompt_str).strip()
    return value or default


def _prompt_yes_no(question: str, default: bool = False) -> bool:
    """Prompt for a yes/no answer using arrow-key selection.

    Returns True for yes and False for no. Raises KeyboardInterrupt on Ctrl-C.
    """
    choices = [
        questionary.Choice("Yes", value=True),
        questionary.Choice("No", value=False),
    ]
    if not default:
        choices = list(reversed(choices))
    result = questionary.select(question, choices=choices).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


# ---------------------------------------------------------------------------
# Git-based auto-detection of Azure DevOps connection details
# ---------------------------------------------------------------------------

def _detect_from_git(cwd: str) -> dict:
    """Infer Azure DevOps org/project/repo/work_dir from the git remote URL.

    Supports the three common remote formats:
      HTTPS : https://[user@]dev.azure.com/ORG/PROJECT/_git/REPO
      SSH   : git@ssh.dev.azure.com:v3/ORG/PROJECT/REPO
      Legacy: https://ORG.visualstudio.com/PROJECT/_git/REPO

    Returns a dict; any value that can't be determined is an empty string.
    """
    result: dict = {"work_dir": "", "org": "", "project": "", "repo": ""}

    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            result["work_dir"] = r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass

    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=result["work_dir"] or cwd, capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            url = r.stdout.strip()
            # HTTPS: https://[user@]dev.azure.com/ORG/PROJECT/_git/REPO
            m = re.match(
                r"https://(?:[^@]+@)?dev\.azure\.com/([^/]+)/([^/]+)/_git/([^/?#]+)", url
            )
            if not m:
                # SSH: git@ssh.dev.azure.com:v3/ORG/PROJECT/REPO
                m = re.match(
                    r"git@ssh\.dev\.azure\.com:v3/([^/]+)/([^/]+)/([^/]+)", url
                )
            if not m:
                # Legacy: https://ORG.visualstudio.com/PROJECT/_git/REPO
                m = re.match(
                    r"https://([^.]+)\.visualstudio\.com/([^/]+)/_git/([^/?#]+)", url
                )
            if m:
                result["org"]     = m.group(1)
                result["project"] = m.group(2)
                result["repo"]    = m.group(3).removesuffix(".git")
    except (OSError, subprocess.TimeoutExpired):
        pass

    return result




# ---------------------------------------------------------------------------
# Azure DevOps helpers
# ---------------------------------------------------------------------------

_SUGGESTION_RE = re.compile(r"```suggestion\r?\n(.*?)```", re.DOTALL)


def _split_suggestion(content: str) -> tuple[str, str | None]:
    """Split a comment body into (text, suggestion) where suggestion is the
    code inside the first ```suggestion ... ``` fence, or None if absent."""
    m = _SUGGESTION_RE.search(content)
    if not m:
        return content.strip(), None
    text = content[: m.start()].strip()
    return text, m.group(1).rstrip("\n")


def _auth_header(pat: str) -> dict:
    """Build the Basic-auth header from a Personal Access Token."""
    token = base64.b64encode(f":{pat}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def fetch_pr_comments(org: str, project: str, repo: str, pr_id: int, pat: str, include_resolved: bool = False) -> tuple[list[dict], dict]:
    """
    Return a tuple of:
      - flat list of processed comments (one per thread)
      - raw JSON response body from the Azure DevOps API
    """
    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/"
        f"{repo}/pullRequests/{pr_id}/threads?api-version=7.0-preview"
    )

    response = requests.get(url, headers=_auth_header(pat), timeout=30)
    response.raise_for_status()
    raw = response.json()

    comments: list[dict] = []
    for thread in response.json().get("value", []):
        # Skip non-active threads unless --include-resolved is set
        if not include_resolved and thread.get("status") != "active":
            continue

        # Scan comments forward: capture the first user comment (the review).
        first = None
        for comment in thread.get("comments", []):
            if comment.get("commentType") == "system":
                continue
            if comment.get("isDeleted", False):
                continue
            content = comment.get("content", "").strip()
            if not content:
                continue
            if first is None:
                first = comment

        if first is None:
            continue

        raw_content = first.get("content", "").strip()
        text, suggestion = _split_suggestion(raw_content)

        published_raw = first.get("publishedDate") or first.get("lastUpdatedDate")
        published_dt: datetime | None = None
        if published_raw:
            try:
                published_dt = datetime.fromisoformat(published_raw.rstrip("Z")).replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        comments.append(
            {
                "thread_id": thread["id"],
                "content": text,
                "thread_context": thread.get("threadContext"),
                "suggestion": suggestion,
                "published_date": published_dt,
            }
        )
    return comments, raw


def fetch_authenticated_user(org: str, pat: str) -> dict:
    """Fetch the authenticated user's identity using the PAT."""
    url = f"https://dev.azure.com/{org}/_apis/connectionData?api-version=7.0-preview"
    response = requests.get(url, headers=_auth_header(pat), timeout=30)
    response.raise_for_status()
    return response.json()


def _detect_pr_from_branch(
    org: str, project: str, repo: str, pat: str, work_dir: str
) -> int | None:
    """Query the Azure DevOps API for active PRs on the current git branch.

    Returns the PR ID when exactly one active PR is found.
    If multiple PRs exist the user is shown a list and asked to choose.
    Returns None if detection fails or no active PR is found.
    """
    # Get current branch name
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=work_dir or os.getcwd(),
            capture_output=True, text=True, timeout=10,
        )
        branch = r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        branch = ""

    if not branch or branch == "HEAD":
        return None

    source_ref = f"refs/heads/{branch}"
    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/"
        f"{repo}/pullRequests"
        f"?searchCriteria.sourceRefName={requests.utils.quote(source_ref, safe='/')}"
        f"&searchCriteria.status=active"
        f"&api-version=7.0"
    )
    try:
        resp = requests.get(url, headers=_auth_header(pat), timeout=15)
        resp.raise_for_status()
        prs = resp.json().get("value", [])
    except Exception:
        return None

    if not prs:
        return None

    if len(prs) == 1:
        pr = prs[0]
        print(
            f"Auto-detected PR from branch '{branch}': "
            f"#{pr['pullRequestId']} — {pr.get('title', '(no title)')}\n"
        )
        return pr["pullRequestId"]

    # Multiple active PRs — let the user pick
    print(f"Multiple active PRs found for branch '{branch}':")
    pr_choices = [
        questionary.Choice(
            f"#{pr['pullRequestId']} → {pr.get('targetRefName', '').replace('refs/heads/', '')}  {pr.get('title', '')}",
            value=pr["pullRequestId"],
        )
        for pr in prs
    ]
    pr_choices.append(questionary.Choice("Skip", value=None))
    result = questionary.select("Select a PR:", choices=pr_choices).ask()
    if result is None:
        return None
    return result


def fetch_pr_details(org: str, project: str, repo: str, pr_id: int, pat: str) -> dict:
    """Fetch PR details including source and target branch information."""
    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/"
        f"{repo}/pullRequests/{pr_id}?api-version=7.0-preview"
    )
    response = requests.get(url, headers=_auth_header(pat), timeout=30)
    response.raise_for_status()
    return response.json()


def resolve_thread(org: str, project: str, repo: str, pr_id: int, thread_id: int, pat: str) -> None:
    """Mark a PR thread as fixed via the Azure DevOps REST API."""
    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/"
        f"{repo}/pullRequests/{pr_id}/threads/{thread_id}?api-version=7.0-preview"
    )
    response = requests.patch(url, headers=_auth_header(pat), json={"status": "fixed"}, timeout=30)
    response.raise_for_status()


def wont_fix_thread(org: str, project: str, repo: str, pr_id: int, thread_id: int, pat: str) -> None:
    """Mark a PR thread as won't fix via the Azure DevOps REST API."""
    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/"
        f"{repo}/pullRequests/{pr_id}/threads/{thread_id}?api-version=7.0-preview"
    )
    response = requests.patch(url, headers=_auth_header(pat), json={"status": "wontFix"}, timeout=30)
    response.raise_for_status()


def post_thread_comment(org: str, project: str, repo: str, pr_id: int, thread_id: int, content: str, pat: str) -> None:
    """Post a reply comment on an existing PR thread."""
    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/"
        f"{repo}/pullRequests/{pr_id}/threads/{thread_id}/comments?api-version=7.0-preview"
    )
    response = requests.post(
        url,
        headers=_auth_header(pat),
        json={"content": content, "parentCommentId": 1, "commentType": 1},
        timeout=30,
    )
    response.raise_for_status()


# ---------------------------------------------------------------------------
# Git diff context helper
# ---------------------------------------------------------------------------

def get_current_branch(work_dir: str) -> str | None:
    """Get the current git branch in the working directory.

    Falls back to ``rev-parse --abbrev-ref HEAD`` for git worktrees where
    ``branch --show-current`` returns an empty string.
    """
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        branch = result.stdout.strip() if result.returncode == 0 else None
        if branch:
            return branch
        # In a git worktree --show-current returns empty; fall back.
        result2 = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result2.returncode == 0:
            value = result2.stdout.strip()
            return value if value and value != "HEAD" else None
        return None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _print_diff_side_by_side(diff_text: str) -> None:
    """
    Render a unified diff in side-by-side format:
      LEFT  column  — removed lines  (prefixed with  - )
      RIGHT column  — added lines    (prefixed with  + )
      Context lines appear on both sides.
    
    If the text is not a unified diff (e.g., annotated source from fallback),
    just print it as-is without side-by-side layout.
    """
    # Check if this is actually a unified diff format
    lines = diff_text.splitlines()
    is_unified_diff = any(
        line.startswith("---") or 
        line.startswith("+++") or 
        line.startswith("@@")
        for line in lines[:20]  # Check first 20 lines
    )
    
    # If not a unified diff, just print the text as-is
    if not is_unified_diff:
        print(diff_text)
        return
    
    cols = shutil.get_terminal_size((120, 40)).columns
    half = max(20, (cols - 3) // 2)  # 3 chars for " │ " separator

    def _trunc(s: str) -> str:
        if len(s) > half:
            return s[: half - 1] + "…"
        return s

    def _row(left: str, sep: str, right: str) -> None:
        print(f"{left:<{half}} {sep} {right}")

    # Parse unified diff hunks into (left, right) pairs
    removed: list[str] = []
    added:   list[str] = []

    def _flush() -> None:
        rows = max(len(removed), len(added))
        for i in range(rows):
            l = _trunc(removed[i]) if i < len(removed) else ""
            r = _trunc(added[i])   if i < len(added)   else ""
            sep = "│" if l and r else ("<" if l else ">")
            _row(l, sep, r)
        removed.clear()
        added.clear()

    for raw_line in lines:
        if raw_line.startswith("---") or raw_line.startswith("+++"):
            _flush()
            _row(_trunc(raw_line), "│", "")
        elif raw_line.startswith("@@"):
            _flush()
            _row(_trunc(raw_line), "│", _trunc(raw_line))
        elif raw_line.startswith("-"):
            removed.append(raw_line)
        elif raw_line.startswith("+"):
            added.append(raw_line)
        else:
            _flush()
            _row(_trunc(raw_line), "│", _trunc(raw_line))
    _flush()


def get_diff_context(work_dir: str, file_path: str, start_line: int | None, end_line: int | None, context: int = 4) -> str | None:
    """
    Return a diff/source snippet for *file_path* around *start_line*–*end_line*.

    Strategy:
      1. Run ``git diff main..HEAD -- <file>`` to compare current branch with main.
      2. If that fails or returns nothing, try ``git diff origin/main..HEAD -- <file>``.
      3. Otherwise read the file directly and return the relevant lines with
         ``>`` markers and surrounding context.
    """
    rel_path = file_path.lstrip("/")
    abs_path = os.path.join(work_dir, rel_path)

    # 1. Try git diff against main branch (origin/main first, then local main)
    for base_ref in ["origin/main", "main"]:
        try:
            result = subprocess.run(
                ["git", "diff", f"{base_ref}..HEAD", "--", rel_path],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                diff_text = result.stdout.strip()
                if diff_text:
                    return diff_text
        except (OSError, subprocess.TimeoutExpired):
            pass

    # 2. Fall back: show annotated source lines
    if start_line is None or not os.path.isfile(abs_path):
        return None
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None

    lo = max(0, start_line - 1 - context)
    hi = min(len(lines), (end_line or start_line) + context)
    numbered: list[str] = []
    for i, line in enumerate(lines[lo:hi], start=lo + 1):
        marker = ">" if start_line <= i <= (end_line or start_line) else " "
        numbered.append(f"  {marker} {i:4d} │ {line.rstrip()}")
    return "\n".join(numbered) if numbered else None


# ---------------------------------------------------------------------------
# Copilot CLI helper
# ---------------------------------------------------------------------------

def send_to_copilot(prompt: str, work_dir: str, model: str = "gpt-4o") -> subprocess.CompletedProcess:
    """
    Run: copilot --model <model> --autopilot --yolo --no-ask-user -p <prompt>
    in the specified working directory.  Output is streamed through sys.stdout
    so it is captured by the session log when logging is active.

    Returns a CompletedProcess whose ``stdout`` attribute contains the full
    combined output (stdout + stderr) so callers can inspect it for errors.
    """
    proc = subprocess.Popen(
        ["copilot", "--model", model, "--autopilot", "--yolo", "--no-ask-user", "-p", prompt],
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    collected: list[str] = []
    for line in proc.stdout:
        print(line, end="", flush=True)
        collected.append(line)
    proc.wait()
    return subprocess.CompletedProcess(
        args=proc.args,
        returncode=proc.returncode,
        stdout="".join(collected),
    )


# ---------------------------------------------------------------------------
# Single-comment fix helper (shared by interactive and batch modes)
# ---------------------------------------------------------------------------

def _build_copilot_prompt(comment: dict) -> str:
    """Return the Copilot prompt string for a single PR comment."""
    ctx = comment.get("thread_context")
    if ctx:
        file_path = ctx.get("filePath", "(unknown)")
        right_start = (ctx.get("rightFileStart") or {}).get("line")
        right_end   = (ctx.get("rightFileEnd")   or {}).get("line")
        left_start  = (ctx.get("leftFileStart")  or {}).get("line")
        left_end    = (ctx.get("leftFileEnd")    or {}).get("line")
        if right_start and right_end and right_start != right_end:
            location = f"lines {right_start}\u2013{right_end}"
        elif right_start:
            location = f"line {right_start}"
        elif left_start and left_end and left_start != left_end:
            location = f"lines {left_start}\u2013{left_end} (old file)"
        elif left_start:
            location = f"line {left_start} (old file)"
        else:
            location = None
        where = f"`{file_path}`" + (f", {location}" if location else "")
        task = f"Fix the following PR review comment in {where}:"
    else:
        task = "Fix the following PR review comment:"

    extra_prompt = (comment.get("extra_prompt") or "").strip()
    diff_block = ""
    if comment.get("diff_snippet"):
        diff_block = f"\n\nHere is the current diff for context:\n\n```diff\n{comment['diff_snippet']}\n```"

    if comment.get("custom_fix"):
        prompt = (
            f"Do NOT commit or stage any changes.\n\n"
            f"{task}{diff_block}\n\n"
            f"{comment['content']}\n\n"
            f"Apply this custom fix:\n\n"
            f"{comment['custom_fix']}"
        )
        if extra_prompt:
            prompt += f"\n\nAlso follow these additional instructions:\n\n{extra_prompt}"
    elif comment.get("suggestion"):
        prompt = (
            f"Do NOT commit or stage any changes.\n\n"
            f"{task}{diff_block}\n\n"
            f"{comment['content']}\n\n"
            f"Apply this exact suggested change:\n\n"
            f"```\n{comment['suggestion']}\n```"
        )
    else:
        prompt = f"Do NOT commit or stage any changes.\n\n{task}{diff_block}\n\n{comment['content']}"
        if extra_prompt:
            prompt += f"\n\nAlso follow these additional instructions:\n\n{extra_prompt}"

    return prompt


def _has_uncommitted_changes(work_dir: str) -> bool:
    """Return True if the working tree has any modifications relative to HEAD."""
    diff_r = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=work_dir, capture_output=True, text=True, timeout=15,
    )
    if diff_r.returncode == 0 and diff_r.stdout.strip():
        return True
    # Also catch untracked new files
    status_r = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=work_dir, capture_output=True, text=True, timeout=10,
    )
    return bool(status_r.returncode == 0 and status_r.stdout.strip())


def _fix_single_comment(comment: dict, label: str, args, work_dir: str) -> None:
    """Build a Copilot prompt for one comment, send it, and resolve the thread on success."""
    prompt = _build_copilot_prompt(comment)

    print(f"{label} Sending to Copilot CLI  model={args.model}  work-dir={work_dir}")
    print(f"\n\u2500\u2500 Prompt \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n{prompt}\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n")
    result = send_to_copilot(prompt, work_dir, model=args.model)

    if result.returncode != 0:
        print(f"Warning: copilot exited with code {result.returncode} — thread will NOT be resolved.")
    elif any(p in result.stdout.lower() for p in _COPILOT_AUTH_ERROR_PATTERNS):
        print("Warning: Copilot output contains an authentication error — thread will NOT be resolved.")
    else:
        if not _has_uncommitted_changes(work_dir):
            print("Copilot made no file changes — comment may already be addressed or invalid.")
        print("Marking thread as resolved \u2026")
        try:
            resolve_thread(
                args.org, args.project, args.repo, args.pr_id,
                comment["thread_id"], args.pat,
            )
            print("Thread marked as fixed.")
        except requests.HTTPError as exc:
            print(f"Warning: could not resolve thread: {exc.response.status_code} {exc.response.text}")
        except requests.RequestException as exc:
            print(f"Warning: network error while resolving thread: {exc}")
    print()


# ---------------------------------------------------------------------------
# Parallel batch execution via git worktrees
# ---------------------------------------------------------------------------

_CONFLICT_MARKER_RE = re.compile(r"^<{7} ", re.MULTILINE)


def _find_conflicted_files(work_dir: str) -> list[str]:
    """Return relative paths of files that contain git conflict markers."""
    status_r = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=work_dir, capture_output=True, text=True, timeout=10,
    )
    # --diff-filter=U lists "unmerged" (conflicted) files only
    files = [f for f in status_r.stdout.splitlines() if f.strip()]
    if files:
        return files

    # Fallback: scan working tree for conflict markers (handles new files too)
    found: list[str] = []
    try:
        grep_r = subprocess.run(
            ["git", "grep", "-l", "--untracked", "^<<<<<<< "],
            cwd=work_dir, capture_output=True, text=True, timeout=15,
        )
        found = [f for f in grep_r.stdout.splitlines() if f.strip()]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return found


def _comments_for_file(rel_path: str, comments: list[dict]) -> list[dict]:
    """Return comments whose thread_context touches *rel_path*, or all if none match."""
    matched = [
        c for c in comments
        if (c.get("thread_context") or {}).get("filePath", "").lstrip("/") == rel_path.lstrip("/")
    ]
    return matched if matched else comments


def _parse_conflict_hunks(abs_path: str) -> list[dict]:
    """Parse a file and return all conflict hunks as a list of dicts.

    Each dict has:
      ``line``   — 1-based line number of the ``<<<<<<<`` marker
      ``ours``   — text between ``<<<<<<<`` and ``=======``
      ``theirs`` — text between ``=======`` and ``>>>>>>>``
    """
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []

    hunks: list[dict] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("<<<<<<<"):
            start = i + 1  # 1-based line of the marker itself
            ours_lines: list[str] = []
            theirs_lines: list[str] = []
            i += 1
            # collect "ours" side
            while i < len(lines) and not lines[i].startswith("======="):
                ours_lines.append(lines[i])
                i += 1
            i += 1  # skip =======
            # collect "theirs" side
            while i < len(lines) and not lines[i].startswith(">>>>>>>"):
                theirs_lines.append(lines[i])
                i += 1
            i += 1  # skip >>>>>>>
            hunks.append({
                "line": start,
                "ours": "".join(ours_lines).rstrip("\n"),
                "theirs": "".join(theirs_lines).rstrip("\n"),
            })
        else:
            i += 1
    return hunks


def _resolve_conflict_file_with_copilot(rel_path: str, comments: list[dict], work_dir: str, args) -> bool:
    """Resolve conflict markers in *rel_path* one hunk at a time.

    Returns True if all conflict hunks were resolved, False if any remain.
    Each hunk gets its own focused Copilot call: the two code sides of the
    conflict plus the *intents* (the original PR comments that produced the two
    fixes). The intents are framed as context for *why* each side exists, not
    as instructions — so Copilot knows to combine both rather than pick one.

    After every call the file is re-read so line numbers remain accurate.
    """
    abs_path = os.path.join(work_dir, rel_path)

    # Build a compact intent block from the PR comments
    intent_lines: list[str] = []
    for i, c in enumerate(comments, start=1):
        ctx = c.get("thread_context") or {}
        rs = (ctx.get("rightFileStart") or {}).get("line")
        re_ = (ctx.get("rightFileEnd") or {}).get("line")
        loc = f" (line {rs}" + (f"\u2013{re_}" if re_ and re_ != rs else "") + ")" if rs else ""
        intent_lines.append(f"  Fix {i}{loc}: {c.get('content', '').strip()}")
    intents_block = "\n".join(intent_lines)

    while True:
        hunks = _parse_conflict_hunks(abs_path)
        if not hunks:
            break
        hunk = hunks[0]  # always resolve the first remaining hunk

        prompt = (
            "Do NOT commit or stage any changes.\n\n"
            f"In `{rel_path}`, around line {hunk['line']}, there is a single "
            "git merge conflict block produced by two parallel automated fixes "
            "that both modified the same area:\n\n"
            "```\n"
            f"<<<<<<< (fix A)\n{hunk['ours']}\n"
            f"=======\n{hunk['theirs']}\n"
            ">>>>>>> (fix B)\n"
            "```\n\n"
            "For context, the original PR review comments that triggered each "
            "fix were:\n\n"
            f"{intents_block}\n\n"
            "Resolve only this one conflict block by combining both sides so "
            "that all changes from fix A and fix B are preserved. Use the PR "
            "comments above only to understand the intent of each side — do "
            "not discard either fix unless they are genuinely incompatible. "
            "Remove the <<<<<<< / ======= / >>>>>>> markers so the file "
            "remains valid source code. Do NOT touch any other part of the "
            "file and do NOT commit or stage the result."
        )
        print(f"    Hunk at line {hunk['line']} ({len(hunks)} remaining) …")
        send_to_copilot(prompt, work_dir, model=args.model)

        # Safety: if Copilot didn't reduce the hunk count, stop to avoid
        # an infinite loop.
        new_hunks = _parse_conflict_hunks(abs_path)
        if len(new_hunks) >= len(hunks):
            print(f"    Warning: hunk at line {hunk['line']} was not resolved. Stopping.")
            return False

    return True


def _run_batch_parallel(to_fix: list[dict], args, work_dir: str, workers: int) -> None:
    """
    Fix each queued comment in an isolated git worktree (parallel), then apply
    the resulting changes as unstaged modifications in the main working directory.
    No commits are created — git history is not affected.

    Each worker:
      1. Runs Copilot in its own worktree (which starts at HEAD).
      2. Produces a ``git diff HEAD`` patch capturing all changes.
      3. Returns the patch and buffered output.

    After all workers finish the main thread applies each patch in order with
    ``git apply --3way`` (which surfaces conflicts as standard conflict markers
    without creating any commits), resolves successful threads, and reports
    any patches with conflicts.
    """
    print(f"Running {len(to_fix)} comment(s) in parallel with up to {workers} worker(s) …\n")
    print("Changes will be applied as unstaged modifications — git history is not affected.\n")

    worktree_base = tempfile.mkdtemp(prefix=f"pr_fix_{args.pr_id}_")
    # List of (comment, worktree_path) for worktrees we successfully created.
    worktrees: list[tuple[dict, str]] = []

    try:
        # ---- Create one worktree per comment ----
        for comment in to_fix:
            wt_path = os.path.join(worktree_base, f"thread_{comment['thread_id']}")
            r = subprocess.run(
                ["git", "worktree", "add", wt_path, "HEAD"],
                cwd=work_dir, capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                print(
                    f"Warning: could not create worktree for thread "
                    f"{comment['thread_id']}: {r.stderr.strip()}\n"
                    f"  This comment will be skipped in parallel mode."
                )
                continue
            worktrees.append((comment, wt_path))

        if not worktrees:
            print("Error: could not create any worktrees. Falling back to sequential mode.")
            for idx, comment in enumerate(to_fix, start=1):
                _fix_single_comment(comment, f"[{idx}/{len(to_fix)}]", args, work_dir)
            return

        # ---- Worker function ----
        _print_lock = threading.Lock()

        def _worker(comment: dict, wt_path: str) -> tuple[int, str, str, int]:
            """Run Copilot in wt_path, produce a diff patch.
            Returns (thread_id, copilot_output, patch, returncode).
            """
            prompt = _build_copilot_prompt(comment)
            proc = subprocess.Popen(
                ["copilot", "--model", args.model, "--autopilot", "--yolo", "--no-ask-user", "-p", prompt],
                cwd=wt_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            assert proc.stdout is not None
            lines: list[str] = []
            for line in proc.stdout:
                lines.append(line)
            proc.wait()

            if proc.returncode != 0:
                return comment["thread_id"], "".join(lines), "", proc.returncode

            # Generate a unified diff patch (no staging or committing needed)
            diff_r = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=wt_path, capture_output=True, text=True, timeout=30,
            )
            patch = diff_r.stdout if diff_r.returncode == 0 else ""

            # Also capture untracked new files as a patch via git diff --no-index /dev/null
            new_files_patch_parts: list[str] = []
            status_r = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=wt_path, capture_output=True, text=True, timeout=10,
            )
            for line in status_r.stdout.splitlines():
                if line.startswith("??"):
                    new_file = line[3:].strip()
                    nf_r = subprocess.run(
                        ["git", "diff", "--no-index", "/dev/null", new_file],
                        cwd=wt_path, capture_output=True, text=True, timeout=10,
                    )
                    if nf_r.stdout:
                        new_files_patch_parts.append(nf_r.stdout)

            full_patch = patch + "".join(new_files_patch_parts)
            return comment["thread_id"], "".join(lines), full_patch, proc.returncode

        # ---- Run workers in parallel ----
        results: dict[int, tuple[str, str, int]] = {}  # thread_id -> (output, patch, returncode)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(_worker, comment, wt_path): (comment, wt_path)
                for comment, wt_path in worktrees
            }
            done = 0
            for future in concurrent.futures.as_completed(future_map):
                done += 1
                comment, _ = future_map[future]
                try:
                    thread_id, output, patch, returncode = future.result()
                    results[thread_id] = (output, patch, returncode)
                    with _print_lock:
                        status = "\u2713" if returncode == 0 else f"exit={returncode}"
                        print(f"  [{done}/{len(worktrees)}] thread {thread_id} done ({status})")
                except Exception as exc:  # noqa: BLE001
                    with _print_lock:
                        print(f"  Warning: worker failed for thread {comment['thread_id']}: {exc}")
                    results[comment["thread_id"]] = ("", "", 1)

        # ---- Pass 1: apply each patch as unstaged changes, collect conflicts ----
        _sep = "═" * 60
        print(f"\n{_sep}")
        print("Pass 1: Applying changes to working directory (no commits) …")
        print(f"{_sep}\n")

        applied_threads:   list[dict] = []
        conflicted_threads: list[dict] = []

        for idx, (comment, wt_path) in enumerate(worktrees, start=1):
            thread_id = comment["thread_id"]
            output, patch, returncode = results.get(thread_id, ("", "", 1))

            print("\u2500" * 60)
            print(f"[{idx}/{len(worktrees)}] Thread {thread_id}")
            if output:
                print(output)

            if returncode != 0:
                print(f"Skipping: Copilot exited with code {returncode}.")
                print()
                continue

            if not patch.strip():
                print("Copilot made no file changes — comment may already be addressed or invalid.")
                applied_threads.append(comment)
                print()
                continue

            # Write patch to a temp file and apply it with --3way so conflicts
            # appear as merge markers in the working tree (no commits created).
            patch_path = os.path.join(worktree_base, f"thread_{thread_id}.patch")
            with open(patch_path, "w", encoding="utf-8") as pf:
                pf.write(patch)

            apply_r = subprocess.run(
                ["git", "apply", "--3way", patch_path],
                cwd=work_dir, capture_output=True, text=True, timeout=120,
            )
            if apply_r.returncode != 0:
                print(f"\u26a0\ufe0f  Conflict applying patch for thread {thread_id}:")
                if apply_r.stdout.strip():
                    print(apply_r.stdout)
                if apply_r.stderr.strip():
                    print(apply_r.stderr)
                print("Conflict markers left in files — will resolve after all patches are applied.")
                conflicted_threads.append(comment)
            else:
                print("Changes applied as unstaged modifications.")
                applied_threads.append(comment)
            print()

        # ---- Pass 2: one focused Copilot call per conflicted file ----
        if conflicted_threads:
            all_conflicted_files = _find_conflicted_files(work_dir)
            if all_conflicted_files:
                print(f"\n{chr(9552) * 60}")
                print(
                    f"Pass 2: Resolving conflicts — "
                    f"{len(all_conflicted_files)} file(s), one Copilot call each …"
                )
                print(f"{chr(9552) * 60}\n")
                failed_files: set[str] = set()
                for f_path in all_conflicted_files:
                    relevant = _comments_for_file(f_path, conflicted_threads)
                    print(f"  Resolving `{f_path}` ({len(relevant)} related comment(s)) …")
                    resolved_ok = _resolve_conflict_file_with_copilot(f_path, relevant, work_dir, args)
                    if not resolved_ok:
                        failed_files.add(f_path)
                still_conflicted = _find_conflicted_files(work_dir)
                unresolved_files = set(still_conflicted) | failed_files
                if unresolved_files:
                    print(
                        f"\n\u26a0\ufe0f  Copilot could not fully resolve conflicts in: "
                        f"{', '.join(sorted(unresolved_files))}\n"
                        "  Conflict markers remain — resolve manually."
                    )
                    # Only promote threads whose files are fully clean
                    for comment in conflicted_threads:
                        fp = (comment.get("thread_context") or {}).get("filePath", "").lstrip("/")
                        if not fp or fp not in unresolved_files:
                            applied_threads.append(comment)
                else:
                    print("All conflicts resolved by Copilot.")
                    applied_threads.extend(conflicted_threads)
            else:
                # --3way failed but left no markers (e.g. binary file); skip those threads
                print("\u26a0\ufe0f  Patches failed but no conflict markers found — skipping conflicted threads.")

        # ---- Pass 3: mark all successfully applied threads as resolved ----
        if applied_threads:
            print(f"\n{chr(9552) * 60}")
            print("Marking resolved threads \u2026")
            print(f"{chr(9552) * 60}\n")
            for comment in applied_threads:
                thread_id = comment["thread_id"]
                print(f"  Thread {thread_id}: marking as fixed \u2026", end=" ")
                try:
                    resolve_thread(
                        args.org, args.project, args.repo, args.pr_id,
                        thread_id, args.pat,
                    )
                    print("done.")
                except requests.HTTPError as exc:
                    print(f"Warning: {exc.response.status_code} {exc.response.text}")
                except requests.RequestException as exc:
                    print(f"Warning: {exc}")
            print()

    finally:
        # Clean up worktrees
        for _, wt_path in worktrees:
            subprocess.run(
                ["git", "worktree", "remove", "--force", wt_path],
                cwd=work_dir, capture_output=True, timeout=30,
            )
        subprocess.run(["git", "worktree", "prune"], cwd=work_dir, capture_output=True, timeout=10)
        shutil.rmtree(worktree_base, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Azure DevOps PR comments and send each one to the GitHub Copilot CLI."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    _env_bool = lambda key: os.environ.get(key, "").lower() in ("1", "true", "yes")

    # Azure DevOps targeting
    parser.add_argument("--pr-id",   default=int(os.environ["PR_ID"]) if os.environ.get("PR_ID") else None, required=False, type=int, help="Pull Request ID (or set PR_ID in .env).")
    parser.add_argument(
        "--pat",
        default=os.environ.get("AZURE_DEVOPS_PAT"),
        help="Personal Access Token (or set AZURE_DEVOPS_PAT in .env). "
             "Needs 'Code (Read & Write)' and 'Pull Request Threads (Read & Write)' scopes.",
    )

    # Copilot execution
    parser.add_argument(
        "--work-dir",
        default=os.environ.get("WORK_DIR") or None,
        help=(
            "Directory where Copilot CLI commands are executed. "
            "Defaults to the git repo root, or the current working directory. Or set WORK_DIR in .env."
        ),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("MODEL", "claude-sonnet-4.6"),
        help="Model to pass to the Copilot CLI (default: claude-sonnet-4.6; or set MODEL in .env).",
    )

    # Misc
    parser.add_argument(
        "--order",
        default=os.environ.get("ORDER", "desc"),
        choices=["asc", "desc", "file"],
        help="Order to process comments: asc (API order), desc (reversed), file (by file path). "
             "Default: desc. Or set ORDER in .env.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=_env_bool("DRY_RUN"),
        help="Print each comment but do NOT call the Copilot CLI. Or set DRY_RUN=true in .env.",
    )
    parser.add_argument(
        "--include-resolved",
        action="store_true",
        default=_env_bool("INCLUDE_RESOLVED"),
        help="Also process comments in resolved/closed threads (skipped by default). Or set INCLUDE_RESOLVED=true in .env.",
    )
    parser.add_argument(
        "--since",
        default=os.environ.get("SINCE") or None,
        metavar="DATE",
        help="Only include comments published on or after this date (YYYY-MM-DD or ISO 8601). Or set SINCE in .env.",
    )
    parser.add_argument(
        "--until",
        default=os.environ.get("UNTIL") or None,
        metavar="DATE",
        help="Only include comments published on or before this date (YYYY-MM-DD or ISO 8601). Or set UNTIL in .env.",
    )
    parser.add_argument(
        "--log-dir",
        default=os.environ.get("LOG_DIR") or None,
        metavar="DIR",
        help="Directory where the session log file is written (default: current working directory). Or set LOG_DIR in .env.",
    )
    parser.add_argument(
        "--workers",
        default=int(os.environ.get("WORKERS", "1")),
        type=int,
        metavar="N",
        help=(
            "Number of parallel Copilot workers for batch mode (default: 1 = sequential). "
            "When > 1, each comment is fixed in an isolated git worktree. Or set WORKERS in .env."
        ),
    )
    parser.add_argument(
        "--mode",
        default=os.environ.get("MODE") or None,
        choices=["interactive", "batch"],
        help="Processing mode: 'interactive' or 'batch'. Skips the mode prompt when set. Or set MODE in .env.",
    )

    return parser


def main() -> None:
    check_dependencies()
    check_copilot_login()

    parser = build_parser()
    args = parser.parse_args()

    # ---- Always auto-detect org/project/repo from git remote ----
    _git = _detect_from_git(args.work_dir or os.getcwd())
    if _git["work_dir"] and not args.work_dir:
        args.work_dir = _git["work_dir"]
    args.org     = _git["org"]
    args.project = _git["project"]
    args.repo    = _git["repo"]
    if _git["org"] or _git["repo"]:
        print(f"Detected from git remote: org={_git['org'] or '?'}  "
              f"project={_git['project'] or '?'}  repo={_git['repo'] or '?'}  "
              f"work_dir={_git['work_dir'] or '?'}\n")

    # ---- Auto-detect PR ID from current branch (requires pat to be available) ----
    _pat_for_detect = args.pat or os.environ.get("AZURE_DEVOPS_PAT", "")
    if not args.pr_id and args.org and args.project and args.repo and _pat_for_detect:
        _detected_pr = _detect_pr_from_branch(
            args.org, args.project, args.repo, _pat_for_detect,
            args.work_dir or "",
        )
        if _detected_pr:
            args.pr_id = _detected_pr

    # ---- Fail fast if org/project/repo could not be auto-detected ----
    if not args.org:
        parser.error("Could not determine Azure DevOps org from git remote. Ensure you are running from inside the repository.")
    if not args.project:
        parser.error("Could not determine Azure DevOps project from git remote. Ensure you are running from inside the repository.")
    if not args.repo:
        parser.error("Could not determine Azure DevOps repo from git remote. Ensure you are running from inside the repository.")

    # ---- Prompt only for fields that are still missing ----
    _need_prompt = not args.pr_id or not args.pat or not args.work_dir

    if _need_prompt:
        print("Some required values are missing — please provide them:\n")

        if not args.work_dir:
            args.work_dir = _prompt("Working directory", default=os.getcwd())
            if not args.work_dir:
                args.work_dir = os.getcwd()

        # Validate work_dir is a git repo before asking for anything else
        _wd_abs = os.path.abspath(args.work_dir)
        if not os.path.isdir(_wd_abs):
            parser.error(f"--work-dir does not exist or is not a directory: {_wd_abs}")
        _git_check_early = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=_wd_abs, capture_output=True, timeout=10,
        )
        if _git_check_early.returncode != 0:
            parser.error(f"--work-dir is not inside a git repository: {_wd_abs}")

        if not args.pr_id:
            raw = _prompt("Pull Request ID")
            try:
                args.pr_id = int(raw)
            except (ValueError, TypeError):
                parser.error("--pr-id must be a non-empty integer")

        if not args.pat:
            args.pat = _prompt("Personal Access Token (PAT)", secret=True)
            if not args.pat:
                parser.error("--pat is required")

        print()

    # ---- Set up session log ----
    _log_dir = os.path.abspath(args.log_dir) if args.log_dir else os.getcwd()
    os.makedirs(_log_dir, exist_ok=True)
    _ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    _log_path = os.path.join(_log_dir, f"pr_{args   .pr_id}_{_ts}.log")
    _log_file = open(_log_path, "w", encoding="utf-8")  # noqa: SIM115
    _log_file.write(
        f"# pr_comments_to_copilot  PR={args.pr_id}"
        f"  started={datetime.now(timezone.utc).isoformat()}\n\n"
    )
    _log_file.flush()
    sys.stdout = _Tee(sys.__stdout__, _log_file)
    print(f"Session log: {_log_path}\n")

    # ---- Validate inputs ----
    def _parse_date_arg(value: str, name: str) -> datetime:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        parser.error(f"--{name} must be YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS, got: {value}")

    since_dt = _parse_date_arg(args.since, "since") if args.since else None
    until_dt = _parse_date_arg(args.until, "until") if args.until else None

    work_dir = os.path.abspath(args.work_dir)
    if not os.path.isdir(work_dir):
        parser.error(f"--work-dir does not exist or is not a directory: {work_dir}")

    # ---- Validate that work_dir is inside a git repository ----
    _git_check = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=work_dir, capture_output=True, timeout=10,
    )
    if _git_check.returncode != 0:
        parser.error(f"--work-dir is not inside a git repository: {work_dir}")

    # ---- Validate PAT owner and PR creator ----
    print(f"Fetching authenticated user identity …")
    try:
        auth_user_data = fetch_authenticated_user(args.org, args.pat)
        auth_user_email = auth_user_data.get("authenticatedUser", {}).get("providerDisplayName", "")
        auth_user_id = auth_user_data.get("authenticatedUser", {}).get("id", "")
    except requests.HTTPError as exc:
        print(f"Error: Azure DevOps API returned {exc.response.status_code}: {exc.response.text}")
        sys.exit(1)
    except requests.RequestException as exc:
        print(f"Network error: {exc}")
        sys.exit(1)

    # ---- Validate branch match ----
    print(f"Fetching PR #{args.pr_id} details from {args.org}/{args.project}/{args.repo} …")
    try:
        pr_details = fetch_pr_details(args.org, args.project, args.repo, args.pr_id, args.pat)
    except requests.HTTPError as exc:
        print(f"Error: Azure DevOps API returned {exc.response.status_code}: {exc.response.text}")
        sys.exit(1)
    except requests.RequestException as exc:
        print(f"Network error: {exc}")
        sys.exit(1)

    pr_creator = pr_details.get("createdBy", {})
    pr_creator_email = pr_creator.get("uniqueName", "")
    pr_creator_id = pr_creator.get("id", "")

    # Check if PAT owner matches PR creator
    if auth_user_id and pr_creator_id and auth_user_id != pr_creator_id:
        print(f"\n⚠️  WARNING: PAT owner does not match PR creator!")
        print(f"   PAT owner:   {auth_user_email}")
        print(f"   PR creator:  {pr_creator_email}")
        print()
        response = _prompt_yes_no("Do you want to proceed anyway?", default=False)
        if not response:
            print("\nAborted by user.")
            sys.exit(0)
        print()
    elif auth_user_id and pr_creator_id:
        print(f"✓ PAT owner matches PR creator: {auth_user_email}\n")

    pr_source_branch = pr_details.get("sourceRefName", "").replace("refs/heads/", "")
    pr_target_branch = pr_details.get("targetRefName", "").replace("refs/heads/", "")
    local_branch = get_current_branch(work_dir)

    if local_branch and pr_source_branch:
        if local_branch != pr_source_branch:
            print(f"\n❌ ERROR: Branch mismatch!")
            print(f"   PR source branch: {pr_source_branch}")
            print(f"   PR target branch: {pr_target_branch}")
            print(f"   Local branch:     {local_branch}")
            print(f"\nPlease checkout the correct branch before running this script.")
            sys.exit(1)
        else:
            print(f"✓ Branch match: {local_branch} → {pr_target_branch}\n")
    elif not local_branch:
        print("⚠️  Warning: Could not detect current git branch.\n")
    elif not pr_source_branch:
        print("⚠️  Warning: Could not detect PR source branch.\n")

    # ---- Fetch comments ----
    print(f"Fetching PR comments …")
    try:
        comments, raw_response = fetch_pr_comments(
            args.org, args.project, args.repo, args.pr_id, args.pat,
            include_resolved=args.include_resolved,
        )
    except requests.HTTPError as exc:
        print(f"Error: Azure DevOps API returned {exc.response.status_code}: {exc.response.text}")
        sys.exit(1)
    except requests.RequestException as exc:
        print(f"Network error: {exc}")
        sys.exit(1)

    if not comments:
        print("No comments found.")
        return

    # ---- Date filtering ----
    if since_dt or until_dt:
        before = len(comments)
        comments = [
            c for c in comments
            if (
                (since_dt is None or (c["published_date"] is not None and c["published_date"] >= since_dt))
                and
                (until_dt is None or (c["published_date"] is not None and c["published_date"] <= until_dt))
            )
        ]
        print(f"Date filter applied: {before} → {len(comments)} comment(s).")
        if not comments:
            print("No comments match the date filter.")
            return

    if args.order == "desc":
        comments = list(reversed(comments))
    elif args.order == "file":
        comments = sorted(comments, key=lambda c: (c.get("thread_context") or {}).get("filePath") or "")

    print(f"Found {len(comments)} comment(s)  order={args.order}.\n")

    if args.dry_run:
        out_path = os.path.join(os.getcwd(), f"pr_comments_{args.pr_id}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(raw_response, f, indent=2, ensure_ascii=False)
        print(f"[dry-run] Raw Azure DevOps response saved to {out_path}")
        return

    # ---- Ask processing mode ----
    if args.mode:
        mode = args.mode
        print(f"Mode: {mode}\n")
    else:
        _mode_input = questionary.select(
            "How would you like to process the comments?",
            choices=[
                questionary.Choice("Batch       — review all comments first, then fix them all at once", value="batch"),
                questionary.Choice("Interactive — apply each fix immediately after you approve it", value="interactive"),
            ],
        ).ask()
        if _mode_input is None:
            print("\nAborted.")
            sys.exit(0)
        mode = _mode_input
        print(f"  Mode: {mode}\n")

    # ---- Phase 1: Review all comments — approve or reject ----
    to_fix: list[dict] = []
    aborted = False
    for idx, comment in enumerate(comments, start=1):
        separator = "─" * 60
        print(separator)
        ctx = comment.get("thread_context")
        if ctx:
            file_path = ctx.get("filePath", "(unknown)")
            right_start = (ctx.get("rightFileStart") or {}).get("line")
            right_end   = (ctx.get("rightFileEnd")   or {}).get("line")
            left_start  = (ctx.get("leftFileStart")  or {}).get("line")
            left_end    = (ctx.get("leftFileEnd")    or {}).get("line")
            if right_start and right_end and right_start != right_end:
                location = f"lines {right_start}–{right_end}"
            elif right_start:
                location = f"line {right_start}"
            elif left_start and left_end and left_start != left_end:
                location = f"lines {left_start}–{left_end} (old)"
            elif left_start:
                location = f"line {left_start} (old)"
            else:
                location = None
            print(f"[{idx}/{len(comments)}] {file_path}" + (f"  [{location}]" if location else ""))
            diff_snippet = get_diff_context(
                work_dir, file_path,
                right_start or left_start,
                right_end or left_end,
            )
            if diff_snippet:
                comment["diff_snippet"] = diff_snippet
                print()
                _print_diff_side_by_side(diff_snippet)
                print()
                print(f"  {comment['content']}")
                if comment.get("suggestion"):
                    print()
                    print("  [suggestion]")
                    for line in comment["suggestion"].splitlines():
                        print(f"    {line}")
            else:
                print(f"  {comment['content']}")
                if comment.get("suggestion"):
                    print()
                    print("  [git suggestion]")
                    for line in comment["suggestion"].splitlines():
                        print(f"    {line}")
        else:
            print(f"[{idx}/{len(comments)}]")
            print(f"  {comment['content']}")
            if comment.get("suggestion"):
                print()
                print("  [git suggestion]")
                for line in comment["suggestion"].splitlines():
                    print(f"    {line}")
        print()

        try:
            answer = _prompt_yes_no("Fix this comment with Copilot?", default=True)
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            aborted = True
            break

        if not answer:
            try:
                reason = input("Why won't you fix this? (press Enter to skip) ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                aborted = True
                break

            if reason:
                print("Posting comment on thread …")
                try:
                    post_thread_comment(
                        args.org, args.project, args.repo, args.pr_id,
                        comment["thread_id"], reason, args.pat,
                    )
                    print("Comment posted.")
                except requests.HTTPError as exc:
                    print(f"Warning: could not post comment: {exc.response.status_code} {exc.response.text}")
                except requests.RequestException as exc:
                    print(f"Warning: network error while posting comment: {exc}")

            print("Marking thread as won't fix …")
            try:
                wont_fix_thread(
                    args.org, args.project, args.repo, args.pr_id,
                    comment["thread_id"], args.pat,
                )
                print("Thread marked as won't fix.\n")
            except requests.HTTPError as exc:
                print(f"Warning: could not update thread: {exc.response.status_code} {exc.response.text}\n")
            except requests.RequestException as exc:
                print(f"Warning: network error while updating thread: {exc}\n")
            continue

        # Ask for optional extra instructions when there is no suggestion,
        # or when the user declines the suggestion.
        if comment.get("suggestion"):
            try:
                use_suggestion = _prompt_yes_no("Use the suggested fix?", default=True)
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                aborted = True
                break

            if not use_suggestion:
                try:
                    extra_prompt = input("Any additional instructions for Copilot before fixing? (press Enter to skip): ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nAborted.")
                    aborted = True
                    break

                if extra_prompt:
                    comment["extra_prompt"] = extra_prompt
                # Clear the suggestion so Copilot can craft a fix using the comment and optional extra instructions.
                comment["suggestion"] = None
        else:
            try:
                extra_prompt = input("Any additional instructions for Copilot before fixing? (press Enter to skip): ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                aborted = True
                break

            if extra_prompt:
                comment["extra_prompt"] = extra_prompt

        if mode == "interactive":
            _fix_single_comment(comment, f"[{idx}/{len(comments)}]", args, work_dir)
        else:
            to_fix.append(comment)
            print("Queued for Copilot.\n")

    # ---- Phase 2: Send all queued comments to Copilot (batch mode only) ----
    if mode == "batch":
        if not to_fix:
            if not aborted:
                print("\nNo comments queued for Copilot.")
            return

        print(f"\n{'═' * 60}")
        print(f"Sending {len(to_fix)} queued comment(s) to Copilot …")
        print(f"{'═' * 60}\n")

        if args.workers > 1:
            _run_batch_parallel(to_fix, args, work_dir, args.workers)
        else:
            for idx, comment in enumerate(to_fix, start=1):
                separator = "─" * 60
                print(separator)
                _fix_single_comment(comment, f"[{idx}/{len(to_fix)}]", args, work_dir)


if __name__ == "__main__":
    main()
