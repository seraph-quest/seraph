#!/usr/bin/env python3
"""Process Seraph's local screenshot-folder backlog in bounded chunks."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.error
import urllib.request


def _get_json(url: str, *, timeout: int) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: dict, *, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _log(message: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone.utc).isoformat()}] {message}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8004")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--sleep-seconds", type=float, default=2.0)
    parser.add_argument("--max-idle-rounds", type=int, default=3)
    parser.add_argument("--request-timeout-seconds", type=int, default=1800)
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    idle_rounds = 0
    _log("screenshot backlog processor started")

    while True:
        try:
            status = _get_json(f"{base_url}/api/settings/artifact-storage", timeout=30)
            analysis = status.get("screenshot_folder", {}).get("analysis", {})
            analysis_status = analysis.get("analysis_status") or {}
            remaining = int(analysis.get("remaining_to_ingest") or 0)
            succeeded = int(analysis_status.get("succeeded") or 0)
            failed = int(analysis_status.get("failed") or 0)
            _log(f"remaining_to_ingest={remaining} succeeded={succeeded} failed={failed}")
            if remaining <= 0:
                break

            result = _post_json(
                f"{base_url}/api/observer/screenshot-folder/scan",
                {"limit": max(1, min(args.batch_size, remaining))},
                timeout=args.request_timeout_seconds,
            )
            _log(f"batch_result={json.dumps(result, sort_keys=True)}")
            if int(result.get("ingested") or 0) <= 0:
                idle_rounds += 1
                if idle_rounds >= args.max_idle_rounds:
                    _log("stopping after idle rounds")
                    break
            else:
                idle_rounds = 0
            time.sleep(max(args.sleep_seconds, 0.0))
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
            _log(f"error={type(exc).__name__}: {exc}")
            time.sleep(30)

    _log("screenshot backlog processor stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
