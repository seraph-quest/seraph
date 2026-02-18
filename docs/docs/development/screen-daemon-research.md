---
sidebar_position: 6
---

# Screen Daemon Research

Research notes for upgrading the Seraph native macOS daemon beyond app name + window title.

## macOS APIs for Screen Context

| API | Permission Required | Data Available | Notes |
|-----|-------------------|----------------|-------|
| `NSWorkspace.sharedWorkspace()` | None | App name, bundle ID, launch date | No prompt, works immediately |
| Accessibility API (AX) | Accessibility (one-time grant) | Window title, UI element tree, focused element | Single prompt, user grants once |
| `CGWindowListCopyWindowInfo` | Screen Recording | Window bounds, on-screen list, owner PID | Prompted per-app |
| `ScreenCaptureKit` | Screen Recording | Full screenshot, window capture, audio | Sequoia: monthly re-confirmation nag |
| AppleScript via `osascript` | Accessibility | Window title, app properties | Uses AX under the hood |

**Current implementation (Level 0):** NSWorkspace (no permission) + AppleScript window title (Accessibility permission). This gives us app name + window title with minimal friction.

## Local Vision Models (Not Viable)

Benchmarked on 2026-02-17 against 4 real macOS screenshots (iTerm2, VS Code, Perplexity, OpenRouter). See `scripts/vlm_benchmark_report.md` for full results.

| Model | Avg Latency | Quality | Notes |
|-------|-------------|---------|-------|
| Moondream v2 1.8B | 4.1s | Hallucinated | Described weather apps, "Lorem ipsum", generic "code" — none matching actual content |
| SmolVLM2 2.2B | 8.3s | Hallucinated | Described Gmail, Excel, Flickr — completely fabricated |
| Qwen3-VL 2B | N/A | Crashed | Metal GPU `command buffer 0 failed with status 3`; 200-313s per image when it didn't crash |

**Conclusion:** Local VLMs in the 0.5B–2B range are not viable for macOS screenshot understanding. They hallucinate entire applications and UI elements rather than reading actual screen content. Cloud VLMs (Gemini 2.5 Flash Lite) correctly identify apps, file names, code, and URLs at $0.15/mo — the cost is negligible compared to the quality gap.

## Cloud Vision API Costs

Monthly cost estimates for an 8-hour workday (22 working days/month), assuming ~500 output tokens per image description:

| Model | Cost/Image | 1/min ($$/mo) | 1/5min ($$/mo) | 1/30min ($$/mo) |
|-------|-----------|---------------:|----------------:|----------------:|
| Gemini 2.5 Flash Lite | $0.000432 | $4.56 | $0.91 | $0.15 |
| GPT-4o (low detail) | $0.000213 | $2.25 | $0.45 | $0.08 |
| Claude 3.5 Haiku | $0.001488 | $15.63 | $3.13 | $0.52 |
| Claude 3.5 Sonnet | $0.004800 | $50.69 | $10.14 | $1.69 |

**Calculation basis:** 8 hours x 60 min = 480 calls/day at 1/min. 22 days/month. ~2,354 input tokens (base64 image + prompt) + ~491 output tokens per call (benchmarked against real macOS screenshots).

**Note:** Gemini 2.0 Flash Lite (`google/gemini-2.0-flash-lite-001`) was deprecated on March 3, 2026. Gemini 2.5 Flash Lite (`google/gemini-2.5-flash-lite`) is the replacement at $0.10/M input + $0.40/M output.

**Takeaway:** Gemini 2.5 Flash Lite costs ~$0.15/mo at default 30s interval and ~$0.91/mo at 1/5min. Still very affordable for cloud VLM polling. Input tokens are higher than text-only estimates due to base64 image encoding.

## OCR-Only Approach

Apple's Vision framework provides `VNRecognizeTextRequest`:

- **Speed:** ~200ms per full-screen capture on Apple Silicon
- **Accuracy:** Near-perfect for rendered text (UI elements, code, documents)
- **Permission:** Requires Screen Recording (screenshot capture)
- **Output:** Structured text with bounding boxes and confidence scores

### Advantages
- No GPU memory pressure (CPU-based)
- Output is plain text — cheap to send as LLM context tokens
- Deterministic, no hallucination risk
- Works offline

### Limitations
- Loses visual layout context (can't distinguish sidebar from main content)
- Can't interpret images, charts, or non-text UI
- Noisy output for complex UIs (captures every label, button, menu item)

### Alternative: Tesseract
- Cross-platform but significantly slower (~1-2s per frame)
- Lower accuracy on macOS UI text compared to Apple Vision
- Not recommended when running on macOS

## Recommendation

Use **Gemini 2.5 Flash Lite via OpenRouter** as the cloud VLM provider. It correctly identifies applications, reads file names, code, URLs, and UI context from macOS screenshots at ~7s latency and $0.15/mo at the default 30s interval.

Apple Vision OCR remains useful as a free offline fallback for text-only extraction (~200ms, no hallucination), but Gemini provides strictly richer context (layout understanding, activity inference) at negligible cost.

## Language Comparison for Daemon

| Language | Pros | Cons |
|----------|------|------|
| **Python + PyObjC** (chosen) | Same ecosystem as backend, easy to prototype, PyObjC is mature | Slightly higher memory footprint (~30-50MB), not a native binary |
| Swift | First-class macOS APIs, smallest binary, best permission UX | Separate build toolchain, harder to iterate, different language from backend |
| Rust + objc2 | Cross-platform potential, small binary | Overkill for this use case, immature macOS bindings, long compile times |

**Decision:** Python + PyObjC. The daemon is simple enough that Python's overhead is negligible, and sharing the language with the backend reduces maintenance burden. If performance becomes an issue (e.g., running VLM inference), we can move to Swift for the capture loop and keep Python for the HTTP client.

## Permission UX (macOS TCC)

macOS uses Transparency, Consent, and Control (TCC) for privacy permissions:

- **Accessibility:** One-time prompt. Once granted, persists until the app is removed or user revokes manually. Needed for window titles.
- **Screen Recording:** Per-app prompt. Starting with macOS Sequoia (15.0), the system nags users monthly to re-confirm. This is the main UX friction point for screenshot-based approaches.

### Sequoia Monthly Nag
macOS 15+ shows a monthly system notification: _"[App] has been recording your screen. Do you want to continue allowing this?"_ This cannot be suppressed programmatically. Users who find this annoying may revoke permission.

**Implication:** For Level 0 (app + title), we only need Accessibility — no monthly nag. Moving to Level 1+ (OCR/screenshots) will trigger the nag. This should be clearly communicated to users.

## Upgrade Path

| Level | Capability | Permission | Status |
|-------|-----------|------------|--------|
| **0** | App name + window title | Accessibility | Implemented (default) |
| **1** | + OCR text extraction (Apple Vision) | + Screen Recording | Implemented (`--ocr` flag) |
| **2** | + Cloud VLM screen understanding (Gemini) | + Screen Recording | Implemented (`--ocr --ocr-provider openrouter`) |

The backend API accepts both `active_window` and `screen_context` fields, so no backend changes are needed for any level.
