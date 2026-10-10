"""Isolation, transport recovery, experiment separation and budget calibration."""
import json
import fcntl
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from sweagent.agent.models import ModelArguments, get_model
from sweagent.environment.ctf_isolation import TaskIsolation, isolated_config, prepare_compose, verify_network_members
from sweagent.environment import utils
import enigma_reproduction.run_nyu as runner


@pytest.fixture
def model(monkeypatch, tmp_path):
    monkeypatch.delenv("ENIGMA_MODEL_SETTINGS", raising=False)
    monkeypatch.setenv("TEAMOROUTER_API_KEY", "synthetic-runtime-credential")
    monkeypatch.setenv("ENIGMA_USAGE_LEDGER", str(tmp_path / "usage.jsonl"))
    monkeypatch.setattr("sweagent.agent.gpt54.OpenAI", Mock(return_value=Mock()))
    result = get_model(ModelArguments(model_name="gpt-5.4", per_instance_cost_limit=.8))
    result.settings["api_retry"] = {"max_attempts": 3, "initial_delay_seconds": 0, "max_delay_seconds": 0}
    return result


def response():
    return SimpleNamespace(model="gpt-5.4", usage=SimpleNamespace(prompt_tokens=100,
        completion_tokens=10, prompt_tokens_details=SimpleNamespace(cached_tokens=0)),
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="ok"))])


def connect_error(cause):
    error = APIConnectionError(request=httpx.Request("POST", "https://relay.invalid/v1/chat/completions"))
    error.__cause__ = cause
    return error


def ledger():
    return [json.loads(line) for line in Path(os.environ["ENIGMA_USAGE_LEDGER"]).read_text().splitlines()]


def test_connect_failure_recovers_and_accounts_once(model):
    error = connect_error(httpx.ConnectError("synthetic-runtime-credential"))
    model.client.chat.completions.create.side_effect = [error, response()]
    assert model.query([]) == "ok"
    assert model.stats.api_calls == 1
    assert model.stats.instance_cost == pytest.approx(.0004)
    records = ledger()
    assert records[0]["accounting_complete"] and records[0]["cost_upper_usd"] == 0
    assert records[0]["cause_types"] == ["APIConnectionError", "ConnectError"]
    assert records[1]["attempt"] == 2
    assert "synthetic-runtime-credential" not in json.dumps(records)
    summary = runner.usage_summary(Path(os.environ["ENIGMA_USAGE_LEDGER"]))
    assert summary["accounting_complete"] and summary["successful_api_calls"] == summary["failed_api_calls"] == 1


def test_read_failure_recovers_but_unknown_bill_is_not_erased(model):
    model.client.chat.completions.create.side_effect = [connect_error(httpx.ReadError("closed")), response()]
    assert model.query([]) == "ok"
    assert ledger()[0]["cost_unknown"] and not ledger()[0]["accounting_complete"]
    summary = runner.usage_summary(Path(os.environ["ENIGMA_USAGE_LEDGER"]))
    assert summary["cost_upper_usd"] is None and summary["known_cost_upper_usd"] > 0


@pytest.mark.parametrize("status,retry", [(400, False), (401, False), (404, False), (429, True), (503, True)])
def test_only_transient_http_errors_retry(model, status, retry):
    request = httpx.Request("POST", "https://relay.invalid/v1/chat/completions")
    error = APIStatusError("synthetic-runtime-credential", response=httpx.Response(status, request=request), body=None)
    model.client.chat.completions.create.side_effect = [error, response()]
    if retry:
        assert model.query([]) == "ok"
        assert model.client.chat.completions.create.call_count == 2
    else:
        with pytest.raises(RuntimeError):
            model.query([])
        assert model.client.chat.completions.create.call_count == 1
    assert ledger()[0]["http_status"] == status


