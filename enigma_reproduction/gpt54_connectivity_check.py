"""Minimal GPT-5.4 / TeamoRouter connectivity check.

Mirrors the exact request the GPT54Model adapter sends, then falls back to
simpler request shapes only to diagnose which parameters the relay accepts.
Prints redacted JSON only; the credential is never printed.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openai import OpenAI

KEY = (ROOT / ".secrets" / "teamorouter_api_key").read_text().strip()
BASE_URL = "https://api.teamorouter.com/v1"
MODEL = "gpt-5.4"


def redact(value):
    return str(value).replace(KEY, "***") if isinstance(value, str) else value


def attempt(label, kwargs):
    client = OpenAI(api_key=KEY, base_url=BASE_URL, max_retries=0, timeout=120.0)
    started = time.perf_counter()
    try:
        response = client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Hello"}], **kwargs)
        usage = getattr(response, "usage", None)
        return {
            "label": label,
            "ok": True,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "response_model": redact(getattr(response, "model", None)),
            "content": redact(response.choices[0].message.content),
            "finish_reason": redact(response.choices[0].finish_reason),
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
        }
    except Exception as e:  # noqa: BLE001 - report type and redacted message only
        return {
            "label": label,
            "ok": False,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "error_type": type(e).__name__,
            "error": redact(getattr(e, "message", str(e))),
            "status_code": getattr(e, "status_code", None),
        }


def main():
    # 1. Exact adapter shape: reasoning_effort=none keeps temperature/top_p.
    exact = {"max_completion_tokens": 4096, "reasoning_effort": "none", "temperature": 0.0, "top_p": 0.95}
    # 2. Reasoning only, no sampling params.
    reasoning_only = {"max_completion_tokens": 4096, "reasoning_effort": "none"}
    # 3. Bare, matching the user's example.
    bare = {}
    results = [
        attempt("exact_adapter_shape", exact),
        attempt("reasoning_effort_only", reasoning_only),
        attempt("bare", bare),
    ]
    print(json.dumps({"model": MODEL, "base_url": BASE_URL, "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
