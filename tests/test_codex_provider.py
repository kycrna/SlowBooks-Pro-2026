import json
from dataclasses import dataclass

from app.routes import analytics as analytics_routes
from app.services.ai_service import PROVIDERS, call_provider
from app.services.codex_adapter import CODEX_PROVIDER_KEY, CODEX_PROVIDER_LABEL


@dataclass(frozen=True)
class _FakeCodexStatus:
    installed: bool = True
    authenticated: bool = True
    auth_mode: str = "chatgpt"
    message: str = "Codex is installed and signed in with ChatGPT"


def _save_provider(client, provider, model=""):
    r = client.put(
        "/api/analytics/ai-config",
        json={"provider": provider, "model": model},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_provider_registry_includes_openai_codex():
    spec = PROVIDERS[CODEX_PROVIDER_KEY]

    assert spec.label == CODEX_PROVIDER_LABEL
    assert spec.needs_api_key is False
    assert PROVIDERS["openai"].needs_api_key is True


def test_call_provider_delegates_to_codex_adapter(monkeypatch):
    seen = {}

    def fake_run_codex_prompt(system, user, model):
        seen.update({"system": system, "user": user, "model": model})
        return "codex analysis"

    monkeypatch.setattr(
        "app.services.ai_service.run_codex_prompt", fake_run_codex_prompt
    )

    text = call_provider(
        provider_key=CODEX_PROVIDER_KEY,
        api_key="",
        model="gpt-codex-test",
        system="system prompt",
        user="user prompt",
    )

    assert text == "codex analysis"
    assert seen == {
        "system": "system prompt",
        "user": "user prompt",
        "model": "gpt-codex-test",
    }


def test_ai_config_reports_codex_status_without_credentials(client, monkeypatch):
    monkeypatch.setattr(
        analytics_routes, "get_codex_status", lambda: _FakeCodexStatus()
    )

    body = _save_provider(client, CODEX_PROVIDER_KEY)
    raw = json.dumps(body)

    assert body["provider"] == CODEX_PROVIDER_KEY
    assert body["codex_status"]["authenticated"] is True
    assert "auth.json" not in raw
    assert "sk-very-secret" not in raw


def test_codex_ai_insights_works_without_api_key(client, monkeypatch):
    monkeypatch.setattr(
        analytics_routes, "get_codex_status", lambda: _FakeCodexStatus()
    )

    def fake_generate_insights(**kwargs):
        assert kwargs["provider_key"] == CODEX_PROVIDER_KEY
        assert kwargs["api_key"] == ""
        return {
            "insights": "codex insight",
            "provider": CODEX_PROVIDER_KEY,
            "provider_label": CODEX_PROVIDER_LABEL,
            "model": kwargs["model"],
            "generated_at": "2026-09-04",
        }

    monkeypatch.setattr(
        analytics_routes, "ai_generate_insights", fake_generate_insights
    )
    _save_provider(client, CODEX_PROVIDER_KEY)

    r = client.post("/api/analytics/ai-insights?period=month")

    assert r.status_code == 200, r.text
    assert r.json()["insights"] == "codex insight"


def test_codex_predefined_action_works_without_api_key(client, monkeypatch):
    monkeypatch.setattr(
        analytics_routes, "get_codex_status", lambda: _FakeCodexStatus()
    )

    def fake_run_action(**kwargs):
        assert kwargs["provider"] == CODEX_PROVIDER_KEY
        assert kwargs["api_key"] == ""
        return {
            "action_key": kwargs["action_key"],
            "label": "Top customers by revenue",
            "category": "Customers & Sales",
            "framing": "frame",
            "analysis": "codex action",
            "data": {},
            "provider": CODEX_PROVIDER_KEY,
            "model": kwargs["model"],
            "uses_period": True,
        }

    monkeypatch.setattr(analytics_routes, "ai_run_action", fake_run_action)
    _save_provider(client, CODEX_PROVIDER_KEY)

    r = client.post("/api/analytics/ai-actions/top_customers?period=month")

    assert r.status_code == 200, r.text
    assert r.json()["analysis"] == "codex action"


def test_openai_still_requires_api_key(client, monkeypatch):
    monkeypatch.setattr(
        analytics_routes, "get_codex_status", lambda: _FakeCodexStatus()
    )
    _save_provider(client, "openai", model="gpt-5.4-mini")

    r = client.post("/api/analytics/ai-insights?period=month")

    assert r.status_code == 400
    assert r.json()["detail"] == "No AI API key configured"


def test_codex_ai_query_returns_version_one_limitation(client, monkeypatch):
    monkeypatch.setattr(
        analytics_routes, "get_codex_status", lambda: _FakeCodexStatus()
    )
    _save_provider(client, CODEX_PROVIDER_KEY)

    r = client.post("/api/analytics/ai-query?question=hello")

    assert r.status_code == 400
    assert (
        "Interactive tool-driven AI Query support is not yet implemented"
        in r.json()["detail"]
    )
