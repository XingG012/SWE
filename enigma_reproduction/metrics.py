"""Passive metrics; never changes an agent action, observation or prompt."""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path


class StepMetrics:
    # The v0.7 hook interface is duck typed. Missing callbacks remain no-ops.
    def __init__(self, path: Path, *, process_logger=None):
        self.path = path
        self.process_logger = process_logger
        self.previous = {"tokens_sent": 0, "tokens_received": 0, "api_calls": 0, "instance_cost": 0}
        self.steps = 0
        self.tools = Counter()
        self.start = None
        self.action_and_summary_seconds = 0.0

    def __getattr__(self, name):
        if name.startswith("on_"):
            return lambda **kwargs: None
        raise AttributeError(name)

    def on_step_start(self):
        self.start = time.perf_counter()

    def on_step_done(self, *, trajectory_step, model_stats):
        self.steps += 1
        action = trajectory_step.get("action", "")
        first = action.strip().split(maxsplit=1)[0] if action.strip() else ""
        tool = first if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", first) else "shell_expression"
        self.tools[tool] += 1
        stats = model_stats.to_dict()
        delta = {key: stats[key] - value for key, value in self.previous.items()}
        self.previous = {key: stats[key] for key in self.previous}
        self.action_and_summary_seconds += trajectory_step.get("execution_time", 0.0)
        observation = trajectory_step.get("observation") or ""
        row = {"step": self.steps, "tool": tool,
               "wall_seconds": time.perf_counter() - self.start if self.start is not None else None,
               "action_and_summary_seconds": trajectory_step.get("execution_time"),
               "observation_characters": len(observation), "observation_lines": len(observation.splitlines()),
               "response_characters": len(trajectory_step.get("response") or ""),
               "tokens_sent": delta["tokens_sent"], "tokens_received": delta["tokens_received"],
               "api_calls": delta["api_calls"], "cost_upper_usd": delta["instance_cost"],
               "cumulative_cost_upper_usd": stats["instance_cost"]}
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        if self.process_logger is not None:
            self.process_logger.info("STEP %s\n%s", self.steps, json.dumps(
                {"metrics": row, "trajectory_step": trajectory_step}, ensure_ascii=False))
