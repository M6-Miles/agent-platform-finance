from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 自动加载项目根目录的 .env 文件（如果存在）
_env_path = PROJECT_ROOT / ".env"
try:
    from dotenv import load_dotenv
    # load_dotenv safely returns False when the file is absent. Calling it in
    # both cases keeps local and clean CI environments behaviorally identical.
    # Explicit process settings must always win over local defaults.
    load_dotenv(_env_path, override=False)
except ImportError:
    pass  # python-dotenv 不可用时不报错
DEFAULT_APP_NAME = "通用 Agent 平台及证券金融分析应用"
DEFAULT_SAMPLE_PRICES_CSV = PROJECT_ROOT / "data" / "sample" / "prices.csv"
DEFAULT_SQLITE_PATH = PROJECT_ROOT / "data" / "app.sqlite3"


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = DEFAULT_APP_NAME
    app_env: str = "local"
    llm_provider: str = "mock"
    market_data_provider: str = "sample"
    sample_prices_csv: Path = DEFAULT_SAMPLE_PRICES_CSV
    sqlite_path: Path = DEFAULT_SQLITE_PATH
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    # LangGraph checkpoint 模式：
    #   False（默认）— 使用 SQLite，checkpoint 写磁盘，interrupt 跨进程可恢复
    #   True          — 使用 MemorySaver（仅测试/临时开发），不跨进程、不跨实例恢复
    langgraph_use_memory_saver: bool = False
    paper_monitor_enabled: bool = False
    paper_monitor_poll_interval_s: float = 30.0
    auth_enabled: bool = False
    auth_secret: str = ""
    auth_token_ttl_s: int = 28_800
    auth_registration_enabled: bool = False
    allowed_origins: tuple[str, ...] = ("http://127.0.0.1:8003", "http://localhost:8003")
    trusted_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "testserver")


def _resolve_project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _parse_bool(value: str) -> bool:
    """严格布尔解析：只接受 "1"/"true"/"yes"/"on"（忽略大小写）为 True。

    注意：bool("false") == True，此函数避免该陷阱。
    """
    return value.strip().lower() in ("1", "true", "yes", "on")


def get_settings() -> Settings:
    settings = Settings(
        app_name=os.getenv("APP_NAME", DEFAULT_APP_NAME),
        app_env=os.getenv("APP_ENV", "local"),
        llm_provider=os.getenv("LLM_PROVIDER", "mock"),
        market_data_provider=os.getenv("MARKET_DATA_PROVIDER", "sample"),
        sample_prices_csv=_resolve_project_path(
            os.getenv("SAMPLE_PRICES_CSV", str(DEFAULT_SAMPLE_PRICES_CSV))
        ),
        sqlite_path=_resolve_project_path(
            os.getenv("SQLITE_PATH", str(DEFAULT_SQLITE_PATH))
        ),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        langgraph_use_memory_saver=_parse_bool(
            os.getenv("LANGGRAPH_USE_MEMORY_SAVER", "false")
        ),
        paper_monitor_enabled=_parse_bool(os.getenv("PAPER_MONITOR_ENABLED", "false")),
        paper_monitor_poll_interval_s=float(
            os.getenv("PAPER_MONITOR_POLL_INTERVAL_S", "30")
        ),
        auth_enabled=_parse_bool(os.getenv("AUTH_ENABLED", "false")),
        auth_secret=os.getenv("AUTH_SECRET", ""),
        auth_token_ttl_s=int(os.getenv("AUTH_TOKEN_TTL_S", "28800")),
        auth_registration_enabled=_parse_bool(
            os.getenv("AUTH_REGISTRATION_ENABLED", "false")
        ),
        allowed_origins=tuple(
            item.strip() for item in os.getenv(
                "ALLOWED_ORIGINS", "http://127.0.0.1:8003,http://localhost:8003"
            ).split(",") if item.strip()
        ),
        trusted_hosts=tuple(
            item.strip() for item in os.getenv(
                "TRUSTED_HOSTS", "127.0.0.1,localhost,testserver"
            ).split(",") if item.strip()
        ),
    )
    if settings.app_env.lower() in {"production", "prod"}:
        if not settings.auth_enabled:
            raise RuntimeError("production 环境必须设置 AUTH_ENABLED=true")
        if len(settings.auth_secret) < 32:
            raise RuntimeError("production 环境 AUTH_SECRET 至少需要 32 个字符")
        if "*" in settings.allowed_origins or "*" in settings.trusted_hosts:
            raise RuntimeError("production 环境禁止使用通配符 CORS 或 TrustedHost")
    if settings.auth_enabled and len(settings.auth_secret) < 32:
        raise RuntimeError("启用认证时 AUTH_SECRET 至少需要 32 个字符")
    return settings