def test_connection_retry_is_bounded(model):
    model.client.chat.completions.create.side_effect = connect_error(httpx.ConnectTimeout("timed out"))
    with pytest.raises(RuntimeError, match="attempts=3"):
        model.query([])
    assert model.client.chat.completions.create.call_count == 3
    assert ledger()[-1]["api_failure_exhausted"] and not ledger()[-1]["will_retry"]


def test_isolation_preserves_aliases_paths_and_private_service_topology():
    original = {"name": "old", "services": {"web": {"image": "test", "container_name": "fixed",
        "ports": ["8000:8000"], "networks": {"ctfnet": {"aliases": ["rev.chal.csaw.io"]}, "backend": {}},
        "volumes": [{"type": "bind", "source": "/dataset/files", "target": "/files", "read_only": True}]},
        "db": {"image": "db", "networks": {"backend": {}}}},
        "networks": {"ctfnet": {"external": True}, "backend": {"name": "shared-db", "internal": True}},
        "volumes": {"db-data": {"name": "shared-data"}}}
    a = isolated_config(original, "task-a", "net-a")
    b = isolated_config(original, "task-b", "net-b")
    assert a["networks"]["ctfnet"]["name"] != b["networks"]["ctfnet"]["name"]
    assert a["services"]["web"]["networks"]["ctfnet"]["aliases"] == ["rev.chal.csaw.io"]
    assert "ports" not in a["services"]["web"] and "container_name" not in a["services"]["web"]
    assert a["services"]["db"]["networks"] == {"backend": {}}
    assert a["networks"]["backend"]["internal"]
    assert a["networks"]["backend"]["name"] != b["networks"]["backend"]["name"]
    assert a["volumes"]["db-data"]["name"] != b["volumes"]["db-data"]["name"]
    assert a["services"]["web"]["volumes"] == original["services"]["web"]["volumes"]
    assert original["name"] == "old" and original["services"]["web"]["ports"] == ["8000:8000"]


def test_host_network_requires_exclusive_execution():
    original = {"services": {"sidecar": {"network_mode": "host"}}}
    with pytest.raises(RuntimeError, match="exclusive"):
        isolated_config(original, "task", "net")
    assert isolated_config(original, "task", "net", allow_host=True)["services"]["sidecar"]["network_mode"] == "host"


def test_host_task_lock_excludes_other_attempts(tmp_path, monkeypatch):
    ordinary = tmp_path / "ordinary"
    special = tmp_path / "special"
    ordinary.mkdir()
    special.mkdir()
    (ordinary / "challenge.json").write_text("{}")
    (special / "challenge.json").write_text('{"compose":true}')
    (special / "docker-compose.yml").write_text('services:\n  helper:\n    network_mode: host\n')
    client = Mock()
    client.containers.list.return_value = []
    client.networks.list.return_value = []
    monkeypatch.setattr("sweagent.environment.ctf_isolation.docker.from_env", Mock(return_value=client))
    lock = tmp_path / "execution.lock"
    with TaskIsolation(tmp_path / "normal-runtime", ordinary / "challenge.json", lock):
        with lock.open("a") as other:
            fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(other, fcntl.LOCK_UN)
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with TaskIsolation(tmp_path / "special-runtime", special / "challenge.json", lock) as task:
        assert task.exclusive
        with lock.open("a") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)


def test_compose_runtime_copy_uses_resolved_paths_and_private_permissions(tmp_path, monkeypatch):
    for key, value in {"ENIGMA_CTF_PROJECT": "task-test", "ENIGMA_CTF_NETWORK": "net-test",
                       "ENIGMA_CTF_DIRECTORY": str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TEAMOROUTER_API_KEY", "synthetic-runtime-credential")
    resolved = {"services": {"service": {"image": "x", "build": {"context": "/source/build"}}}}
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(resolved)))
    monkeypatch.setattr("sweagent.environment.ctf_isolation.subprocess.run", run)
    source = tmp_path / "original.yml"
    source.write_text("original")
    target = prepare_compose(source)
    assert json.loads(target.read_text())["services"]["service"]["build"]["context"] == "/source/build"
    assert source.read_text() == "original"
    assert target.stat().st_mode & 0o077 == 0
    assert "TEAMOROUTER_API_KEY" not in run.call_args.kwargs["env"]


