"""Gemini Flash through TeamoRouter's OpenAI-compatible /v1 API."""
from __future__ import annotations

import httpx
from openai import OpenAI

from sweagent.agent.deepseek_flash import DeepSeekFlashModel, model_settings
from sweagent.utils.config import keys_config
from sweagent.utils.secrets import register_secret


class GeminiFlashModel(DeepSeekFlashModel):
    MODELS = {"gemini-3.6-flash": {
        "max_context": 1_000_000,
        "cost_per_input_token": .3 / 1_000_000,
        "cost_per_output_token": 1.5 / 1_000_000,
    }}

    def _setup_client(self):
        self.settings = model_settings(self.api_model, "gemini_settings.json")
        self._setup_pricing()
        base_url = keys_config.get("TEAMOROUTER_API_BASE_URL", self.settings["base_url"])
        if base_url.rstrip("/") != "https://api.teamorouter.com/v1":
            raise ValueError("Gemini reproduction requires the configured TeamoRouter /v1 endpoint")
        key = keys_config["TEAMOROUTER_API_KEY"]
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Missing TeamoRouter credential")
        register_secret(key)
        self._disable_http_logging()
        self.client = OpenAI(api_key=key, base_url=base_url, max_retries=0, timeout=120.0,
                             http_client=httpx.Client(follow_redirects=False))
        self.budget_source = None
        self.accounting_role = "primary"

    def _request(self, messages):
        return self.client.chat.completions.create(
            messages=messages, model=self.api_model,
            temperature=self.args.temperature, top_p=self.args.top_p,
            max_tokens=self.settings["max_output_tokens"],
        )
