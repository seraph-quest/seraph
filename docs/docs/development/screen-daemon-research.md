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

## Local Vision Models

For future screenshot understanding (Level 2):

| Model | Params | RAM (Apple Silicon) | Speed (M1 Pro) | Notes |
|-------|--------|--------------------:|-----------------|-------|
| FastVLM 0.5B | 500M | ~1-2 GB | ~0.5s/frame | Apple's own model, MLX-native, optimized for Apple Silicon |
| Moondream 0.5B | 500M | ~1-2 GB | ~1s/frame | Strong on UI understanding, good at describing screen content |
| SmolVLM2 500M | 500M | &lt;1 GB | ~0.8s/frame | HuggingFace, smallest footprint |
| Qwen2-VL 2B | 2B | ~3-4 GB | ~2s/frame | More capable but heavier |

**Recommendation:** FastVLM 0.5B or Moondream 0.5B. Both fit comfortably in memory alongside other apps and process a frame in under 1 second on Apple Silicon.

## Cloud Vision API Costs

Monthly cost estimates for an 8-hour workday (22 working days/month), assuming ~500 tokens per image description:

| Model | Cost/Image | 1/min ($$/mo) | 1/5min ($$/mo) | 1/30min ($$/mo) |
|-------|-----------|---------------:|----------------:|----------------:|
| Gemini 2.0 Flash Lite | $0.000042 | $0.44 | $0.09 | $0.01 |
| GPT-4o (low detail) | $0.000213 | $2.25 | $0.45 | $0.08 |
| Claude 3.5 Haiku | $0.001488 | $15.63 | $3.13 | $0.52 |
| Claude 3.5 Sonnet | $0.004800 | $50.69 | $10.14 | $1.69 |

**Calculation basis:** 8 hours x 60 min = 480 calls/day at 1/min. 22 days/month.

**Takeaway:** Gemini 2.0 Flash Lite (`google/gemini-2.0-flash-lite-001`) is essentially free even at 1/min. GPT-4o low is ~$2/mo. Cloud VLMs are viable for infrequent polling (1/5min or less).

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

## Hybrid Recommendation

The best approach combines OCR and VLM at different frequencies:

```
OCR every 10-30s:
  → Extract visible text (file paths, function names, error messages)
  → Cheap, fast, deterministic
  → Feed as context tokens to strategist

VLM every 1-5min (on change):
  → Capture what the user is doing at a high level
  → "User is debugging a test failure in the terminal"
  → Richer context but more expensive
```

This gives the strategist both precise text context (from OCR) and high-level activity understanding (from VLM) without excessive cost or battery drain.

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

## Recommended Upgrade Path

| Level | Capability | Permission | Daemon Changes |
|-------|-----------|------------|----------------|
| **0** | App name + window title | Accessibility | `seraph_daemon.py` (default, no `--ocr` flag) |
| **1 (implemented)** | + OCR text extraction | + Screen Recording | `--ocr` flag, pluggable providers: `apple-vision` (local) or `openrouter` (cloud) |
| **2** | + Local VLM descriptions | + Screen Recording | Add FastVLM/Moondream inference, structured activity summary |
| **3** | + Cloud VLM (on-demand) | + Screen Recording | Add cloud API call for complex scenes, hybrid with local VLM |

Each level is additive. The daemon's `--level` flag (future) would control which capture pipeline runs. The backend API already accepts both `active_window` and `screen_context` fields, so no backend changes are needed for any level.