def test_failed_compose_start_is_not_treated_as_ready(monkeypatch, tmp_path):
    monkeypatch.delenv("ENIGMA_CTF_PROJECT", raising=False)
    monkeypatch.setattr(utils.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=1, stderr="port conflict")))
    with pytest.raises(RuntimeError, match="startup failed"):
        utils.get_docker_compose(tmp_path / "compose.yml")


def test_task_resources_are_cleaned_after_interrupt(tmp_path, monkeypatch):
    challenge = tmp_path / "challenge.json"
    challenge.write_text("{}")
    client = Mock()
    client.containers.list.return_value = []
    client.networks.list.return_value = []
    monkeypatch.setattr("sweagent.environment.ctf_isolation.docker.from_env", Mock(return_value=client))
    monkeypatch.setenv("ENIGMA_CTF_NETWORK", "previous-net")
    task = TaskIsolation(tmp_path / "runtime", challenge, tmp_path / "lock")
    with pytest.raises(KeyboardInterrupt):
        with task:
            assert os.environ["ENIGMA_CTF_NETWORK"] == task.network
            raise KeyboardInterrupt
    assert os.environ["ENIGMA_CTF_NETWORK"] == "previous-net"
    client.networks.create.return_value.remove.assert_called_once()
    client.close.assert_called_once()
    assert task.lock is None


def test_network_cannot_include_another_tasks_service(monkeypatch):
    client = Mock()
    client.networks.get.return_value.attrs = {"Containers": {"wrong-service": {}}}
    client.containers.get.return_value.attrs = {"Config": {"Labels": {"com.docker.compose.project": "other-task"}}}
    monkeypatch.setattr("sweagent.environment.ctf_isolation.docker.from_env", Mock(return_value=client))
    with pytest.raises(RuntimeError, match="another attempt"):
        verify_network_members("task-network", "this-task")
    client.close.assert_called_once()


def test_resume_and_budget_ignore_old_protocol_and_api_infrastructure_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GENERATED", tmp_path)
    for name, protocol, costs in [("run-20261010", runner.RUNTIME_PROTOCOL, [.2, .6]),
                                   ("run-20261009", {}, [10.0])]:
        d = tmp_path / name
        d.mkdir()
        (d / "run_config.json").write_text(json.dumps({"settings": runner.SETTINGS, "runtime_protocol": protocol,
            "per_instance_cost_limit": .8}))
        rows = [{"id": name+str(i), "exit_status": "submitted", "solved": True,
                 "accounting_complete": True, "cost_upper_usd": cost} for i, cost in enumerate(costs)]
        rows += [{"id": "api-error", "exit_status": "exit_error", "api_failure_exhausted": True,
                  "accounting_complete": True, "solved": False}]
        (d / "results.json").write_text(json.dumps(rows))
    assert runner.prior_completed_ids("run", .8) == {"run-202610100", "run-202610101"}
    report = runner.budget_report()
    assert report["solved_sample_size"] == 2
    assert report["mean_solved_cost_upper_usd"] == pytest.approx(.4)
    assert report["recommended_per_instance_cost_limit_usd"] == pytest.approx(.8)


def test_selected_settings_reach_adapter_without_changing_default_file(model, monkeypatch, tmp_path):
    from sweagent.agent.deepseek_flash import model_settings
    selected = tmp_path / "selected.json"
    selected.write_text(json.dumps({"model": "gpt-5.4", "reasoning_effort": "medium"}))
    monkeypatch.setenv("ENIGMA_MODEL_SETTINGS", str(selected))
    assert model_settings("gpt-5.4", "unused.json")["reasoning_effort"] == "medium"
    with pytest.raises(ValueError, match="match"):
        model_settings("deepseek-flash", "unused.json")


