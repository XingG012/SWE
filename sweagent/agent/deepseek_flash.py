"""OpenAI-compatible Flash adapter; retain v0.7 message construction and ACI."""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from openai import BadRequestError, OpenAI

from sweagent import REPO_ROOT
from sweagent.agent.models import ContextWindowExceededError, CostLimitExceededError, OpenAIModel
from sweagent.utils.config import keys_config
from sweagent.utils.secrets import redact, register_secret


class DeepSeekFlashModel(OpenAIModel):
    MODELS = {"deepseek-flash": {"max_context": 1_000_000,
              "cost_per_input_token": 0.3 / 1_000_000,
              "cost_per_output_token": 1.2 / 1_000_000}}
    SHORTCUTS = {}
    strict_accounting = True

    def _setup_client(self):
        self.settings = json.loads((REPO_ROOT / "enigma_reproduction/settings.json").read_text())
        self.pricing = self.settings["pricing"]
        if self.pricing["unit"] != "per_million_tokens" or self.pricing["currency"] != "USD":
            raise ValueError("Flash pricing must be specified in USD per million tokens")
        for tier in ("peak", "off_peak"):
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
                   for v in self.pricing[tier].values()):
                raise ValueError("Flash pricing must be finite and strictly positive")
            if set(self.pricing[tier]) != {"cache_hit", "cache_miss", "output"}:
                raise ValueError("Incomplete Flash pricing")
        base_url = keys_config.get("DEEPSEEK_API_BASE_URL", self.settings["base_url"])
        if base_url.rstrip("/") != "https://api.deepseek.com":
            raise ValueError("This reproduction only permits the official DeepSeek endpoint")
        key = keys_config["DEEPSEEK_API_KEY"]
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Missing DeepSeek credential")
        register_secret(key)
        for name in ("openai", "openai._base_client", "httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
            logging.getLogger(name).disabled = True
        self.client = OpenAI(api_key=key, base_url=base_url, max_retries=0, timeout=120.0)
        self.budget_source = None
        self.accounting_role = "primary"

    def _check_budget(self):
        instance_cost = self.stats.instance_cost
        total_cost = self.stats.total_cost
        if self.budget_source is not None:
            instance_cost += self.budget_source.stats.instance_cost
            total_cost += self.budget_source.stats.total_cost
        if (0 < self.args.per_instance_cost_limit <= instance_cost or
                0 < self.args.total_cost_limit <= total_cost):
            raise CostLimitExceededError("Combined primary and summarizer cost limit reached")

    def _ledger(self, data):
        path = Path(os.getenv("ENIGMA_USAGE_LEDGER", str(REPO_ROOT / "results/enigma/usage.jsonl")))
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
                  "model": self.api_model, "role": self.accounting_role,
                  "pricing_verified_date": self.pricing["verified_date"], **data}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _integer(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def query(self, history):
        self._check_budget()
        messages = self.history_to_messages(history)
        if any(redact(item["content"]) != item["content"] for item in messages):
            raise RuntimeError("Credential-like content detected in model input; request blocked")
        started = time.perf_counter()
        try:
            response = self.client.chat.completions.create(
                messages=messages, model=self.api_model,
                temperature=self.args.temperature, top_p=self.args.top_p,
                max_tokens=self.settings["max_output_tokens"],
                extra_body={"thinking": {"type": self.settings["thinking"]}},
            )
        except BadRequestError as e:
            # Never expose server bodies, headers, request objects or exception chains.
            self._ledger({"accounting_complete": False, "error": "BadRequestError",
                          "latency_seconds": time.perf_counter() - started,
                          "cost_upper_usd": None})
            if getattr(e, "code", None) == "context_length_exceeded":
                raise ContextWindowExceededError("Flash context limit exceeded") from None
            raise RuntimeError("Flash request rejected; accounting incomplete, no automatic retry") from None
        except Exception as e:
            self._ledger({"accounting_complete": False, "error": type(e).__name__,
                          "latency_seconds": time.perf_counter() - started,
                          "cost_upper_usd": None})
            raise RuntimeError("Flash API request failed; accounting incomplete, no automatic retry") from None

        usage = getattr(response, "usage", None)
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        if not self._integer(prompt) or not self._integer(completion) or prompt + completion <= 0:
            self._ledger({"accounting_complete": False, "error": "MissingOrInvalidUsage",
                          "cost_upper_usd": None})
            raise RuntimeError("Flash returned no valid usage; cannot estimate cost, stopping")
        hit = getattr(usage, "prompt_cache_hit_tokens", None)
        if hit is None:
            hit = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0)
        if not self._integer(hit) or hit > prompt:
            self._ledger({"accounting_complete": False, "error": "InvalidCacheUsage",
                          "cost_upper_usd": None})
            raise RuntimeError("Flash cache usage invalid; stopping")
        miss = prompt - hit
        reported_miss = getattr(usage, "prompt_cache_miss_tokens", miss)
        if reported_miss != miss:
            self._ledger({"accounting_complete": False, "error": "InconsistentCacheUsage",
                          "cost_upper_usd": None})
            raise RuntimeError("Flash cache usage inconsistent; stopping")
        costs = {tier: (hit * rates["cache_hit"] + miss * rates["cache_miss"] +
                       completion * rates["output"]) / 1_000_000
                 for tier, rates in ((t, self.pricing[t]) for t in ("peak", "off_peak"))}
        # completion_tokens includes reasoning tokens. Do not add them a second time.
        self.stats.total_cost += costs["peak"]
        self.stats.instance_cost += costs["peak"]
        self.stats.tokens_sent += prompt
        self.stats.tokens_received += completion
        self.stats.api_calls += 1
        latency = time.perf_counter() - started
        self._ledger({"accounting_complete": True, "response_model": redact(str(response.model)),
                      "latency_seconds": latency,
                      "finish_reason": redact(str(response.choices[0].finish_reason)),
                      "messages_count": len(messages),
                      "input_characters": sum(len(m["content"]) for m in messages),
                      "output_characters": len(response.choices[0].message.content or ""),
                      "output_tokens_per_second": completion / latency if latency > 0 else None,
                      "prompt_tokens": prompt, "cache_hit_tokens": hit, "cache_miss_tokens": miss,
                      "completion_tokens": completion,
                      "cost_upper_usd": costs["peak"], "cost_lower_usd": costs["off_peak"]})
        self._check_budget()
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Flash returned empty content; usage charged, stopping")
        return redact(content)
