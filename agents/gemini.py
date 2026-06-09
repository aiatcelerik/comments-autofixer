"""Google Gemini CLI agent implementation."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class GeminiAgent:
    name: str = "gemini"
    display_name: str = "Gemini CLI"
    default_model: str = "gemini-2.5-pro"
    auth_error_patterns: tuple[str, ...] = (
        "not logged in",
        "not authenticated",
        "unauthenticated",
        "unauthorized",
        "please log in",
        "login required",
        "authentication required",
        "invalid api key",
        "google_api_key",
        "api key not valid",
        "permission denied",
    )

    def required_tools(self) -> dict[str, str]:
        return {
            "gemini": (
                "Gemini CLI ('gemini') is not found on PATH.\n"
                "  Fix: install via 'npm install -g @google/gemini-cli' and authenticate."
            ),
        }

    def preflight(self) -> None:
        """Verify Gemini CLI is available before processing comments."""
        try:
            result = subprocess.run(
                ["gemini", "--version"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            print(
                "ERROR: Gemini CLI version check timed out after 30 s.\n"
                "  Run 'gemini --version' manually to verify installation.",
                file=sys.stderr,
            )
            sys.exit(1)
        except OSError as exc:
            print(
                f"ERROR: could not run Gemini CLI: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

        if result.returncode != 0:
            print(
                f"ERROR: Gemini CLI check failed (exit code {result.returncode}).\n"
                "  Ensure 'gemini' is installed and authenticated.\n"
                f"  Output:\n{(result.stdout + result.stderr).strip()}",
                file=sys.stderr,
            )
            sys.exit(1)

    def build_command(self, prompt: str, work_dir: str, model: str) -> list[str]:
        gemini_model = model
        if model:
            model_lower = model.lower()
            if model_lower in ("claude-sonnet-4.6", "claude-sonnet-4-6"):
                gemini_model = "gemini-2.5-pro"

        return [
            "gemini",
            "--model",
            gemini_model,
            "--yolo",
            "--skip-trust",
            "-p",
            prompt,
        ]


AGENT = GeminiAgent()
