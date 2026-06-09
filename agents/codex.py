"""OpenAI Codex CLI agent implementation."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class CodexAgent:
    name: str = "codex"
    display_name: str = "Codex CLI"
    default_model: str = "gpt-4o"
    auth_error_patterns: tuple[str, ...] = (
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

    def required_tools(self) -> dict[str, str]:
        return {
            "codex": (
                "Codex CLI ('codex') is not found on PATH.\n"
                "  Fix: install and authenticate the OpenAI Codex CLI."
            ),
        }

    def preflight(self) -> None:
        """Verify Codex CLI authentication before processing comments."""
        try:
            result = subprocess.run(
                ["codex", "login", "status"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            print(
                "ERROR: Codex CLI auth check timed out after 30 s.\n"
                "  Run 'codex login status' manually and ensure you're logged in.",
                file=sys.stderr,
            )
            sys.exit(1)
        except OSError as exc:
            print(
                f"ERROR: could not run Codex CLI auth check: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

        if result.returncode != 0:
            print(
                f"ERROR: Codex CLI is not authenticated (exit code {result.returncode}).\n"
                "  Run 'codex login' and try again.\n"
                f"  Output:\n{(result.stdout + result.stderr).strip()}",
                file=sys.stderr,
            )
            sys.exit(1)

    def build_command(self, prompt: str, work_dir: str, model: str) -> list[str]:
        return [
            "codex",
            "--dangerously-bypass-approvals-and-sandbox",
            "exec",
            "--model",
            model,
            "--cd",
            work_dir,
            prompt,
        ]


AGENT = CodexAgent()
