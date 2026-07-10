# Seraph Backend

> The canonical product and provider boundary is defined by the
> [Project Constitution](../docs/implementation/00-project-constitution.md).
> Model profiles provide inference only; Seraph owns its capability runtime.

AI assistant backend powered by FastAPI, smolagents, and LiteLLM-compatible provider routing.

## Setup

1. Copy env vars and choose a provider profile:
   ```bash
   cp ../env.dev.example ../.env.dev

   # In the root .env.dev, OpenRouter is the default example:
   OPENROUTER_API_KEY=your-actual-key
   DEFAULT_MODEL=openrouter/anthropic/claude-sonnet-4
   ```

2. Install dependencies:
   ```bash
   cd backend
   uv sync
   ```

3. Run the recommended local stack from the repo root:
   ```bash
   ./manage.sh -e dev local up
   ```

4. Run via Docker:
   ```bash
   ./manage.sh -e dev up -d
   ```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/api/chat` | POST | Send message, get AI response |
| `/ws/chat` | WS | Streaming chat via WebSocket |
| `/docs` | GET | Swagger UI |

### POST /api/chat

```json
{
  "message": "Hello, what can you do?",
  "session_id": "optional-session-id"
}
```

### WS /ws/chat

Send:
```json
{"type": "message", "message": "Hello", "session_id": null}
```

Receive (streamed):
```json
{"type": "step", "content": "...", "session_id": "abc", "step": 1}
{"type": "final", "content": "...", "session_id": "abc"}
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENROUTER_API_KEY` | - | OpenRouter API key used by the default example profile |
| `OPENAI_API_KEY` | - | Remote OpenAI API key used by named OpenAI model profiles |
| `ANTHROPIC_API_KEY` | - | Anthropic API key used by the `claude-anthropic` built-in profile |
| `DEFAULT_MODEL` | `openrouter/anthropic/claude-sonnet-4` | LLM model identifier |
| `LLM_API_KEY` | - | Generic primary API key override for LiteLLM-compatible providers |
| `LLM_API_BASE` | `https://openrouter.ai/api/v1` | Primary OpenAI-compatible API base |
| `LLM_PROVIDER_PROFILES` | - | JSON object defining named inference profiles with provider kind, model, API base, env secret, options, metadata, fallback chain, enabled state, and safety notes |
| `MODEL_TEMPERATURE` | `0.7` | Generation temperature |
| `MODEL_MAX_TOKENS` | `4096` | Max response tokens |
| `AGENT_MAX_STEPS` | `10` | Max agent reasoning steps |
| `DEBUG` | `false` | Enable debug mode |
| `WORKSPACE_DIR` | `/app/data` | Agent file workspace |
| `LOCAL_MODEL` | - | Model id for the local runtime profile |
| `LOCAL_LLM_API_KEY` | - | Optional API key for the local runtime profile |
| `LOCAL_LLM_API_BASE` | - | API base for the local runtime profile |
| `SCREEN_CAPTURE_ARCHIVE_DIR` | `~/Library/Application Support/Seraph/artifacts/screen-captures` | Durable local archive root for preserved screen capture images, redacted provider output, and normalized JSON served by localhost-only observer artifact endpoints |
| `SCREEN_ANALYSIS_MIN_SECONDS_BETWEEN_CAPTURES` | `0` | Minimum seconds between screen-analysis captures; `0` disables this throttle |
| `SCREEN_ANALYSIS_MAX_DAILY_CAPTURES` | `0` | Daily screen-analysis capture cap; `0` means unlimited |
| `SCREEN_CAPTURE_ARCHIVE_RETENTION_DAYS` | `365` | Operator-visible retention posture for preserved captures |
| `SCREEN_CAPTURE_ARCHIVE_MAX_MB` | `0` | Operator-visible archive size budget; `0` means unlimited |
| `REPORT_ARCHIVE_DIR` | `$WORKSPACE_DIR/artifacts/reports` | Durable local archive root for stored report text/JSON artifacts used by future analysis |
| `END_OF_DAY_REPORT_ENABLED` | `true` | Enables the stored end-of-day goal report scheduler job |
| `END_OF_DAY_REPORT_HOUR` | `21` | Local hour for the end-of-day goal report |
| `END_OF_DAY_REPORT_LLM_ENABLED` | `false` | Enables LLM drafting for the report; default deterministic mode avoids sending screen-derived summaries to a provider |
| `EMAIL_REPORTS_ENABLED` | `false` | Enables email delivery for reports after local storage |
| `EMAIL_REPORTS_PREVIEW_REQUIRED` | `true` | Blocks scheduled outbound report email unless operator preview is acknowledged for manual sends or deliberately disabled for scheduled delivery |
| `EMAIL_REPORTS_TO` | - | Recipient for report email |
| `EMAIL_REPORTS_TO_ALLOWLIST` | - | Comma-separated recipient allowlist; recipient must be present before sending |
| `EMAIL_REPORTS_FROM` | - | Sender address for report email |
| `SMTP_HOST` | - | SMTP host for report delivery |
| `SMTP_PORT` | `587` | SMTP port for report delivery |
| `SMTP_USERNAME` | - | SMTP username |
| `SMTP_PASSWORD` | - | SMTP password |
| `SMTP_USE_TLS` | `true` | Use STARTTLS before SMTP login/send |
| `RESEND_API_KEY` | - | Resend API key for hosted-template delivery; if empty, Seraph can reuse `SMTP_PASSWORD` when `SMTP_HOST=smtp.resend.com` and `SMTP_USERNAME=resend` |
| `RESEND_API_URL` | `https://api.resend.com/emails` | Resend Email API endpoint |
| `RESEND_TEMPLATE_ID` | - | Published Resend template id or alias. When set, Seraph sends report variables through the Resend API instead of SMTP inline HTML |
| `LOCAL_RUNTIME_PATHS` | - | Comma-separated runtime paths or glob patterns that should prefer the local profile |
| `RUNTIME_PROFILE_PREFERENCES` | - | Semicolon-separated `runtime_path=profile_a|profile_b` chains; `runtime_path` may be an exact path or glob |
| `RUNTIME_POLICY_INTENTS` | - | Semicolon-separated `runtime_path=intent_a|intent_b` entries for capability-aware routing; `runtime_path` may be an exact path or glob |
| `RUNTIME_POLICY_REQUIREMENTS` | - | Semicolon-separated `runtime_path=intent_a|intent_b` hard requirements, applied when compliant targets exist |
| `RUNTIME_POLICY_SCORES` | - | Semicolon-separated `runtime_path=intent_a:weight|intent_b:weight` entries that turn policy tags into weighted ranking signals |
| `RUNTIME_MAX_COST_TIER` | - | Semicolon-separated `runtime_path=low|medium|high` guardrails |
| `RUNTIME_MAX_LATENCY_TIER` | - | Semicolon-separated `runtime_path=low|medium|high` guardrails |
| `RUNTIME_TASK_CLASS` | - | Semicolon-separated `runtime_path=task_class` labels |
| `RUNTIME_MAX_BUDGET_CLASS` | - | Semicolon-separated `runtime_path=low|medium|high` budget guardrails |
| `RUNTIME_MODEL_OVERRIDES` | - | Comma-separated `runtime_path=model` or `runtime_path=profile:model` overrides; `runtime_path` may be an exact path or glob |
| `RUNTIME_FALLBACK_OVERRIDES` | - | Semicolon-separated `runtime_path=model_a|model_b` fallback chains; `runtime_path` may be an exact path or glob |
| `PROVIDER_CAPABILITY_OVERRIDES` | - | Semicolon-separated `model_or_glob=capability_a|capability_b` tags used by `RUNTIME_POLICY_INTENTS` |
| `PROVIDER_COST_TIERS` | - | Semicolon-separated `model_or_glob=low|medium|high` provider cost tiers |
| `PROVIDER_LATENCY_TIERS` | - | Semicolon-separated `model_or_glob=low|medium|high` provider latency tiers |
| `PROVIDER_TASK_CLASSES` | - | Semicolon-separated `model_or_glob=task_class` provider labels |
| `PROVIDER_BUDGET_CLASSES` | - | Semicolon-separated `model_or_glob=low|medium|high` provider budget tiers |
| `FALLBACK_MODEL` | - | Legacy single fallback target |
| `FALLBACK_MODELS` | - | Comma-separated ordered global fallback chain |
| `FALLBACK_LLM_API_KEY` | - | Optional API key override for fallback calls |
| `FALLBACK_LLM_API_BASE` | - | Optional API base override for fallback calls |
| `LLM_TARGET_COOLDOWN_SECONDS` | `300` | Temporarily deprioritize failed LLM targets across requests |
| `LLM_LOG_ENABLED` | `true` | Enable LLM call logging to JSONL file |
| `LLM_LOG_CONTENT` | `false` | Include full messages/response in log |
| `LLM_LOG_DIR` | `/app/logs` | Log file directory |
| `LLM_LOG_MAX_BYTES` | `52428800` | Max bytes per log file before rotation (50 MB) |
| `LLM_LOG_BACKUP_COUNT` | `5` | Number of rotated log files to keep |

