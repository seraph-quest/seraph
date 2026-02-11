from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    openrouter_api_key: str = ""
    default_model: str = "openrouter/anthropic/claude-sonnet-4"
    model_temperature: float = 0.7
    model_max_tokens: int = 4096
    agent_max_steps: int = 10
    debug: bool = False
    workspace_dir: str = "/app/data"

    # Phase 1 — Soul & Memory
    soul_file: str = "soul.md"
    embedding_model: str = "all-MiniLM-L6-v2"
    memory_search_top_k: int = 5
    session_history_window: int = 50

    # Phase 2 — Capable Executor
    sandbox_url: str = "http://sandbox:8060"
    sandbox_timeout: int = 35
    browser_timeout: int = 30
    google_credentials_path: str = "/app/config/google_credentials.json"
    google_calendar_token_path: str = "/app/data/google_calendar_token.json"
    google_gmail_token_path: str = "/app/data/google_gmail_token.json"

    # MCP — External tool servers
    things_mcp_url: str = ""  # e.g. "http://host.docker.internal:9100/mcp"
    github_mcp_url: str = ""  # e.g. "http://github-mcp:8090/mcp"
    github_mcp_toolsets: str = "repos,issues,pull_requests,code_security"

    # Phase 3 — Scheduler & Proactivity
    scheduler_enabled: bool = True
    proactivity_level: int = 3  # 1-5 scale
    morning_briefing_hour: int = 8
    evening_review_hour: int = 21
    memory_consolidation_interval_min: int = 30
    goal_check_interval_hours: int = 4
    calendar_scan_interval_min: int = 15
    strategist_interval_min: int = 15
    user_timezone: str = "UTC"
    working_hours_start: int = 9
    working_hours_end: int = 17
    observer_git_repo_path: str = ""
    deep_work_apps: str = ""  # comma-separated extra app keywords for deep work detection

    model_config = {"env_file": ".env.dev", "env_file_encoding": "utf-8"}


settings = Settings()
