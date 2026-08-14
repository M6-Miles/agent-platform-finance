from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent_platform.finance.analysis import SecurityAnalysisResult


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    title: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: str
    session_id: str
    role: str
    content: str
    provider: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class UserRecord:
    id: str
    username: str
    email: str | None
    avatar_color: str
    created_at: str
    role: str = "user"


@dataclass(frozen=True, slots=True)
class AnalysisRecord:
    id: str
    session_id: str | None
    trigger: str
    symbol: str
    market: str
    name: str
    start_date: str
    end_date: str
    source: str
    data_updated_at: str
    total_return_pct: float
    annualized_volatility_pct: float
    max_drawdown_pct: float
    latest_close: float
    latest_ma5: float
    disclaimer: str
    created_at: str


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    provider TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_chat_messages_session
                ON chat_messages(session_id, created_at);

                CREATE TABLE IF NOT EXISTS analysis_records (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    trigger TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    name TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    data_updated_at TEXT NOT NULL,
                    total_return_pct REAL NOT NULL,
                    annualized_volatility_pct REAL NOT NULL,
                    max_drawdown_pct REAL NOT NULL,
                    latest_close REAL NOT NULL,
                    latest_ma5 REAL NOT NULL,
                    disclaimer TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_analysis_records_created
                ON analysis_records(created_at DESC);

                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    avatar_color TEXT NOT NULL DEFAULT '#2a78d6',
                    created_at TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user'
                );

                CREATE TABLE IF NOT EXISTS resource_ownership (
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(resource_type, resource_id),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_resource_owner
                ON resource_ownership(user_id, resource_type);
                """
            )
            connection.execute("PRAGMA user_version = 2")
        # 向后兼容：为旧数据库添加 pinned 列（SQLite 不支持 IF NOT EXISTS on ALTER）
        with self._connect() as connection:
            try:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"
                )
            except Exception:
                pass  # 列已存在，跳过

        with self._connect() as connection:
            user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
            if "role" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
            admin_count = connection.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin'"
            ).fetchone()[0]
            if admin_count == 0:
                first_user = connection.execute(
                    "SELECT id FROM users ORDER BY created_at ASC, rowid ASC LIMIT 1"
                ).fetchone()
                if first_user is not None:
                    connection.execute(
                        "UPDATE users SET role='admin' WHERE id=?", (first_user["id"],)
                    )

    def rename_session(self, session_id: str, new_title: str) -> None:
        """重命名会话。标题为空时保留原标题。会话不存在时抛出 ValueError。"""
        normalized = (new_title or "").strip()
        if not normalized:
            return
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (normalized[:120], utc_now(), session_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"会话不存在：{session_id}")

    def delete_session(self, session_id: str) -> None:
        """删除会话及其所有消息（analysis_records 中的关联置 NULL）。会话不存在时抛出 ValueError。"""
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            if cursor.rowcount == 0:
                raise ValueError(f"会话不存在：{session_id}")

    def create_session(self, title: str | None = None) -> SessionRecord:
        session_id = str(uuid4())
        timestamp = utc_now()
        normalized_title = (title or "新会话").strip() or "新会话"
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, normalized_title, timestamp, timestamp),
            )
        return SessionRecord(session_id, normalized_title, timestamp, timestamp)

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, title, created_at, updated_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return self._session_from_row(row) if row else None

    def list_sessions(self, limit: int = 20) -> list[SessionRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, title, created_at, updated_at
                FROM sessions
                ORDER BY COALESCE(pinned, 0) DESC, updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def pin_session(self, session_id: str) -> None:
        """置顶会话：设置 pinned=1，updated_at 置为极远未来以确保排在最前。"""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET pinned = 1, updated_at = ? WHERE id = ?",
                ("9999-12-31T23:59:59+00:00", session_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"会话不存在：{session_id}")

    def unpin_session(self, session_id: str) -> None:
        """取消置顶：恢复 pinned=0，updated_at 重置为当前时间。"""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET pinned = 0, updated_at = ? WHERE id = ?",
                (utc_now(), session_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"会话不存在：{session_id}")

    # ── 用户认证 ──────────────────────────────────────────────────────────────

    _AVATAR_COLORS = [
        "#2a78d6", "#16a34a", "#dc2626", "#d97706",
        "#7c3aed", "#0891b2", "#be185d", "#ea580c",
    ]

    @staticmethod
    def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
        """返回 (hash_hex, salt_hex)。"""
        if salt is None:
            salt = secrets.token_hex(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), 260_000)
        return dk.hex(), salt

    def create_user(
        self,
        username: str,
        password: str,
        email: str | None = None,
        avatar_color: str | None = None,
        role: str = "user",
    ) -> UserRecord:
        """创建新用户。用户名重复时抛出 ValueError。"""
        username = username.strip()
        if len(username) < 2 or len(username) > 20:
            raise ValueError("用户名长度需为 2–20 个字符")
        if len(password) < 8:
            raise ValueError("密码长度至少 8 位")
        if role not in {"admin", "user"}:
            raise ValueError("role 必须是 admin 或 user")
        pwd_hash, salt = self._hash_password(password)
        color = avatar_color or self._AVATAR_COLORS[hash(username) % len(self._AVATAR_COLORS)]
        user_id = str(uuid4())
        timestamp = utc_now()
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO users(id, username, email, password_hash, salt, avatar_color, created_at, role)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, username, email, pwd_hash, salt, color, timestamp, role),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"用户名「{username}」已被占用") from exc
        return UserRecord(user_id, username, email, color, timestamp, role)

    def get_user_by_username(self, username: str) -> UserRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, username, email, avatar_color, created_at, role"
                " FROM users WHERE username = ?",
                (username.strip(),),
            ).fetchone()
        return UserRecord(**dict(row)) if row else None

    def list_users(self) -> list[UserRecord]:
        """Return public user profile fields for administrator views."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, username, email, avatar_color, created_at, role"
                " FROM users ORDER BY created_at ASC, username ASC"
            ).fetchall()
        return [UserRecord(**dict(row)) for row in rows]

    def update_user_role(self, user_id: str, role: str) -> UserRecord:
        """Update a role while guaranteeing that at least one admin remains."""
        if role not in {"admin", "user"}:
            raise ValueError("role 必须是 admin 或 user")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, role FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if row is None:
                raise LookupError("用户不存在")
            if row["role"] == "admin" and role != "admin":
                admin_count = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE role = 'admin'"
                ).fetchone()[0]
                if admin_count <= 1:
                    raise ValueError("不能移除系统中最后一个管理员")
            connection.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
            updated = connection.execute(
                "SELECT id, username, email, avatar_color, created_at, role"
                " FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return UserRecord(**dict(updated))

    def verify_user(self, username: str, password: str) -> UserRecord | None:
        """验证密码，成功返回 UserRecord，失败返回 None。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, username, email, password_hash, salt, avatar_color, created_at, role"
                " FROM users WHERE username = ?",
                (username.strip(),),
            ).fetchone()
        if row is None:
            return None
        expected_hash, _ = self._hash_password(password, salt=row["salt"])
        if not hmac.compare_digest(expected_hash, row["password_hash"]):
            return None
        return UserRecord(row["id"], row["username"], row["email"], row["avatar_color"], row["created_at"], row["role"])

    def change_password(self, user_id: str, current_password: str, new_password: str) -> None:
        """Verify the current password and replace its PBKDF2 hash using a new salt."""
        if len(new_password) < 8:
            raise ValueError("新密码长度至少 8 位")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT password_hash, salt FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if row is None:
                raise LookupError("用户不存在")
            current_hash, _ = self._hash_password(current_password, salt=row["salt"])
            if not hmac.compare_digest(current_hash, row["password_hash"]):
                raise PermissionError("当前密码错误")
            password_hash, salt = self._hash_password(new_password)
            connection.execute(
                "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
                (password_hash, salt, user_id),
            )

    def has_any_user(self) -> bool:
        """是否已有任何用户（首次启动引导）。"""
        with self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return count > 0

    def claim_resource(self, resource_type: str, resource_id: str, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO resource_ownership VALUES (?, ?, ?, ?)",
                (resource_type, resource_id, user_id, utc_now()),
            )

    def resource_owner(self, resource_type: str, resource_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id FROM resource_ownership WHERE resource_type=? AND resource_id=?",
                (resource_type, resource_id),
            ).fetchone()
        return str(row["user_id"]) if row else None

    def owned_resource_ids(self, resource_type: str, user_id: str) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT resource_id FROM resource_ownership WHERE resource_type=? AND user_id=?",
                (resource_type, user_id),
            ).fetchall()
        return {str(row["resource_id"]) for row in rows}

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        provider: str | None = None,
    ) -> MessageRecord:
        if self.get_session(session_id) is None:
            raise ValueError(f"会话不存在：{session_id}")
        message_id = str(uuid4())
        timestamp = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO chat_messages(id, session_id, role, content, provider, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (message_id, session_id, role, content, provider, timestamp),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (timestamp, session_id),
            )
        return MessageRecord(message_id, session_id, role, content, provider, timestamp)

    def list_messages(self, session_id: str) -> list[MessageRecord]:
        if self.get_session(session_id) is None:
            raise ValueError(f"会话不存在：{session_id}")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, session_id, role, content, provider, created_at
                FROM chat_messages
                WHERE session_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (session_id,),
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def add_analysis(
        self,
        result: SecurityAnalysisResult,
        trigger: str,
        session_id: str | None = None,
    ) -> AnalysisRecord:
        if session_id is not None and self.get_session(session_id) is None:
            raise ValueError(f"会话不存在：{session_id}")
        record_id = str(uuid4())
        timestamp = utc_now()
        values = (
            record_id,
            session_id,
            trigger,
            result.symbol,
            result.market,
            result.name,
            result.start_date,
            result.end_date,
            result.source,
            result.updated_at,
            result.total_return_pct,
            result.annualized_volatility_pct,
            result.max_drawdown_pct,
            result.latest_close,
            result.latest_ma5,
            result.disclaimer,
            timestamp,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO analysis_records(
                    id, session_id, trigger, symbol, market, name,
                    start_date, end_date, source, data_updated_at,
                    total_return_pct, annualized_volatility_pct,
                    max_drawdown_pct, latest_close, latest_ma5,
                    disclaimer, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        return AnalysisRecord(*values)

    def list_analyses(self, limit: int = 20) -> list[AnalysisRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, session_id, trigger, symbol, market, name,
                       start_date, end_date, source, data_updated_at,
                       total_return_pct, annualized_volatility_pct,
                       max_drawdown_pct, latest_close, latest_ma5,
                       disclaimer, created_at
                FROM analysis_records
                ORDER BY created_at DESC, rowid DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [AnalysisRecord(**dict(row)) for row in rows]

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(**dict(row))

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(**dict(row))
