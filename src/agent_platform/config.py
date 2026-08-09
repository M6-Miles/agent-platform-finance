from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 自动加载项目根目录的 .env 文件（如果存在）
_env_path = PROJECT_ROOT / ".env"
if _env_path.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path, override=True)
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


def _resolve_project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _parse_bool(value: str) -> bool:
    """严格布尔解析：只接受 "1"/"true"/"yes"/"on"（忽略大小写）为 True。

    注意：bool("false") == True，此函数避免该陷阱。
    """
    return value.strip().lower() in ("1", "true", "yes", "on")


def get_settings() -> Settings:
    return Settings(
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
    )
