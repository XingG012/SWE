#!/usr/bin/env python3
"""Prepare, check and run NYU EnIGMA using the unchanged category configurations."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SETTINGS = json.loads((ROOT / "enigma_reproduction/settings.json").read_text())
GENERATED = ROOT / "enigma_reproduction/generated"
CATEGORIES = {"crypto", "forensics", "misc", "pwn", "rev", "web"}
os.environ["TOGETHER_NO_BANNER"] = "1"
os.environ.pop("OPENAI_LOG", None)
os.environ.setdefault("SWE_AGENT_LOG_STREAM_LEVEL", "INFO")
os.environ.setdefault("SWE_AGENT_LOG_FILE_LEVEL", "INFO")


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
    return ROOT / ".secrets/deepseek_api_key"


def check_credential():
    if os.environ.get("DEEPSEEK_API_KEY", "").strip():
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
                                 ":(exclude)sweagent/utils/secrets.py"], cwd=ROOT, capture_output=True)
    working_diff = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=ROOT, capture_output=True)
    return {"upstream_commit": base, "runtime_commit": head.stdout.strip(),
            "v0_7_ancestor": head.returncode == 0 and ancestry.returncode == 0,
            "original_method_files_unchanged": comparison.returncode == 0,
            "working_tree_diff_sha256": hashlib.sha256(working_diff.stdout).hexdigest()
                if working_diff.returncode == 0 else None}


def preflight(image):
    problems = []
    repository = repository_status()
    if not repository["v0_7_ancestor"]:
        problems.append("项目不是固定的官方 v0.7 commit 或其复现补丁后继")
    if not repository["original_method_files_unchanged"]:
        problems.append("官方方法文件与固定的 v0.7 基线不同；只允许已记录的接口兼容与计费补丁")
    if not check_credential():
        problems.append("未加载 DEEPSEEK_API_KEY，或 .secrets/deepseek_api_key 不存在/权限不是私密")
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
            args = AgentArguments(model=ModelArguments(model_name="deepseek-flash"),
                                  config_file=ROOT / f"config/ctf/ctf_{category}.yaml")
            if args.config.summarizer_config.function.__class__.__name__ != "LMSummarizer":
                problems.append("官方类别配置的摘要模块不符合完整 EnIGMA")
            for demo in args.config.demonstrations:
                if not Path(demo).is_file():
                    problems.append("官方类别示例缺失")
    except Exception as e:
        problems.append(f"Python 依赖或官方配置导入失败（{type(e).__name__}）")
    report = {"ready": not problems, "blockers": problems, "repository": repository,
              "resolved_image": resolved_image,
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


def prior_completed_ids(label):
    """IDs already recorded as completed valid attempts in earlier `label` batches.

    A task counts as done when a previous results.json entry has complete accounting
    and a non-infrastructure exit status. The interrupted task is never appended to
    results.json, so it is re-attempted on resume; completed failed attempts stay
    skipped to preserve pass@1 accounting. Pilot and formal runs are kept separate.
    """
    done = set()
    for path in GENERATED.glob(f"{label}-*/results.json"):
        try:
            rows = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for row in rows:
            if (row.get("accounting_complete") is True
                    and row.get("exit_status") != "infrastructure_error"):
                done.add(row.get("id"))
    return done


def pilot(records, image, per_instance, total, identifiers, label="pilot"):
    report = preflight(image)
    if not report["ready"]:
        return 2
    if not os.environ.get("DEEPSEEK_API_KEY"):
        os.environ["DEEPSEEK_API_KEY"] = credential_path().read_text().strip()
    # Do not import any Ours module, config, log or prompt.
    from run import ActionsArguments, Main, ScriptArguments
    from sweagent.agent.agents import AgentArguments
    from sweagent.agent.models import ModelArguments
    from sweagent.environment.swe_env import EnvironmentArguments
    from sweagent.utils.secrets import redact
    from sweagent.utils.log import get_logger, remove_file_handlers
    from enigma_reproduction.metrics import StepMetrics
    from enigma_reproduction.task_log import TaskLog
    import docker

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
        "inventory_sha256": sha(GENERATED / "inventory.json")}, indent=2) + "\n")
    results = []
    spent = 0.0
    client = docker.from_env(timeout=5)
    if not client.networks.list(names=["ctfnet"]):
        client.networks.create("ctfnet")
    client.close()
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
            agent=AgentArguments(model=ModelArguments(model_name="deepseek-flash",
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
        metrics = StepMetrics(directory / f"{identifier}-steps.jsonl",
                              process_logger=get_logger("enigma-task-process"))
        started = time.perf_counter()
        result = {"id": identifier, "name": row["name"], "category": row["category"], "solved": False,
                  "exit_status": "interrupted", "log_file": str(task_log.path),
                  "usage_ledger": str(ledger), "step_metrics": str(metrics.path),
                  "per_instance_cost_limit_usd": min(per_instance, remaining)}
        try:
            main = Main(args, log_path=task_log.path)
            result["initialization_seconds"] = time.perf_counter() - started
            main.agent.add_hook(metrics)
            main.run(0)
            result["exit_status"] = main.agent.info.get("exit_status")
            # The official env assigns 'submitted' only after validate_submission succeeds.
            result["solved"] = result["exit_status"] == "submitted"
            result["trajectory_dir"] = str(main.traj_dir)
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
            remove_file_handlers()
            result.update(usage_summary(ledger))
            result.update({"wall_seconds": time.perf_counter() - started, "steps": metrics.steps,
                           "tool_counts": dict(metrics.tools),
                           "action_and_summary_seconds": metrics.action_and_summary_seconds})
            task_log.finalize(result)
        spent += result["known_cost_upper_usd"]
        results.append(result)
        (directory / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps(result, ensure_ascii=False))
        if not result["accounting_complete"] or result["exit_status"] in {"infrastructure_error", "exit_error"}:
            break
    solved_costs = [r["cost_upper_usd"] for r in results if r["solved"] and r["accounting_complete"]]
    valid = [r for r in results if r["accounting_complete"] and r["exit_status"] != "infrastructure_error"]
    summary = {"attempted": len(results), "solved": len(solved_costs),
               "selected_count": len(identifiers),
               "valid_attempted": len(valid),
               "coverage": len(valid) / len(identifiers),
               "success_rate_on_valid_attempted": len(solved_costs) / len(valid) if valid else None,
               "full_test_pass_at_1": len(solved_costs) / 200
                   if label == "run" and len(valid) == len(identifiers) == 200 else None,
               "accounting_complete": all(r["accounting_complete"] for r in results),
               "cost_upper_usd": spent if all(r["accounting_complete"] for r in results) else None,
               "known_cost_upper_usd": spent, "cost_is_peak_rate_upper_bound": True,
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
               "max_solved_cost_upper_usd": max(solved_costs) if solved_costs else None,
               "scope": "small diagnostic sample, not a full benchmark result" if label == "pilot" else "NYU test run"}
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--pilot", action="store_true")
    mode.add_argument("--run", action="store_true", help="Run selected IDs, or all 200 NYU test tasks")
    parser.add_argument("--image", default=SETTINGS["image"])
    parser.add_argument("--per-instance", type=float, default=None)
    parser.add_argument("--total", type=float, default=None)
    parser.add_argument("--ids", nargs="+", default=None)
    parser.add_argument("--from-scratch", action="store_true",
                        help="Ignore prior completed tasks and run the full selected set again")
    args = parser.parse_args()
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
    records = prepare()
    if args.preflight:
        return 0 if preflight(args.image)["ready"] else 2
    if args.pilot or args.run:
        label = "pilot" if args.pilot else "run"
        identifiers = args.ids or (SETTINGS["pilot_ids"] if args.pilot else [r["id"] for r in records])
        if not args.from_scratch:
            selected = len(identifiers)
            identifiers = [i for i in identifiers if i not in prior_completed_ids(label)]
            if len(identifiers) < selected:
                print(json.dumps({"resume": True, "selected": selected,
                                  "skipped_completed": selected - len(identifiers),
                                  "remaining": len(identifiers)}, ensure_ascii=False))
        if not identifiers:
            print(json.dumps({"remaining": 0,
                              "message": "All selected tasks already have completed results; nothing to run."},
                             ensure_ascii=False))
            return 0
        return pilot(records, args.image, args.per_instance, args.total, identifiers, label=label)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # Avoid printing credential-bearing exception messages or traceback locals.
        print(f"Preparation failed: {type(e).__name__}", file=sys.stderr)
        sys.exit(2)
