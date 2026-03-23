"""Rule-based scanning for suspicious prompt-bearing extension content."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ContextScanFinding:
    code: str
    description: str
    excerpt: str


_SUSPICIOUS_CONTEXT_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        "instruction override language",
        re.compile(
            r"^(?:please |now |then )?ignore (?:all |any |the )?(?:previous|prior|earlier) (?:instructions|prompts|rules)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt_exfiltration",
        "prompt or secret exfiltration language",
        re.compile(
            r"^(?:please )?(?:reveal|show|print|dump|exfiltrate|leak)\b.{0,80}\b(?:system prompt|developer message|hidden prompt|instructions|secret|api key|token|password)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "policy_bypass",
        "approval or policy bypass language",
        re.compile(
            r"^(?:please )?(?:bypass|disable|ignore)\b.{0,40}\b(?:approval|policy|guardrails?|safety checks?)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "system_impersonation",
        "system-role impersonation language",
        re.compile(r"^(?:please )?(?:you are now|act as)\b.{0,30}\b(?:system|developer)\b", re.IGNORECASE | re.DOTALL),
    ),
)


def _compact_excerpt(text: str) -> str:
    return " ".join(text.split())[:120]


def _candidate_segments(text: str) -> list[str]:
    segments: list[str] = []
    in_fence = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        normalized = re.sub(r"^(?:[-*+]\s+|\d+\.\s+|>\s+)", "", stripped)
        segments.append(normalized)
    return segments


def scan_text_for_suspicious_context(text: str) -> list[ContextScanFinding]:
    """Return suspicious prompt-bearing patterns that should block extension content."""
    findings: list[ContextScanFinding] = []
    for segment in _candidate_segments(text):
        for code, description, pattern in _SUSPICIOUS_CONTEXT_PATTERNS:
            match = pattern.search(segment)
            if match is None:
                continue
            findings.append(
                ContextScanFinding(
                    code=code,
                    description=description,
                    excerpt=_compact_excerpt(match.group(0)),
                )
            )
    return findings
