"""Adapter for SlowBooks' local OpenAI Codex / ChatGPT provider.

This module intentionally talks only to the official local ``codex`` CLI.
It never reads Codex credential files and never copies OAuth state into
SlowBooks. The CLI remains responsible for ChatGPT authentication.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

CODEX_PROVIDER_KEY = "openai_codex"
CODEX_PROVIDER_LABEL = "OpenAI Codex / ChatGPT"
CODEX_DEFAULT_MODEL = "gpt-5.5"
CODEX_TIMEOUT_ENV = "SLOWBOOKS_CODEX_TIMEOUT_SECONDS"
CODEX_DEFAULT_TIMEOUT = 180.0
CODEX_STATUS_TIMEOUT = 15.0
CODEX_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@+-]{1,128}$")


class CodexAdapterError(Exception):
    """Base class for user-safe Codex adapter failures."""

    user_message = "Codex returned an error"

    def __str__(self) -> str:
        return self.user_message


class CodexUnavailableError(CodexAdapterError):
    user_message = "Codex CLI is not installed"


class CodexUnauthenticatedError(CodexAdapterError):
    user_message = (
        "Codex is installed but not signed in with ChatGPT. Run `codex login` "
        "in Terminal and complete the official Codex login workflow."
    )


class CodexTimeoutError(CodexAdapterError):
    user_message = "Codex request timed out"


class CodexExecutionError(CodexAdapterError):
    user_message = "Codex returned an error"


class CodexEmptyResponseError(CodexAdapterError):
    user_message = "Codex returned no usable response"


class CodexMalformedOutputError(CodexAdapterError):
    user_message = "Codex returned malformed output"


class CodexInvalidModelError(CodexAdapterError):
    user_message = "Codex model name is invalid"


@dataclass(frozen=True)
class CodexStatus:
    installed: bool
    authenticated: bool
    auth_mode: str = ""
    message: str = ""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _codex_executable() -> str:
    exe = shutil.which("codex")
    if not exe:
        raise CodexUnavailableError()
    return exe


def _timeout() -> float:
    raw = os.getenv(CODEX_TIMEOUT_ENV, "").strip()
    if not raw:
        return CODEX_DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return CODEX_DEFAULT_TIMEOUT
    return value if value > 0 else CODEX_DEFAULT_TIMEOUT


def get_status(runner: Runner = subprocess.run) -> CodexStatus:
    """Return whether the Codex CLI is installed and signed in via ChatGPT."""
    try:
        exe = _codex_executable()
    except CodexUnavailableError:
        return CodexStatus(
            installed=False,
            authenticated=False,
            message="Codex CLI is not installed",
        )

    try:
        proc = runner(
            [exe, "login", "status"],
            capture_output=True,
            text=True,
            timeout=CODEX_STATUS_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        return CodexStatus(
            installed=False,
            authenticated=False,
            message="Codex CLI is not installed",
        )
    except subprocess.TimeoutExpired:
        return CodexStatus(
            installed=True,
            authenticated=False,
            message="Codex login status timed out",
        )

    output = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode == 0 and "Logged in using ChatGPT" in output:
        return CodexStatus(
            installed=True,
            authenticated=True,
            auth_mode="chatgpt",
            message="Codex is installed and signed in with ChatGPT",
        )

    if proc.returncode == 0 and "Logged in" in output:
        return CodexStatus(
            installed=True,
            authenticated=False,
            auth_mode="other",
            message=(
                "Codex is installed, but it is not signed in with ChatGPT. "
                "Run `codex login` and choose ChatGPT login."
            ),
        )

    return CodexStatus(
        installed=True,
        authenticated=False,
        message=(
            "Codex is installed but not signed in. Run `codex login` in Terminal "
            "and complete the official Codex login workflow."
        ),
    )


def ensure_authenticated(runner: Runner = subprocess.run) -> CodexStatus:
    status = get_status(runner=runner)
    if not status.installed:
        raise CodexUnavailableError()
    if not status.authenticated:
        raise CodexUnauthenticatedError()
    return status


def _parse_jsonl_events(output: str) -> None:
    """Validate Codex JSONL enough to catch broken output early."""
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("WARNING:"):
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexMalformedOutputError() from exc


def build_codex_prompt(system: str, user: str) -> str:
    return (
        "You are acting as the SlowBooks AI analysis provider.\n"
        "Return only the requested SlowBooks business analysis. Do not comment "
        "on the development environment, repository, Codex, or tooling.\n\n"
        "SYSTEM INSTRUCTIONS:\n"
        f"{system.strip()}\n\n"
        "USER REQUEST:\n"
        f"{user.strip()}"
    )


def run_prompt(
    system: str,
    user: str,
    model: str = "",
    timeout: Optional[float] = None,
    runner: Runner = subprocess.run,
) -> str:
    """Run a single prompt through local ``codex exec`` and return final text."""
    ensure_authenticated(runner=runner)
    exe = _codex_executable()
    prompt = build_codex_prompt(system, user)
    effective_timeout = timeout if timeout is not None else _timeout()
    model = (model or CODEX_DEFAULT_MODEL).strip()
    if not CODEX_MODEL_RE.match(model):
        raise CodexInvalidModelError()

    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="slowbooks-codex-", suffix=".txt", delete=False
        ) as tmp:
            tmp_path = tmp.name

        cmd = [
            exe,
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--output-last-message",
            tmp_path,
        ]
        cmd.extend(["--model", model])
        cmd.append(prompt)

        try:
            proc = runner(
                cmd,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CodexUnavailableError() from exc
        except subprocess.TimeoutExpired as exc:
            raise CodexTimeoutError() from exc

        if proc.returncode != 0:
            combined = f"{proc.stdout}\n{proc.stderr}"
            lowered = combined.lower()
            if "not logged in" in lowered or "login" in lowered:
                raise CodexUnauthenticatedError()
            raise CodexExecutionError()

        _parse_jsonl_events(proc.stdout)

        try:
            final_text = Path(tmp_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CodexEmptyResponseError() from exc

        if not final_text:
            raise CodexEmptyResponseError()
        return final_text
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass
