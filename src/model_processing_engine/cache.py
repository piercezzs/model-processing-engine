from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .exceptions import AsyncQueueFullError
from .file_store import ensure_private_file


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
        self.path = Path(path).expanduser().absolute()
        ensure_private_file(self.path)
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
                    kind TEXT NOT NULL DEFAULT 'task',
                    provider_id TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    envelope_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS execution_records_status_idx
                ON execution_records(status, updated_at);

                CREATE TABLE IF NOT EXISTS provider_call_records (
                    call_id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    provider_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    reasoning_effort TEXT NOT NULL DEFAULT 'auto',
                    status TEXT NOT NULL,
                    usage_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    elapsed_ms INTEGER NOT NULL,
                    error TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS provider_call_execution_idx
                ON provider_call_records(execution_id, sequence);

                CREATE TABLE IF NOT EXISTS async_execution_queue (
                    execution_id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'running')),
                    enqueued_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    recovery_count INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(execution_id) REFERENCES execution_records(execution_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS async_execution_queue_status_idx
                ON async_execution_queue(status, enqueued_at);
                """
            )
            _migrate_execution_history_schema(connection)

    def get_cache(self, namespace: str, key: str) -> CacheRecord | None:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cache_entries WHERE namespace = ? AND cache_key = ?",
                (namespace, key),
            ).fetchone()
            if row is None:
                return None
            expires_at = float(row["expires_at"]) if row["expires_at"] is not None else None
            if expires_at is not None and expires_at <= now:
                connection.execute(
                    "DELETE FROM cache_entries WHERE namespace = ? AND cache_key = ?",
                    (namespace, key),
                )
                return None
            return CacheRecord(
                namespace=namespace,
                key=key,
                result=_json_object(row["result_json"]),
                metadata=_json_object(row["metadata_json"]),
                created_at=float(row["created_at"]),
                expires_at=expires_at,
                hit_count=int(row["hit_count"]),
            )

    def save_cache_hit_execution(
        self,
        *,
        namespace: str,
        key: str,
        expected_created_at: float,
        envelope: dict[str, Any],
    ) -> int | None:
        """Persist one cache hit and its terminal execution in one transaction."""
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE cache_entries
                SET hit_count = hit_count + 1, last_accessed_at = ?
                WHERE namespace = ?
                  AND cache_key = ?
                  AND created_at = ?
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (now, namespace, key, expected_created_at, now),
            )
            if updated.rowcount != 1:
                return None
            row = connection.execute(
                """
                SELECT hit_count
                FROM cache_entries
                WHERE namespace = ? AND cache_key = ? AND created_at = ?
                """,
                (namespace, key, expected_created_at),
            ).fetchone()
            if row is None:
                return None
            hit_count = int(row["hit_count"])
            cache = envelope.get("cache")
            if isinstance(cache, dict):
                cache["hitCount"] = hit_count
            _upsert_execution(connection, envelope, now=now)
            return hit_count

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

    def purge_cache_schema_mismatches(self, expected_schema: str) -> int:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT namespace, cache_key, metadata_json FROM cache_entries"
            ).fetchall()
            stale_entries = [
                (str(row["namespace"]), str(row["cache_key"]))
                for row in rows
                if _json_object(row["metadata_json"]).get("cacheSchema")
                != expected_schema
            ]
            connection.executemany(
                "DELETE FROM cache_entries WHERE namespace = ? AND cache_key = ?",
                stale_entries,
            )
            return len(stale_entries)

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
        with self._connect() as connection:
            _upsert_execution(connection, envelope, now=now)

    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT envelope_json FROM execution_records WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return _json_object(row["envelope_json"]) if row else None

    def enqueue_async_execution(
        self,
        *,
        envelope: dict[str, Any],
        request: dict[str, Any],
        capacity: int,
    ) -> None:
        now = time.time()
        execution_id = str(envelope.get("executionId") or "")
        if not execution_id:
            raise ValueError("executionId is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM async_execution_queue"
            ).fetchone()
            if int(row["count"] if row else 0) >= max(1, capacity):
                raise AsyncQueueFullError(
                    f"Async execution queue is full (capacity {max(1, capacity)})"
                )
            _upsert_execution(connection, envelope, now=now)
            connection.execute(
                """
                INSERT INTO async_execution_queue (
                    execution_id, request_json, status, enqueued_at, updated_at,
                    recovery_count
                ) VALUES (?, ?, 'queued', ?, ?, 0)
                """,
                (execution_id, _json_text(request), now, now),
            )

    def claim_next_async_execution(
        self,
    ) -> tuple[str, dict[str, Any]] | None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT execution_id, request_json
                FROM async_execution_queue
                WHERE status = 'queued'
                ORDER BY enqueued_at, execution_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            execution_id = str(row["execution_id"])
            connection.execute(
                """
                UPDATE async_execution_queue
                SET status = 'running', updated_at = ?
                WHERE execution_id = ?
                """,
                (now, execution_id),
            )
            execution_row = connection.execute(
                "SELECT envelope_json FROM execution_records WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if execution_row:
                envelope = _json_object(execution_row["envelope_json"])
                envelope["status"] = "running"
                envelope["progress"] = {"event": "dequeued"}
                _upsert_execution(connection, envelope, now=now)
            return execution_id, _json_object(row["request_json"])

    def finish_async_execution(self, execution_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM async_execution_queue WHERE execution_id = ?",
                (execution_id,),
            )

    def fail_async_execution(self, execution_id: str, *, error: str) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT envelope_json FROM execution_records WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row:
                envelope = _json_object(row["envelope_json"])
                envelope.update(
                    {
                        "status": "failed",
                        "error": str(error)[:1000],
                        "progress": {"event": "failed"},
                    }
                )
                _upsert_execution(connection, envelope, now=now)
            connection.execute(
                "DELETE FROM async_execution_queue WHERE execution_id = ?",
                (execution_id,),
            )

    def async_queue_stats(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM async_execution_queue
                GROUP BY status
                """
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        queued = counts.get("queued", 0)
        running = counts.get("running", 0)
        return {"queued": queued, "running": running, "total": queued + running}

    def has_queued_async_executions(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM async_execution_queue
                WHERE status = 'queued'
                LIMIT 1
                """
            ).fetchone()
        return row is not None

    def recover_async_executions(self) -> dict[str, int]:
        now = time.time()
        recovered = 0
        orphaned = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT queue.execution_id, queue.status, records.status AS record_status,
                    records.envelope_json
                FROM async_execution_queue AS queue
                JOIN execution_records AS records
                    ON records.execution_id = queue.execution_id
                ORDER BY queue.enqueued_at
                """
            ).fetchall()
            for row in rows:
                execution_id = str(row["execution_id"])
                if str(row["record_status"]) in {"succeeded", "failed", "cancelled"}:
                    connection.execute(
                        "DELETE FROM async_execution_queue WHERE execution_id = ?",
                        (execution_id,),
                    )
                    continue
                was_running = str(row["status"]) == "running"
                envelope = _json_object(row["envelope_json"])
                envelope["status"] = "queued"
                envelope["error"] = None
                if was_running:
                    recovered += 1
                    envelope["progress"] = {"event": "recovered_after_restart"}
                _upsert_execution(connection, envelope, now=now)
                connection.execute(
                    """
                    UPDATE async_execution_queue
                    SET status = 'queued', updated_at = ?,
                        recovery_count = recovery_count + ?
                    WHERE execution_id = ?
                    """,
                    (now, int(was_running), execution_id),
                )

            orphan_rows = connection.execute(
                """
                SELECT records.execution_id, records.envelope_json
                FROM execution_records AS records
                LEFT JOIN async_execution_queue AS queue
                    ON queue.execution_id = records.execution_id
                WHERE records.status IN ('queued', 'running')
                    AND queue.execution_id IS NULL
                """
            ).fetchall()
            for row in orphan_rows:
                envelope = _json_object(row["envelope_json"])
                envelope.update(
                    {
                        "status": "failed",
                        "error": "Execution was interrupted before durable queue recovery was available",
                        "progress": {"event": "failed"},
                    }
                )
                _upsert_execution(connection, envelope, now=now)
                orphaned += 1
        return {"recovered": recovered, "orphaned": orphaned}

    def save_provider_call(self, record: dict[str, Any]) -> None:
        call_id = str(record.get("callId") or "")
        execution_id = str(record.get("executionId") or "")
        if not call_id or not execution_id:
            raise ValueError("callId and executionId are required")
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_call_records (
                    call_id, execution_id, purpose, sequence, provider_id,
                    model, reasoning_effort, status, usage_json, attempts,
                    elapsed_ms, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    execution_id,
                    str(record.get("purpose") or "task"),
                    max(1, int(record.get("sequence") or 1)),
                    str(record.get("providerId") or ""),
                    str(record.get("model") or ""),
                    str(record.get("reasoningEffort") or "auto"),
                    str(record.get("status") or "failed"),
                    _json_text(usage),
                    _non_negative_int(record.get("attempts")),
                    _non_negative_int(record.get("elapsedMs")),
                    str(record.get("error") or "")[:240],
                    time.time(),
                ),
            )

    def execution_history(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        namespace: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        provider_id: str | None = None,
        model: str | None = None,
        created_from: float | None = None,
        created_to: float | None = None,
        include_summary: bool = True,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if namespace:
            clauses.append("namespace = ?")
            parameters.append(namespace)
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        if kind:
            clauses.append("kind = ?")
            parameters.append(kind)
        if provider_id:
            clauses.append("provider_id = ?")
            parameters.append(provider_id)
        if model:
            clauses.append("model = ?")
            parameters.append(model)
        if created_from is not None:
            clauses.append("created_at >= ?")
            parameters.append(float(created_from))
        if created_to is not None:
            clauses.append("created_at < ?")
            parameters.append(float(created_to))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded_limit = max(1, min(int(limit), 200))
        bounded_offset = max(0, int(offset))
        with self._connect() as connection:
            total_row = connection.execute(
                f"SELECT COUNT(*) AS count FROM execution_records{where}",
                tuple(parameters),
            ).fetchone()
            page_rows = connection.execute(
                f"""
                SELECT execution_id, envelope_json, created_at
                FROM execution_records{where}
                ORDER BY created_at DESC, execution_id DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, bounded_limit, bounded_offset),
            ).fetchall()
            provider_calls = _provider_calls_by_execution(
                connection,
                [str(row["execution_id"]) for row in page_rows],
            )
            items = [
                _execution_summary(
                    _json_object(row["envelope_json"]),
                    provider_calls.get(str(row["execution_id"]), []),
                    recorded_at=float(row["created_at"]),
                )
                for row in page_rows
            ]
            summary = None
            if include_summary:
                summary_cursor = connection.execute(
                    f"""
                    SELECT execution_id, envelope_json, created_at
                    FROM execution_records{where}
                    """,
                    tuple(parameters),
                )
                summary = _execution_aggregate(
                    _iter_execution_summaries(connection, summary_cursor)
                )
        total = int(total_row["count"] if total_row else 0)
        payload = {
            "items": items,
            "summary": summary,
            "pagination": {
                "limit": bounded_limit,
                "offset": bounded_offset,
                "total": total,
                "hasMore": bounded_offset + len(items) < total,
            },
        }
        return payload

    def execution_statistics(
        self,
        *,
        period: str,
        anchor: str,
        timezone_name: str,
        namespace: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        kind: str | None = "task",
        provider_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        window = _period_window(period, anchor, timezone_name)
        base_clauses = ["created_at >= ?", "created_at < ?"]
        base_parameters: list[Any] = [
            window["startTimestamp"],
            window["endTimestamp"],
        ]
        if namespace:
            base_clauses.append("namespace = ?")
            base_parameters.append(namespace)
        if task_id:
            base_clauses.append("task_id = ?")
            base_parameters.append(task_id)
        if status:
            base_clauses.append("status = ?")
            base_parameters.append(status)
        if kind:
            base_clauses.append("kind = ?")
            base_parameters.append(kind)
        filtered_clauses = list(base_clauses)
        filtered_parameters = list(base_parameters)
        if provider_id:
            filtered_clauses.append("provider_id = ?")
            filtered_parameters.append(provider_id)
        if model:
            filtered_clauses.append("model = ?")
            filtered_parameters.append(model)
        base_where = f" WHERE {' AND '.join(base_clauses)}"
        filtered_where = f" WHERE {' AND '.join(filtered_clauses)}"
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                SELECT execution_id, envelope_json, created_at
                FROM execution_records{filtered_where}
                ORDER BY created_at, execution_id
                """,
                tuple(filtered_parameters),
            )
            summary, series, models = _execution_statistics_components(
                _iter_execution_summaries(connection, cursor),
                window=window,
            )
            facet_rows = connection.execute(
                f"""
                SELECT DISTINCT provider_id, model
                FROM execution_records{base_where}
                ORDER BY provider_id, model
                """,
                tuple(base_parameters),
            )
            facets = _execution_facets(
                {
                    "provider": {
                        "id": str(row["provider_id"]),
                        "model": str(row["model"]),
                    }
                }
                for row in facet_rows
            )
        return {
            "period": {
                "kind": period,
                "anchor": anchor,
                "timezone": timezone_name,
                "start": window["start"].isoformat(),
                "end": window["end"].isoformat(),
            },
            "summary": summary,
            "series": series,
            "models": models,
            "facets": facets,
        }

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


def _migrate_execution_history_schema(connection: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(execution_records)").fetchall()
    }
    definitions = {
        "kind": "TEXT NOT NULL DEFAULT 'task'",
        "provider_id": "TEXT NOT NULL DEFAULT ''",
        "model": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in definitions.items():
        if name not in columns:
            connection.execute(
                f"ALTER TABLE execution_records ADD COLUMN {name} {definition}"
            )

    provider_call_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(provider_call_records)").fetchall()
    }
    if "reasoning_effort" not in provider_call_columns:
        connection.execute(
            "ALTER TABLE provider_call_records "
            "ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT 'auto'"
        )

    rows = connection.execute(
        """
        SELECT execution_id, envelope_json, kind, provider_id, model
        FROM execution_records
        """
    ).fetchall()
    updates: list[tuple[str, str, str, str]] = []
    for row in rows:
        envelope = _json_object(row["envelope_json"])
        task = envelope.get("task") if isinstance(envelope.get("task"), dict) else {}
        provider = (
            envelope.get("provider")
            if isinstance(envelope.get("provider"), dict)
            else {}
        )
        expected = (
            str(task.get("kind") or "task"),
            str(provider.get("id") or ""),
            str(provider.get("model") or ""),
        )
        actual = (
            str(row["kind"]),
            str(row["provider_id"]),
            str(row["model"]),
        )
        if actual != expected:
            updates.append((*expected, str(row["execution_id"])))
    connection.executemany(
        """
        UPDATE execution_records
        SET kind = ?, provider_id = ?, model = ?
        WHERE execution_id = ?
        """,
        updates,
    )
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS execution_records_created_idx
        ON execution_records(created_at);

        CREATE INDEX IF NOT EXISTS execution_records_namespace_task_created_idx
        ON execution_records(namespace, task_id, created_at);

        CREATE INDEX IF NOT EXISTS execution_records_provider_model_created_idx
        ON execution_records(provider_id, model, created_at);

        CREATE INDEX IF NOT EXISTS execution_records_kind_created_idx
        ON execution_records(kind, created_at);

        CREATE INDEX IF NOT EXISTS provider_call_provider_model_created_idx
        ON provider_call_records(provider_id, model, created_at);
        """
    )


def _upsert_execution(
    connection: sqlite3.Connection,
    envelope: dict[str, Any],
    *,
    now: float,
) -> None:
    task = envelope.get("task") if isinstance(envelope.get("task"), dict) else {}
    provider = (
        envelope.get("provider")
        if isinstance(envelope.get("provider"), dict)
        else {}
    )
    execution_id = str(envelope.get("executionId") or "")
    if not execution_id:
        raise ValueError("executionId is required")
    connection.execute(
        """
        INSERT INTO execution_records (
            execution_id, status, namespace, task_id, task_version,
            kind, provider_id, model, envelope_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(execution_id) DO UPDATE SET
            status = excluded.status,
            namespace = excluded.namespace,
            task_id = excluded.task_id,
            task_version = excluded.task_version,
            kind = excluded.kind,
            provider_id = excluded.provider_id,
            model = excluded.model,
            envelope_json = excluded.envelope_json,
            updated_at = excluded.updated_at
        """,
        (
            execution_id,
            str(envelope.get("status") or "failed"),
            str(task.get("namespace") or ""),
            str(task.get("id") or ""),
            str(task.get("version") or ""),
            str(task.get("kind") or "task"),
            str(provider.get("id") or ""),
            str(provider.get("model") or ""),
            _json_text(envelope),
            now,
            now,
        ),
    )


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    return dict(parsed) if isinstance(parsed, dict) else {}


def _execution_summary(
    envelope: dict[str, Any],
    provider_calls: list[dict[str, Any]] | None = None,
    *,
    recorded_at: float | None = None,
) -> dict[str, Any]:
    task = envelope.get("task") if isinstance(envelope.get("task"), dict) else {}
    provider = (
        envelope.get("provider")
        if isinstance(envelope.get("provider"), dict)
        else {}
    )
    reasoning = (
        provider.get("reasoning")
        if isinstance(provider.get("reasoning"), dict)
        else {}
    )
    cache = envelope.get("cache") if isinstance(envelope.get("cache"), dict) else {}
    usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    timing = envelope.get("timing") if isinstance(envelope.get("timing"), dict) else {}
    calls = provider_calls or []
    if calls:
        usage = _provider_call_usage(calls)
        timing = {
            **timing,
            "providerElapsedMs": sum(
                _non_negative_int(call.get("elapsedMs")) for call in calls
            ),
            "providerCallCount": len(calls),
            "transportRetries": sum(
                max(0, _non_negative_int(call.get("attempts")) - 1)
                for call in calls
            ),
            "contractRepairs": sum(
                int(call.get("purpose") == "contract_repair")
                for call in calls
            ),
        }
    call_error = next(
        (str(call.get("error") or "") for call in calls if call.get("status") == "failed"),
        "",
    )
    return {
        "executionId": str(envelope.get("executionId") or ""),
        "status": str(envelope.get("status") or "failed"),
        "kind": str(task.get("kind") or "task"),
        "task": {
            "namespace": str(task.get("namespace") or ""),
            "id": str(task.get("id") or ""),
            "version": str(task.get("version") or ""),
        },
        "provider": {
            "id": str(provider.get("id") or ""),
            "model": str(provider.get("model") or ""),
            "reasoning": {
                "requested": str(reasoning.get("requested") or "auto"),
                "effective": (
                    str(reasoning["effective"])
                    if reasoning.get("effective") is not None
                    else None
                ),
                "source": str(reasoning.get("source") or ""),
            },
        },
        "cache": {
            "hit": bool(cache.get("hit")),
            "source": str(cache.get("source") or ""),
            "hitCount": _non_negative_int(cache.get("hitCount")),
        },
        "usage": {
            "available": bool(usage.get("available")),
            "inputTokens": _non_negative_int(usage.get("inputTokens")),
            "outputTokens": _non_negative_int(usage.get("outputTokens")),
            "totalTokens": _non_negative_int(usage.get("totalTokens")),
            "cacheReadInputTokens": _non_negative_int(
                usage.get("cacheReadInputTokens")
            ),
            "source": str(usage.get("source") or ""),
        },
        "timing": {
            "createdAt": str(
                timing.get("createdAt")
                or _timestamp_iso(recorded_at)
            ),
            "completedAt": str(timing.get("completedAt") or ""),
            "elapsedMs": _non_negative_int(timing.get("elapsedMs")),
            "providerElapsedMs": _non_negative_int(
                timing.get("providerElapsedMs")
            ),
            "providerCallCount": _non_negative_int(
                timing.get("providerCallCount")
            ),
            "transportRetries": _non_negative_int(
                timing.get("transportRetries")
            ),
            "contractRepairs": _non_negative_int(
                timing.get("contractRepairs")
            ),
        },
        "error": call_error or _redacted_history_error(envelope.get("error")),
    }


def _iter_execution_summaries(
    connection: sqlite3.Connection,
    cursor: sqlite3.Cursor,
    *,
    batch_size: int = 100,
) -> Iterator[dict[str, Any]]:
    while rows := cursor.fetchmany(batch_size):
        provider_calls = _provider_calls_by_execution(
            connection,
            [str(row["execution_id"]) for row in rows],
        )
        for row in rows:
            execution_id = str(row["execution_id"])
            yield _execution_summary(
                _json_object(row["envelope_json"]),
                provider_calls.get(execution_id, []),
                recorded_at=float(row["created_at"]),
            )


def _new_execution_aggregate_state() -> dict[str, Any]:
    return {
        "total": 0,
        "statuses": {
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
        },
        "usage": {
            "inputTokens": 0,
            "outputTokens": 0,
            "totalTokens": 0,
            "cacheReadInputTokens": 0,
        },
        "usageAvailable": 0,
        "cacheHits": 0,
        "providerCacheHitExecutions": 0,
        "providerCalls": 0,
        "transportRetries": 0,
        "contractRepairs": 0,
        "providerTests": 0,
        "elapsedTotal": 0,
        "elapsedAvailable": 0,
    }


def _update_execution_aggregate_state(
    state: dict[str, Any],
    item: dict[str, Any],
) -> None:
    state["total"] += 1
    statuses = state["statuses"]
    status = item.get("status")
    if status in statuses:
        statuses[status] += 1
    if item.get("kind") == "provider_test":
        state["providerTests"] += 1
    state["cacheHits"] += int(bool(item.get("cache", {}).get("hit")))
    item_usage = item.get("usage", {})
    state["usageAvailable"] += int(bool(item_usage.get("available")))
    state["providerCacheHitExecutions"] += int(
        _non_negative_int(item_usage.get("cacheReadInputTokens")) > 0
    )
    for field in state["usage"]:
        state["usage"][field] += _non_negative_int(item_usage.get(field))
    timing = item.get("timing", {})
    state["providerCalls"] += _non_negative_int(timing.get("providerCallCount"))
    state["transportRetries"] += _non_negative_int(timing.get("transportRetries"))
    state["contractRepairs"] += _non_negative_int(timing.get("contractRepairs"))
    elapsed_ms = _non_negative_int(timing.get("elapsedMs"))
    if elapsed_ms:
        state["elapsedTotal"] += elapsed_ms
        state["elapsedAvailable"] += 1


def _finish_execution_aggregate_state(state: dict[str, Any]) -> dict[str, Any]:
    statuses = state["statuses"]
    terminal = statuses["succeeded"] + statuses["failed"]
    elapsed_available = state["elapsedAvailable"]
    return {
        "total": state["total"],
        **statuses,
        "successRate": round(
            statuses["succeeded"] / terminal * 100,
            1,
        ) if terminal else 0.0,
        "providerTests": state["providerTests"],
        "cacheHits": state["cacheHits"],
        "providerCacheHitExecutions": state["providerCacheHitExecutions"],
        "providerCallCount": state["providerCalls"],
        "transportRetries": state["transportRetries"],
        "contractRepairs": state["contractRepairs"],
        "averageElapsedMs": round(
            state["elapsedTotal"] / elapsed_available,
        ) if elapsed_available else 0,
        "usageAvailableExecutions": state["usageAvailable"],
        "usage": state["usage"],
    }


def _execution_aggregate(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    state = _new_execution_aggregate_state()
    for item in items:
        _update_execution_aggregate_state(state, item)
    return _finish_execution_aggregate_state(state)


def _timestamp_iso(value: float | None) -> str:
    if value is None:
        return ""
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _period_window(
    period: str,
    anchor: str,
    timezone_name: str,
) -> dict[str, Any]:
    try:
        local_zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc

    try:
        if period == "day":
            parsed = datetime.strptime(anchor, "%Y-%m-%d")
            start = datetime(
                parsed.year,
                parsed.month,
                parsed.day,
                tzinfo=local_zone,
            )
            end = start + timedelta(days=1)
        elif period == "month":
            parsed = datetime.strptime(anchor, "%Y-%m")
            start = datetime(parsed.year, parsed.month, 1, tzinfo=local_zone)
            end = (
                datetime(parsed.year + 1, 1, 1, tzinfo=local_zone)
                if parsed.month == 12
                else datetime(parsed.year, parsed.month + 1, 1, tzinfo=local_zone)
            )
        elif period == "year":
            parsed = datetime.strptime(anchor, "%Y")
            start = datetime(parsed.year, 1, 1, tzinfo=local_zone)
            end = datetime(parsed.year + 1, 1, 1, tzinfo=local_zone)
        else:
            raise ValueError("period must be day, month, or year")
    except ValueError as exc:
        if str(exc).startswith("period must"):
            raise
        raise ValueError(f"Invalid {period} anchor: {anchor}") from exc
    return {
        "period": period,
        "timezone": local_zone,
        "start": start,
        "end": end,
        "startTimestamp": start.timestamp(),
        "endTimestamp": end.timestamp(),
    }


def _execution_datetime(
    item: dict[str, Any],
    local_zone: ZoneInfo,
) -> datetime | None:
    value = str(item.get("timing", {}).get("createdAt") or "")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(local_zone)


def _new_execution_series(window: dict[str, Any]) -> list[dict[str, Any]]:
    period = str(window["period"])
    start = window["start"]
    end = window["end"]
    buckets: list[dict[str, Any]] = []
    if period == "day":
        for hour in range(24):
            buckets.append(
                {
                    "key": f"{hour:02d}",
                    "label": f"{hour:02d}:00",
                    "executions": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "cancelled": 0,
                    "providerCalls": 0,
                    "totalTokens": 0,
                    "cacheReadInputTokens": 0,
                }
            )
    elif period == "month":
        day_count = (end.date() - start.date()).days
        for day in range(1, day_count + 1):
            buckets.append(
                {
                    "key": f"{day:02d}",
                    "label": f"{day}日",
                    "executions": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "cancelled": 0,
                    "providerCalls": 0,
                    "totalTokens": 0,
                    "cacheReadInputTokens": 0,
                }
            )
    else:
        for month in range(1, 13):
            buckets.append(
                {
                    "key": f"{month:02d}",
                    "label": f"{month}月",
                    "executions": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "cancelled": 0,
                    "providerCalls": 0,
                    "totalTokens": 0,
                    "cacheReadInputTokens": 0,
                }
            )
    return buckets


def _update_execution_series(
    buckets: list[dict[str, Any]],
    *,
    window: dict[str, Any],
    item: dict[str, Any],
) -> None:
    local_time = _execution_datetime(item, window["timezone"])
    if local_time is None:
        return
    period = str(window["period"])
    if period == "day":
        index = local_time.hour
    elif period == "month":
        index = local_time.day - 1
    else:
        index = local_time.month - 1
    if not 0 <= index < len(buckets):
        return
    bucket = buckets[index]
    bucket["executions"] += 1
    status = item.get("status")
    if status in {"succeeded", "failed", "cancelled"}:
        bucket[status] += 1
    timing = item.get("timing", {})
    usage = item.get("usage", {})
    bucket["providerCalls"] += _non_negative_int(timing.get("providerCallCount"))
    bucket["totalTokens"] += _non_negative_int(usage.get("totalTokens"))
    bucket["cacheReadInputTokens"] += _non_negative_int(
        usage.get("cacheReadInputTokens")
    )


def _execution_series(
    items: Iterable[dict[str, Any]],
    *,
    window: dict[str, Any],
) -> list[dict[str, Any]]:
    buckets = _new_execution_series(window)

    for item in items:
        _update_execution_series(buckets, window=window, item=item)
    return buckets


def _update_execution_model_groups(
    groups: dict[tuple[str, str], dict[str, Any]],
    item: dict[str, Any],
) -> None:
    provider = item.get("provider", {})
    key = (
        str(provider.get("id") or "unknown"),
        str(provider.get("model") or "unknown"),
    )
    group = groups.setdefault(
        key,
        {
            "providerId": key[0],
            "model": key[1],
            "executions": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
            "providerCalls": 0,
            "elapsedMs": 0,
            "elapsedAvailable": 0,
            "usage": {
                "inputTokens": 0,
                "outputTokens": 0,
                "totalTokens": 0,
                "cacheReadInputTokens": 0,
            },
        },
    )
    group["executions"] += 1
    status = item.get("status")
    if status in {"succeeded", "failed", "cancelled"}:
        group[status] += 1
    timing = item.get("timing", {})
    group["providerCalls"] += _non_negative_int(timing.get("providerCallCount"))
    elapsed_ms = _non_negative_int(timing.get("elapsedMs"))
    if elapsed_ms:
        group["elapsedMs"] += elapsed_ms
        group["elapsedAvailable"] += 1
    usage = item.get("usage", {})
    for field in group["usage"]:
        group["usage"][field] += _non_negative_int(usage.get(field))


def _finish_execution_model_groups(
    groups: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for group in groups.values():
        terminal = group["succeeded"] + group["failed"]
        result.append(
            {
                "providerId": group["providerId"],
                "model": group["model"],
                "executions": group["executions"],
                "succeeded": group["succeeded"],
                "failed": group["failed"],
                "cancelled": group["cancelled"],
                "providerCalls": group["providerCalls"],
                "successRate": round(
                    group["succeeded"] / terminal * 100,
                    1,
                ) if terminal else 0.0,
                "averageElapsedMs": round(
                    group["elapsedMs"] / group["elapsedAvailable"],
                ) if group["elapsedAvailable"] else 0,
                "usage": group["usage"],
            }
        )
    return sorted(
        result,
        key=lambda item: (
            -int(item["providerCalls"]),
            -int(item["usage"]["totalTokens"]),
            str(item["providerId"]).casefold(),
            str(item["model"]).casefold(),
        ),
    )


def _execution_model_breakdown(
    items: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        _update_execution_model_groups(groups, item)
    return _finish_execution_model_groups(groups)


def _execution_statistics_components(
    items: Iterable[dict[str, Any]],
    *,
    window: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    aggregate_state = _new_execution_aggregate_state()
    series = _new_execution_series(window)
    model_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        _update_execution_aggregate_state(aggregate_state, item)
        _update_execution_series(series, window=window, item=item)
        _update_execution_model_groups(model_groups, item)
    return (
        _finish_execution_aggregate_state(aggregate_state),
        series,
        _finish_execution_model_groups(model_groups),
    )


def _execution_facets(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    providers: dict[str, set[str]] = {}
    for item in items:
        provider = item.get("provider", {})
        provider_id = str(provider.get("id") or "")
        model = str(provider.get("model") or "")
        if provider_id:
            providers.setdefault(provider_id, set())
            if model:
                providers[provider_id].add(model)
    return {
        "providers": [
            {
                "id": provider_id,
                "models": sorted(models, key=str.casefold),
            }
            for provider_id, models in sorted(
                providers.items(),
                key=lambda pair: pair[0].casefold(),
            )
        ]
    }


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _provider_calls_by_execution(
    connection: sqlite3.Connection,
    execution_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    unique_ids = list(dict.fromkeys(value for value in execution_ids if value))
    for start in range(0, len(unique_ids), 500):
        chunk = unique_ids[start : start + 500]
        placeholders = ",".join("?" for _value in chunk)
        rows = connection.execute(
            f"""
            SELECT execution_id, purpose, status, usage_json, attempts, elapsed_ms, error
            FROM provider_call_records
            WHERE execution_id IN ({placeholders})
            ORDER BY execution_id, sequence, created_at
            """,
            tuple(chunk),
        ).fetchall()
        for row in rows:
            grouped.setdefault(str(row["execution_id"]), []).append(
                {
                    "status": str(row["status"]),
                    "purpose": str(row["purpose"]),
                    "usage": _json_object(row["usage_json"]),
                    "attempts": int(row["attempts"]),
                    "elapsedMs": int(row["elapsed_ms"]),
                    "error": str(row["error"]),
                }
            )
    return grouped


def _provider_call_usage(calls: list[dict[str, Any]]) -> dict[str, Any]:
    fields = ("inputTokens", "outputTokens", "totalTokens", "cacheReadInputTokens")
    return {
        "available": any(bool(call.get("usage", {}).get("available")) for call in calls),
        **{
            field: sum(
                _non_negative_int(call.get("usage", {}).get(field))
                for call in calls
            )
            for field in fields
        },
        "source": "provider_call_audit",
    }


def _redacted_history_error(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    prefix, separator, _detail = text.partition(":")
    if separator and prefix.endswith(("Error", "Exception")):
        return prefix
    return text[:240]
