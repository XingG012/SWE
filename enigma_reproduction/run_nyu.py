#!/usr/bin/env python3
"""Prepare, check and run NYU EnIGMA using the unchanged category configurations."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SETTINGS = json.loads((ROOT / "enigma_reproduction/settings.json").read_text())
GENERATED = ROOT / "enigma_reproduction/generated"
RUNTIME_PROTOCOL = {"ctf_isolation": "per_task_v1", "api_retry": "bounded_v1"}
EXPERIMENT = "default"
CATEGORIES = {"crypto", "forensics", "misc", "pwn", "rev", "web"}
os.environ["TOGETHER_NO_BANNER"] = "1"
os.environ.pop("OPENAI_LOG", None)
os.environ.setdefault("SWE_AGENT_LOG_STREAM_LEVEL", "INFO")
os.environ.setdefault("SWE_AGENT_LOG_FILE_LEVEL", "INFO")


def write_json_atomic(path, value):
    """Never expose a partially written checkpoint to resume or the parent."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as output:
        json.dump(value, output, indent=2, ensure_ascii=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


@contextmanager
def experiment_lock(label):
    """A second top-level launcher must not duplicate the same experiment."""
    GENERATED.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(f"{SETTINGS['model']}:{label}:{EXPERIMENT}".encode()).hexdigest()[:24]
    with (GENERATED / f"experiment-{identity}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("This experiment is already running; stop its parent before resuming") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dataset():
    root = Path(SETTINGS["dataset_root"]).resolve(strict=True)
    manifest = root / SETTINGS["dataset_manifest"]
    data = json.loads(manifest.read_text())
    records = []
    for identifier, meta in data.items():
        directory = (root / meta["path"]).resolve(strict=True)
        if not directory.is_relative_to(root / "test") or meta["category"] not in CATEGORIES:
            raise ValueError("Only NYU test challenges from the six official categories are permitted")
        challenge_path = directory / "challenge.json"
        challenge = json.loads(challenge_path.read_text())
        if challenge["category"] != meta["category"]:
            raise ValueError("Manifest and challenge categories differ")
        missing = []
        for filename in challenge.get("files", []):
            file = (directory / filename).resolve()
            if not file.is_relative_to(directory) or file.name in {"challenge.json", "keys.cfg"}:
                raise ValueError("Invalid challenge attachment path")
            if not file.exists():
                missing.append(filename)
        records.append({"id": identifier, "name": challenge["name"],
                        "category": meta["category"], "path": str(directory),
                        "challenge_sha256": sha(challenge_path),
                        "missing_attachments": missing,
                        "config": f"config/ctf/ctf_{meta['category']}.yaml"})
    return records, {"path": str(root), "manifest_sha256": sha(manifest),
                     "count": len(records), "categories": dict(Counter(r["category"] for r in records))}


def prepare():
    records, inventory = dataset()
    GENERATED.mkdir(parents=True, exist_ok=True)
    reference_paths = list((ROOT / "config/ctf").glob("ctf_*.yaml"))
    reference_paths += list((ROOT / "config/commands").glob("*"))
    reference_paths += list((ROOT / "trajectories/demonstrations/ctf").rglob("*.traj"))
    reference_paths += [ROOT / "docker/enigma.Dockerfile", ROOT / "docker/ctf_requirements.txt"]
    inventory["reference_sha256"] = {str(p.relative_to(ROOT)): sha(p) for p in reference_paths if p.is_file()}
    inventory["records"] = records
    (GENERATED / "inventory.json").write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({k: inventory[k] for k in ("path", "count", "categories")}, ensure_ascii=False))
    return records


def credential_path():
    return ROOT / ".secrets" / SETTINGS.get("credential_file", "deepseek_api_key")


def credential_env():
    return SETTINGS.get("credential_env", "DEEPSEEK_API_KEY")


def check_credential():
    if os.environ.get(credential_env(), "").strip():
        return True
    path = credential_path()
    return path.is_file() and path.stat().st_mode & 0o077 == 0 and bool(path.read_text().strip())


def repository_status():
    """Permit committed compatibility patches, while checking the v0.7 method."""
    base = SETTINGS["upstream_commit"]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", base, "HEAD"],
                              cwd=ROOT, capture_output=True)
    # Only the documented interface, accounting, logging and dependency fixes
    # may differ in the official Python package. All other method files are pinned.
    adapted = ("sweagent/agent/agents.py", "sweagent/agent/models.py",
               "sweagent/agent/summarizer.py", "sweagent/environment/utils.py",
               "sweagent/utils/config.py", "sweagent/utils/log.py")
    original_paths = ["sweagent", "config", "trajectories/demonstrations/ctf",
                      "docker/enigma.Dockerfile", "docker/ctf_requirements.txt"]
    comparison = subprocess.run(["git", "diff", "--quiet", base, "--", *original_paths,
                                 *(f":(exclude){path}" for path in adapted),
                                 ":(exclude)sweagent/agent/deepseek_flash.py",
                                 ":(exclude)sweagent/agent/gemini_flash.py",
                                 ":(exclude)sweagent/agent/gpt54.py",
                                 ":(exclude)sweagent/agent/qwen3_max.py",
                                 ":(exclude)sweagent/agent/claude_sonnet5.py",
                                 ":(exclude)sweagent/environment/ctf_isolation.py",
                                 ":(exclude)sweagent/utils/secrets.py"], cwd=ROOT, capture_output=True)
    working_diff = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=ROOT, capture_output=True)
    return {"upstream_commit": base, "runtime_commit": head.stdout.strip(),
            "v0_7_ancestor": head.returncode == 0 and ancestry.returncode == 0,
            "original_method_files_unchanged": comparison.returncode == 0,
            "working_tree_diff_sha256": hashlib.sha256(working_diff.stdout).hexdigest()
                if working_diff.returncode == 0 else None}


