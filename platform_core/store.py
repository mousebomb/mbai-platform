"""任务存储：SQLite（WAL）。整条 Job 以 JSON 存一份，避免过早拆表。

队列语义：state=queued 按创建时间取最早一条，用条件更新做原子领取。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .contracts import Job, JobState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state_created ON jobs(state, created_at);
"""


class JobStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")

    def add(self, job: Job) -> Job:
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs(id, state, created_at, payload) VALUES (?,?,?,?)",
                (job.id, job.state.value, job.created_at, job.model_dump_json()),
            )
            self._conn.commit()
        return job

    def save(self, job: Job) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET state=?, started_at=?, finished_at=?, payload=? WHERE id=?",
                (job.state.value, job.started_at, job.finished_at, job.model_dump_json(), job.id),
            )
            self._conn.commit()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.model_validate_json(row["payload"]) if row else None

    def list(self, limit: int = 50, state: JobState | None = None) -> list[Job]:
        sql = "SELECT payload FROM jobs"
        args: list[object] = []
        if state:
            sql += " WHERE state=?"
            args.append(state.value)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [Job.model_validate_json(r["payload"]) for r in rows]

    def claim_next(self) -> Job | None:
        """原子地把最早的一条 queued 置为 running；队列空则返回 None。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT id, payload FROM jobs WHERE state=? ORDER BY created_at LIMIT 1",
                    (JobState.QUEUED.value,),
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    return None
                job = Job.model_validate_json(row["payload"])
                job.state = JobState.RUNNING
                job.started_at = time.time()
                self._conn.execute(
                    "UPDATE jobs SET state=?, started_at=?, payload=? WHERE id=?",
                    (job.state.value, job.started_at, job.model_dump_json(), job.id),
                )
                self._conn.commit()
                return job
            except Exception:
                self._conn.rollback()
                raise

    def delete(self, job_id: str) -> bool:
        """删除任务记录；产物文件由调用方负责清理。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def requeue_running(self) -> int:
        """进程重启时把中断的任务放回队列，避免永久卡在 running。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM jobs WHERE state=?", (JobState.RUNNING.value,)
            ).fetchall()
            for row in rows:
                job = Job.model_validate_json(row["payload"])
                job.state = JobState.QUEUED
                job.started_at = None
                job.engine_ref = None
                self._conn.execute(
                    "UPDATE jobs SET state=?, started_at=NULL, payload=? WHERE id=?",
                    (job.state.value, job.model_dump_json(), job.id),
                )
            self._conn.commit()
            return len(rows)
