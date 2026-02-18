#!/usr/bin/env python3
"""VLM Screenshot Benchmark — benchmark Gemini 2.5 Flash Lite on real screenshots.

Usage:
    OPENROUTER_API_KEY=sk-or-... python scripts/vlm_benchmark.py
    python scripts/vlm_benchmark.py --screenshots-dir ~/Desktop/screen
"""

from __future__ import annotations

import base64
import argparse
import json
import os
import sys
import time
from pathlib import Path

SCREENSHOT_DIR = Path.home() / "Desktop" / "screen"
RESULTS_FILE = Path(__file__).parent / "vlm_benchmark_results.json"
REPORT_FILE = Path(__file__).parent / "vlm_benchmark_report.md"

PROMPT = (
    "Describe what you see on this screen. Focus on: application name, "
    "visible text, file names, code, UI elements, and what the user is doing."
)


def load_screenshots() -> list[tuple[str, bytes]]:
    """Load all screenshots from the screenshot directory."""
    screenshots = sorted(SCREENSHOT_DIR.glob("Screenshot *.png"))
    if not screenshots:
        print(f"No screenshots found in {SCREENSHOT_DIR}")
        sys.exit(1)
    print(f"Found {len(screenshots)} screenshots:")
    result = []
    for p in screenshots:
        print(f"  {p.name} ({p.stat().st_size / 1024:.0f} KB)")
        result.append((p.name, p.read_bytes()))
    return result


def benchmark_gemini(images: list[tuple[str, bytes]]) -> list[dict]:
    """Benchmark Gemini 2.5 Flash Lite via OpenRouter API."""
    import httpx

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip().strip("'\"")
    if not api_key:
        print("\n  Skipping — OPENROUTER_API_KEY not set")
        return []

    model_id = "google/gemini-2.5-flash-lite"

    print(f"\n{'=' * 60}")
    print(f"  Gemini 2.5 Flash Lite (OpenRouter: {model_id})")
    print(f"{'=' * 60}")

    results = []
    with httpx.Client(timeout=60.0) as client:
        for name, img_bytes in images:
            b64 = base64.b64encode(img_bytes).decode("ascii")
            start = time.monotonic()
            try:
                resp = client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model_id,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": PROMPT},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                                },
                            ],
                        }],
                        "max_tokens": 500,
                    },
                )
                elapsed = time.monotonic() - start
                resp.raise_for_status()
                data = resp.json()
                text = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                results.append({
                    "screenshot": name,
                    "model": "Gemini 2.5 Flash Lite",
                    "response": text,
                    "latency_s": round(elapsed, 2),
                    "usage": usage,
                    "error": None,
                })
                print(f"  {name}: {elapsed:.1f}s ({len(text)} chars)")
            except Exception as e:
                elapsed = time.monotonic() - start
                results.append({
                    "screenshot": name,
                    "model": "Gemini 2.5 Flash Lite",
                    "response": "",
                    "latency_s": round(elapsed, 2),
                    "error": str(e),
                })
                print(f"  {name}: ERROR ({e})")
    return results


def generate_report(all_results: list[dict], screenshot_names: list[str]) -> str:
    """Generate a markdown report from benchmark results."""
    lines = [
        "# VLM Screenshot Benchmark Results",
        "",
        f"**Date:** {time.strftime('%Y-%m-%d %H:%M')}",
        f"**Screenshots:** {len(screenshot_names)} images from `~/Desktop/screen/`",
        f"**Prompt:** \"{PROMPT}\"",
        "",
        "## Summary",
        "",
        "| Model | Avg Latency | Min | Max | Errors |",
        "|-------|-------------|-----|-----|--------|",
    ]

    successful = [r for r in all_results if not r.get("error")]
    errors = sum(1 for r in all_results if r.get("error"))
    if successful:
        latencies = [r["latency_s"] for r in successful]
        avg = sum(latencies) / len(latencies)
        mn = min(latencies)
        mx = max(latencies)
        lines.append(f"| Gemini 2.5 Flash Lite | {avg:.1f}s | {mn:.1f}s | {mx:.1f}s | {errors} |")
    else:
        lines.append(f"| Gemini 2.5 Flash Lite | — | — | — | {errors} |")

    lines.extend(["", "## Per-Screenshot Results", ""])

    for ss_name in screenshot_names:
        lines.extend([f"### {ss_name}", ""])
        matching = [r for r in all_results if r["screenshot"] == ss_name]
        if matching:
            r = matching[0]
            if r.get("error"):
                lines.append(f"> ERROR: {r['error']}")
            else:
                lines.append(f"**Latency:** {r['latency_s']}s")
                lines.append("")
                for resp_line in r["response"].splitlines():
                    lines.append(f"> {resp_line}")
            lines.append("")

    # Cost analysis
    usage_list = [r["usage"] for r in successful if r.get("usage")]
    if usage_list:
        avg_input = sum(u.get("prompt_tokens", 0) for u in usage_list) / len(usage_list)
        avg_output = sum(u.get("completion_tokens", 0) for u in usage_list) / len(usage_list)
        cost_per_image = (avg_input * 0.10 + avg_output * 0.40) / 1_000_000
        avg_latency = sum(r["latency_s"] for r in successful) / len(successful)

        lines.extend([
            "## Cost Analysis",
            "",
            f"- **Avg input tokens:** {avg_input:.0f}",
            f"- **Avg output tokens:** {avg_output:.0f}",
            f"- **Cost per image:** ${cost_per_image:.6f}",
            f"- **Avg latency:** {avg_latency:.1f}s",
            f"- **Pricing:** $0.10/M input + $0.40/M output (OpenRouter)",
            "",
            "Monthly estimates (8h/day, 22 working days):",
            "",
            "| Interval | Calls/month | Cost/month |",
            "|----------|-------------|------------|",
            f"| 1/min | 10,560 | ${10_560 * cost_per_image:.2f} |",
            f"| 1/5min | 2,112 | ${2_112 * cost_per_image:.2f} |",
            f"| 1/30min (default) | 352 | ${352 * cost_per_image:.2f} |",
            "",
        ])

    return "\n".join(lines)


def main():
    global SCREENSHOT_DIR

    parser = argparse.ArgumentParser(description="VLM Screenshot Benchmark")
    parser.add_argument(
        "--screenshots-dir",
        default=str(SCREENSHOT_DIR),
        help=f"Directory containing screenshots (default: {SCREENSHOT_DIR})",
    )
    args = parser.parse_args()
    SCREENSHOT_DIR = Path(args.screenshots_dir)

    print("VLM Screenshot Benchmark")
    print("=" * 60)

    images = load_screenshots()
    screenshot_names = [name for name, _ in images]

    results = benchmark_gemini(images)

    if not results:
        print("\nNo results collected. Check OPENROUTER_API_KEY is set.")
        sys.exit(1)

    # Save JSON results
    RESULTS_FILE.write_text(json.dumps(results, indent=2))
    print(f"\nResults saved to {RESULTS_FILE}")

    # Generate markdown report
    report = generate_report(results, screenshot_names)
    REPORT_FILE.write_text(report)
    print(f"Report saved to {REPORT_FILE}")


if __name__ == "__main__":
    main()
