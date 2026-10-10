#!/usr/bin/env python3
"""Small parallel requests through the actual Qwen adapter, without CTF tasks."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["TOGETHER_NO_BANNER"] = "1"

from sweagent.agent.models import ModelArguments, get_model
from sweagent.utils.secrets import redact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--num-workers", type=int, choices=(1, 2, 3, 4), default=2)
    parser.add_argument("--diagnose-network", action="store_true",
                        help="Unauthenticated TLS probes only; makes no model requests")
    args = parser.parse_args()
    settings_path = ROOT / "enigma_reproduction/qwen3_max_settings.json"
    settings = json.loads(settings_path.read_text())
    if args.diagnose_network:
        from urllib.parse import urlsplit
        host = urlsplit(settings["base_url"]).hostname
        reports = []
        for target in (host, "dashscope.aliyuncs.com"):
            addresses = sorted({row[4][0] for row in socket.getaddrinfo(target, 443, type=socket.SOCK_STREAM)
                                if row[0] == socket.AF_INET})
            for address in addresses:
                for tls12 in (False, True):
                    context = ssl.create_default_context()
                    if tls12:
                        context.maximum_version = ssl.TLSVersion.TLSv1_2
                    try:
                        with socket.create_connection((address, 443), timeout=10) as connection:
                            with context.wrap_socket(connection, server_hostname=target) as connection:
                                reports.append({"host": target, "address": address,
                                                "tls12_only": tls12, "ok": True, "tls": connection.version()})
                    except Exception as error:
                        reports.append({"host": target, "address": address, "tls12_only": tls12,
                                        "ok": False, "error": str(error)[:300]})
        print(json.dumps({"model_calls_made": 0, "tls_probes": reports}, ensure_ascii=False))
        return 0 if any(row["ok"] and row["host"] == host for row in reports) else 2
    os.environ["ENIGMA_MODEL_SETTINGS"] = str(settings_path)
    if not os.environ.get("DASHSCOPE_API_KEY", "").strip():
        os.environ["DASHSCOPE_API_KEY"] = (ROOT / ".secrets/dashscope_api_key").read_text().strip()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = ROOT / "enigma_reproduction/generated" / f"qwen3-max-connectivity-{stamp}"
    directory.mkdir(mode=0o700, parents=True)
    os.environ["ENIGMA_USAGE_LEDGER"] = str(directory / "usage.jsonl")
    barrier = threading.Barrier(args.num_workers)

    def request(index):
        model = None
        started = time.perf_counter()
        try:
            model = get_model(ModelArguments(model_name="qwen3-max", per_instance_cost_limit=.1))
            barrier.wait(timeout=30)
            requested = time.perf_counter()
            text = model.query([{"role": "user", "content": f"Reply with exactly OK{index}."}])
            return {"worker": index, "ok": text.strip() == f"OK{index}", "answer": redact(text),
                    "request_started": requested, "request_finished": time.perf_counter(),
                    "cost_estimate_usd": model.stats.instance_cost,
                    "prompt_tokens": model.stats.tokens_sent, "completion_tokens": model.stats.tokens_received}
        except Exception as error:
            return {"worker": index, "ok": False, "elapsed_seconds": time.perf_counter() - started,
                    "error": redact(str(error)), "error_type": type(error).__name__}
        finally:
            if model is not None:
                model.client.close()

    with ThreadPoolExecutor(max_workers=args.num_workers) as pool:
        results = list(pool.map(request, range(1, args.num_workers + 1)))
    success = all(row["ok"] for row in results)
    overlaps = success and max(row["request_started"] for row in results) < min(
        row["request_finished"] for row in results)
    report = {"ok": success, "model": "qwen3-max", "enable_thinking": True,
              "workers": args.num_workers, "requests_overlapped": overlaps,
              "results": results, "report_directory": str(directory)}
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
