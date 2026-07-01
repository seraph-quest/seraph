"""Update and publish the Resend end-of-day report template from local env."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env.dev"


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


HTML = """<!doctype html>
<html>
  <body style="margin:0;background:#06111a;color:#d7eef5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;">
    <div style="display:none;overflow:hidden;line-height:1px;opacity:0;max-height:0;max-width:0;color:transparent;">
      Daily goal and screen-observation summary from Seraph.
    </div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#06111a;padding:24px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;border:1px solid #1e4150;background:#071722;">
            <tr>
              <td style="padding:22px 22px 12px;border-bottom:1px solid #1e4150;">
                <div style="font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:#66ffc8;font-weight:700;">Seraph</div>
                <h1 style="margin:8px 0 4px;font-size:26px;line-height:1.15;color:#f2fdff;font-weight:800;">End-of-day report</h1>
                <div style="font-size:13px;color:#8fb5c4;">{{{REPORT_DATE}}} · {{{TIMEZONE}}} · {{{ANALYSIS_PROVIDER}}}</div>
              </td>
            </tr>
            <tr>
              <td style="padding:14px 16px 4px;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                  <tr>
                    <td style="width:50%;padding:6px;"><div style="border:1px solid #1f4f63;background:#081923;padding:12px;"><div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#7fb4c7;">Tracked</div><div style="font-size:22px;color:#e8fbff;font-weight:700;margin-top:5px;">{{{TOTAL_TRACKED_MINUTES}}}m</div></div></td>
                    <td style="width:50%;padding:6px;"><div style="border:1px solid #1f4f63;background:#081923;padding:12px;"><div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#7fb4c7;">Switches</div><div style="font-size:22px;color:#e8fbff;font-weight:700;margin-top:5px;">{{{CONTEXT_SWITCHES}}}</div></div></td>
                  </tr>
                  <tr>
                    <td style="width:50%;padding:6px;"><div style="border:1px solid #1f4f63;background:#081923;padding:12px;"><div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#7fb4c7;">Screens</div><div style="font-size:22px;color:#e8fbff;font-weight:700;margin-top:5px;">{{{SCREEN_OBSERVATIONS}}}</div></div></td>
                    <td style="width:50%;padding:6px;"><div style="border:1px solid #1f4f63;background:#081923;padding:12px;"><div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#7fb4c7;">Digests</div><div style="font-size:22px;color:#e8fbff;font-weight:700;margin-top:5px;">{{{SCREENSHOT_DIGESTS}}}</div></div></td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td style="padding:8px 22px 4px;">
                <div style="font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:#7fb4c7;margin-bottom:8px;">Goal signal</div>
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                  <tr>
                    <td style="padding:5px;"><div style="border:1px solid #243c49;background:#0b1f29;padding:10px;color:#cfeef7;">Aligned <strong style="float:right;color:#66ffc8;">{{{ALIGNED_GOALS}}}</strong></div></td>
                    <td style="padding:5px;"><div style="border:1px solid #243c49;background:#0b1f29;padding:10px;color:#cfeef7;">Pushed <strong style="float:right;color:#66ffc8;">{{{PUSHED_GOALS}}}</strong></div></td>
                  </tr>
                  <tr>
                    <td style="padding:5px;"><div style="border:1px solid #243c49;background:#0b1f29;padding:10px;color:#cfeef7;">Drifted <strong style="float:right;color:#ffd166;">{{{DRIFTED_GOALS}}}</strong></div></td>
                    <td style="padding:5px;"><div style="border:1px solid #243c49;background:#0b1f29;padding:10px;color:#cfeef7;">Blocked <strong style="float:right;color:#ff7b7b;">{{{BLOCKED_GOALS}}}</strong></div></td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td style="padding:18px 22px 26px;">
                <div style="border-top:1px solid #1e4150;padding-top:16px;font-size:15px;line-height:1.6;color:#d7eef5;white-space:pre-wrap;">{{{REPORT_BODY}}}</div>
              </td>
            </tr>
            <tr>
              <td style="padding:13px 22px;border-top:1px solid #1e4150;color:#6e95a5;font-size:12px;">
                Local-first report generated from Seraph observations. Raw screenshots are not attached.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


VARIABLES = [
    {"key": "REPORT_DATE", "type": "string", "fallbackValue": "today"},
    {"key": "TIMEZONE", "type": "string", "fallbackValue": "local"},
    {"key": "ANALYSIS_PROVIDER", "type": "string", "fallbackValue": "local"},
    {"key": "TOTAL_TRACKED_MINUTES", "type": "number", "fallbackValue": 0},
    {"key": "CONTEXT_SWITCHES", "type": "number", "fallbackValue": 0},
    {"key": "SCREEN_OBSERVATIONS", "type": "number", "fallbackValue": 0},
    {"key": "SCREENSHOT_DIGESTS", "type": "number", "fallbackValue": 0},
    {"key": "ALIGNED_GOALS", "type": "number", "fallbackValue": 0},
    {"key": "PUSHED_GOALS", "type": "number", "fallbackValue": 0},
    {"key": "DRIFTED_GOALS", "type": "number", "fallbackValue": 0},
    {"key": "BLOCKED_GOALS", "type": "number", "fallbackValue": 0},
    {"key": "REPORT_BODY", "type": "string", "fallbackValue": "No report generated yet."},
]


def _request(method: str, path: str, api_key: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"https://api.resend.com{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "seraph-template-updater/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Resend API {method} {path} failed: HTTP {exc.code}: {details}") from exc


def main() -> None:
    env = _load_env(ENV_PATH)
    template_id = env.get("RESEND_TEMPLATE_ID", "").strip()
    api_key = env.get("RESEND_API_KEY", "").strip() or env.get("SMTP_PASSWORD", "").strip()
    sender = env.get("EMAIL_REPORTS_FROM", "").strip() or "nat@neurion.ai"
    if "<" not in sender:
        sender = f"Seraph <{sender}>"
    if not template_id:
        raise SystemExit("RESEND_TEMPLATE_ID is missing")
    if not api_key:
        raise SystemExit("RESEND_API_KEY or SMTP_PASSWORD is missing")

    update_payload: dict[str, object] = {
        "name": "Seraph end-of-day report",
        "html": HTML,
        "from": sender,
        "subject": "Seraph end-of-day report: {{{REPORT_DATE}}}",
        "text": "{{{REPORT_BODY}}}",
        "variables": VARIABLES,
    }
    update = _request("PATCH", f"/templates/{template_id}", api_key, update_payload)
    publish = _request("POST", f"/templates/{template_id}/publish", api_key)
    print(json.dumps({"updated": update, "published": publish}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
