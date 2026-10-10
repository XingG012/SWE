"""Claude Sonnet 5 through x1api's OpenAI-compatible endpoint."""
from __future__ import annotations

import httpx
from openai import OpenAI

from sweagent.agent.deepseek_flash import DeepSeekFlashModel, model_settings
from sweagent.utils.config import keys_config
from sweagent.utils.secrets import register_secret


class ClaudeSonnet5Model(DeepSeekFlashModel):
    MODELS = {"claude-sonnet-5": {"max_context": 1_000_000,
              "cost_per_input_token": 2.0 / 1_000_000,
              "cost_per_output_token": 10.0 / 1_000_000}}

    def _setup_client(self):
        self.settings = model_settings(self.api_model, "claude_settings.json")
        self._setup_pricing()
        base_url = keys_config.get("X1API_API_BASE_URL", self.settings["base_url"])
        if base_url.rstrip("/") != "https://x1api.top/v1":
            raise ValueError("Sonnet 5 reproduction requires the configured x1api /v1 endpoint")
        key = keys_config["X1API_API_KEY"]
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Missing x1api credential")
        register_secret(key)
        self._disable_http_logging()
        self.client = OpenAI(api_key=key, base_url=base_url, max_retries=0, timeout=120.0,
                             http_client=httpx.Client(follow_redirects=False))
        self.budget_source = None
        self.accounting_role = "primary"
        self._cache_write_tokens = 0

    def _request(self, messages):
        # Sonnet 5 rejects non-default sampling settings. Let the provider use
        # its defaults, including adaptive thinking, without GPT/DeepSeek fields.
        response = self.client.chat.completions.create(
            model=self.api_model, messages=messages,
            max_tokens=self.settings["max_output_tokens"],
        )
        usage = getattr(response, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        self._cache_write_tokens = getattr(usage, "cache_creation_input_tokens", None)
        if self._cache_write_tokens is None:
            self._cache_write_tokens = getattr(details, "cache_creation_tokens", 0)
        return response

    def _costs(self, prompt, completion, hit):
        write = self._cache_write_tokens
        if not self._integer(write) or write > prompt - hit:
            self._ledger({"accounting_complete": False, "error": "InvalidCacheWriteUsage",
                          "cost_upper_usd": None})
            raise RuntimeError("Sonnet 5 cache write usage invalid; stopping")
        costs = super()._costs(prompt, completion, hit)
        # If the proxy reports writes but omits their TTL, bound both durations.
        costs["peak"] += write * (self.pricing["cache_write_1h"] - self.pricing["peak"]["cache_miss"]) / 1_000_000
        costs["off_peak"] += write * (self.pricing["cache_write_5m"] - self.pricing["off_peak"]["cache_miss"]) / 1_000_000
        return costs

    def _ledger(self, data):
        super()._ledger({"base_url": self.settings["base_url"],
                         "sampling_mode": "provider_default",
                         "thinking": "provider_default_adaptive",
                         "cache_write_tokens": self._cache_write_tokens, **data})