Inference profile recipes:

```bash
# Built-in named profiles:
#   openrouter
#   openai-compatible
#   codex-openai
#   claude-anthropic
#   gpt-5.5-low
#   local-ollama, when LOCAL_MODEL is configured

# Custom typed profile. Provider profiles decide who thinks; tool presets and
# runtime policies still decide what the selected route may do.
LLM_PROVIDER_PROFILES={"profiles":{"team-router":{"provider_kind":"openai_compatible","model":"team/model","api_base":"https://llm.example.com/v1","env_secret":"TEAM_LLM_API_KEY","options":{"reasoning_effort":"low"},"capabilities":["reasoning","tool_use"],"cost":"medium","latency":"low","task":"coding","budget":"medium","fallback":["team/small"],"enabled":true,"safety_notes":"Team-scoped profile; tool permissions remain separate."}}}
RUNTIME_PROFILE_PREFERENCES=chat_agent=team-router|openrouter

# Local Ollama for selected helper/operator paths.
LOCAL_MODEL=ollama/qwen2.5-coder:7b
LOCAL_LLM_API_BASE=http://localhost:11434/v1
RUNTIME_PROFILE_PREFERENCES=session_title_generation=local-ollama|openrouter;mcp_*=local-ollama|openrouter
RUNTIME_POLICY_INTENTS=session_title_generation=local_first|fast|cheap;mcp_*=local_first|tool_use
PROVIDER_CAPABILITY_OVERRIDES=ollama/*=local|fast|cheap;openrouter/anthropic/claude-sonnet-4=reasoning|tool_use

# OpenRouter default profile.
OPENROUTER_API_KEY=your-openrouter-key
LLM_API_BASE=https://openrouter.ai/api/v1
DEFAULT_MODEL=openrouter/anthropic/claude-sonnet-4
FALLBACK_MODELS=openai/gpt-4.1-mini,openai/gpt-4.1-nano

# Remote OpenAI API model profiles. Profile names are API routing identifiers,
# not external agent runtimes. Low reasoning is a request option, not part of
# the model id.
OPENAI_API_KEY=your-openai-key
RUNTIME_PROFILE_PREFERENCES=chat_agent=codex-openai|openrouter

# Anthropic/Claude-oriented routes.
ANTHROPIC_API_KEY=your-anthropic-key
RUNTIME_PROFILE_PREFERENCES=chat_agent=claude-anthropic|openrouter
PROVIDER_CAPABILITY_OVERRIDES=openrouter/anthropic/*=reasoning|tool_use

# Generic OpenAI-compatible endpoint.
LLM_API_KEY=your-compatible-api-key
LLM_API_BASE=https://your-provider.example.com/v1
DEFAULT_MODEL=your-provider/model-id
RUNTIME_PROFILE_PREFERENCES=chat_agent=openai-compatible|openrouter
FALLBACK_MODELS=your-provider/smaller-model
```

