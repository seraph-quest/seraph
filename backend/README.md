# Seraph Backend

AI assistant backend powered by FastAPI, smolagents, and LiteLLM (via OpenRouter).

## Setup

1. Copy env vars and set your API key:
   ```bash
   # In the root .env.dev, set:
   OPENROUTER_API_KEY=your-actual-key
   ```

2. Install dependencies:
   ```bash
   cd backend
   uv sync
   ```

3. Run locally:
   ```bash
   uv run main.py
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
| `OPENROUTER_API_KEY` | - | OpenRouter API key (required) |
| `DEFAULT_MODEL` | `openrouter/anthropic/claude-sonnet-4` | LLM model identifier |
| `MODEL_TEMPERATURE` | `0.7` | Generation temperature |
| `MODEL_MAX_TOKENS` | `4096` | Max response tokens |
| `AGENT_MAX_STEPS` | `10` | Max agent reasoning steps |
| `DEBUG` | `false` | Enable debug mode |
| `WORKSPACE_DIR` | `/app/data` | Agent file workspace |
| `LOCAL_MODEL` | - | Model id for the local runtime profile |
| `LOCAL_LLM_API_BASE` | - | API base for the local runtime profile |
| `LOCAL_RUNTIME_PATHS` | - | Comma-separated runtime paths that should prefer the local profile |
| `RUNTIME_MODEL_OVERRIDES` | - | Comma-separated `runtime_path=model` or `runtime_path=profile:model` overrides |
| `RUNTIME_FALLBACK_OVERRIDES` | - | Semicolon-separated `runtime_path=model_a|model_b` fallback chains |
| `FALLBACK_MODEL` | - | Legacy single fallback target |
| `FALLBACK_MODELS` | - | Comma-separated ordered global fallback chain |
| `FALLBACK_LLM_API_BASE` | - | Optional API base override for fallback calls |
| `LLM_LOG_ENABLED` | `true` | Enable LLM call logging to JSONL file |
| `LLM_LOG_CONTENT` | `false` | Include full messages/response in log |
| `LLM_LOG_DIR` | `/app/logs` | Log file directory |
| `LLM_LOG_MAX_BYTES` | `52428800` | Max bytes per log file before rotation (50 MB) |
| `LLM_LOG_BACKUP_COUNT` | `5` | Number of rotated log files to keep |

Runtime routing examples:

```bash
LOCAL_RUNTIME_PATHS=chat_agent,session_consolidation,daily_briefing
RUNTIME_MODEL_OVERRIDES=chat_agent=default:openai/gpt-4.1-mini
RUNTIME_FALLBACK_OVERRIDES=chat_agent=openai/gpt-4.1-mini|openai/gpt-4.1-nano;session_title_generation=openai/gpt-4o-mini|openai/gpt-4.1-mini
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
