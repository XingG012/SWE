"""Qwen streaming, accounting, cohort and resume regression coverage."""
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

import enigma_reproduction.run_nyu as runner
from sweagent.agent.agents import Agent, AgentArguments
from sweagent.agent.models import APIStats, CostLimitExceededError, ModelArguments, get_model
from sweagent.agent.qwen3_max import Qwen3MaxModel
from sweagent.utils.secrets import credential_free_environment


@pytest.fixture
def qwen(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-qwen-credential")
    monkeypatch.setenv("ENIGMA_MODEL_SETTINGS", str(Path("enigma_reproduction/qwen3_max_settings.json").resolve()))
    monkeypatch.setenv("ENIGMA_USAGE_LEDGER", str(tmp_path / "usage.jsonl"))
    factory = Mock(return_value=Mock())
    monkeypatch.setattr("sweagent.agent.qwen3_max.OpenAI", factory)
    model = get_model(ModelArguments(model_name="qwen3-max", per_instance_cost_limit=.1,
                                    total_cost_limit=.3, temperature=0, top_p=.95))
    assert isinstance(model, Qwen3MaxModel)
    assert factory.call_args.kwargs["base_url"] == model.settings["base_url"]
    assert factory.call_args.kwargs["api_key"] == "synthetic-qwen-credential"
    assert factory.call_args.kwargs["max_retries"] == 0
    return model


def streamed_response(*, usage=True):
    chunks = [NS(model="qwen3-max", usage=None, choices=[NS(index=0, finish_reason=None,
              delta=NS(content=None, reasoning_content="reasoning only"))]),
              NS(model="qwen3-max", usage=None, choices=[NS(index=0, finish_reason=None,
              delta=NS(content="DISCUSSION\nInspect files\n```\n", reasoning_content=None))]),
              NS(model="qwen3-max", usage=None, choices=[NS(index=0, finish_reason="stop",
              delta=NS(content="ls\n```", reasoning_content=None))])]
    if usage:
        chunks.append(NS(model="qwen3-max", choices=[], usage=NS(
            prompt_tokens=1000, completion_tokens=100,
            prompt_tokens_details=NS(cached_tokens=200),
            completion_tokens_details=NS(reasoning_tokens=30))))
    stream = Mock()
    stream.__iter__ = Mock(return_value=iter(chunks))
    return stream


def test_stream_usage_and_reasoning_accounting(qwen, tmp_path):
    stream = streamed_response()
    qwen.client.chat.completions.create.return_value = stream
    answer = qwen.query([{"role": "user", "content": "challenge", "agent": "primary"}])
    assert answer == "DISCUSSION\nInspect files\n```\nls\n```"
    assert qwen.client.chat.completions.create.call_args.kwargs == {
        "model": "qwen3-max", "messages": [{"role": "user", "content": "challenge"}],
        "temperature": 0, "top_p": .95, "max_tokens": 4096,
        "extra_body": {"enable_thinking": True}, "stream": True,
        "stream_options": {"include_usage": True}}
    stream.close.assert_called_once()
    assert qwen.stats.instance_cost == pytest.approx((1000*.359 + 100*1.434)/1e6)
    assert qwen.stats.tokens_received == 100  # reasoning already included
    row = json.loads((tmp_path / "usage.jsonl").read_text())
    assert row["reasoning_tokens"] == 30 and row["cache_hit_tokens"] == 200


@pytest.mark.parametrize("prompt,input_rate,output_rate", [
    (32768,.359,1.434), (32769,.574,2.294), (131072,.574,2.294),
    (131073,1.004,4.014), (262144,1.004,4.014), (1000,.359,1.434)])
def test_request_context_tiers_and_cache_reference_estimate(qwen, prompt, input_rate, output_rate):
    qwen._costs(200000, 100, 0)
    assert qwen._costs(prompt, 100, prompt // 2)["peak"] == pytest.approx(
        (prompt*input_rate + 100*output_rate)/1e6)


def test_missing_stream_usage_stops_and_does_not_return_answer(qwen, tmp_path):
    stream = streamed_response(usage=False)
    qwen.client.chat.completions.create.return_value = stream
    with pytest.raises(RuntimeError, match="no valid usage"):
        qwen.query([])
    stream.close.assert_called_once()
    assert json.loads((tmp_path / "usage.jsonl").read_text())["accounting_complete"] is False


def test_partial_stream_closes_and_records_unknown_billing(qwen, tmp_path):
    stream = Mock()
    def broken():
        yield NS(model="qwen3-max", usage=None, choices=[])
        raise ValueError("synthetic-qwen-credential")
    stream.__iter__ = Mock(return_value=broken())
    qwen.client.chat.completions.create.return_value = stream
    with pytest.raises(RuntimeError) as error:
        qwen.query([])
    stream.close.assert_called_once()
    assert "synthetic-qwen-credential" not in str(error.value)
    assert json.loads((tmp_path / "usage.jsonl").read_text())["cost_unknown"] is True


def test_summary_uses_qwen_and_shared_budget(qwen, monkeypatch):
    models = []
    def fake_model(args, *unused):
        assert args.model_name == "qwen3-max"
        model = Mock(strict_accounting=True)
        models.append(model)
        return model
    monkeypatch.setattr("sweagent.agent.agents.get_model", fake_model)
    agent = Agent("primary", AgentArguments(model=qwen.args,
                  config_file=Path("config/ctf/ctf_crypto.yaml")))
    assert len(models) == 2 and agent.summarizer_model.budget_source is agent.model
    qwen.budget_source = NS(stats=APIStats(instance_cost=.0999, total_cost=.0999))
    qwen.client.chat.completions.create.return_value = streamed_response()
    with pytest.raises(CostLimitExceededError):
        qwen.query([])
    with pytest.raises(CostLimitExceededError):
        qwen.query([])
    assert qwen.stats.api_calls == 1


def test_credential_blocked_in_prompt_and_compose(qwen):
    assert "DASHSCOPE_API_KEY" not in credential_free_environment()
    with pytest.raises(RuntimeError, match="request blocked"):
        qwen.query([{"role": "user", "content": "synthetic-qwen-credential"}])
    qwen.client.chat.completions.create.assert_not_called()


def test_resume_separates_qwen_thinking_and_other_models(tmp_path, monkeypatch):
    settings = json.loads(Path("enigma_reproduction/qwen3_max_settings.json").read_text())
    monkeypatch.setattr(runner, "SETTINGS", settings)
    monkeypatch.setattr(runner, "EXPERIMENT", settings["experiment"])
    monkeypatch.setattr(runner, "GENERATED", tmp_path)
    for index, changes in enumerate([{}, {"enable_thinking": False}, {"model": "gpt-5.4"},
                                     {"base_url": "https://other.invalid/v1"}]):
        directory = tmp_path / f"run-{index}"
        directory.mkdir()
        (directory / "run_config.json").write_text(json.dumps({"settings": {**settings, **changes},
            "experiment": runner.EXPERIMENT, "runtime_protocol": runner.RUNTIME_PROTOCOL,
            "per_instance_cost_limit": .1}))
        (directory / "results.json").write_text(json.dumps([
            {"id": f"{index}-done", "exit_status": "submitted"},
            {"id": f"{index}-failed", "exit_status": "exit_cost"},
            {"id": f"{index}-interrupted", "exit_status": "interrupted"}]))
    assert runner.prior_completed_ids("run", .1) == {"0-done", "0-failed"}


def test_qwen_cohort_is_exactly_82_baseline_successes_without_gpt_outcomes(monkeypatch):
    settings = json.loads(Path("enigma_reproduction/qwen3_max_settings.json").read_text())
    monkeypatch.setattr(runner, "SETTINGS", settings)
    records, _ = runner.dataset()
    cohort = runner.task_selection(records)
    baseline = json.loads(Path("enigma_reproduction/gpt54_deepseek82_tasks.json").read_text())
    assert cohort["selected_ids"] == baseline["selected_ids"]
    assert len(cohort["selected_ids"]) == 82
    assert cohort["already_solved_ids"] == cohort["already_failed_ids"] == []
    assert set(settings["pilot_ids"]).issubset(cohort["selected_ids"])
    for task in cohort["tasks"]:
        evidence = task["deepseek_success_evidence"]
        assert runner.sha(Path(evidence["log_file"])) == evidence["log_sha256"]
        rows = json.loads(Path(evidence["result_file"]).read_text())
        assert any(row["id"] == task["id"] and row["solved"] for row in rows)


def test_explicit_skip_can_move_past_interrupted_task(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GENERATED", tmp_path)
    monkeypatch.setattr(runner, "task_selection", lambda records: {
        "selected_ids": ["interrupted", "done", "next"], "already_solved_ids": [], "already_failed_ids": []})
    monkeypatch.setattr(runner, "prior_completed_ids", lambda *unused: {"done"})
    args = NS(pilot=False, ids=None, skip_ids=["interrupted"], from_scratch=False,
              per_instance=.1, total=8.2, dry_run=True, num_workers=3)
    records = [{"id": identifier, "category": "rev"} for identifier in ("interrupted", "done", "next")]
    assert runner.run_selected(records, args) == 0
    preview = json.loads((tmp_path / "selection-preview.json").read_text())
    assert preview["pending_ids"] == ["next"] and preview["excluded_ids"] == ["interrupted"]
    assert preview["skipped_ids"] == ["done"]
    args.skip_ids = ["unknown"]
    with pytest.raises(ValueError, match="Skipped IDs"):
        runner.run_selected(records, args)