def test_named_experiments_keep_old_gpt_results_out_of_resume_and_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'GENERATED', tmp_path)
    for experiment, identifier, cost in [('old-gpt', 'old-task', 7.0), ('fresh-gpt', 'new-task', .3)]:
        directory = tmp_path / ('run-' + experiment)
        directory.mkdir()
        (directory / 'run_config.json').write_text(json.dumps({
            'settings': runner.SETTINGS, 'runtime_protocol': runner.RUNTIME_PROTOCOL,
            'experiment': experiment, 'per_instance_cost_limit': .8}))
        (directory / 'results.json').write_text(json.dumps([
            {'id': identifier, 'exit_status': 'submitted', 'solved': True,
             'accounting_complete': True, 'cost_upper_usd': cost},
            {'id': 'interrupted-task', 'exit_status': 'interrupted'},
            {'id': 'docker-error-task', 'exit_status': 'infrastructure_error'}]))
    monkeypatch.setattr(runner, 'EXPERIMENT', 'fresh-gpt')
    assert runner.prior_completed_ids('run', .8) == {'new-task'}
    assert runner.budget_report()['mean_solved_cost_upper_usd'] == pytest.approx(.3)
    monkeypatch.setattr(runner, 'EXPERIMENT', 'brand-new-gpt')
    assert runner.prior_completed_ids('run', .8) == set()
    assert runner.budget_report()['solved_sample_size'] == 0


def test_experiment_lock_rejects_duplicate_launcher_and_releases_on_interrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'GENERATED', tmp_path)
    with pytest.raises(KeyboardInterrupt):
        with runner.experiment_lock('run'):
            with pytest.raises(RuntimeError, match='already running'):
                with runner.experiment_lock('run'):
                    pass
            raise KeyboardInterrupt
    with runner.experiment_lock('run'):
        pass


def test_atomic_checkpoint_preserves_last_complete_results_on_write_failure(tmp_path, monkeypatch):
    path = tmp_path / 'results.json'
    runner.write_json_atomic(path, [{'id': 'completed'}])
    monkeypatch.setattr(runner.json, 'dump', Mock(side_effect=OSError('disk write failure')))
    with pytest.raises(OSError):
        runner.write_json_atomic(path, [{'id': 'new-partial'}])
    assert json.loads(path.read_text()) == [{'id': 'completed'}]


