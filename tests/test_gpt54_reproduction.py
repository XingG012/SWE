"""Offline GPT-5.4 routing, request compatibility and budget checks."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sweagent.agent.agents import Agent, AgentArguments
from sweagent.agent.gpt54 import GPT54Model
from sweagent.agent.models import APIStats, CostLimitExceededError, ModelArguments, get_model
from sweagent.utils.secrets import credential_free_environment
import enigma_reproduction.run_nyu as runner


@pytest.fixture(autouse=True)
def gpt_runner_settings(monkeypatch):
    monkeypatch.setattr(runner, "SETTINGS", json.loads(Path("enigma_reproduction/gpt54_settings.json").read_text()))


@pytest.fixture
def gpt(monkeypatch, tmp_path):
    monkeypatch.setenv("TEAMOROUTER_API_KEY", "synthetic-gpt54-credential")
    monkeypatch.setenv("ENIGMA_USAGE_LEDGER", str(tmp_path / "usage.jsonl"))
    factory = Mock(return_value=Mock())
    monkeypatch.setattr("sweagent.agent.gpt54.OpenAI", factory)
    model = get_model(ModelArguments(model_name="gpt-5.4", per_instance_cost_limit=.8,
                                    total_cost_limit=2.4, temperature=0, top_p=.95))
    assert isinstance(model, GPT54Model)
    assert factory.call_args.kwargs["base_url"] == "https://api.teamorouter.com/v1"
    assert factory.call_args.kwargs["api_key"] == "synthetic-gpt54-credential"
    assert factory.call_args.kwargs["max_retries"] == 0
    return model


def response():
    return SimpleNamespace(model="gpt-5.4", usage=SimpleNamespace(
        prompt_tokens=1000, completion_tokens=100,
        prompt_tokens_details=SimpleNamespace(cached_tokens=200),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=30)),
        choices=[SimpleNamespace(finish_reason="stop",
                 message=SimpleNamespace(content="DISCUSSION\nInspect files\n```\nls\n```"))])


def test_gpt_request_and_cache_reasoning_accounting(gpt, tmp_path):
    gpt.client.chat.completions.create.return_value = response()
    gpt.query([{"role": "system", "content": "EnIGMA"},
               {"role": "user", "content": "challenge", "agent": "primary"}])
    kwargs = gpt.client.chat.completions.create.call_args.kwargs
    assert kwargs == {"model": "gpt-5.4", "messages": [
        {"role": "system", "content": "EnIGMA"}, {"role": "user", "content": "challenge"}],
        "max_completion_tokens": 4096, "reasoning_effort": "none", "temperature": 0, "top_p": .95}
    expected = (200*.25 + 800*2.5 + 100*15)/1e6
    assert gpt.stats.instance_cost == pytest.approx(expected)
    assert gpt.stats.tokens_received == 100  # Already includes reasoning usage.
    row = json.loads((tmp_path / "usage.jsonl").read_text())
    assert row["model"] == "gpt-5.4" and row["response_model"] == "gpt-5.4"
    assert row["pricing_accounting"] == "official_rate_reference_estimate"


def test_reasoning_mode_omits_incompatible_sampling(gpt):
    gpt.settings["reasoning_effort"] = "medium"
    gpt._request([])
    kwargs = gpt.client.chat.completions.create.call_args.kwargs
    assert kwargs["reasoning_effort"] == "medium"
    assert "temperature" not in kwargs and "top_p" not in kwargs


def test_long_context_rates_apply_to_full_session(gpt):
    regular = gpt._costs(272000, 100, 200)["peak"]
    assert regular == pytest.approx((200*.25 + 271800*2.5 + 100*15)/1e6)
    long = gpt._costs(272001, 100, 200)["peak"]
    assert long == pytest.approx((200*.5 + 271801*5 + 100*22.5)/1e6)
    assert gpt._costs(1000, 100, 200)["peak"] == pytest.approx((200*.5 + 800*5 + 100*22.5)/1e6)


def test_summary_model_propagation_and_shared_budget(gpt, monkeypatch):
    models = []
    def fake_model(args, *unused):
        assert args.model_name == "gpt-5.4"
        model = Mock(strict_accounting=True)
        models.append(model)
        return model
    monkeypatch.setattr("sweagent.agent.agents.get_model", fake_model)
    agent = Agent("primary", AgentArguments(model=gpt.args,
                  config_file=Path("config/ctf/ctf_crypto.yaml")))
    assert len(models) == 2 and agent.summarizer_model.budget_source is agent.model
    gpt.budget_source = SimpleNamespace(stats=APIStats(instance_cost=.7999, total_cost=.7999))
    gpt.client.chat.completions.create.return_value = response()
    with pytest.raises(CostLimitExceededError):
        gpt.query([])
    with pytest.raises(CostLimitExceededError):
        gpt.query([])
    assert gpt.stats.api_calls == 1


def test_gpt_credentials_and_api_error_are_redacted(gpt, tmp_path):
    assert "TEAMOROUTER_API_KEY" not in credential_free_environment()
    with pytest.raises(RuntimeError, match="request blocked"):
        gpt.query([{"role": "user", "content": "synthetic-gpt54-credential"}])
    gpt.client.chat.completions.create.assert_not_called()
    gpt.client.chat.completions.create.side_effect = ValueError("synthetic-gpt54-credential")
    with pytest.raises(RuntimeError) as error:
        gpt.query([])
    assert "synthetic-gpt54-credential" not in str(error.value)
    assert "synthetic-gpt54-credential" not in (tmp_path / "usage.jsonl").read_text()
    assert gpt.client.chat.completions.create.call_count == 1


def test_gpt_resume_is_separate_from_gemini_and_other_reasoning(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GENERATED", tmp_path)
    for index, (model, endpoint, effort) in enumerate([
        ("gemini-3.6-flash", "https://api.teamorouter.com/v1", None),
        ("gpt-5.4", "https://api.teamorouter.com/v1", "none"),
        ("gpt-5.4", "https://other.example/v1", "none"),
        ("gpt-5.4", "https://api.teamorouter.com/v1", "medium"),
    ]):
        directory = tmp_path / f"run-{index}"
        directory.mkdir()
        settings = {**runner.SETTINGS, "model": model, "base_url": endpoint, "reasoning_effort": effort}
        (directory / "run_config.json").write_text(json.dumps({"settings": settings,
                                                "per_instance_cost_limit": .8, "runtime_protocol": runner.RUNTIME_PROTOCOL}))
        (directory / "results.json").write_text(json.dumps([{"id": str(index), "exit_status": "submitted"}]))
    assert runner.prior_completed_ids("run", .8) == {"1"}
