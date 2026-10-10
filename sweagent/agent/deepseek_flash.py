"""OpenAI-compatible Flash adapter; retain v0.7 message construction and ACI."""
from __future__ import annotations

import json
import logging
import math
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from openai import APIConnectionError, APIStatusError, BadRequestError, OpenAI

from sweagent import REPO_ROOT
from sweagent.agent.models import ContextWindowExceededError, CostLimitExceededError, OpenAIModel
from sweagent.utils.config import keys_config
from sweagent.utils.secrets import redact, register_secret


def model_settings(model: str, fallback: str) -> dict:
    selected = os.environ.get("ENIGMA_MODEL_SETTINGS")
    path = Path(selected) if selected else REPO_ROOT / "enigma_reproduction/settings.json"
    settings = json.loads(path.read_text())
    if not selected and settings["model"] != model:
        settings = json.loads((REPO_ROOT / "enigma_reproduction" / fallback).read_text())
    if settings["model"] != model:
        raise ValueError("Model settings do not match the requested model")
    return settings


class DeepSeekFlashModel(OpenAIModel):
    MODELS = {"deepseek-flash": {"max_context": 1_000_000,
              "cost_per_input_token": 0.3 / 1_000_000,
              "cost_per_output_token": 1.2 / 1_000_000}}
    SHORTCUTS = {}
    strict_accounting = True

    def _setup_client(self):
        self.settings = model_settings(self.api_model, "deepseek_settings.json")
        self._setup_pricing()
        base_url = keys_config.get("DEEPSEEK_API_BASE_URL", self.settings["base_url"])
        if base_url.rstrip("/") != "https://api.deepseek.com":
            raise ValueError("This reproduction only permits the official DeepSeek endpoint")
        key = keys_config["DEEPSEEK_API_KEY"]
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Missing DeepSeek credential")
        register_secret(key)
        self._disable_http_logging()
        self.client = OpenAI(api_key=key, base_url=base_url, max_retries=0, timeout=120.0)
        self.budget_source = None
        self.accounting_role = "primary"

    def _setup_pricing(self):
        self.pricing = self.settings["pricing"]
        if self.pricing["unit"] != "per_million_tokens" or self.pricing["currency"] != "USD":
            raise ValueError("Flash pricing must be specified in USD per million tokens")
        for tier in ("peak", "off_peak"):
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
                   for v in self.pricing[tier].values()):
                raise ValueError("Flash pricing must be finite and strictly positive")
            if set(self.pricing[tier]) != {"cache_hit", "cache_miss", "output"}:
                raise ValueError("Incomplete Flash pricing")

    @staticmethod
    def _disable_http_logging():
        for name in ("openai", "openai._base_client", "httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
            logging.getLogger(name).disabled = True

    def _request(self, messages):
        return self.client.chat.completions.create(
            messages=messages, model=self.api_model,
            temperature=self.args.temperature, top_p=self.args.top_p,
            max_tokens=self.settings["max_output_tokens"],
            extra_body={"thinking": {"type": self.settings["thinking"]}},
        )

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
                  "pricing_verified_date": self.pricing["verified_date"],
                  "pricing_accounting": self.pricing["accounting"],
                  "pricing_rates": self.pricing["peak"], **data}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _integer(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def _costs(self, prompt, completion, hit):
        return {tier: (hit * rates["cache_hit"] + (prompt - hit) * rates["cache_miss"] +
                       completion * rates["output"]) / 1_000_000
                for tier, rates in ((t, self.pricing[t]) for t in ("peak", "off_peak"))}

    @staticmethod
    def _api_error_details(error):
        """Keep bounded, redacted diagnostic fields; never dump the response body."""
        body = getattr(error, "body", None)
        details = body.get("error", body) if isinstance(body, dict) else {}
        if not isinstance(details, dict):
            details = {}
        values = {"api_error_code": getattr(error, "code", None) or details.get("code"),
                  "api_error_param": getattr(error, "param", None) or details.get("param"),
                  "api_error_type": getattr(error, "type", None) or details.get("type"),
                  "api_error_message": details.get("message")}
        return {key: " ".join(redact(str(value)).split())[:500 if key == "api_error_message" else 160]
                for key, value in values.items() if isinstance(value, (str, int)) and str(value)}

    def _request_with_retries(self, messages):
        policy = self.settings.get("api_retry", {})
        attempts = policy.get("max_attempts", 4)
        delay = policy.get("initial_delay_seconds", 1.0)
        cap = policy.get("max_delay_seconds", 10.0)
        if not isinstance(attempts, int) or not 1 <= attempts <= 10:
            raise ValueError("API max_attempts must be between 1 and 10")
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in (delay, cap)):
            raise ValueError("API retry delays must be finite and nonnegative")
        for attempt in range(1, attempts + 1):
            self._check_budget()
            started = time.perf_counter()
            try:
                response = self._request(messages)
                return response, time.perf_counter() - started, attempt
            except Exception as error:
                cause = error
                seen = set()
                causes = []
                while cause is not None and id(cause) not in seen:
                    seen.add(id(cause))
                    causes.append(type(cause).__name__)
                    root = cause
                    cause = cause.__cause__
                status = getattr(error, "status_code", None)
                retryable = isinstance(error, APIConnectionError) or (
                    isinstance(error, APIStatusError) and
                    (status in (408, 409, 429) or isinstance(status, int) and status >= 500))
                # A failed connect/pool acquisition cannot have sent the model request.
                unsent = isinstance(error, APIConnectionError) and any(
                    name in {"ConnectError", "ConnectTimeout", "PoolTimeout"} for name in causes)
                again = retryable and attempt < attempts
                record = {"accounting_complete": unsent, "request_succeeded": False,
                          "error": type(error).__name__, "cause_types": causes,
                          "transport_errno": getattr(root, "errno", None),
                          "http_status": status if isinstance(status, int) else None,
                          "attempt": attempt, "retryable": retryable, "will_retry": again,
                          "api_failure_exhausted": retryable and not again,
                          "latency_seconds": time.perf_counter() - started,
                          "cost_upper_usd": 0.0 if unsent else None,
                          "cost_lower_usd": 0.0 if unsent else None,
                          "cost_unknown": not unsent}
                record.update(self._api_error_details(error))
                request_id = getattr(error, "request_id", None)
                if isinstance(request_id, str):
                    record["request_id"] = redact(request_id)[:128]
                # Only the selected redacted error fields, never full bodies or headers.
                self._ledger(record)
                if isinstance(error, BadRequestError) and getattr(error, "code", None) == "context_length_exceeded":
                    raise ContextWindowExceededError("Flash context limit exceeded") from None
                if not again:
                    detail = "; ".join(f"{key}={record[key]}" for key in
                                       ("http_status", "api_error_code", "api_error_param", "api_error_message")
                                       if record.get(key) is not None)
                    raise RuntimeError(f"Model API request failed ({type(error).__name__}); "
                                       f"attempts={attempt}, cost_unknown={not unsent}" +
                                       (f"; {detail}" if detail else "")) from None
                wait = min(cap, delay * 2 ** (attempt - 1)) * random.uniform(.8, 1.0)
                logging.getLogger(__name__).warning(
                    "Transient model API error %s (status=%s); retry %s/%s in %.2fs",
                    type(error).__name__, record["http_status"], attempt + 1, attempts, wait)
                time.sleep(wait)

    def query(self, history):
        self._check_budget()
        messages = self.history_to_messages(history)
        if any(redact(item["content"]) != item["content"] for item in messages):
            raise RuntimeError("Credential-like content detected in model input; request blocked")
        response, latency, attempt = self._request_with_retries(messages)

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
        costs = self._costs(prompt, completion, hit)
        # completion_tokens includes reasoning tokens. Do not add them a second time.
        self.stats.total_cost += costs["peak"]
        self.stats.instance_cost += costs["peak"]
        self.stats.tokens_sent += prompt
        self.stats.tokens_received += completion
        self.stats.api_calls += 1
        self._ledger({"accounting_complete": True, "response_model": redact(str(response.model)),
                      "request_succeeded": True, "attempt": attempt,
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
