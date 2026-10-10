"""Qwen3-Max streaming adapter for the configured Bailian deployment."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
from openai import OpenAI

from sweagent.agent.deepseek_flash import DeepSeekFlashModel, model_settings
from sweagent.utils.config import keys_config
from sweagent.utils.secrets import register_secret


class Qwen3MaxModel(DeepSeekFlashModel):
    MODELS = {"qwen3-max": {"max_context": 262_144,
              "cost_per_input_token": .359 / 1_000_000,
              "cost_per_output_token": 1.434 / 1_000_000}}

    def _setup_client(self):
        self.settings = model_settings(self.api_model, "qwen3_max_settings.json")
        self._setup_pricing()
        base_url = keys_config.get("DASHSCOPE_API_BASE_URL", self.settings["base_url"])
        if base_url.rstrip("/") != self.settings["base_url"].rstrip("/"):
            raise ValueError("Qwen3-Max requires the endpoint pinned in the selected settings")
        key = keys_config["DASHSCOPE_API_KEY"]
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Missing Bailian credential")
        if not isinstance(self.settings["enable_thinking"], bool):
            raise ValueError("enable_thinking must be a boolean")
        register_secret(key)
        self._disable_http_logging()
        self.client = OpenAI(api_key=key, base_url=base_url, max_retries=0, timeout=300.0,
                             http_client=httpx.Client(follow_redirects=False))
        self.budget_source = None
        self.accounting_role = "primary"
        self._rates = self.pricing["peak"].copy()
        self._reasoning_tokens = None

    def _request(self, messages):
        self._reasoning_tokens = None
        # Usage is a separate final chunk with no choices. Consume it before
        # returning the assembled answer to the original agent/parser.
        stream = self.client.chat.completions.create(
            model=self.api_model, messages=messages,
            temperature=self.args.temperature, top_p=self.args.top_p,
            max_tokens=self.settings["max_output_tokens"],
            extra_body={"enable_thinking": self.settings["enable_thinking"]},
            stream=True, stream_options={"include_usage": True},
        )
        content = []
        usage = None
        finish_reason = None
        response_model = self.api_model
        try:
            for chunk in stream:
                response_model = getattr(chunk, "model", None) or response_model
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage
                for choice in chunk.choices:
                    if choice.index != 0:
                        continue
                    delta = getattr(choice.delta, "content", None)
                    if delta:
                        content.append(delta)
                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason
        finally:
            stream.close()
        self._reasoning_tokens = getattr(getattr(usage, "completion_tokens_details", None),
                                         "reasoning_tokens", None)
        return SimpleNamespace(model=response_model, usage=usage, choices=[
            SimpleNamespace(finish_reason=finish_reason,
                            message=SimpleNamespace(content="".join(content)))])

    def _costs(self, prompt, completion, hit):
        # The tier depends on this request's entire input, including cached
        # tokens. Conservatively estimate all input at the standard list rate;
        # deployment discounts/contract prices are not available from usage.
        tier = next(t for t in self.pricing["context_tiers"] if prompt <= t["max_input_tokens"])
        self._rates = {"cache_hit": tier["input"], "cache_miss": tier["input"], "output": tier["output"]}
        cost = (prompt * tier["input"] + completion * tier["output"]) / 1_000_000
        return {"peak": cost, "off_peak": cost}

    def _ledger(self, data):
        super()._ledger({"base_url": self.settings["base_url"],
                         "enable_thinking": self.settings["enable_thinking"],
                         "pricing_rates": self._rates,
                         "reasoning_tokens": self._reasoning_tokens, **data})
