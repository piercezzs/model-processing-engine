from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True)
class CacheRecord:
    namespace: str
    key: str
    result: dict[str, Any]
    metadata: dict[str, Any]
    created_at: float
    expires_at: float | None
    hit_count: int


class SQLiteRuntimeStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    namespace TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    task_version TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    last_accessed_at REAL NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (namespace, cache_key)
                );

                CREATE INDEX IF NOT EXISTS cache_entries_expiry_idx
                ON cache_entries(expires_at);

                CREATE TABLE IF NOT EXISTS execution_records (
                    execution_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    task_version TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS execution_records_status_idx
                ON execution_records(status, updated_at);
                """
            )

    def get_cache(self, namespace: str, key: str) -> CacheRecord | None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM cache_entries WHERE namespace = ? AND cache_key = ?",
                (namespace, key),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            expires_at = float(row["expires_at"]) if row["expires_at"] is not None else None
            if expires_at is not None and expires_at <= now:
                connection.execute(
                    "DELETE FROM cache_entries WHERE namespace = ? AND cache_key = ?",
                    (namespace, key),
                )
                connection.commit()
                return None
            hit_count = int(row["hit_count"]) + 1
            connection.execute(
                """
                UPDATE cache_entries
                SET hit_count = ?, last_accessed_at = ?
                WHERE namespace = ? AND cache_key = ?
                """,
                (hit_count, now, namespace, key),
            )
            connection.commit()
            return CacheRecord(
                namespace=namespace,
                key=key,
                result=_json_object(row["result_json"]),
                metadata=_json_object(row["metadata_json"]),
                created_at=float(row["created_at"]),
                expires_at=expires_at,
                hit_count=hit_count,
            )

    def put_cache(
        self,
        *,
        namespace: str,
        key: str,
        task_id: str,
        task_version: str,
        provider_id: str,
        model: str,
        result: dict[str, Any],
        metadata: dict[str, Any],
        ttl_seconds: int | None,
    ) -> None:
        now = time.time()
        expires_at = now + ttl_seconds if ttl_seconds is not None else None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cache_entries (
                    namespace, cache_key, task_id, task_version, provider_id, model,
                    result_json, metadata_json, created_at, expires_at,
                    last_accessed_at, hit_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(namespace, cache_key) DO UPDATE SET
                    task_id = excluded.task_id,
                    task_version = excluded.task_version,
                    provider_id = excluded.provider_id,
                    model = excluded.model,
                    result_json = excluded.result_json,
                    metadata_json = excluded.metadata_json,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at,
                    last_accessed_at = excluded.last_accessed_at,
                    hit_count = 0
                """,
                (
                    namespace,
                    key,
                    task_id,
                    task_version,
                    provider_id,
                    model,
                    _json_text(result),
                    _json_text(metadata),
                    now,
                    expires_at,
                    now,
                ),
            )

    def delete_cache(self, namespace: str, key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM cache_entries WHERE namespace = ? AND cache_key = ?",
                (namespace, key),
            )

    def cleanup_expired(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM cache_entries WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (time.time(),),
            )
            return max(0, int(cursor.rowcount))

    def purge_namespace(self, namespace: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM cache_entries WHERE namespace = ?",
                (namespace,),
            )
            return max(0, int(cursor.rowcount))

    def cache_count(self, namespace: str | None = None) -> int:
        with self._connect() as connection:
            if namespace is None:
                row = connection.execute("SELECT COUNT(*) AS count FROM cache_entries").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM cache_entries WHERE namespace = ?",
                    (namespace,),
                ).fetchone()
            return int(row["count"] if row else 0)

    def save_execution(self, envelope: dict[str, Any]) -> None:
        now = time.time()
        task = envelope.get("task") if isinstance(envelope.get("task"), dict) else {}
        execution_id = str(envelope.get("executionId") or "")
        if not execution_id:
            raise ValueError("executionId is required")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO execution_records (
                    execution_id, status, namespace, task_id, task_version,
                    envelope_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET
                    status = excluded.status,
                    namespace = excluded.namespace,
                    task_id = excluded.task_id,
                    task_version = excluded.task_version,
                    envelope_json = excluded.envelope_json,
                    updated_at = excluded.updated_at
                """,
                (
                    execution_id,
                    str(envelope.get("status") or "failed"),
                    str(task.get("namespace") or ""),
                    str(task.get("id") or ""),
                    str(task.get("version") or ""),
                    _json_text(envelope),
                    now,
                    now,
                ),
            )

    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT envelope_json FROM execution_records WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return _json_object(row["envelope_json"]) if row else None

    def recover_incomplete_executions(self) -> int:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT execution_id, envelope_json FROM execution_records WHERE status IN ('queued', 'running')"
            ).fetchall()
            for row in rows:
                envelope = _json_object(row["envelope_json"])
                envelope.update(
                    {
                        "status": "failed",
                        "error": "Execution was interrupted by a runtime restart",
                    }
                )
                connection.execute(
                    """
                    UPDATE execution_records
                    SET status = 'failed', envelope_json = ?, updated_at = ?
                    WHERE execution_id = ?
                    """,
                    (_json_text(envelope), time.time(), row["execution_id"]),
                )
            return len(rows)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    return dict(parsed) if isinstance(parsed, dict) else {}
