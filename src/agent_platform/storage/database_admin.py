"""SQLite backup, integrity verification, and retention utilities."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path


def backup_database(source: Path, destination: Path) -> dict[str, str | int]:
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source == destination:
        raise ValueError("备份目标不能与源数据库相同")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source, timeout=10.0) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"备份完整性检查失败: {check}")
    return {
        "source": str(source), "destination": str(destination),
        "size_bytes": destination.stat().st_size,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def database_health(path: Path) -> dict[str, str | int]:
    with sqlite3.connect(path, timeout=5.0) as connection:
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    return {
        "integrity": integrity, "journal_mode": journal_mode,
        "schema_version": schema_version,
    }


def prune_operational_data(path: Path, *, retention_days: int = 90) -> dict[str, int]:
    if retention_days < 7:
        raise ValueError("retention_days 不能小于 7")
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat(timespec="seconds")
    deleted: dict[str, int] = {}
    with sqlite3.connect(path, timeout=10.0) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for table, column in (
            ("observability_calls", "started_at"),
            ("paper_order_requests", "created_at"),
        ):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists:
                cursor = connection.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                deleted[table] = cursor.rowcount
    return deleted
