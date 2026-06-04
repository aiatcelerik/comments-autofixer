"""GitHub Copilot CLI agent implementation."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from os import environ


@dataclass(frozen=True)
class CopilotAgent:
    name: str = "copilot"
    display_name: str = "Copilot CLI"
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
        tools = {
            "copilot": (
                "GitHub Copilot CLI ('copilot') is not found on PATH.\n"
                "  Fix: install via 'gh extension install github/gh-copilot' "
                "and ensure 'gh copilot' (or a 'copilot' wrapper) is on your PATH."
            ),
        }
        if not any(environ.get(key) for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")):
            tools["gh"] = (
                "GitHub CLI ('gh') is not found on PATH.\n"
                "  Fix: install from https://cli.github.com/"
            )
        return tools

    def preflight(self) -> None:
        """Verify Copilot has a usable auth source before processing comments."""
        if any(environ.get(key) for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")):
            return

        try:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            print(
                "ERROR: GitHub CLI auth check timed out after 30 s.\n"
                "  Run 'gh auth status' manually and ensure you're logged in.",
                file=sys.stderr,
            )
            sys.exit(1)
        except OSError as exc:
            print(
                f"ERROR: could not run GitHub CLI auth check: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

        if result.returncode != 0:
            print(
                f"ERROR: GitHub CLI is not authenticated (exit code {result.returncode}).\n"
                "  Run 'gh auth login' and try again.\n"
                f"  Output:\n{(result.stdout + result.stderr).strip()}",
                file=sys.stderr,
            )
            sys.exit(1)

    def build_command(self, prompt: str, work_dir: str, model: str) -> list[str]:
        return [
            "copilot",
            "--model",
            model,
            "--autopilot",
            "--allow-all",
            "--no-ask-user",
            "-p",
            prompt,
        ]


AGENT = CopilotAgent()
