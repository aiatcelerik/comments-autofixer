"""Anthropic Claude Code agent implementation."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class ClaudeAgent:
    name: str = "claude"
    display_name: str = "Claude Code"
    auth_error_patterns: tuple[str, ...] = (
        "not logged in",
        "not authenticated",
        "unauthenticated",
        "unauthorized",
        "please log in",
        "login required",
        "authentication required",
        "invalid api key",
        "anthropic_api_key",
    )

    def required_tools(self) -> dict[str, str]:
        return {
            "claude": (
                "Claude Code ('claude') is not found on PATH.\n"
                "  Fix: install and authenticate Claude Code."
            ),
        }

    def preflight(self) -> None:
        """Verify Claude Code authentication before processing comments."""
        try:
            result = subprocess.run(
                ["claude", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            print(
                "ERROR: Claude Code auth check timed out after 30 s.\n"
                "  Run 'claude auth status' manually and ensure you're logged in.",
                file=sys.stderr,
            )
            sys.exit(1)
        except OSError as exc:
            print(
                f"ERROR: could not run Claude Code auth check: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

        if result.returncode != 0:
            print(
                f"ERROR: Claude Code is not authenticated (exit code {result.returncode}).\n"
                "  Run 'claude auth login' and try again.\n"
                f"  Output:\n{(result.stdout + result.stderr).strip()}",
                file=sys.stderr,
            )
            sys.exit(1)

    def build_command(self, prompt: str, work_dir: str, model: str) -> list[str]:
        claude_model = "claude-sonnet-4-6" if model == "claude-sonnet-4.6" else model
        return [
            "claude",
            "--dangerously-skip-permissions",
            "--model",
            claude_model,
            "--print",
            prompt,
        ]


AGENT = ClaudeAgent()
