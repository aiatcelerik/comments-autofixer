"""Agent registry and shared execution helpers."""

from __future__ import annotations

import subprocess
from typing import Protocol

from . import claude, codex, copilot


class Agent(Protocol):
    name: str
    display_name: str
    auth_error_patterns: tuple[str, ...]

    def required_tools(self) -> dict[str, str]:
        """Return required CLI tools mapped to user-facing error messages."""
        ...

    def preflight(self) -> None:
        """Run agent-specific checks before processing comments."""
        ...

    def build_command(self, prompt: str, work_dir: str, model: str) -> list[str]:
        """Return the command line for this agent."""
        ...


_AGENTS: dict[str, Agent] = {
    copilot.AGENT.name: copilot.AGENT,
    codex.AGENT.name: codex.AGENT,
    claude.AGENT.name: claude.AGENT,
}
DEFAULT_NAME = copilot.AGENT.name


def names() -> tuple[str, ...]:
    """Return all supported agent names."""
    return tuple(_AGENTS)


def get(name: str) -> Agent:
    """Return the selected agent implementation."""
    try:
        return _AGENTS[name]
    except KeyError as exc:
        raise ValueError(f"Unsupported agent: {name}") from exc


def has_auth_error(agent: Agent, output: str) -> bool:
    """Return True if *output* appears to contain an auth failure."""
    lowered = output.lower()
    return any(pattern in lowered for pattern in agent.auth_error_patterns)


def send(agent: Agent, prompt: str, work_dir: str, model: str) -> subprocess.CompletedProcess:
    """
    Run an AI coding CLI in the specified working directory.

    Output is streamed through stdout so it is captured by the session log when
    logging is active. The returned CompletedProcess stdout contains the full
    combined output (stdout + stderr) so callers can inspect it for errors.
    """
    proc = subprocess.Popen(
        agent.build_command(prompt, work_dir, model),
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