These examples configure inference routing only. Names such as `codex-openai`
and `claude-anthropic` identify API model profiles; they do not invoke Codex CLI,
Claude Code, or another external agent runtime, and do not imply equivalent
model behavior.

Runtime routing examples:

```bash
LOCAL_RUNTIME_PATHS=chat_agent,session_consolidation,daily_briefing
RUNTIME_PROFILE_PREFERENCES=chat_agent=local|default;session_consolidation=local|default
RUNTIME_POLICY_INTENTS=chat_agent=local_first|reasoning|tool_use;session_title_generation=fast|cheap
RUNTIME_POLICY_REQUIREMENTS=chat_agent=tool_use
RUNTIME_POLICY_SCORES=chat_agent=reasoning:5|tool_use:4;session_title_generation=fast:5|cheap:3
RUNTIME_MODEL_OVERRIDES=chat_agent=default:openai/gpt-4.1-mini
RUNTIME_FALLBACK_OVERRIDES=chat_agent=openai/gpt-4.1-mini|openai/gpt-4.1-nano;session_title_generation=openai/gpt-4o-mini|openai/gpt-4.1-mini
PROVIDER_CAPABILITY_OVERRIDES=openrouter/anthropic/claude-sonnet-4=reasoning|tool_use;openai/gpt-4o-mini=fast|cheap;openai/gpt-4.1-mini=reasoning|tool_use
PROVIDER_COST_TIERS=openrouter/anthropic/claude-sonnet-4=high;openai/gpt-4.1-mini=medium;openai/gpt-4.1-nano=low
PROVIDER_LATENCY_TIERS=openrouter/anthropic/claude-sonnet-4=medium;openai/gpt-4.1-mini=medium;openai/gpt-4.1-nano=low
PROVIDER_BUDGET_CLASSES=openrouter/anthropic/claude-sonnet-4=high;openai/gpt-4.1-mini=medium;openai/gpt-4.1-nano=low
```

Pattern-based examples for dynamic runtime paths:

```bash
LOCAL_RUNTIME_PATHS=mcp_*
RUNTIME_PROFILE_PREFERENCES=mcp_*=local|default
RUNTIME_POLICY_INTENTS=mcp_*=local_first|tool_use
RUNTIME_POLICY_SCORES=mcp_*=tool_use:5
RUNTIME_MODEL_OVERRIDES=mcp_*=openai/gpt-4.1-mini,mcp_github_actions=local:ollama/coder
RUNTIME_FALLBACK_OVERRIDES=mcp_*=openai/gpt-4.1-mini|openai/gpt-4.1-nano;mcp_github_actions=openai/gpt-4o-mini|openai/gpt-4.1-mini
PROVIDER_CAPABILITY_OVERRIDES=openai/gpt-4.1-mini=reasoning|tool_use;openai/gpt-4o-mini=fast|cheap
```

## Testing

```bash
cd backend
uv run pytest tests/ -v
```

## Tools

- **read_file / write_file** - Filesystem operations within workspace
- **web_search** - DuckDuckGo web search
- **fill_template** - Template variable substitution
