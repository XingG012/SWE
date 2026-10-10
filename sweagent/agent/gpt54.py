"""GPT-5.4 through TeamoRouter's OpenAI-compatible /v1 API, with shared budget accounting."""
from __future__ import annotations

import httpx
from openai import OpenAI

from sweagent.agent.deepseek_flash import DeepSeekFlashModel, model_settings
from sweagent.utils.config import keys_config
from sweagent.utils.secrets import register_secret


class GPT54Model(DeepSeekFlashModel):
    MODELS = {"gpt-5.4": {"max_context": 1_050_000,
              "cost_per_input_token": 2.5 / 1_000_000,
              "cost_per_output_token": 15.0 / 1_000_000}}

    def _setup_client(self):
        self.settings = model_settings(self.api_model, "gpt54_settings.json")
        self._setup_pricing()
        base_url = keys_config.get("TEAMOROUTER_API_BASE_URL", self.settings["base_url"])
        if base_url.rstrip("/") != "https://api.teamorouter.com/v1":
            raise ValueError("GPT-5.4 reproduction requires the configured TeamoRouter /v1 endpoint")
        key = keys_config["TEAMOROUTER_API_KEY"]
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Missing TeamoRouter credential")
        register_secret(key)
        self._disable_http_logging()
        effort = self.settings["reasoning_effort"]
        if effort not in {"none", "low", "medium", "high", "xhigh"}:
            raise ValueError("Invalid GPT-5.4 reasoning effort")
        self.client = OpenAI(api_key=key, base_url=base_url, max_retries=0, timeout=120.0,
                             http_client=httpx.Client(follow_redirects=False))
        self.budget_source = None
        self.accounting_role = "primary"
        self._long_context_session = False

    def _request(self, messages):
        kwargs = {"messages": messages, "model": self.api_model,
                  "max_completion_tokens": self.settings["max_output_tokens"],
                  "reasoning_effort": self.settings["reasoning_effort"]}
        # GPT-5.4 only accepts these sampling parameters with reasoning=none.
        if self.settings["reasoning_effort"] == "none":
            kwargs.update(temperature=self.args.temperature, top_p=self.args.top_p)
        return self.client.chat.completions.create(**kwargs)

    def _effective_rates(self, tier):
        rates = self.pricing[tier].copy()
        if self._long_context_session:
            for name in ("cache_hit", "cache_miss"):
                rates[name] *= self.pricing["long_context_input_multiplier"]
            rates["output"] *= self.pricing["long_context_output_multiplier"]
        return rates

    def _costs(self, prompt, completion, hit):
        self._long_context_session |= prompt > self.pricing["long_context_threshold"]
        return {tier: (hit * rates["cache_hit"] + (prompt-hit) * rates["cache_miss"] +
                       completion * rates["output"]) / 1_000_000
                for tier, rates in ((t, self._effective_rates(t)) for t in ("peak", "off_peak"))}

    def _ledger(self, data):
        super()._ledger({"base_url": self.settings["base_url"],
                         "reasoning_effort": self.settings["reasoning_effort"],
                         "long_context_session": self._long_context_session,
                         "pricing_rates": self._effective_rates("peak"), **data})
