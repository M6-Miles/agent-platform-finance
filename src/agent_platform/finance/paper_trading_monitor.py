"""Persistent daily paper-trading monitor.

The monitor records wall-clock evidence for the specification's one-to-two-week
paper-trading acceptance. It never connects to a broker and never places a real
order. A unique ``(job_id, trading_date)`` key makes restarts and repeated polls
idempotent.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_platform.finance.paper_broker_service import PaperBrokerService
from agent_platform.finance.trading_calendar import TradingCalendar, WeekdayCandidateCalendar

logger = logging.getLogger(__name__)


def _local_now() -> datetime:
    return datetime.now().astimezone()


class PaperTradingMonitor:
    def __init__(
        self,
        path: Path | str,
        broker: PaperBrokerService,
        *,
        poll_interval_s: float = 30.0,
        configured_enabled: bool = False,
        calendar: TradingCalendar | None = None,
    ) -> None:
        self.path = Path(path)
        self.broker = broker
        self.poll_interval_s = max(1.0, float(poll_interval_s))
        self.configured_enabled = bool(configured_enabled)
        self.calendar = calendar or WeekdayCandidateCalendar()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._instance_id = str(uuid4())
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_monitor_jobs (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    symbols_json TEXT NOT NULL,
                    data_mode TEXT NOT NULL,
                    run_time TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_monitor_runs (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    trading_date TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    snapshot_json TEXT,
                    error TEXT,
                    UNIQUE(job_id, trading_date),
                    FOREIGN KEY(job_id) REFERENCES paper_monitor_jobs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_paper_monitor_runs_job
                ON paper_monitor_runs(job_id, trading_date DESC);
                CREATE TABLE IF NOT EXISTS paper_monitor_scheduler_lease (
                    lease_name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                """
            )

    def create_job(
        self,
        symbols: list[str],
        *,
        data_mode: str = "auto",
        run_time: str = "15:10",
        initial_cash: float = 1_000_000.0,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        codes = sorted({str(value).strip().upper() for value in symbols if str(value).strip()})
        if not codes:
            raise ValueError("symbols 不能为空")
        if data_mode not in {"auto", "offline"}:
            raise ValueError("data_mode 必须是 auto 或 offline")
        try:
            datetime.strptime(run_time, "%H:%M")
        except ValueError as exc:
            raise ValueError("run_time 必须使用 HH:MM 24 小时格式") from exc
        # Avoid creating an orphan account when the same logical task already exists.
        if account_id is not None:
            self.broker.get_account(account_id)

        for existing in self.list_jobs():
            same_account = account_id is None or existing["account_id"] == account_id
            if (
                existing["enabled"]
                and same_account
                and existing["symbols"] == codes
                and existing["data_mode"] == data_mode
                and existing["run_time"] == run_time
            ):
                return {**existing, "deduplicated": True}

        if account_id is None:
            account_id = self.broker.create_account(initial_cash)["account_id"]

        now = _local_now().isoformat(timespec="seconds")
        job_id = str(uuid4())
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO paper_monitor_jobs
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                (job_id, account_id, json.dumps(codes), data_mode, run_time, now, now),
            )
        return {**self.get_job(job_id), "deduplicated": False}

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM paper_monitor_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"模拟盘监控任务不存在: {job_id}")
        return self._job_dict(row)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM paper_monitor_jobs ORDER BY created_at DESC"
            ).fetchall()
        return [self._with_summary(self._job_dict(row)) for row in rows]

    def delete_job(self, job_id: str) -> None:
        """Delete a task and its monitor evidence, but preserve its paper account."""
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM paper_monitor_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(f"paper monitor job not found: {job_id}")
            connection.execute("DELETE FROM paper_monitor_runs WHERE job_id = ?", (job_id,))
            connection.execute("DELETE FROM paper_monitor_jobs WHERE id = ?", (job_id,))

    def set_enabled(self, job_id: str, enabled: bool) -> dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE paper_monitor_jobs SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), _local_now().isoformat(timespec="seconds"), job_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(f"模拟盘监控任务不存在: {job_id}")
        return self.get_job(job_id)

    def run_job(self, job_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or _local_now()
        job = self.get_job(job_id)
        trading_date = current.date().isoformat()
        run_id = str(uuid4())
        started_at = current.isoformat(timespec="seconds")

        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO paper_monitor_runs(
                           id, job_id, trading_date, started_at, status
                       ) VALUES (?, ?, ?, ?, 'running')""",
                    (run_id, job_id, trading_date, started_at),
                )
        except sqlite3.IntegrityError:
            return {**self.get_run_for_date(job_id, trading_date), "deduplicated": True}

        if not self.calendar.is_trading_day(current.date()):
            finished_at = _local_now().isoformat(timespec="seconds")
            with self._connect() as connection:
                connection.execute(
                    """UPDATE paper_monitor_runs
                       SET finished_at = ?, status = 'skipped_non_trading_day'
                       WHERE id = ?""",
                    (finished_at, run_id),
                )
            return {**self.get_run_for_date(job_id, trading_date), "deduplicated": False}

        try:
            account = self.broker.refresh(
                job["account_id"], symbols=job["symbols"], data_mode=job["data_mode"]
            )
            snapshot = {
                "recorded_at": _local_now().isoformat(timespec="seconds"),
                "trading_date": trading_date,
                "account_id": job["account_id"],
                "symbols": job["symbols"],
                "data_mode": job["data_mode"],
                "cash": account.get("cash"),
                "portfolio_value": account.get("portfolio_value"),
                "positions": account.get("positions", {}),
                "orders": account.get("orders", []),
                "trades": account.get("trades", []),
                "quotes": account.get("quotes", {}),
                "quote_errors": account.get("quote_errors", {}),
                "broker_kind": account.get("broker_kind"),
            }
            status = "partial" if snapshot["quote_errors"] else "completed"
            error = None
        except Exception as exc:  # persist operational failures for audit
            snapshot = None
            status = "failed"
            # Persist an actionable category without leaking provider URLs or secrets.
            error = f"{type(exc).__name__}: market data refresh failed"

        finished_at = _local_now().isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """UPDATE paper_monitor_runs
                   SET finished_at = ?, status = ?, snapshot_json = ?, error = ?
                   WHERE id = ?""",
                (
                    finished_at, status,
                    json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None,
                    error, run_id,
                ),
            )
        return {**self.get_run_for_date(job_id, trading_date), "deduplicated": False}

    def run_due(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or _local_now()
        if not self.calendar.is_trading_day(current.date()):
            return []
        if not self._acquire_scheduler_lease(current.timestamp()):
            return []
        hhmm = current.strftime("%H:%M")
        return [
            self.run_job(job["id"], now=current)
            for job in self.list_jobs()
            if job["enabled"] and job["run_time"] <= hhmm
        ]

    def _acquire_scheduler_lease(self, now_ts: float) -> bool:
        expires_at = now_ts + max(5.0, self.poll_interval_s * 2.5)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_id, expires_at FROM paper_monitor_scheduler_lease "
                "WHERE lease_name='daily-monitor'"
            ).fetchone()
            if row and row["owner_id"] != self._instance_id and float(row["expires_at"]) > now_ts:
                return False
            connection.execute(
                "INSERT INTO paper_monitor_scheduler_lease VALUES ('daily-monitor', ?, ?) "
                "ON CONFLICT(lease_name) DO UPDATE SET owner_id=excluded.owner_id, "
                "expires_at=excluded.expires_at",
                (self._instance_id, expires_at),
            )
        return True

    def list_runs(self, job_id: str) -> list[dict[str, Any]]:
        self.get_job(job_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM paper_monitor_runs
                   WHERE job_id = ? ORDER BY trading_date DESC""",
                (job_id,),
            ).fetchall()
        return [self._run_dict(row) for row in rows]

    def get_run_for_date(self, job_id: str, trading_date: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM paper_monitor_runs
                   WHERE job_id = ? AND trading_date = ?""",
                (job_id, trading_date),
            ).fetchone()
        if row is None:
            raise KeyError(f"任务 {job_id} 在 {trading_date} 没有运行记录")
        return self._run_dict(row)

    def status(self) -> dict[str, Any]:
        thread_alive = bool(self._thread and self._thread.is_alive())
        return {
            "configured_enabled": self.configured_enabled,
            "running": thread_alive,
            "thread_alive": thread_alive,
            "poll_interval_s": self.poll_interval_s,
            "current_time": _local_now().isoformat(timespec="seconds"),
            "job_count": len(self.list_jobs()),
            "calendar_basis": self.calendar.name,
            "calendar_authoritative": self.calendar.authoritative,
            "calendar_limitation": None if self.calendar.authoritative else "未接入交易所节假日日历，法定工作日仅作为候选交易日",
        }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        # Catch up today's due work immediately after a service restart. The
        # unique (job_id, trading_date) constraint keeps this idempotent.
        try:
            self.run_due()
        except Exception:
            logger.exception("paper monitor startup catch-up failed")

        def worker() -> None:
            while not self._stop.wait(self.poll_interval_s):
                try:
                    self.run_due()
                except Exception:
                    # Individual runs persist their own failure. A scheduler-level
                    # fault must not permanently kill the daemon thread.
                    logger.exception("paper monitor scheduler iteration failed")
                    continue

        self._thread = threading.Thread(
            target=worker, name="paper-trading-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(2.0, self.poll_interval_s + 1.0))
        self._thread = None

    def _with_summary(self, job: dict[str, Any]) -> dict[str, Any]:
        runs = self.list_runs(job["id"])
        completed = [run for run in runs if run["status"] == "completed"]
        valid_real = [run for run in completed if self._is_valid_real_evidence(run)]
        valid_dates = sorted({run["trading_date"] for run in valid_real})
        first_date = valid_dates[0] if valid_dates else None
        last_date = valid_dates[-1] if valid_dates else None
        missing_dates = self._missing_candidate_dates(first_date, last_date, set(valid_dates))
        evidence_days = len(valid_dates)
        minimum_met = evidence_days >= 7 and not missing_dates
        target_met = evidence_days >= 14 and not missing_dates
        skipped = [run for run in runs if run["status"] == "skipped_non_trading_day"]
        failed = [run for run in runs if run["status"] in {"failed", "partial"}]
        if not runs:
            evidence_status = "not_started"
        elif target_met:
            evidence_status = "validated_14_days"
        elif minimum_met:
            evidence_status = "validated_7_days"
        elif failed and not completed:
            evidence_status = "failed"
        else:
            evidence_status = "insufficient_observation"
        if job["enabled"] and not self.status_shallow()["running"] and not minimum_met:
            evidence_status = "scheduler_disabled"
        next_run = self._next_run_at(job["run_time"]) if job["enabled"] else None
        return {
            **job,
            "summary": {
                "total_runs": len(runs),
                "completed_candidate_days": len(completed),
                "valid_real_evidence_days": evidence_days,
                "first_valid_evidence_date": first_date,
                "last_valid_evidence_date": last_date,
                "calendar_span_days": (
                    (date.fromisoformat(last_date) - date.fromisoformat(first_date)).days + 1
                    if first_date and last_date else 0
                ),
                "missing_candidate_dates": missing_dates,
                "skipped_non_trading_days": len(skipped),
                "failed_or_partial_runs": len(failed),
                "target_min_days": 7,
                "target_max_days": 14,
                "remaining_min_days": max(0, 7 - evidence_days),
                "remaining_target_days": max(0, 14 - evidence_days),
                "minimum_acceptance_met": minimum_met,
                "full_target_met": target_met,
                "evidence_status": evidence_status,
                "latest_run": runs[0] if runs else None,
                "next_run_at": next_run,
                "calendar_basis": "weekday_candidate_only",
            },
        }

    def _missing_candidate_dates(
        self,
        first_date: str | None,
        last_date: str | None,
        observed: set[str],
    ) -> list[str]:
        if not first_date or not last_date:
            return []
        current = date.fromisoformat(first_date)
        end = date.fromisoformat(last_date)
        missing: list[str] = []
        while current <= end:
            if self.calendar.is_trading_day(current) and current.isoformat() not in observed:
                missing.append(current.isoformat())
            current += timedelta(days=1)
        return missing

    def status_shallow(self) -> dict[str, bool]:
        alive = bool(self._thread and self._thread.is_alive())
        return {"running": alive, "configured_enabled": self.configured_enabled}

    @staticmethod
    def _is_valid_real_evidence(run: dict[str, Any]) -> bool:
        snapshot = run.get("snapshot") or {}
        if snapshot.get("data_mode") != "auto" or snapshot.get("quote_errors"):
            return False
        quotes = snapshot.get("quotes") or {}
        return bool(quotes) and all(
            quote.get("data_status") == "live" and quote.get("source")
            for quote in quotes.values()
        )

    @staticmethod
    def _next_run_at(run_time: str) -> str:
        now = _local_now()
        hour, minute = (int(part) for part in run_time.split(":"))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate.isoformat(timespec="seconds")

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "account_id": row["account_id"],
            "symbols": json.loads(row["symbols_json"]), "data_mode": row["data_mode"],
            "run_time": row["run_time"], "enabled": bool(row["enabled"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "broker_kind": "MockBroker(本地模拟撮合，无真实券商连接)",
        }

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "job_id": row["job_id"],
            "trading_date": row["trading_date"], "started_at": row["started_at"],
            "finished_at": row["finished_at"], "status": row["status"],
            "snapshot": json.loads(row["snapshot_json"]) if row["snapshot_json"] else None,
            "error": row["error"],
        }