def preflight(image):
    problems = []
    selection = None
    try:
        selection = task_selection(load_inventory_records())
    except (OSError, ValueError, KeyError, TypeError) as error:
        problems.append(f"题目选择配置校验失败（{type(error).__name__}）")
    repository = repository_status()
    if not repository["v0_7_ancestor"]:
        problems.append("项目不是固定的官方 v0.7 commit 或其复现补丁后继")
    if not repository["original_method_files_unchanged"]:
        problems.append("官方方法文件与固定的 v0.7 基线不同；只允许已记录的接口兼容与计费补丁")
    if not check_credential():
        problems.append(f"未加载 {credential_env()}，或 {credential_path()} 不存在/权限不是私密")
    resolved_image = None
    try:
        import docker
        client = docker.from_env(timeout=5)
        client.ping()
        try:
            obj = client.images.get(image)
            # A local image ID is immutable; no mutable latest tag reaches the solver.
            resolved_image = obj.id
        except docker.errors.ImageNotFound:
            problems.append("未找到 EnIGMA 镜像；需拉取官方镜像或按官方 Dockerfile 构建")
        client.close()
    except Exception:
        problems.append("当前执行环境无法连接 Docker daemon，不能启动论文解题容器")
    try:
        import sweagent
        from sweagent.agent.agents import AgentArguments
        from sweagent.agent.models import ModelArguments
        if sweagent.__version__ != "0.7.0":
            problems.append("Python 导入的不是 SWE-agent 0.7.0")
        for category in sorted(CATEGORIES):
            args = AgentArguments(model=ModelArguments(model_name=SETTINGS["model"]),
                                  config_file=ROOT / f"config/ctf/ctf_{category}.yaml")
            if args.config.summarizer_config.function.__class__.__name__ != "LMSummarizer":
                problems.append("官方类别配置的摘要模块不符合完整 EnIGMA")
            for demo in args.config.demonstrations:
                if not Path(demo).is_file():
                    problems.append("官方类别示例缺失")
    except Exception as e:
        problems.append(f"Python 依赖或官方配置导入失败（{type(e).__name__}）")
    report = {"ready": not problems, "blockers": problems, "repository": repository,
              "model": SETTINGS["model"], "base_url": SETTINGS["base_url"],
              "resolved_image": resolved_image,
              "task_selection_count": len(selection["selected_ids"]) if selection else None,
              "carried_success_count": len(selection["already_solved_ids"]) if selection else 0,
              "carried_failure_count": len(selection.get("already_failed_ids", [])) if selection else 0,
              "model_calls_made": 0}
    GENERATED.mkdir(parents=True, exist_ok=True)
    (GENERATED / "preflight.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return report


def usage_summary(path):
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    complete = all(row.get("accounting_complete") is True for row in rows)
    prompt = sum(row.get("prompt_tokens", 0) for row in rows)
    completion = sum(row.get("completion_tokens", 0) for row in rows)
    hit = sum(row.get("cache_hit_tokens", 0) for row in rows)
    known_upper = sum(row["cost_upper_usd"] for row in rows if row["accounting_complete"])
    known_lower = sum(row["cost_lower_usd"] for row in rows if row["accounting_complete"])
    return {"accounting_complete": complete, "api_calls": len(rows),
            "successful_api_calls": sum(row.get("request_succeeded", "response_model" in row) for row in rows),
            "failed_api_calls": sum(row.get("request_succeeded") is False or "error" in row for row in rows),
            "api_failure_exhausted": any(row.get("api_failure_exhausted") for row in rows),
            "api_request_failed": bool(rows and rows[-1].get("request_succeeded") is False),
            "last_api_error": {key: rows[-1][key] for key in
                ("error", "http_status", "api_error_code", "api_error_param", "api_error_type",
                 "api_error_message", "request_id", "cause_types", "attempt", "retryable", "will_retry")
                if key in rows[-1]} if rows and rows[-1].get("request_succeeded") is False else None,
            "prompt_tokens": prompt, "completion_tokens": completion, "cache_hit_tokens": hit,
            "cache_hit_ratio": hit / prompt if prompt else None,
            "api_latency_seconds": sum(row.get("latency_seconds", 0) for row in rows),
            "cost_upper_usd": known_upper if complete else None,
            "cost_lower_usd": known_lower if complete else None,
            "known_cost_upper_usd": known_upper, "known_cost_lower_usd": known_lower,
            "by_role": {role: {"api_calls": sum(row["role"] == role for row in rows),
                       "cost_upper_usd": sum(row["cost_upper_usd"] for row in rows
                                              if row["role"] == role and row["accounting_complete"])}
                        for role in ("primary", "summarizer")}}


def prior_completed_ids(label, per_instance=None):
    """IDs already recorded as finished attempts in earlier `label` batches.

    Only reuse the same settings and isolation/retry protocol. Exhausted API
    transport failures and infrastructure failures remain eligible for resume;
    ordinary solver failures remain skipped. Historical attempts are preserved.
    """
    done = set()
    budget = per_instance if per_instance is not None else SETTINGS[
        "pilot_per_instance_cost_limit" if label == "pilot" else "per_instance_cost_limit"]
    for path in GENERATED.glob(f"{label}-*/results.json"):
        try:
            config_path = path.parent / "run_config.json"
            # Only reuse this protocol's results with matching model/settings.
            if config_path.exists():
                config = json.loads(config_path.read_text())
                if config.get("runtime_protocol") != RUNTIME_PROTOCOL:
                    continue
                if config.get("experiment", "default") != EXPERIMENT:
                    continue
                old = config.get("settings", {})
                if any(old.get(key) != SETTINGS.get(key) for key in
                       ("model", "base_url", "temperature", "top_p", "max_output_tokens", "reasoning_effort",
                        "sampling_mode", "thinking", "enable_thinking", "thinking_budget", "stop_sequences",
                        "output_protocol", "output_retry_attempts", "pricing", "api_retry", "task_selection")):
                    continue
                if config.get("per_instance_cost_limit") != budget:
                    continue
            else:
                continue
            rows = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for row in rows:
            status = row.get("exit_status")
            if status not in (None, "interrupted", "infrastructure_error") and not (
                    row.get("api_failure_exhausted") or
                    status == "exit_error" and (row.get("api_request_failed") or
                                                row.get("accounting_complete") is False)):
                done.add(row.get("id"))
    return done


def split_chunks(items, workers):
    """Split `items` into at most `workers` contiguous chunks, dropping empty ones."""
    size = math.ceil(len(items) / workers) if workers > 0 else len(items)
    return [items[i:i + size] for i in range(0, len(items), size)]


def load_inventory_records():
    """Reuse the inventory written by `prepare()` so workers skip the dataset scan."""
    return json.loads((GENERATED / "inventory.json").read_text())["records"]


def task_selection(records):
    """Read a pinned cohort and outcomes explicitly carried from earlier budgets."""
    spec = SETTINGS.get("task_selection")
    if spec is None:
        return None
    path = (ROOT / spec["file"]).resolve(strict=True)
    if sha(path) != spec["sha256"]:
        raise ValueError("Task selection file differs from its pinned SHA256")
    selection = json.loads(path.read_text())
    if selection.get("schema_version") != 1 or selection.get("model") != SETTINGS["model"]:
        raise ValueError("Task selection schema/model does not match the selected settings")
    selected = selection.get("selected_ids")
    solved = selection.get("already_solved_ids", [])
    failed = selection.get("already_failed_ids", [])
    if (not isinstance(selected, list) or not selected or
            any(not isinstance(i, str) for i in selected) or len(set(selected)) != len(selected)):
        raise ValueError("Task selection must contain unique nonempty challenge IDs")
    if selection.get("baseline_unique_successes") != len(selected):
        raise ValueError("Task selection count does not match the baseline success count")
    allowed = {r["id"] for r in records}
    if not set(selected).issubset(allowed):
        raise ValueError("Task selection contains IDs outside the NYU test manifest")
    if (not isinstance(solved, list) or any(not isinstance(i, str) for i in solved) or
            len(set(solved)) != len(solved) or not set(solved).issubset(selected)):
        raise ValueError("Carried successes must be unique members of the selected cohort")
    if (not isinstance(failed, list) or any(not isinstance(i, str) for i in failed) or
            len(set(failed)) != len(failed) or not set(failed).issubset(selected)):
        raise ValueError("Carried failures must be unique members of the selected cohort")
    if set(solved) & set(failed):
        raise ValueError("Carried successes and failures must not overlap")
    manifest = Path(SETTINGS["dataset_root"]) / SETTINGS["dataset_manifest"]
    if selection.get("dataset_manifest_sha256") != sha(manifest):
        raise ValueError("Task selection belongs to a different dataset manifest")
    return selection


def count_batch_progress(before, batch_id=None):
    """Count tasks finished so far across this batch's result directories.

    Workers append to their own `run-*/results.json` after every task, so the
    entry count is the number of completed tasks in the current batch.
    """
    completed = 0
    for name in {p.name for p in GENERATED.glob("run-*")} - before:
        path = GENERATED / name / "results.json"
        if path.is_file():
            try:
                if batch_id and json.loads((path.parent / "run_config.json").read_text()).get("batch_id") != batch_id:
                    continue
                completed += len(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
    return completed


def stop_workers(children, timeout=45):
    """Ask workers to checkpoint and clean up, then bound the shutdown wait."""
    for _, _, proc, _ in children:
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + timeout
    while any(proc.poll() is None for _, _, proc, _ in children) and time.monotonic() < deadline:
        time.sleep(.1)
    for _, _, proc, _ in children:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)


def run_in_parallel(records, image, per_instance, total, identifiers, workers, from_scratch):
    """Split the run across `workers` subprocesses and wait for all of them.

    Each worker is a separate process writing its own `run-*/results.json`, ledger and
    logs, so no shared process-global state (logger, ENIGMA_USAGE_LEDGER, spent) is
    crossed. The parent divides `total` proportionally to chunk size and reports a
    combined tally from the result directories created by this batch.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log_dir = GENERATED / f"parallel-{stamp}"
    log_dir.mkdir(mode=0o700)
    batch_id = uuid.uuid4().hex
    before = {p.name for p in GENERATED.glob("run-*")}
    chunks = split_chunks(identifiers, workers)
    script = str(Path(__file__).resolve())
    children = []
    codes = []
    interval = SETTINGS.get("progress_interval_seconds", 300)
    last_report = time.monotonic()
    interrupted = False
    try:
        for index, chunk in enumerate(chunks, start=1):
            worker_total = total * len(chunk) / len(identifiers)
            argv = [sys.executable, script, "--run", "--reuse-inventory",
                    "--image", image,
                    "--per-instance", repr(per_instance), "--total", repr(worker_total),
                    "--num-workers", "1", "--experiment", EXPERIMENT,
                    "--batch-id", batch_id, "--ids", *chunk]
            if os.environ.get("ENIGMA_MODEL_SETTINGS"):
                argv += ["--settings", os.environ["ENIGMA_MODEL_SETTINGS"]]
            if from_scratch:
                argv.append("--from-scratch")
            log = open(log_dir / f"worker-{index}.log", "wb")
            try:
                proc = subprocess.Popen(argv, cwd=str(ROOT), stdout=log,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            except BaseException:
                log.close()
                raise
            children.append((index, len(chunk), proc, log))
            write_json_atomic(log_dir / "batch.json", {
                "experiment": EXPERIMENT, "batch_id": batch_id, "parent_pid": os.getpid(),
                "settings_file": os.environ.get("ENIGMA_MODEL_SETTINGS"),
                "per_instance_cost_limit": per_instance, "total_cost_limit": total,
                "selected_ids": identifiers,
                "workers": [{"worker": i, "pid": p.pid, "count": n} for i, n, p, _ in children]})
            print(json.dumps({"worker": index, "pid": proc.pid, "count": len(chunk),
                              "total_cost_limit": worker_total,
                              "log": str(log_dir / f"worker-{index}.log")}, ensure_ascii=False), flush=True)
        while any(entry[2].poll() is None for entry in children):
            if time.monotonic() - last_report >= interval:
                completed = count_batch_progress(before, batch_id)
                print(json.dumps({"progress": {"completed": completed,
                                               "remaining": len(identifiers) - completed,
                                               "total": len(identifiers)}},
                                 ensure_ascii=False), flush=True)
                last_report = time.monotonic()
            time.sleep(1)
    except KeyboardInterrupt:
        interrupted = True
        print("Stopping workers; saving interrupted tasks for resume.", flush=True)
        stop_workers(children)
    except BaseException:
        stop_workers(children)
        for _, _, _, log in children:
            log.close()
        raise
    for index, count, proc, log in children:
        code = proc.wait()
        log.close()
        codes.append((index, count, code))
        print(json.dumps({"worker": index, "count": count, "exit_code": code}, ensure_ascii=False))
    batch_dirs = sorted({p.name for p in GENERATED.glob("run-*")} - before)
    results = []
    matching_dirs = []
    for name in batch_dirs:
        path = GENERATED / name / "results.json"
        if path.is_file():
            try:
                if json.loads((path.parent / "run_config.json").read_text()).get("batch_id") != batch_id:
                    continue
                matching_dirs.append(name)
                results.extend(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
    solved = [r for r in results if r.get("solved") and r.get("accounting_complete")]
    summary = {"experiment": EXPERIMENT, "batch_id": batch_id, "interrupted": interrupted,
               "workers": len(children), "attempted": len(results), "solved": sum(bool(r.get("solved")) for r in results),
               "mean_solved_cost_upper_usd": statistics.mean(r["cost_upper_usd"] for r in solved) if solved else None,
               "recommended_per_instance_cost_limit_usd":
                   2 * statistics.mean(r["cost_upper_usd"] for r in solved) if solved else None,
               "known_cost_upper_usd": sum(r.get("known_cost_upper_usd") or 0 for r in results),
               "exit_codes": {index: code for index, _, code in codes},
               "result_directories": matching_dirs, "worker_logs": str(log_dir)}
    write_json_atomic(log_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    if interrupted:
        return 130
    return 0 if all(code == 0 for _, _, code in codes) else 2


def pilot(records, image, per_instance, total, identifiers, label="pilot"):
    report = preflight(image)
    if not report["ready"]:
        return 2
    if not os.environ.get(credential_env(), "").strip():
        os.environ[credential_env()] = credential_path().read_text().strip()
    # Do not import any Ours module, config, log or prompt.
    from run import ActionsArguments, Main, ScriptArguments
    from sweagent.agent.agents import AgentArguments
    from sweagent.agent.models import ModelArguments
    from sweagent.environment.swe_env import EnvironmentArguments
    from sweagent.utils.secrets import redact
    from sweagent.utils.log import get_logger, remove_file_handlers
    from enigma_reproduction.metrics import StepMetrics
    from enigma_reproduction.task_log import TaskLog
    from sweagent.environment.ctf_isolation import TaskIsolation

    lookup = {row["id"]: row for row in records}
    if len(set(identifiers)) != len(identifiers) or any(i not in lookup for i in identifiers):
        raise ValueError("Pilot IDs must be unique members of the NYU test manifest")
    if any(lookup[i]["missing_attachments"] for i in identifiers):
        raise ValueError("A selected pilot challenge has missing attachments")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = GENERATED / f"{label}-{stamp}"
    directory.mkdir(mode=0o700)
    (directory / "run_config.json").write_text(json.dumps({
        "upstream_commit": SETTINGS["upstream_commit"], "settings": SETTINGS,
        "repository": report.get("repository"),
        "resolved_image": report["resolved_image"], "selected_ids": identifiers,
        "per_instance_cost_limit": per_instance, "total_cost_limit": total,
        "runtime_protocol": RUNTIME_PROTOCOL,
        "task_selection": SETTINGS.get("task_selection"),
        "experiment": EXPERIMENT, "batch_id": os.environ.get("ENIGMA_BATCH_ID"),
        "inventory_sha256": sha(GENERATED / "inventory.json")}, indent=2) + "\n")
    results = []
    spent = 0.0
    for identifier in identifiers:
        remaining = total - spent
        if remaining <= 0:
            break
        row = lookup[identifier]
        ledger = directory / f"{identifier}-usage.jsonl"
        os.environ["ENIGMA_USAGE_LEDGER"] = str(ledger)
        args = ScriptArguments(
            environment=EnvironmentArguments(data_path=str(Path(row["path"]) / "challenge.json"),
                repo_path=row["path"], base_commit="nyu-test-local", image_name=report["resolved_image"],
                install_environment=False, verbose=False, container_mounts=[]),
            agent=AgentArguments(model=ModelArguments(model_name=SETTINGS["model"],
                per_instance_cost_limit=min(per_instance, remaining), total_cost_limit=remaining,
                temperature=SETTINGS["temperature"], top_p=SETTINGS["top_p"]),
                config_file=ROOT / row["config"]),
            actions=ActionsArguments(open_pr=False, apply_patch_locally=False),
            ctf=True, skip_existing=False, print_config=False, raise_exceptions=True,
            suffix=f"{label}-{stamp}-{identifier}",
        )
        task_log = TaskLog(ROOT / "logs", model=SETTINGS["model"], name=row["name"],
                           identifier=identifier, category=row["category"], budget=min(per_instance, remaining))
        main = None
        isolation = None
        metrics = StepMetrics(directory / f"{identifier}-steps.jsonl",
                              process_logger=get_logger("enigma-task-process"))
        started = time.perf_counter()
        result = {"id": identifier, "name": row["name"], "category": row["category"], "solved": False,
                  "model": SETTINGS["model"], "pricing_accounting": SETTINGS["pricing"]["accounting"],
                  "exit_status": "interrupted", "log_file": str(task_log.path),
                  "usage_ledger": str(ledger), "step_metrics": str(metrics.path),
                  "per_instance_cost_limit_usd": min(per_instance, remaining)}
        try:
            isolation = TaskIsolation(directory / f"{identifier}-environment", Path(row["path"]) / "challenge.json",
                                      GENERATED / "ctf-execution.lock")
            result.update(ctf_project=isolation.project, ctf_network=isolation.network,
                          exclusive_host_network=isolation.exclusive)
            isolation.__enter__()
            main = Main(args, log_path=task_log.path)
            result["initialization_seconds"] = time.perf_counter() - started
            main.agent.add_hook(metrics)
            main.run(0)
            result["exit_status"] = main.agent.info.get("exit_status")
            # The official env assigns 'submitted' only after validate_submission succeeds.
            result["solved"] = result["exit_status"] == "submitted"
            result["trajectory_dir"] = str(main.traj_dir)
        except KeyboardInterrupt:
            result["exit_status"] = "interrupted"
            raise
        except Exception as e:
            result["exit_status"] = "infrastructure_error"
            result["error_type"] = type(e).__name__
            result["error_message"] = redact(str(e))
            print(redact(f"Trial stopped: {type(e).__name__}: {e}"))
        finally:
            if main is not None:
                for resource in (main.env, main.agent.model.client, main.agent.summarizer_model.client):
                    try:
                        resource.close()
                    except Exception as e:
                        result.setdefault("cleanup_errors", []).append(redact(f"{type(e).__name__}: {e}"))
            if isolation is not None:
                isolation.close()
                if isolation.cleanup_errors:
                    result.setdefault("cleanup_errors", []).extend(isolation.cleanup_errors)
            remove_file_handlers()
            result.update(usage_summary(ledger))
            if result["exit_status"] == "interrupted":
                # An in-flight request may have been billed without returning usage.
                result.update(accounting_complete=False, cost_upper_usd=None, cost_lower_usd=None)
            result.update({"wall_seconds": time.perf_counter() - started, "steps": metrics.steps,
                           "tool_counts": dict(metrics.tools),
                           "action_and_summary_seconds": metrics.action_and_summary_seconds})
            spent += result["known_cost_upper_usd"]
            results.append(result)
            write_json_atomic(directory / "results.json", results)
            try:
                task_log.finalize(result)
            except Exception as error:
                # Readable logging must not erase a checkpoint or stop the next task.
                result["log_finalize_error"] = redact(f"{type(error).__name__}: {error}")
                write_json_atomic(directory / "results.json", results)
        print(json.dumps(result, ensure_ascii=False))
        # Never halt the batch on a single task's failure. Every exit status is a
        # recorded result — including `exit_error` (failed model call, possibly
        # `accounting_complete: False`) and `infrastructure_error` (e.g. a
        # per-task container timeout like this run's haySTACK). The loop only
        # stops when the budget is spent or all selected tasks are done. Truly
        # fatal host failures are caught by `preflight()` before this loop, and a
        # manual interrupt checkpoints the in-flight task as interrupted,
        # so resume re-attempts it.
    solved_costs = [r["cost_upper_usd"] for r in results if r["solved"] and r["accounting_complete"]]
    valid = [r for r in results if r["exit_status"] != "infrastructure_error" and not (
        r["exit_status"] == "exit_error" and (r.get("api_failure_exhausted") or r.get("api_request_failed")))]
    solved_count = sum(bool(r["solved"]) for r in results)
    summary = {"attempted": len(results), "solved": solved_count,
               "selected_count": len(identifiers),
               "valid_attempted": len(valid),
               "coverage": len(valid) / len(identifiers),
               "success_rate_on_valid_attempted": solved_count / len(valid) if valid else None,
               "full_test_pass_at_1": solved_count / 200
                   if label == "run" and len(valid) == len(identifiers) == 200 else None,
               "accounting_complete": all(r["accounting_complete"] for r in results),
               "cost_upper_usd": spent if all(r["accounting_complete"] for r in results) else None,
               "known_cost_upper_usd": spent,
               "cost_is_peak_rate_upper_bound": SETTINGS["pricing"]["accounting"] == "peak_rate_upper_bound",
               "pricing_accounting": SETTINGS["pricing"]["accounting"],
               "cost_lower_usd": sum(r["known_cost_lower_usd"] for r in results)
                   if all(r["accounting_complete"] for r in results) else None,
               "total_api_calls": sum(r["api_calls"] for r in results),
               "total_prompt_tokens": sum(r["prompt_tokens"] for r in results),
               "total_completion_tokens": sum(r["completion_tokens"] for r in results),
               "exit_status_counts": dict(Counter(r["exit_status"] for r in results)),
               "by_category": {cat: {"attempted": sum(r["category"] == cat for r in results),
                    "solved": sum(r["category"] == cat and r["solved"] for r in results),
                    "known_cost_upper_usd": sum(r["known_cost_upper_usd"] for r in results if r["category"] == cat)}
                    for cat in sorted(CATEGORIES)},
               "median_solved_cost_upper_usd": statistics.median(solved_costs) if solved_costs else None,
               "mean_solved_cost_upper_usd": statistics.mean(solved_costs) if solved_costs else None,
               "recommended_per_instance_cost_limit_usd": 2 * statistics.mean(solved_costs) if solved_costs else None,
               "max_solved_cost_upper_usd": max(solved_costs) if solved_costs else None,
               "scope": "small diagnostic sample, not a full benchmark result" if label == "pilot" else "NYU test run"}
    write_json_atomic(directory / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def budget_report():
    """Recommend a fixed next-batch cap from successes of this experiment only."""
    latest = {}
    keys = ("model", "base_url", "temperature", "top_p", "max_output_tokens", "reasoning_effort",
            "sampling_mode", "thinking", "pricing", "api_retry", "task_selection")
    for path in sorted(GENERATED.glob("*/results.json"), key=lambda p: p.parent.name.split("-", 1)[-1]):
        if not path.parent.name.startswith(("pilot-", "run-")):
            continue
        config_path = path.parent / "run_config.json"
        if not config_path.is_file():
            continue
        config = json.loads(config_path.read_text())
        if config.get("experiment", "default") != EXPERIMENT:
            continue
        if config.get("runtime_protocol") != RUNTIME_PROTOCOL or any(
                config.get("settings", {}).get(key) != SETTINGS.get(key) for key in keys):
            continue
        for row in json.loads(path.read_text()):
            latest[row["id"]] = row
    costs = [r["cost_upper_usd"] for r in latest.values() if r.get("solved") and
             r.get("accounting_complete") and isinstance(r.get("cost_upper_usd"), (int, float))]
    mean = statistics.mean(costs) if costs else None
    result = {"model": SETTINGS["model"], "experiment": EXPERIMENT, "solved_sample_size": len(costs),
              "mean_solved_cost_upper_usd": mean,
              "recommended_per_instance_cost_limit_usd": 2 * mean if mean is not None else None,
              "current_per_instance_cost_limit_usd": SETTINGS["per_instance_cost_limit"],
              "pricing_accounting": SETTINGS["pricing"]["accounting"],
              "note": "Freeze the chosen cap before the next batch; no automatic mid-batch changes."}
    print(json.dumps(result, ensure_ascii=False))
    return result


def main():
    global SETTINGS, EXPERIMENT
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--pilot", action="store_true")
    mode.add_argument("--run", action="store_true", help="Run selected IDs, or all 200 NYU test tasks")
    mode.add_argument("--budget-report", action="store_true", help="Report twice the mean successful-task cost")
    parser.add_argument("--settings", type=Path, default=ROOT / "enigma_reproduction/settings.json")
    parser.add_argument("--experiment", default=None,
                        help="Independent experiment ID; reuse it to resume, change it to start fresh")
    parser.add_argument("--batch-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--image", default=None)
    parser.add_argument("--per-instance", type=float, default=None)
    parser.add_argument("--total", type=float, default=None)
    parser.add_argument("--ids", nargs="+", default=None)
    parser.add_argument("--skip-ids", nargs="+", default=None,
                        help="Skip these cohort IDs for this invocation, including interrupted/problem tasks")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show selected/skipped/pending IDs without Docker or model calls")
    parser.add_argument("--from-scratch", action="store_true",
                        help="Ignore prior completed tasks and run the full selected set again")
    parser.add_argument("--num-workers", type=int, default=1,
                        help="Split a --run into this many parallel processes")
    parser.add_argument("--reuse-inventory", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    SETTINGS = json.loads(args.settings.resolve().read_text())
    EXPERIMENT = args.experiment or SETTINGS.get("experiment", "default")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", EXPERIMENT):
        parser.error("--experiment must use 1-96 letters, digits, underscores, dots or hyphens")
    os.environ["ENIGMA_MODEL_SETTINGS"] = str(args.settings.resolve())
    if args.batch_id:
        os.environ["ENIGMA_BATCH_ID"] = args.batch_id
    else:
        os.environ.pop("ENIGMA_BATCH_ID", None)
    if args.budget_report:
        budget_report()
        return 0
    if args.image is None:
        args.image = SETTINGS["image"]
    if args.num_workers < 1:
        parser.error("--num-workers must be at least 1")
    if args.per_instance is None:
        args.per_instance = (SETTINGS["pilot_per_instance_cost_limit"] if args.pilot
                             else SETTINGS["per_instance_cost_limit"])
    if args.run and args.total is None:
        parser.error("--run requires an explicit --total budget")
    if args.total is None:
        args.total = SETTINGS["pilot_total_cost_limit"]
    if any(not math.isfinite(v) or v <= 0 for v in (args.per_instance, args.total)):
        parser.error("Both budget limits must be finite and strictly positive")
    os.chdir(ROOT)
    records = load_inventory_records() if args.reuse_inventory else prepare()
    if args.preflight:
        return 0 if preflight(args.image)["ready"] else 2
    if args.pilot or args.run:
        if args.reuse_inventory or args.dry_run:
            return run_selected(records, args)
        with experiment_lock("pilot" if args.pilot else "run"):
            return run_selected(records, args)
    return 0


def run_selected(records, args):
    label = "pilot" if args.pilot else "run"
    selection = task_selection(records)
    identifiers = args.ids or (SETTINGS["pilot_ids"] if args.pilot else
                              selection["selected_ids"] if selection else [r["id"] for r in records])
    allowed = set(selection["selected_ids"]) if selection else {r["id"] for r in records}
    if len(set(identifiers)) != len(identifiers) or not set(identifiers).issubset(allowed):
        raise ValueError("Selected IDs must be unique members of the configured challenge cohort")
    excluded = set(getattr(args, "skip_ids", None) or [])
    if not excluded.issubset(allowed):
        raise ValueError("Skipped IDs must be members of the configured challenge cohort")
    identifiers = [i for i in identifiers if i not in excluded]
    requested = list(identifiers)
    carried = set(selection["already_solved_ids"]) if selection else set()
    failed = set(selection.get("already_failed_ids", [])) if selection else set()
    completed = set()
    if not args.from_scratch:
        selected = len(identifiers)
        completed = prior_completed_ids(label, args.per_instance) | carried | failed
        identifiers = [i for i in identifiers if i not in completed]
        if len(identifiers) < selected:
            print(json.dumps({"resume": True, "selected": selected,
                              "skipped_completed": selected - len(identifiers),
                              "remaining": len(identifiers)}, ensure_ascii=False))
    if args.dry_run:
        result = {"dry_run": True, "model_calls_made": 0, "model": SETTINGS["model"],
                  "experiment": EXPERIMENT, "cohort_count": len(allowed), "selected_count": len(requested),
                  "carried_success_count": len(set(requested) & carried) if not args.from_scratch else 0,
                  "carried_failure_count": len(set(requested) & failed) if not args.from_scratch else 0,
                  "skipped_completed_count": len(set(requested) & completed),
                  "remaining_count": len(identifiers), "num_workers": min(args.num_workers, len(identifiers)),
                  "per_instance_cost_limit_usd": args.per_instance, "total_cost_limit_usd": args.total,
                  "pending_ids": identifiers, "skipped_ids": [i for i in requested if i in completed],
                  "excluded_ids": sorted(excluded),
                  "categories": dict(Counter(r["category"] for r in records if r["id"] in identifiers)),
                  "task_selection": SETTINGS.get("task_selection")}
        GENERATED.mkdir(parents=True, exist_ok=True)
        write_json_atomic(GENERATED / "selection-preview.json", result)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if not identifiers:
        print(json.dumps({"remaining": 0,
                          "message": "All selected tasks already have completed results; nothing to run."},
                         ensure_ascii=False))
        return 0
    if args.run and args.num_workers > 1 and not args.reuse_inventory:
        return run_in_parallel(records, args.image, args.per_instance, args.total,
                               identifiers, args.num_workers, args.from_scratch)
    return pilot(records, args.image, args.per_instance, args.total, identifiers, label=label)


if __name__ == "__main__":
    def stop_signal(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, stop_signal)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted; rerun with the same experiment ID to resume.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        # Avoid printing credential-bearing exception messages or traceback locals.
        print(f"Preparation failed: {type(e).__name__}", file=sys.stderr)
        sys.exit(2)
