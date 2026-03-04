#!/usr/bin/env python3
"""Simple local load test for AgentBridge HTTP API."""

from __future__ import annotations

import argparse
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentBridge load test")
    parser.add_argument("--base-url", default="http://127.0.0.1:7890")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--messages-per-worker", type=int, default=100)
    parser.add_argument("--token", default="", help="AGENTBRIDGE_TOKEN value")
    args = parser.parse_args()

    headers = {"X-AgentBridge-Token": args.token} if args.token else {}
    sender = "load-tester"
    total = args.workers * args.messages_per_worker
    session = requests.Session()

    r = session.post(
        f"{args.base_url}/agents",
        json={"name": sender, "role": "load-test"},
        headers=headers,
        timeout=5,
    )
    r.raise_for_status()

    errors = []
    start = time.perf_counter()
    lock = threading.Lock()

    def worker(worker_id: int):
        local = requests.Session()
        for i in range(args.messages_per_worker):
            payload = {
                "sender": sender,
                "thread": "load-test",
                "msg_type": "status",
                "content": f"worker={worker_id} idx={i}",
            }
            try:
                resp = local.post(
                    f"{args.base_url}/messages",
                    json=payload,
                    headers=headers,
                    timeout=5,
                )
                resp.raise_for_status()
            except Exception as exc:  # pragma: no cover
                with lock:
                    errors.append(exc)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(worker, i) for i in range(args.workers)]
        for future in as_completed(futures):
            future.result()

    elapsed = time.perf_counter() - start
    mps = total / elapsed if elapsed > 0 else 0.0

    read_resp = session.get(
        f"{args.base_url}/messages",
        params={"thread": "load-test", "limit": min(total, 500)},
        headers=headers,
        timeout=10,
    )
    read_resp.raise_for_status()
    found = len(read_resp.json())

    print(f"workers={args.workers}")
    print(f"messages_sent={total}")
    print(f"elapsed_seconds={elapsed:.3f}")
    print(f"messages_per_second={mps:.2f}")
    print(f"messages_found_latest_window={found}")
    print(f"errors={len(errors)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
