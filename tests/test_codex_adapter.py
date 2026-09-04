import json
import subprocess
from pathlib import Path

import pytest

from app.services import codex_adapter
from app.services.codex_adapter import (
    CodexEmptyResponseError,
    CodexExecutionError,
    CodexMalformedOutputError,
    CodexInvalidModelError,
    CodexTimeoutError,
    CodexUnauthenticatedError,
    CodexUnavailableError,
    get_status,
    run_prompt,
)


def _proc(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=args, returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_status_reports_codex_unavailable(monkeypatch):
    monkeypatch.setattr(codex_adapter.shutil, "which", lambda _name: None)

    status = get_status()

    assert status.installed is False
    assert status.authenticated is False
    assert status.message == "Codex CLI is not installed"


def test_status_reports_chatgpt_authenticated(monkeypatch):
    monkeypatch.setattr(
        codex_adapter.shutil, "which", lambda _name: "/usr/local/bin/codex"
    )

    def runner(args, **kwargs):
        return _proc(args, stdout="Logged in using ChatGPT\n")

    status = get_status(runner=runner)

    assert status.installed is True
    assert status.authenticated is True
    assert status.auth_mode == "chatgpt"


def test_status_reports_authenticated_but_not_chatgpt(monkeypatch):
    monkeypatch.setattr(
        codex_adapter.shutil, "which", lambda _name: "/usr/local/bin/codex"
    )

    def runner(args, **kwargs):
        return _proc(args, stdout="Logged in using API key\n")

    status = get_status(runner=runner)

    assert status.installed is True
    assert status.authenticated is False
    assert status.auth_mode == "other"


def test_status_reports_unauthenticated(monkeypatch):
    monkeypatch.setattr(
        codex_adapter.shutil, "which", lambda _name: "/usr/local/bin/codex"
    )

    def runner(args, **kwargs):
        return _proc(args, returncode=1, stderr="Not logged in\n")

    status = get_status(runner=runner)

    assert status.installed is True
    assert status.authenticated is False


def test_run_prompt_invokes_codex_exec_and_returns_final_message(monkeypatch):
    monkeypatch.setattr(
        codex_adapter.shutil, "which", lambda _name: "/usr/local/bin/codex"
    )
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if args[1:3] == ["login", "status"]:
            return _proc(args, stdout="Logged in using ChatGPT\n")
        out_file = Path(args[args.index("--output-last-message") + 1])
        out_file.write_text("analysis text\n", encoding="utf-8")
        return _proc(
            args,
            stdout=json.dumps(
                {"type": "item.completed", "item": {"text": "analysis text"}}
            )
            + "\n",
        )

    result = run_prompt("system", "user", model="gpt-test", timeout=3, runner=runner)

    assert result == "analysis text"
    assert calls[1][:5] == [
        "/usr/local/bin/codex",
        "exec",
        "--json",
        "--sandbox",
        "read-only",
    ]
    assert "--model" in calls[1]
    assert "gpt-test" in calls[1]


def test_run_prompt_missing_executable(monkeypatch):
    monkeypatch.setattr(codex_adapter.shutil, "which", lambda _name: None)

    with pytest.raises(CodexUnavailableError):
        run_prompt("system", "user")


def test_run_prompt_rejects_invalid_model(monkeypatch):
    monkeypatch.setattr(
        codex_adapter.shutil, "which", lambda _name: "/usr/local/bin/codex"
    )

    def runner(args, **kwargs):
        return _proc(args, stdout="Logged in using ChatGPT\n")

    with pytest.raises(CodexInvalidModelError):
        run_prompt("system", "user", model="bad model; rm -rf /", runner=runner)


def test_run_prompt_unauthenticated(monkeypatch):
    monkeypatch.setattr(
        codex_adapter.shutil, "which", lambda _name: "/usr/local/bin/codex"
    )

    def runner(args, **kwargs):
        return _proc(args, returncode=1, stderr="Not logged in\n")

    with pytest.raises(CodexUnauthenticatedError):
        run_prompt("system", "user", runner=runner)


def test_run_prompt_timeout(monkeypatch):
    monkeypatch.setattr(
        codex_adapter.shutil, "which", lambda _name: "/usr/local/bin/codex"
    )

    def runner(args, **kwargs):
        if args[1:3] == ["login", "status"]:
            return _proc(args, stdout="Logged in using ChatGPT\n")
        raise subprocess.TimeoutExpired(args, timeout=3)

    with pytest.raises(CodexTimeoutError):
        run_prompt("system", "user", timeout=3, runner=runner)


def test_run_prompt_nonzero_exit_is_generic(monkeypatch):
    monkeypatch.setattr(
        codex_adapter.shutil, "which", lambda _name: "/usr/local/bin/codex"
    )

    def runner(args, **kwargs):
        if args[1:3] == ["login", "status"]:
            return _proc(args, stdout="Logged in using ChatGPT\n")
        return _proc(args, returncode=2, stderr="secret-ish implementation detail")

    with pytest.raises(CodexExecutionError) as exc:
        run_prompt("system", "user", runner=runner)

    assert str(exc.value) == "Codex returned an error"
    assert "secret-ish" not in str(exc.value)


def test_run_prompt_empty_output(monkeypatch):
    monkeypatch.setattr(
        codex_adapter.shutil, "which", lambda _name: "/usr/local/bin/codex"
    )

    def runner(args, **kwargs):
        if args[1:3] == ["login", "status"]:
            return _proc(args, stdout="Logged in using ChatGPT\n")
        return _proc(args, stdout=json.dumps({"type": "turn.completed"}) + "\n")

    with pytest.raises(CodexEmptyResponseError):
        run_prompt("system", "user", runner=runner)


def test_run_prompt_malformed_jsonl(monkeypatch):
    monkeypatch.setattr(
        codex_adapter.shutil, "which", lambda _name: "/usr/local/bin/codex"
    )

    def runner(args, **kwargs):
        if args[1:3] == ["login", "status"]:
            return _proc(args, stdout="Logged in using ChatGPT\n")
        out_file = Path(args[args.index("--output-last-message") + 1])
        out_file.write_text("analysis text\n", encoding="utf-8")
        return _proc(args, stdout="{not-json}\n")

    with pytest.raises(CodexMalformedOutputError):
        run_prompt("system", "user", runner=runner)
