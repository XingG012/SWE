"""Offline Sonnet 5 compatibility, shared budgets and cache accounting."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from openai.types.chat import ChatCompletion

import pytest

from sweagent.agent.agents import Agent, AgentArguments
from sweagent.agent.claude_sonnet5 import ClaudeSonnet5Model
from sweagent.agent.models import APIStats, CostLimitExceededError, ModelArguments, get_model
import enigma_reproduction.run_nyu as runner


@pytest.fixture(autouse=True)
def sonnet_runner_settings(monkeypatch):
    settings = json.loads(Path("enigma_reproduction/claude_settings.json").read_text())
    monkeypatch.setattr(runner, "SETTINGS", settings)
    monkeypatch.setattr(runner, "EXPERIMENT", settings["experiment"])


@pytest.fixture
def sonnet(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_SONNET5_API_KEY", "synthetic-sonnet5-credential")
    monkeypatch.setenv("X1API_API_KEY", "different-relay-credential")
    monkeypatch.setenv("ENIGMA_MODEL_SETTINGS", str(Path("enigma_reproduction/claude_settings.json").resolve()))
    monkeypatch.setenv("ENIGMA_USAGE_LEDGER", str(tmp_path / "usage.jsonl"))
    factory = Mock(return_value=Mock())
    monkeypatch.setattr("sweagent.agent.claude_sonnet5.OpenAI", factory)
    model = get_model(ModelArguments(model_name="claude-sonnet-5", per_instance_cost_limit=.8,
                                    total_cost_limit=2.4, temperature=0, top_p=.95))
    assert isinstance(model, ClaudeSonnet5Model)
    assert factory.call_args.kwargs["base_url"] == "https://x1api.top/v1"
    assert factory.call_args.kwargs["api_key"] == "synthetic-sonnet5-credential"
    assert factory.call_args.kwargs["max_retries"] == 0
    return model


def response(write=0):
    return SimpleNamespace(model="claude-sonnet-5", usage=SimpleNamespace(
        prompt_tokens=1000, completion_tokens=100, cache_creation_input_tokens=write,
        prompt_tokens_details=SimpleNamespace(cached_tokens=200),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=30)),
        choices=[SimpleNamespace(finish_reason="stop",
                 message=SimpleNamespace(content="DISCUSSION\nInspect files\n```\nls\n```"))])


def test_sonnet_request_omits_unsupported_sampling(sonnet, tmp_path):
    sonnet.client.chat.completions.create.return_value = response()
    sonnet.query([{"role": "system", "content": "EnIGMA"},
                  {"role": "user", "content": "challenge", "agent": "primary"}])
    assert sonnet.client.chat.completions.create.call_args.kwargs == {
        "model": "claude-sonnet-5", "max_tokens": 4096,
        "messages": [{"role": "system", "content": "EnIGMA"},
                     {"role": "user", "content": "challenge"}]}
    assert sonnet.stats.instance_cost == pytest.approx((200*.2 + 800*2 + 100*10)/1e6)
    assert sonnet.stats.tokens_received == 100
    row = json.loads((tmp_path / "usage.jsonl").read_text())
    assert row["response_model"] == "claude-sonnet-5" and row["sampling_mode"] == "provider_default"


def test_reported_cache_write_cost_is_included(sonnet, tmp_path):
    sonnet.client.chat.completions.create.return_value = response(write=100)
    sonnet.query([])
    row = json.loads((tmp_path / "usage.jsonl").read_text())
    assert row["cache_write_tokens"] == 100
    assert row["cost_upper_usd"] == pytest.approx((200*.2 + 700*2 + 100*4 + 100*10)/1e6)
    assert row["cost_lower_usd"] == pytest.approx((200*.2 + 700*2 + 100*2.5 + 100*10)/1e6)


def test_real_x1api_null_cached_tokens_are_charged_and_answer_is_kept(sonnet, tmp_path):
    # Captured numeric usage from a fresh-prefix synthetic request, not a task.
    reply = ChatCompletion.model_validate({"id": "synthetic", "created": 0,
        "object": "chat.completion", "model": "claude-sonnet-5",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "```\nls\n```"}}],
        "usage": {"completion_tokens": 2, "prompt_tokens": 8930, "total_tokens": 8932,
                  "completion_tokens_details": None,
                  "prompt_tokens_details": {"audio_tokens": None, "cached_tokens": None,
                                            "cache_creation_tokens": 8930}}})
    sonnet.client.chat.completions.create.return_value = reply
    assert sonnet.query([]) == "```\nls\n```"
    row = json.loads((tmp_path / "usage.jsonl").read_text())
    assert row["raw_usage"]["cached_tokens"] is None
    assert row["cache_hit_tokens_reported"] is False and row["request_succeeded"]
    assert row["cost_upper_usd"] == pytest.approx((8930*4 + 2*10)/1e6)
    assert row["cost_lower_usd"] == pytest.approx((8930*2.5 + 2*10)/1e6)
    assert sonnet.stats.instance_cost == pytest.approx(row["cost_upper_usd"])


def test_unknown_cache_hit_discount_is_bounded_not_reported_as_free(sonnet, tmp_path):
    reply = response(write=300)
    reply.usage.prompt_tokens_details.cached_tokens = None
    sonnet.client.chat.completions.create.return_value = reply
    sonnet.query([])
    row = json.loads((tmp_path / "usage.jsonl").read_text())
    assert row["cost_upper_usd"] == pytest.approx((700*2 + 300*4 + 100*10)/1e6)
    assert row["cost_lower_usd"] == pytest.approx((700*.2 + 300*2.5 + 100*10)/1e6)


@pytest.mark.parametrize("hit", [-1, 1001, True, "invalid"])
def test_malformed_cache_hit_still_stops_with_diagnostic_values(sonnet, tmp_path, hit):
    reply = response()
    reply.usage.prompt_tokens_details.cached_tokens = hit
    sonnet.client.chat.completions.create.return_value = reply
    with pytest.raises(RuntimeError, match="cache usage invalid"):
        sonnet.query([])
    row = json.loads((tmp_path / "usage.jsonl").read_text())
    assert row["raw_usage"]["cached_tokens"] == hit
    assert not row["accounting_complete"] and row["request_succeeded"]


def test_invalid_usage_diagnostics_redact_credentials(sonnet, tmp_path):
    reply = response()
    reply.usage.prompt_tokens_details.cached_tokens = "synthetic-sonnet5-credential"
    reply.model = "synthetic-sonnet5-credential"
    sonnet.client.chat.completions.create.return_value = reply
    with pytest.raises(RuntimeError, match="cache usage invalid"):
        sonnet.query([])
    text = (tmp_path / "usage.jsonl").read_text()
    assert "synthetic-sonnet5-credential" not in text
    assert json.loads(text)["raw_usage"]["cached_tokens"] == "[REDACTED]"


@pytest.mark.parametrize("bad", [-1, 801, None, True])
def test_invalid_cache_writes_stop_with_unknown_cost(sonnet, tmp_path, bad):
    r = response(write=bad)
    if bad is None:
        r.usage.prompt_tokens_details.cache_creation_tokens = None
    sonnet.client.chat.completions.create.return_value = r
    with pytest.raises(RuntimeError, match="cache write usage"):
        sonnet.query([])
    row = json.loads((tmp_path / "usage.jsonl").read_text())
    assert row["accounting_complete"] is False and row["cost_upper_usd"] is None


def test_summary_propagation_and_shared_budget(sonnet, monkeypatch):
    models = []
    def fake_model(args, *unused):
        assert args.model_name == "claude-sonnet-5"
        model = Mock(strict_accounting=True)
        models.append(model)
        return model
    monkeypatch.setattr("sweagent.agent.agents.get_model", fake_model)
    agent = Agent("primary", AgentArguments(model=sonnet.args,
                  config_file=Path("config/ctf/ctf_web.yaml")))
    assert len(models) == 2 and agent.summarizer_model.budget_source is agent.model
    sonnet.budget_source = SimpleNamespace(stats=APIStats(instance_cost=.7999, total_cost=.7999))
    sonnet.client.chat.completions.create.return_value = response()
    with pytest.raises(CostLimitExceededError):
        sonnet.query([])
    with pytest.raises(CostLimitExceededError):
        sonnet.query([])
    assert sonnet.stats.api_calls == 1


def test_secret_input_and_api_failure_remain_redacted(sonnet, tmp_path):
    with pytest.raises(RuntimeError, match="request blocked"):
        sonnet.query([{"role": "user", "content": "synthetic-sonnet5-credential"}])
    sonnet.client.chat.completions.create.assert_not_called()
    sonnet.client.chat.completions.create.side_effect = ValueError("synthetic-sonnet5-credential")
    with pytest.raises(RuntimeError) as error:
        sonnet.query([])
    assert "synthetic-sonnet5-credential" not in str(error.value)
    assert "synthetic-sonnet5-credential" not in (tmp_path / "usage.jsonl").read_text()


def test_resume_only_uses_current_sonnet_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GENERATED", tmp_path)
    for index, model in enumerate(["gpt-5.4", "gemini-3.6-flash", "claude-sonnet-5"]):
        directory = tmp_path / f"run-{index}"
        directory.mkdir()
        (directory / "run_config.json").write_text(json.dumps({"settings": {**runner.SETTINGS, "model": model},
                                                "experiment": runner.EXPERIMENT,
                                                "per_instance_cost_limit": .8, "runtime_protocol": runner.RUNTIME_PROTOCOL}))
        (directory / "results.json").write_text(json.dumps([{"id": str(index), "exit_status": "submitted"}]))
    assert runner.prior_completed_ids("run", .8) == {"2"}


def test_sonnet_selection_starts_with_all_82_and_resumes_only_this_experiment(tmp_path, monkeypatch):
    records, _ = runner.dataset()
    selection = runner.task_selection(records)
    baseline = json.loads(Path("enigma_reproduction/qwen3_max_deepseek82_tasks.json").read_text())
    assert selection["selected_ids"] == baseline["selected_ids"]
    assert len(selection["selected_ids"]) == 82
    assert selection["already_solved_ids"] == selection["already_failed_ids"] == []
    monkeypatch.setattr(runner, "GENERATED", tmp_path)
    for index, (experiment, budget, status) in enumerate([
            (runner.EXPERIMENT, .8, "submitted"), ("default", .8, "submitted"),
            (runner.EXPERIMENT, 1.4, "submitted"), (runner.EXPERIMENT, .8, "interrupted"),
            (runner.EXPERIMENT, .8, "exit_cost"), (runner.EXPERIMENT, .8, "infrastructure_error")]):
        directory = tmp_path / f"run-{index}"
        directory.mkdir()
        (directory / "run_config.json").write_text(json.dumps({"settings": runner.SETTINGS,
            "experiment": experiment, "per_instance_cost_limit": budget,
            "runtime_protocol": runner.RUNTIME_PROTOCOL}))
        (directory / "results.json").write_text(json.dumps([{"id": str(index), "exit_status": status}]))
    assert runner.prior_completed_ids("run", .8) == {"0", "4"}
