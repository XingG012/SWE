"""Offline Gemini integration, shared accounting and model resume checks."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sweagent.agent.agents import Agent, AgentArguments
from sweagent.agent.gemini_flash import GeminiFlashModel
from sweagent.agent.models import APIStats, CostLimitExceededError, ModelArguments, get_model
from sweagent.utils.secrets import credential_free_environment
import enigma_reproduction.run_nyu as runner


@pytest.fixture(autouse=True)
def gemini_runner_settings(monkeypatch):
    settings = json.loads(Path("enigma_reproduction/gemini_settings.json").read_text())
    monkeypatch.setattr(runner, "SETTINGS", settings)


@pytest.fixture
def gemini(monkeypatch, tmp_path):
    monkeypatch.setenv("TEAMOROUTER_API_KEY", "synthetic-gemini-credential")
    monkeypatch.setenv("ENIGMA_USAGE_LEDGER", str(tmp_path / "usage.jsonl"))
    factory = Mock(return_value=Mock())
    monkeypatch.setattr("sweagent.agent.gemini_flash.OpenAI", factory)
    model = get_model(ModelArguments(model_name="gemini-3.6-flash",
                                    per_instance_cost_limit=.8, total_cost_limit=2.4,
                                    temperature=0, top_p=.95))
    assert isinstance(model, GeminiFlashModel)
    assert factory.call_args.kwargs["base_url"] == "https://api.teamorouter.com/v1"
    assert factory.call_args.kwargs["max_retries"] == 0
    return model


def response(usage=None, content="DISCUSSION\nInspect files\n```\nls\n```"):
    if usage is None:
        usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=100,
                                prompt_tokens_details=SimpleNamespace(cached_tokens=200))
    return SimpleNamespace(model="gemini-3.6-flash", usage=usage,
                           choices=[SimpleNamespace(finish_reason="stop",
                                   message=SimpleNamespace(content=content))])


def test_gemini_request_and_accounting(gemini, tmp_path):
    gemini.client.chat.completions.create.return_value = response()
    gemini.query([{"role": "system", "content": "EnIGMA"},
                  {"role": "user", "content": "challenge", "agent": "primary"}])
    kwargs = gemini.client.chat.completions.create.call_args.kwargs
    assert kwargs == {"messages": [{"role": "system", "content": "EnIGMA"},
                                   {"role": "user", "content": "challenge"}],
                      "model": "gemini-3.6-flash", "temperature": 0,
                      "top_p": .95, "max_tokens": 4096}
    assert gemini.stats.instance_cost == pytest.approx((200*.03 + 800*.3 + 100*1.5)/1e6)
    row = json.loads((tmp_path / "usage.jsonl").read_text())
    assert row["model"] == "gemini-3.6-flash" and row["accounting_complete"]
    assert row["pricing_accounting"] == "router_rate_snapshot_estimate"


def test_summary_uses_gemini_and_shares_budget(gemini, monkeypatch):
    # Real category arguments propagate the chosen model to the summarizer.
    models = []
    def fake_model(args, *unused):
        assert args.model_name == "gemini-3.6-flash"
        model = Mock(strict_accounting=True)
        models.append(model)
        return model
    monkeypatch.setattr("sweagent.agent.agents.get_model", fake_model)
    agent = Agent("primary", AgentArguments(model=gemini.args,
                  config_file=Path("config/ctf/ctf_crypto.yaml")))
    assert len(models) == 2 and agent.summarizer_model.budget_source is agent.model
    gemini.budget_source = SimpleNamespace(stats=APIStats(instance_cost=.7999, total_cost=.7999))
    gemini.client.chat.completions.create.return_value = response()
    with pytest.raises(CostLimitExceededError):
        gemini.query([])
    with pytest.raises(CostLimitExceededError):
        gemini.query([])
    assert gemini.stats.api_calls == 1


@pytest.mark.parametrize("bad", [None, SimpleNamespace(prompt_tokens=-1, completion_tokens=1),
                                  SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                                                  prompt_tokens_details=SimpleNamespace(cached_tokens=101))])
def test_invalid_gemini_usage_stops(gemini, tmp_path, bad):
    result = response()
    result.usage = bad
    gemini.client.chat.completions.create.return_value = result
    with pytest.raises(RuntimeError):
        gemini.query([])
    assert json.loads((tmp_path / "usage.jsonl").read_text())["accounting_complete"] is False


def test_gemini_credential_redaction_and_api_failure(gemini, tmp_path):
    assert "TEAMOROUTER_API_KEY" not in credential_free_environment()
    with pytest.raises(RuntimeError, match="request blocked"):
        gemini.query([{"role": "user", "content": "synthetic-gemini-credential"}])
    gemini.client.chat.completions.create.assert_not_called()
    gemini.client.chat.completions.create.side_effect = ValueError("synthetic-gemini-credential")
    with pytest.raises(RuntimeError) as error:
        gemini.query([])
    assert "synthetic-gemini-credential" not in str(error.value)
    assert "synthetic-gemini-credential" not in (tmp_path / "usage.jsonl").read_text()
    assert gemini.client.chat.completions.create.call_count == 1


def test_resume_excludes_other_models_endpoints_and_budgets(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GENERATED", tmp_path)
    for index, (model, endpoint, budget) in enumerate([
        ("deepseek-flash", "https://api.deepseek.com", .1),
        ("gemini-3.6-flash", "https://api.teamorouter.com/v1", .8),
        ("gemini-3.6-flash", "https://other.example/v1", .8),
        ("gemini-3.6-flash", "https://api.teamorouter.com/v1", .1),
    ]):
        directory = tmp_path / f"run-{index}"
        directory.mkdir()
        settings = {**runner.SETTINGS, "model": model, "base_url": endpoint}
        (directory / "run_config.json").write_text(json.dumps({"settings": settings,
                                                "per_instance_cost_limit": budget, "runtime_protocol": runner.RUNTIME_PROTOCOL}))
        (directory / "results.json").write_text(json.dumps([{"id": str(index), "exit_status": "submitted"}]))
    legacy = tmp_path / "run-legacy"
    legacy.mkdir()
    (legacy / "results.json").write_text('[{"id":"legacy","exit_status":"submitted"}]')
    assert runner.prior_completed_ids("run", .8) == {"1"}
    assert runner.prior_completed_ids("run", .1) == {"3"}


def test_gemini_private_credential_file(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.delenv("TEAMOROUTER_API_KEY", raising=False)
    path = runner.credential_path()
    path.parent.mkdir()
    path.write_text("synthetic-gemini-credential")
    path.chmod(0o600)
    assert runner.check_credential()
    path.chmod(0o644)
    assert not runner.check_credential()
