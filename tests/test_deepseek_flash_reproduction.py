"""Offline checks only: no API requests and no Docker solver execution."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sweagent.agent.agents import Agent, AgentArguments
from sweagent.agent.deepseek_flash import DeepSeekFlashModel
from sweagent.agent.models import APIStats, CostLimitExceededError, ModelArguments, get_model
from sweagent.agent.summarizer import LMSummarizer
from sweagent.utils.secrets import SecretFilter
from sweagent.utils.secrets import credential_free_environment
from enigma_reproduction.metrics import StepMetrics
from enigma_reproduction.task_log import TaskLog
from enigma_reproduction.run_nyu import usage_summary
from sweagent.utils.log import add_file_handler, get_logger, remove_file_handlers
from sweagent.environment.utils import image_exists


@pytest.fixture
def flash(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-credential")
    monkeypatch.setenv("ENIGMA_USAGE_LEDGER", str(tmp_path / "usage.jsonl"))
    client = Mock()
    monkeypatch.setattr("sweagent.agent.deepseek_flash.OpenAI", Mock(return_value=client))
    model = get_model(ModelArguments(model_name="deepseek-flash", per_instance_cost_limit=3,
                                   total_cost_limit=9, temperature=0, top_p=.95))
    assert isinstance(model, DeepSeekFlashModel)
    return model


def response(prompt=1000, completion=100, hit=200, content="DISCUSSION\nCheck files\n```\nls\n```"):
    return SimpleNamespace(model="deepseek-flash", usage=SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion,
        prompt_cache_hit_tokens=hit, prompt_cache_miss_tokens=prompt-hit),
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")])


def test_usage_cache_and_interface(flash):
    flash.client.chat.completions.create.return_value = response()
    flash.query([{"role": "user", "content": "challenge", "agent": "primary", "is_demo": True}])
    cost = (200*.006 + 800*.3 + 100*1.2)/1e6
    assert flash.stats.instance_cost == pytest.approx(cost)
    assert flash.stats.api_calls == 1
    kwargs = flash.client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "deepseek-flash"
    assert kwargs["messages"] == [{"role": "user", "content": "challenge"}]
    assert kwargs["temperature"] == 0 and kwargs["top_p"] == .95
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert kwargs["max_tokens"] == 4096
    assert "tools" not in kwargs


@pytest.mark.parametrize("usage", [None, SimpleNamespace(prompt_tokens=-1, completion_tokens=1),
                                SimpleNamespace(prompt_tokens=0, completion_tokens=0)])
def test_missing_usage_fails_closed(flash, usage, monkeypatch):
    r = response(); r.usage = usage
    flash.client.chat.completions.create.return_value = r
    with pytest.raises(RuntimeError, match="valid usage"):
        flash.query([])
    ledger = json.loads(__import__("pathlib").Path(__import__("os").environ["ENIGMA_USAGE_LEDGER"]).read_text())
    assert ledger["accounting_complete"] is False
    assert ledger["cost_upper_usd"] is None


def test_bad_cache_usage_fails_closed(flash):
    r = response(); r.usage.prompt_cache_hit_tokens = 1001
    flash.client.chat.completions.create.return_value = r
    with pytest.raises(RuntimeError, match="cache usage"):
        flash.query([])


def test_empty_output_charges_before_error(flash):
    flash.client.chat.completions.create.return_value = response(content="")
    with pytest.raises(RuntimeError, match="usage charged"):
        flash.query([])
    assert flash.stats.instance_cost > 0 and flash.stats.api_calls == 1


def test_credentials_not_sent_or_returned(flash):
    with pytest.raises(RuntimeError, match="request blocked"):
        flash.query([{"role": "user", "content": "synthetic-test-credential"}])
    flash.client.chat.completions.create.assert_not_called()
    flash.client.chat.completions.create.return_value = response(content="synthetic-test-credential")
    assert flash.query([]) == "[REDACTED]"


def test_api_errors_no_secret_and_no_retry(flash):
    flash.client.chat.completions.create.side_effect = ValueError("synthetic-test-credential")
    with pytest.raises(RuntimeError) as error:
        flash.query([])
    assert "synthetic-test-credential" not in str(error.value)
    assert flash.client.chat.completions.create.call_count == 1


def test_combined_budget_before_and_after_summary(flash):
    primary = SimpleNamespace(stats=APIStats(instance_cost=2.9999, total_cost=2.9999))
    flash.budget_source = primary
    flash.accounting_role = "summarizer"
    flash.client.chat.completions.create.return_value = response()
    with pytest.raises(CostLimitExceededError):
        flash.query([])
    assert flash.stats.instance_cost > 0  # The limit-crossing response is still accounted.
    with pytest.raises(CostLimitExceededError):
        flash.query([])
    assert flash.client.chat.completions.create.call_count == 1


def test_log_redaction_handles_tracebacks(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-sensitive-value")
    try:
        raise ValueError("synthetic-sensitive-value")
    except ValueError:
        import sys
        record = logging.LogRecord("test", logging.ERROR, "", 1,
                                   "Authorization: Bearer %s", ("synthetic-sensitive-value",), sys.exc_info())
    assert SecretFilter().filter(record)
    assert "synthetic-sensitive-value" not in record.getMessage()
    assert "Bearer" not in record.getMessage()
    assert record.exc_info is None


def test_summary_success_and_failure_leave_single_system_message(flash, monkeypatch):
    summary = LMSummarizer(105)
    summary.history = [{"role": "system", "content": "Summarize"}]
    summary.instance_template = "{command}\n{observation}"
    summary.instance_args = {}; summary.system_args = {}; summary.name = "ctf_summarizer"
    monkeypatch.setattr(summary, "_upload_file_to_container", Mock())
    flash.client.chat.completions.create.return_value = response(content="summary")
    text, stats = summary("ls", "line\n"*106, Mock(), flash)
    assert "SUMMARY" in text and stats.api_calls == 1
    assert flash.stats.api_calls == 0 and len(summary.history) == 1
    flash.client.chat.completions.create.return_value = response(content="")
    with pytest.raises(RuntimeError):
        summary("ls", "line\n"*106, Mock(), flash)
    assert flash.stats.api_calls == 1 and len(summary.history) == 1


def test_agent_keeps_summary_failure_cost_and_stops(flash, monkeypatch):
    args = AgentArguments(model=flash.args, config_file="config/ctf/ctf_crypto.yaml")
    monkeypatch.setattr("sweagent.agent.agents.get_model", Mock(side_effect=[flash, flash]))
    agent = Agent("primary", args)
    agent._env = Mock()
    agent._env.step.side_effect = [("output", 0, False, {}), ("stopped", 0, True, {"exit_status": "exit_cost"})]
    # Separate stats objects, as in the real main and summary models.
    summary = Mock(strict_accounting=True, stats=APIStats(instance_cost=.4, total_cost=.4, api_calls=1))
    agent.summarizer_model = summary
    object.__setattr__(agent.config.summarizer_config, "function", Mock(side_effect=CostLimitExceededError("limit")))
    agent.info = {}
    _, done = agent._run_sub_action({"agent": "primary", "cmd_name": "ls", "action": "ls"})
    assert done and agent.info["exit_status"] == "exit_cost"
    assert agent.model.stats.instance_cost == pytest.approx(.4)
    assert agent.info["summarizer"]["model_stats"]["instance_cost"] == pytest.approx(.4)
    summary.reset_stats.assert_called_once()


def test_compose_environment_drops_credentials(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-credential")
    monkeypatch.setenv("ENIGMA_NON_SECRET_SETTING", "also-sensitive-by-name")
    monkeypatch.setenv("CTF_TEST_PUBLIC_PORT", "80")
    clean = credential_free_environment()
    assert "DEEPSEEK_API_KEY" not in clean
    assert "ENIGMA_NON_SECRET_SETTING" not in clean
    assert clean["CTF_TEST_PUBLIC_PORT"] == "80"


def test_step_metrics_are_passive_and_include_summary_cost(tmp_path):
    metrics = StepMetrics(tmp_path / "steps.jsonl")
    step = {"action": "debug_exec disassemble", "observation": "one\ntwo", "response": "text",
            "execution_time": .2}
    stats = APIStats(instance_cost=.03, total_cost=.03, tokens_sent=50, tokens_received=5, api_calls=2)
    metrics.on_step_start()
    metrics.on_step_done(trajectory_step=step, model_stats=stats)
    metrics.on_step_start()
    metrics.on_step_done(trajectory_step=step, model_stats=stats)
    rows = [json.loads(line) for line in metrics.path.read_text().splitlines()]
    assert rows[0]["api_calls"] == 2 and rows[0]["cost_upper_usd"] == .03
    assert rows[1]["api_calls"] == 0 and rows[1]["cost_upper_usd"] == 0
    assert rows[0]["observation_lines"] == 2 and rows[0]["tool"] == "debug_exec"
    assert step["response"] == "text" and stats.api_calls == 2


def test_unknown_cost_is_not_reported_as_zero(tmp_path):
    path = tmp_path / "usage.jsonl"
    path.write_text(json.dumps({"role": "primary", "accounting_complete": False,
                               "cost_upper_usd": None}) + "\n")
    stats = usage_summary(path)
    assert stats["accounting_complete"] is False
    assert stats["cost_upper_usd"] is None and stats["cost_lower_usd"] is None


def test_batch_logs_do_not_contaminate_previous_task(tmp_path):
    logger = get_logger("reproduction-test-log")
    first, second = tmp_path / "first.log", tmp_path / "second.log"
    add_file_handler(first)
    logger.warning("first-task-message")
    remove_file_handlers()
    add_file_handler(second)
    logger.warning("second-task-message")
    remove_file_handlers()
    assert "second-task-message" not in first.read_text()
    assert "first-task-message" not in second.read_text()


def test_immutable_docker_image_is_recognized(monkeypatch):
    client = Mock()
    client.images.list.return_value = []
    client.images.get.return_value.attrs = {"RepoTags": [], "Created": "test", "Os": "linux", "Architecture": "amd64"}
    monkeypatch.setattr("sweagent.environment.utils.docker.from_env", Mock(return_value=client))
    assert image_exists("sha256:" + "a"*64)
    client.images.get.assert_called_once_with("sha256:" + "a"*64)


def test_task_log_final_metrics_precede_process_and_keep_private_names(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-log-credential")
    task = TaskLog(tmp_path / "logs", model="deepseek-flash",
                   name="../中文题目/" + "长" * 100, identifier="nyu-test-id", category="crypto", budget=.4)
    assert task.path.parent == tmp_path / "logs/deepseek-flash"
    assert len(task.path.name.encode("utf-8")) <= 255
    assert task.path.stat().st_mode & 0o777 == 0o600
    assert '"exit_status": "running"' in task.path.read_text()
    logger = get_logger("reproduction-task-log-test")
    add_file_handler(task.path)
    # A large Unicode body exercises streamed copying and byte offsets.
    logger.info("解题输出 %s %s", "输出" * 10000, "synthetic-log-credential")
    remove_file_handlers()
    body = task.path.read_bytes()[task.process_offset:]
    task.finalize({"exit_status": "submitted", "solved": True, "steps": 13,
                   "cost_upper_usd": .02, "cost_lower_usd": .01, "accounting_complete": True})
    final = task.path.read_text()
    header, process = final.split("=== 解题过程 ===\n", 1)
    assert '"exit_status": "submitted"' in header and '"cost_upper_usd": 0.02' in header
    assert '"per_instance_cost_limit_usd": 0.4' in header
    assert process.encode("utf-8") == body
    assert "synthetic-log-credential" not in final and "[REDACTED]" in process
    assert len(list(task.path.parent.iterdir())) == 1


@pytest.mark.parametrize("label", ["run", "pilot"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_formal_and_pilot_share_single_log_layout_and_budget(tmp_path, monkeypatch, label, interrupted):
    import os
    import docker
    import run
    import enigma_reproduction.run_nyu as runner

    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "inventory.json").write_text("{}")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "GENERATED", generated)
    monkeypatch.setattr(runner, "preflight", lambda image: {"ready": True, "resolved_image": "sha256:test"})
    monkeypatch.setenv(runner.credential_env(), "synthetic-log-credential")
    monkeypatch.setenv("ENIGMA_USAGE_LEDGER", "unused-test-ledger")
    client = Mock()
    client.containers.list.return_value = []
    client.networks.list.return_value = []
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))
    (tmp_path / "challenge.json").write_text("{}")
    # Real official category config/arguments are loaded; only Docker and Main are replaced.
    records = [{"id": "nyu-test-id", "name": "Example Challenge", "category": "crypto",
                "path": str(tmp_path), "missing_attachments": [],
                "config": str(Path("config/ctf/ctf_crypto.yaml").resolve())}]
    used_budgets = []

    class OfflineMain:
        def __init__(self, args, *, log_path):
            used_budgets.append(args.agent.model.per_instance_cost_limit)
            add_file_handler(log_path)
            self.agent = SimpleNamespace(info={"exit_status": "submitted"},
                model=Mock(), summarizer_model=Mock(), add_hook=lambda hook: setattr(self, "hook", hook))
            self.env = Mock()
            self.traj_dir = tmp_path / "trajectory"

        def run(self, index):
            self.hook.on_step_start()
            self.hook.on_step_done(trajectory_step={"response": "DISCUSSION\nSolve\n```\nsubmit flag\n```",
                "action": "submit flag", "observation": "Accepted", "execution_time": .1},
                model_stats=APIStats(instance_cost=.02, total_cost=.02, api_calls=1,
                                     tokens_sent=100, tokens_received=5))
            Path(os.environ["ENIGMA_USAGE_LEDGER"]).write_text(json.dumps({"role": "primary",
                "accounting_complete": True, "prompt_tokens": 100, "completion_tokens": 5,
                "cache_hit_tokens": 20, "cost_upper_usd": .02, "cost_lower_usd": .01}) + "\n")
            if interrupted:
                raise KeyboardInterrupt

    monkeypatch.setattr(run, "Main", OfflineMain)
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            runner.pilot(records, "unused-test-image", .4, .8, ["nyu-test-id"], label=label)
    else:
        assert runner.pilot(records, "unused-test-image", .4, .8, ["nyu-test-id"], label=label) == 0
    logs = list((tmp_path / "logs" / runner.SETTINGS["model"]).glob("*.log"))
    assert len(logs) == 1 and logs[0].name.startswith("Example_Challenge_")
    content = logs[0].read_text()
    header, process = content.split("=== 解题过程 ===\n", 1)
    if interrupted:
        assert '"exit_status": "interrupted"' in header
        assert '"known_cost_upper_usd": 0.02' in header
        result = json.loads(next(generated.glob(f"{label}-*/results.json")).read_text())[0]
        assert result["exit_status"] == "interrupted" and result["known_cost_upper_usd"] == .02
        assert runner.prior_completed_ids(label, .4) == set()
        return
    assert '"cost_upper_usd": 0.02' in header
    assert '"solved": true' in header
    assert "Accepted" in process and "submit flag" in process and "STEP 1" in process
    assert used_budgets == [.4]
    result = json.loads(next(generated.glob(f"{label}-*/results.json")).read_text())[0]
    assert result["log_file"] == str(logs[0]) and result["api_calls"] == 1


def test_main_explicit_log_path_avoids_extra_trajectory_log(tmp_path, monkeypatch):
    import run
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run, "Agent", Mock())
    monkeypatch.setattr(run, "SWEEnv", Mock())
    monkeypatch.setattr(run.Main, "_save_arguments", Mock())
    path = tmp_path / "Example_20261008_120000.log"
    try:
        main = run.Main(SimpleNamespace(run_name="offline-test", print_config=False,
                                       agent=Mock(), environment=Mock(),
                                       actions=run.ActionsArguments()), log_path=path)
    finally:
        remove_file_handlers()
    assert path.is_file() and not list(main.traj_dir.glob("*.log"))


def test_repository_accepts_committed_patches_but_rejects_method_changes(tmp_path, monkeypatch):
    import subprocess
    import enigma_reproduction.run_nyu as runner

    def git(*args, check=True):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, check=check)

    git("init", "-q")
    git("config", "user.name", "Offline test")
    git("config", "user.email", "offline@example.invalid")
    method = tmp_path / "config/ctf/ctf_crypto.yaml"
    method.parent.mkdir(parents=True)
    method.write_text("official method")
    adapter = tmp_path / "sweagent/agent/models.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text("official models")
    git("add", ".")
    git("commit", "-qm", "baseline")
    base = git("rev-parse", "HEAD").stdout.decode().strip()
    adapter.write_text("documented model dispatch patch")
    git("add", ".")
    git("commit", "-qm", "compatibility patch")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "SETTINGS", {"upstream_commit": base})
    status = runner.repository_status()
    assert status["v0_7_ancestor"] and status["original_method_files_unchanged"]
    assert status["runtime_commit"] != base
    method.write_text("changed prompt")
    assert not runner.repository_status()["original_method_files_unchanged"]
    git("add", ".")
    git("commit", "-qm", "method change")
    assert not runner.repository_status()["original_method_files_unchanged"]
    monkeypatch.setattr(runner, "SETTINGS", {"upstream_commit": "0" * 40})
    assert not runner.repository_status()["v0_7_ancestor"]


def test_resume_skips_completed_but_retries_infra_and_interrupted(tmp_path, monkeypatch):
    import enigma_reproduction.run_nyu as runner
    generated = tmp_path / "generated"
    (generated / "run-20261008T000000000000Z").mkdir(parents=True)
    (generated / "run-20261008T000000000000Z" / "results.json").write_text(json.dumps([
        {"id": "done-solved", "accounting_complete": True, "exit_status": "submitted"},
        {"id": "done-failed", "accounting_complete": True, "exit_status": "exit_format"},
        {"id": "retry-infra", "accounting_complete": True, "exit_status": "infrastructure_error"},
        {"id": "retry-unknown", "accounting_complete": False, "exit_status": "submitted"},
    ]))
    # A pilot batch must not leak into the formal run's completed set.
    (generated / "pilot-20261008T000000000000Z").mkdir(parents=True)
    (generated / "pilot-20261008T000000000000Z" / "results.json").write_text(json.dumps([
        {"id": "pilot-only", "accounting_complete": True, "exit_status": "submitted"},
    ]))
    monkeypatch.setattr(runner, "GENERATED", generated)
    monkeypatch.setitem(runner.SETTINGS, "model", "deepseek-flash")
    for directory in generated.iterdir():
        (directory / "run_config.json").write_text(json.dumps({"settings": runner.SETTINGS,
            "runtime_protocol": runner.RUNTIME_PROTOCOL,
            "per_instance_cost_limit": runner.SETTINGS["pilot_per_instance_cost_limit" if directory.name.startswith("pilot-") else "per_instance_cost_limit"]}))
    assert runner.prior_completed_ids("run") == {"done-solved", "done-failed", "retry-unknown"}
    assert runner.prior_completed_ids("pilot") == {"pilot-only"}


def test_split_chunks_is_contiguous_and_capped():
    import enigma_reproduction.run_nyu as runner
    ids = [f"t{i}" for i in range(200)]
    chunks = runner.split_chunks(ids, 4)
    assert [len(c) for c in chunks] == [50, 50, 50, 50]
    assert [i for c in chunks for i in c] == ids
    assert runner.split_chunks(ids[:3], 4) == [["t0"], ["t1"], ["t2"]]


@pytest.mark.parametrize("first_raises", [False, True])
@pytest.mark.parametrize("log_finalize_fails", [False, True])
def test_exit_error_does_not_halt_the_batch(tmp_path, monkeypatch, first_raises, log_finalize_fails):
    import os
    import docker
    import run
    import enigma_reproduction.run_nyu as runner

    if log_finalize_fails:
        from enigma_reproduction.task_log import TaskLog
        monkeypatch.setattr(TaskLog, "finalize", Mock(side_effect=FileNotFoundError("synthetic missing readable log")))

    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "inventory.json").write_text("{}")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "GENERATED", generated)
    monkeypatch.setattr(runner, "preflight", lambda image: {"ready": True, "resolved_image": "sha256:test"})
    monkeypatch.setenv(runner.credential_env(), "synthetic-log-credential")
    monkeypatch.setenv("ENIGMA_USAGE_LEDGER", "unused-test-ledger")
    client = Mock()
    client.containers.list.return_value = []
    client.networks.list.return_value = []
    monkeypatch.setattr(docker, "from_env", Mock(return_value=client))
    (tmp_path / "challenge.json").write_text("{}")
    config = str(Path("config/ctf/ctf_crypto.yaml").resolve())
    records = [{"id": "nyu-test-a", "name": "First", "category": "crypto",
                "path": str(tmp_path), "missing_attachments": [], "config": config},
               {"id": "nyu-test-b", "name": "Second", "category": "crypto",
                "path": str(tmp_path), "missing_attachments": [], "config": config}]
    statuses = iter(["exit_error", "submitted"])

    class OfflineMain:
        def __init__(self, args, *, log_path):
            add_file_handler(log_path)
            self.agent = SimpleNamespace(info={"exit_status": next(statuses)},
                model=Mock(), summarizer_model=Mock(), add_hook=lambda hook: setattr(self, "hook", hook))
            self.env = Mock()
            self.traj_dir = tmp_path / "trajectory"

        def run(self, index):
            if first_raises and self.agent.info["exit_status"] == "exit_error":
                raise RuntimeError("Synthetic per-task Docker startup failure")
            Path(os.environ["ENIGMA_USAGE_LEDGER"]).write_text(json.dumps({"role": "primary",
                "accounting_complete": True, "prompt_tokens": 100, "completion_tokens": 5,
                "cache_hit_tokens": 20, "cost_upper_usd": .02, "cost_lower_usd": .01}) + "\n")

    monkeypatch.setattr(run, "Main", OfflineMain)
    assert runner.pilot(records, "unused-test-image", .4, .8, ["nyu-test-a", "nyu-test-b"], label="run") == 0
    rows = json.loads(next(generated.glob("run-*/results.json")).read_text())
    assert [r["id"] for r in rows] == ["nyu-test-a", "nyu-test-b"]
    assert [r["exit_status"] for r in rows] == ["infrastructure_error" if first_raises else "exit_error", "submitted"]
    if log_finalize_fails:
        assert all("log_finalize_error" in row for row in rows)


def test_network_connection_has_own_timeout_and_no_credential_environment(monkeypatch):
    import sweagent.environment.utils as utils
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-network-credential")
    connect = Mock()
    monkeypatch.setattr(utils.subprocess, "run", connect)
    utils.attach_network_interface_to_container("owned-test-container")
    args, kwargs = connect.call_args
    assert args[0] == ["docker", "network", "connect", "ctfnet", "owned-test-container"]
    assert kwargs["timeout"] == 60 and kwargs["check"] is True
    assert "DEEPSEEK_API_KEY" not in kwargs["env"]


def test_network_connection_reports_timeout_and_failed_exit(monkeypatch):
    import subprocess
    import sweagent.environment.utils as utils
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-network-credential")
    connect = Mock(side_effect=subprocess.TimeoutExpired(["docker"], 60))
    monkeypatch.setattr(utils.subprocess, "run", connect)
    with pytest.raises(RuntimeError, match="timed out after 60 seconds"):
        utils.attach_network_interface_to_container("owned-test-container")
    # A daemon error must stop initialization, rather than solving without ctfnet.
    connect.side_effect = subprocess.CalledProcessError(1, ["docker"],
        stderr="Docker error synthetic-network-credential")
    with pytest.raises(RuntimeError, match="connection failed") as error:
        utils.attach_network_interface_to_container("owned-test-container")
    assert "synthetic-network-credential" not in str(error.value)
