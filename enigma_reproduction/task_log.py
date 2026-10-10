"""One readable log per task, with final metrics before the streamed process.

This module only handles files; it never changes prompts, tools or agent state.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sweagent.utils.secrets import redact


def safe_name(value: str) -> str:
    value = re.sub(r'[\s\\/:*?"<>|\x00-\x1f\x7f]+', "_", redact(value))
    return value.strip("._").encode("utf-8")[:160].decode("utf-8", errors="ignore") or "challenge"


class TaskLog:
    def __init__(self, root: Path, *, model: str, name: str, identifier: str,
                 category: str, budget: float):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        directory = root / safe_name(model)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.path = directory / f"{safe_name(name)}_{now.strftime('%Y%m%d_%H%M%S_%f')}.log"
        self.metadata = {"name": name, "id": identifier, "category": category,
                         "model": model, "per_instance_cost_limit_usd": budget,
                         "started_at": now.isoformat(timespec="seconds")}
        header = self._header({"exit_status": "running"}).encode("utf-8")
        self.process_offset = len(header)
        # Exclusive creation preserves earlier logs even for repeated runs.
        with os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(header)

    def _header(self, metrics: dict) -> str:
        values = {**self.metadata, **metrics}
        groups = (
            ("题目", ("name", "id", "category", "model")),
            ("结果", ("exit_status", "solved", "accounting_complete")),
            ("费用与预算（USD）", ("per_instance_cost_limit_usd", "cost_upper_usd", "cost_lower_usd",
                              "known_cost_upper_usd", "known_cost_lower_usd", "pricing_accounting")),
            ("步骤与模型调用", ("steps", "api_calls", "by_role")),
            ("API 重试与隔离", ("successful_api_calls", "failed_api_calls", "api_failure_exhausted", "api_request_failed",
                               "last_api_error", "ctf_project", "ctf_network", "exclusive_host_network")),
            ("Token 与缓存", ("prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_hit_ratio")),
            ("耗时（秒）", ("wall_seconds", "initialization_seconds", "api_latency_seconds",
                         "action_and_summary_seconds")),
            ("时间（Asia/Shanghai）", ("started_at", "finished_at")),
            ("工具调用", ("tool_counts",)),
            ("诊断与原始数据", ("error_type", "error_message", "cleanup_errors", "trajectory_dir",
                           "usage_ledger", "step_metrics", "process_log_missing")),
        )
        lines = ["=== 主要指标（null 表示尚未统计或未知；费用按配置价格估算） ==="]
        for label, keys in groups:
            lines.append(label + ": " + json.dumps({k: values.get(k) for k in keys}, ensure_ascii=False))
        lines.append("=== 解题过程 ===")
        return redact("\n".join(lines) + "\n")

    def finalize(self, metrics: dict) -> None:
        """Replace the initial metrics only, after the logging handler is closed.

        Copy the process in chunks, so large challenge outputs are not buffered.
        Atomic replacement keeps a readable log even if finalization is interrupted.
        """
        metrics = {**metrics, "finished_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            source = self.path.open("rb")
        except FileNotFoundError:
            source = None
            metrics["process_log_missing"] = True
        descriptor, temporary = tempfile.mkstemp(prefix=".task-log-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as target:
                target.write(self._header(metrics).encode("utf-8"))
                if source is not None:
                    source.seek(self.process_offset)
                    shutil.copyfileobj(source, target)
                else:
                    target.write("原始过程日志不存在；本文件仅恢复最终指标。\n".encode("utf-8"))
            os.replace(temporary, self.path)
        finally:
            if source is not None:
                source.close()
            if os.path.exists(temporary):
                os.unlink(temporary)