def test_parallel_interrupt_stops_real_workers_and_saves_checkpoint(tmp_path, monkeypatch):
    import time
    import textwrap
    generated = tmp_path / 'generated'
    generated.mkdir()
    script = tmp_path / 'offline_worker.py'
    script.write_text(textwrap.dedent('''\
        import argparse, json, os, signal, time
        from pathlib import Path
        parser = argparse.ArgumentParser()
        parser.add_argument('--batch-id')
        parser.add_argument('--experiment')
        parser.add_argument('--ids', nargs='+')
        args, _ = parser.parse_known_args()
        directory = Path(__file__).parent / 'generated' / ('run-offline-' + str(os.getpid()))
        directory.mkdir()
        (directory / 'run_config.json').write_text(json.dumps({'batch_id': args.batch_id}))
        rows = [{'id': args.ids[0], 'exit_status': 'submitted', 'solved': True,
                 'accounting_complete': True, 'cost_upper_usd': .1, 'known_cost_upper_usd': .1}]
        (directory / 'results.json').write_text(json.dumps(rows))
        def stop(signum, frame):
            rows.append({'id': args.ids[1], 'exit_status': 'interrupted', 'solved': False,
                         'accounting_complete': False, 'known_cost_upper_usd': 0})
            (directory / 'results.json').write_text(json.dumps(rows))
            raise SystemExit(130)
        signal.signal(signal.SIGINT, stop)
        (directory / 'ready').touch()
        while True:
            time.sleep(1)
    '''))
    monkeypatch.setattr(runner, '__file__', str(script))
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'GENERATED', generated)
    monkeypatch.setattr(runner, 'EXPERIMENT', 'offline-resume')
    original_sleep = time.sleep
    interrupt_sent = False

    def interrupt_after_workers_ready(seconds):
        nonlocal interrupt_sent
        if interrupt_sent:
            return original_sleep(seconds)
        deadline = time.monotonic() + 10
        while len(list(generated.glob('run-offline-*/ready'))) < 2:
            assert time.monotonic() < deadline, 'offline workers did not become ready'
            original_sleep(.01)
        foreign = generated / 'run-unrelated'
        foreign.mkdir()
        (foreign / 'run_config.json').write_text('{"batch_id":"unrelated"}')
        (foreign / 'results.json').write_text('[{"solved":true,"accounting_complete":true,"cost_upper_usd":99}]')
        interrupt_sent = True
        raise KeyboardInterrupt

    monkeypatch.setattr(runner.time, 'sleep', interrupt_after_workers_ready)
    assert runner.run_in_parallel([], 'unused', .8, 3.2, ['a', 'b', 'c', 'd'], 2, False) == 130
    summary = json.loads(next(generated.glob('parallel-*/summary.json')).read_text())
    batch = json.loads(next(generated.glob('parallel-*/batch.json')).read_text())
    assert summary['interrupted'] and summary['attempted'] == 4 and summary['solved'] == 2
    assert summary['known_cost_upper_usd'] == pytest.approx(.2)
    assert len(summary['result_directories']) == 2
    assert all(code == 130 for code in summary['exit_codes'].values())
    assert all(not Path('/proc', str(worker['pid'])).exists() for worker in batch['workers'])
    rows = [row for path in generated.glob('run-offline-*/results.json') for row in json.loads(path.read_text())]
    assert {row['id'] for row in rows if row['exit_status'] == 'interrupted'} == {'b', 'd'}


def test_missing_readable_log_is_recovered_without_losing_task_result(tmp_path):
    import shutil
    from enigma_reproduction.task_log import TaskLog
    log = TaskLog(tmp_path, model='offline-model', name='offline-task', identifier='id', category='rev', budget=.8)
    shutil.rmtree(log.path.parent)
    log.finalize({'exit_status': 'interrupted', 'solved': False})
    content = log.path.read_text()
    assert '"exit_status": "interrupted"' in content
    assert '"process_log_missing": true' in content


@pytest.mark.parametrize('nested', [False, True])
def test_http_error_diagnostics_keep_reason_and_redact_credentials(model, nested):
    from openai import BadRequestError
    details = {'code': 'invalid_parameter', 'type': 'invalid_request_error', 'param': 'messages',
               'message': 'Request rejected: synthetic-runtime-credential\n' + 'x' * 800,
               'internal_debug': 'must-not-save-whole-body'}
    body = {'error': details} if nested else details
    request = httpx.Request('POST', 'https://relay.invalid/v1/chat/completions')
    response = httpx.Response(400, request=request, headers={'x-request-id': 'test-request-id'})
    error = BadRequestError('opaque error wrapper', response=response, body=body)
    model.client.chat.completions.create.side_effect = error
    with pytest.raises(RuntimeError) as raised:
        model.query([])
    record = ledger()[-1]
    assert record['api_error_code'] == 'invalid_parameter'
    assert record['api_error_param'] == 'messages' and record['http_status'] == 400
    assert record['request_id'] == 'test-request-id'
    assert len(record['api_error_message']) == 500
    assert 'synthetic-runtime-credential' not in json.dumps(record) + str(raised.value)
    assert '[REDACTED]' in record['api_error_message']
    assert '\n' not in record['api_error_message']
    assert 'must-not-save-whole-body' not in json.dumps(record)
    assert model.client.chat.completions.create.call_count == 1
    summary = runner.usage_summary(Path(os.environ['ENIGMA_USAGE_LEDGER']))
    assert summary['last_api_error']['api_error_code'] == 'invalid_parameter'
    assert summary['last_api_error']['api_error_param'] == 'messages'


