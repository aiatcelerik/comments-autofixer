"""Google Antigravity CLI agent implementation."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class AntigravityAgent:
    name: str = "antigravity"
    display_name: str = "Antigravity CLI"
    default_model: str = "Gemini 3.5 Flash (Medium)"
    auth_error_patterns: tuple[str, ...] = (
        "not logged in",
        "not authenticated",
        "unauthenticated",
        "unauthorized",
        "please log in",
        "login required",
        "authentication required",
        "sign in",
        "google sign-in",
        "authorization url",
        "authorization code",
        "keyring",
    )

    def required_tools(self) -> dict[str, str]:
        return {
            "agy": (
                "Antigravity CLI ('agy') is not found on PATH.\n"
                "  Fix: install Antigravity CLI and ensure 'agy' is on your PATH."
            ),
        }

    def preflight(self) -> None:
        """Verify Antigravity CLI is available before processing comments."""
        try:
            result = subprocess.run(
                ["agy", "--version"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            print(
                "ERROR: Antigravity CLI version check timed out after 30 s.\n"
                "  Run 'agy --version' manually to verify installation.",
                file=sys.stderr,
            )
            sys.exit(1)
        except OSError as exc:
            print(
                f"ERROR: could not run Antigravity CLI: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

        if result.returncode != 0:
            print(
                f"ERROR: Antigravity CLI check failed (exit code {result.returncode}).\n"
                "  Ensure 'agy' is installed and can launch successfully.\n"
                f"  Output:\n{(result.stdout + result.stderr).strip()}",
                file=sys.stderr,
            )
            sys.exit(1)

    def build_command(self, prompt: str, work_dir: str, model: str) -> list[str]:
        mapped_model = model
        if model:
            model_lower = model.lower()
            if model_lower in ("claude-sonnet-4.6", "claude-sonnet-4-6"):
                mapped_model = "Claude Sonnet 4.6 (Thinking)"
            elif model_lower == "claude-opus":
                mapped_model = "Claude Opus 4.6 (Thinking)"
            elif model_lower in ("gemini-3.5-flash", "gemini-flash", "flash-3.5", "gemini-3.5-flash-medium"):
                mapped_model = "Gemini 3.5 Flash (Medium)"
            elif model_lower == "gemini-3.5-flash-high":
                mapped_model = "Gemini 3.5 Flash (High)"
            elif model_lower == "gemini-3.5-flash-low":
                mapped_model = "Gemini 3.5 Flash (Low)"
            elif model_lower in ("gemini-3.1-pro", "gemini-pro", "pro-3.1", "gemini-3.1-pro-low"):
                mapped_model = "Gemini 3.1 Pro (Low)"
            elif model_lower == "gemini-3.1-pro-high":
                mapped_model = "Gemini 3.1 Pro (High)"

        cmd = [
            "agy",
            "--dangerously-skip-permissions",
            "--print",
        ]
        if mapped_model:
            cmd.extend(["--model", mapped_model])
        cmd.append(prompt)
        return cmd


AGENT = AntigravityAgent()
