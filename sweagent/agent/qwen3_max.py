"""Qwen3-Max streaming adapter for the configured Bailian deployment."""
from __future__ import annotations

import math
import re
from types import SimpleNamespace

import httpx
from openai import OpenAI

from sweagent.agent.deepseek_flash import DeepSeekFlashModel, model_settings
from sweagent.utils.config import keys_config
from sweagent.utils.secrets import redact, register_secret


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
        self._raw_response = ""
        self._finish_reason = None
        self._reference_cost = 0.0
        self._output_attempt = 1
        if self.settings.get("output_protocol", "legacy") not in {"legacy", "first_command_v1"}:
            raise ValueError("Unknown Qwen output protocol")
        thinking_budget = self.settings.get("thinking_budget")
        if thinking_budget is not None and (type(thinking_budget) is not int or not 1 <= thinking_budget <= 81920):
            raise ValueError("thinking_budget must be an integer between 1 and 81920")
        multiplier = self.pricing.get("implicit_cache_multiplier", 1.0)
        if type(multiplier) not in (int, float) or not math.isfinite(multiplier) or not 0 < multiplier <= 1:
            raise ValueError("implicit_cache_multiplier must be between 0 and 1")

    def _request(self, messages):
        self._reasoning_tokens = None
        self._raw_response = ""
        self._finish_reason = None
        # Usage is a separate final chunk with no choices. Consume it before
        # returning the assembled answer to the original agent/parser.
        extra_body = {"enable_thinking": self.settings["enable_thinking"]}
        if self.settings.get("thinking_budget") is not None and self.settings["enable_thinking"]:
            extra_body["thinking_budget"] = self.settings["thinking_budget"]
        stop = self.settings.get("stop_sequences") if self.accounting_role == "primary" else None
        kwargs = {"stop": stop} if stop else {}
        stream = self.client.chat.completions.create(
            model=self.api_model, messages=messages,
            temperature=self.args.temperature, top_p=self.args.top_p,
            max_tokens=self.settings["max_output_tokens"],
            extra_body=extra_body,
            stream=True, stream_options={"include_usage": True},
            **kwargs,
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
        self._raw_response = "".join(content)
        self._finish_reason = finish_reason
        return SimpleNamespace(model=response_model, usage=usage, choices=[
            SimpleNamespace(finish_reason=finish_reason,
                            message=SimpleNamespace(content=self._raw_response))])

    @staticmethod
    def first_command(content):
        """Return only the first complete action; simulated later turns never
        enter the original parser or the assistant's subsequent history.

        Keep the official discussion and first action. Never concatenate code
        blocks, select a later action, or complete a truncated command.
        """
        clean = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
        clean = clean.replace("</think>", "")
        opening = re.search(r"^```([^\n`]*)\n", clean, re.MULTILINE)
        if opening is None:
            raise ValueError("missing_command")
        closing = re.search(r"^```[ \t]*$", clean[opening.end():], re.MULTILINE)
        if closing is None:
            raise ValueError("incomplete_command")
        prefix = clean[:opening.start()]
        command = clean[opening.end():opening.end() + closing.start()]
        if re.search(r"<think>|^\(Open file:|^\(Current directory:|^bash-\$", prefix, re.MULTILINE):
            raise ValueError("simulated_observation_before_command")
        if not command.strip():
            raise ValueError("empty_command")
        if opening.group(1).strip().lower() not in {"", "bash", "sh", "shell", "text"}:
            raise ValueError("non_shell_code_block")
        if re.search(r"^\s*(?:</?think>|\(Open file:|\(Current directory:|bash-\$|def \w+\(|class \w+|from .+ import |import \w+|print\()",
                     command, re.MULTILINE) and not re.search(
                         r"^\s*(?:edit\s|python\S*\s|sage\s|\S+\s.*<<)", command):
            raise ValueError("non_shell_command")
        return (prefix.rstrip() + "\n```\n" + command + "```").strip()

    def query(self, history):
        if self.accounting_role != "primary" or self.settings.get("output_protocol") != "first_command_v1":
            return super().query(history)
        attempts = self.settings.get("output_retry_attempts", 2)
        if type(attempts) is not int or not 1 <= attempts <= 3:
            raise ValueError("output_retry_attempts must be between 1 and 3")
        request_history = list(history)
        for attempt in range(1, attempts + 1):
            self._output_attempt = attempt
            try:
                raw = super().query(request_history)
            except RuntimeError as error:
                # Empty successful responses have already been charged. Do not
                # retry unknown usage, API failures, or other runtime errors.
                if str(error) != "Flash returned empty content; usage charged, stopping":
                    raise
                raw = ""
            try:
                return self.first_command(raw)
            except ValueError as error:
                reason = str(error)
            if attempt < attempts:
                request_history.append({"role": "user", "agent": "primary", "content":
                    "Your last reply did not contain a complete executable shell action. "
                    "Return one DISCUSSION and one fenced shell command, then stop. "
                    "Wait for real tool feedback; do not simulate commands, terminal output or flags. "
                    "Use the current state and observation above. Do not repeat the terminal prompt."})
        raise RuntimeError(f"Qwen returned no usable single command after {attempts} charged requests ({reason})")

    def _costs(self, prompt, completion, hit):
        # Select the tier using all input, then discount only reported cached
        # tokens. Preserve the no-discount reference estimate alongside it.
        tier = next(t for t in self.pricing["context_tiers"] if prompt <= t["max_input_tokens"])
        self._rates = {"cache_hit": tier["input"] * self.pricing.get("implicit_cache_multiplier", 1.0),
                       "cache_miss": tier["input"], "output": tier["output"]}
        self._reference_cost = (prompt * tier["input"] + completion * tier["output"]) / 1_000_000
        cost = (hit * self._rates["cache_hit"] + (prompt - hit) * tier["input"] +
                completion * tier["output"]) / 1_000_000
        return {"peak": cost, "off_peak": cost}

    def _ledger(self, data):
        details = {}
        if data.get("request_succeeded"):
            details["reference_cost_without_cache_discount_usd"] = self._reference_cost
            budget = self.settings.get("thinking_budget")
            if budget is not None and self._integer(self._reasoning_tokens):
                details["thinking_budget_honored"] = self._reasoning_tokens <= budget + 10
            if self.accounting_role == "primary" and self.settings.get("output_protocol") == "first_command_v1":
                details["raw_response"] = redact(self._raw_response)
                try:
                    normalized = self.first_command(self._raw_response)
                    details.update(response_normalization="first_complete_command",
                                   normalized_response_characters=len(normalized))
                except ValueError as error:
                    details["response_normalization"] = str(error)
        super()._ledger({"base_url": self.settings["base_url"],
                         "enable_thinking": self.settings["enable_thinking"],
                         "thinking_budget": self.settings.get("thinking_budget"),
                         "output_protocol": self.settings.get("output_protocol", "legacy"),
                         "output_attempt": self._output_attempt,
                         "pricing_rates": self._rates,
                         "reasoning_tokens": self._reasoning_tokens, **details, **data})