@pytest.fixture
def cohort_context(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'
    root.mkdir()
    dataset = root / 'dataset'
    dataset.mkdir()
    manifest = dataset / 'test_dataset.json'
    manifest.write_text('{}')
    records = [{'id': identifier, 'category': 'rev', 'name': identifier} for identifier in 'abcde']
    data = {'schema_version': 1, 'model': 'gpt-5.4', 'baseline_unique_successes': 4,
            'dataset_manifest_sha256': runner.sha(manifest), 'selected_ids': list('abcd'),
            'already_solved_ids': ['b']}
    path = root / 'cohort.json'
    path.write_text(json.dumps(data))
    settings = {**runner.SETTINGS, 'model': 'gpt-5.4', 'dataset_root': str(dataset),
                'dataset_manifest': 'test_dataset.json', 'per_instance_cost_limit': 1.0,
                'task_selection': {'file': 'cohort.json', 'sha256': runner.sha(path)}}
    monkeypatch.setattr(runner, 'ROOT', root)
    monkeypatch.setattr(runner, 'GENERATED', root / 'generated')
    monkeypatch.setattr(runner, 'SETTINGS', settings)
    monkeypatch.setattr(runner, 'EXPERIMENT', 'new-budget')
    args = SimpleNamespace(run=True, pilot=False, ids=None, from_scratch=False, dry_run=True,
                           reuse_inventory=False, num_workers=3, per_instance=1.0, total=4.0, image='unused')
    return records, args, path, data


def test_cohort_dry_run_carries_old_success_but_retries_old_budget_failures(cohort_context, monkeypatch):
    records, args, _, _ = cohort_context
    runner.GENERATED.mkdir()
    for name, experiment, budget, rows in [
        ('run-old', 'old-budget', .8, [{'id': 'a', 'exit_status': 'exit_cost'},
                                     {'id': 'b', 'exit_status': 'submitted', 'solved': True}]),
        ('run-new', 'new-budget', 1.0, [{'id': 'c', 'exit_status': 'interrupted'},
                                      {'id': 'd', 'exit_status': 'submitted', 'solved': True}])]:
        directory = runner.GENERATED / name
        directory.mkdir()
        (directory / 'run_config.json').write_text(json.dumps({'settings': runner.SETTINGS,
            'experiment': experiment, 'per_instance_cost_limit': budget, 'runtime_protocol': runner.RUNTIME_PROTOCOL}))
        (directory / 'results.json').write_text(json.dumps(rows))
    monkeypatch.setattr(runner, 'run_in_parallel', Mock(side_effect=AssertionError('dry run must not spawn workers')))
    monkeypatch.setattr(runner, 'pilot', Mock(side_effect=AssertionError('dry run must not run solver')))
    assert runner.run_selected(records, args) == 0
    preview = json.loads((runner.GENERATED / 'selection-preview.json').read_text())
    assert preview['cohort_count'] == 4 and preview['pending_ids'] == ['a', 'c']
    assert preview['skipped_ids'] == ['b', 'd'] and preview['carried_success_count'] == 1
    assert preview['per_instance_cost_limit_usd'] == 1.0 and preview['model_calls_made'] == 0
    assert 'e' not in preview['pending_ids']


def test_cohort_rejects_explicit_ids_outside_baseline(cohort_context):
    records, args, _, _ = cohort_context
    args.ids = ['e']
    with pytest.raises(ValueError, match='configured challenge cohort'):
        runner.run_selected(records, args)


def test_cohort_skips_explicit_old_failures_and_retains_other_pending_tasks(cohort_context, monkeypatch):
    records, args, path, data = cohort_context
    data['already_failed_ids'] = ['a']
    path.write_text(json.dumps(data))
    runner.SETTINGS['task_selection']['sha256'] = runner.sha(path)
    monkeypatch.setattr(runner, 'prior_completed_ids', lambda *_: set())
    monkeypatch.setattr(runner, 'run_in_parallel', Mock(side_effect=AssertionError('dry run must not spawn workers')))
    monkeypatch.setattr(runner, 'pilot', Mock(side_effect=AssertionError('dry run must not run solver')))
    assert runner.run_selected(records, args) == 0
    preview = json.loads((runner.GENERATED / 'selection-preview.json').read_text())
    assert preview['pending_ids'] == ['c', 'd']
    assert preview['skipped_ids'] == ['a', 'b']
    assert preview['carried_failure_count'] == preview['carried_success_count'] == 1
    assert preview['skipped_completed_count'] == preview['remaining_count'] == 2
    assert preview['per_instance_cost_limit_usd'] == 1.0 and preview['model_calls_made'] == 0


def test_cohort_checksum_detects_selection_changes(cohort_context):
    records, _, path, data = cohort_context
    data['selected_ids'].append('e')
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='SHA256'):
        runner.task_selection(records)


@pytest.mark.parametrize('change,match', [
    ({'model': 'deepseek-flash'}, 'schema/model'),
    ({'selected_ids': ['a', 'b', 'b', 'd']}, 'unique'),
    ({'selected_ids': ['a', 'b', 'c', 'z']}, 'outside'),
    ({'already_solved_ids': ['e']}, 'Carried successes'),
    ({'already_failed_ids': ['e']}, 'Carried failures'),
    ({'already_failed_ids': ['a', 'a']}, 'Carried failures'),
    ({'already_failed_ids': 'a'}, 'Carried failures'),
    ({'already_failed_ids': ['b']}, 'must not overlap'),
    ({'baseline_unique_successes': 82}, 'count'),
    ({'dataset_manifest_sha256': 'different-dataset'}, 'different dataset'),
])
def test_cohort_rejects_invalid_manifest(cohort_context, change, match):
    records, _, path, data = cohort_context
    data.update(change)
    path.write_text(json.dumps(data))
    runner.SETTINGS['task_selection']['sha256'] = runner.sha(path)
    with pytest.raises(ValueError, match=match):
        runner.task_selection(records)


def test_explicit_from_scratch_can_override_carried_successes(cohort_context):
    records, args, _, _ = cohort_context
    args.from_scratch = True
    assert runner.run_selected(records, args) == 0
    preview = json.loads((runner.GENERATED / 'selection-preview.json').read_text())
    assert preview['pending_ids'] == list('abcd') and preview['skipped_ids'] == []


def test_real_gpt_cohort_carries_three_successes_and_two_old_failures(monkeypatch):
    settings = json.loads((Path(__file__).resolve().parents[1] / 'enigma_reproduction/gpt54_settings.json').read_text())
    monkeypatch.setattr(runner, 'SETTINGS', settings)
    records, _ = runner.dataset()
    selection = runner.task_selection(records)
    assert len(selection['selected_ids']) == 82
    assert set(selection['already_solved_ids']) == {'2021f-cry-collision_course',
                                                  '2022q-rev-dockreleakage', '2019q-rev-gibberish_check'}
    assert set(selection['already_failed_ids']) == {'2021f-rev-maze', '2021f-rev-sfc'}
    skipped = set(selection['already_solved_ids']) | set(selection['already_failed_ids'])
    pending = [i for i in selection['selected_ids'] if i not in skipped]
    assert len(pending) == 77
    assert '2022q-rev-the_big_bang' in pending
    assert '2019q-rev-call_site' not in pending and '2019q-rev-halfpike' not in pending
    assert '2022f-msc-embrey0_leak' not in selection['selected_ids']
    assert [len(chunk) for chunk in runner.split_chunks(pending, 3)] == [26, 26, 25]
    assert set(settings['pilot_ids']).issubset(pending)
    assert settings['per_instance_cost_limit'] == settings['pilot_per_instance_cost_limit'] == 1.0
