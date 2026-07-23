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

                CREATE TABLE IF NOT EXISTS provider_call_records (
                    call_id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    provider_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    usage_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    elapsed_ms INTEGER NOT NULL,
                    error TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS provider_call_execution_idx
                ON provider_call_records(execution_id, sequence);
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
                    model, status, usage_json, attempts, elapsed_ms, error,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_id,
                    execution_id,
                    str(record.get("purpose") or "task"),
                    max(1, int(record.get("sequence") or 1)),
                    str(record.get("providerId") or ""),
                    str(record.get("model") or ""),
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
        status: str | None = None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if namespace:
            clauses.append("namespace = ?")
            parameters.append(namespace)
        if status:
            clauses.append("status = ?")
            parameters.append(status)
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
                SELECT execution_id, envelope_json
                FROM execution_records{where}
                ORDER BY updated_at DESC, execution_id DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, bounded_limit, bounded_offset),
            ).fetchall()
            aggregate_rows = connection.execute(
                f"SELECT execution_id, envelope_json FROM execution_records{where}",
                tuple(parameters),
            ).fetchall()
            provider_calls = _provider_calls_by_execution(
                connection,
                [str(row["execution_id"]) for row in aggregate_rows],
            )
        items = [
            _execution_summary(
                _json_object(row["envelope_json"]),
                provider_calls.get(str(row["execution_id"]), []),
            )
            for row in page_rows
        ]
        aggregate_items = [
            _execution_summary(
                _json_object(row["envelope_json"]),
                provider_calls.get(str(row["execution_id"]), []),
            )
            for row in aggregate_rows
        ]
        total = int(total_row["count"] if total_row else 0)
        return {
            "items": items,
            "summary": _execution_aggregate(aggregate_items),
            "pagination": {
                "limit": bounded_limit,
                "offset": bounded_offset,
                "total": total,
                "hasMore": bounded_offset + len(items) < total,
            },
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


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    return dict(parsed) if isinstance(parsed, dict) else {}


def _execution_summary(
    envelope: dict[str, Any],
    provider_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    task = envelope.get("task") if isinstance(envelope.get("task"), dict) else {}
    provider = (
        envelope.get("provider")
        if isinstance(envelope.get("provider"), dict)
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
            "createdAt": str(timing.get("createdAt") or ""),
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
        },
        "error": call_error or _redacted_history_error(envelope.get("error")),
    }


def _execution_aggregate(items: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = {"queued": 0, "running": 0, "succeeded": 0, "failed": 0}
    usage_fields = (
        "inputTokens",
        "outputTokens",
        "totalTokens",
        "cacheReadInputTokens",
    )
    usage = {field: 0 for field in usage_fields}
    usage_available = 0
    cache_hits = 0
    provider_cache_hit_executions = 0
    provider_calls = 0
    transport_retries = 0
    provider_tests = 0
    for item in items:
        status = item.get("status")
        if status in statuses:
            statuses[status] += 1
        if item.get("kind") == "provider_test":
            provider_tests += 1
        cache_hits += int(bool(item.get("cache", {}).get("hit")))
        item_usage = item.get("usage", {})
        usage_available += int(bool(item_usage.get("available")))
        provider_cache_hit_executions += int(
            _non_negative_int(item_usage.get("cacheReadInputTokens")) > 0
        )
        for field in usage_fields:
            usage[field] += _non_negative_int(item_usage.get(field))
        timing = item.get("timing", {})
        provider_calls += _non_negative_int(timing.get("providerCallCount"))
        transport_retries += _non_negative_int(timing.get("transportRetries"))
    return {
        "total": len(items),
        **statuses,
        "providerTests": provider_tests,
        "cacheHits": cache_hits,
        "providerCacheHitExecutions": provider_cache_hit_executions,
        "providerCallCount": provider_calls,
        "transportRetries": transport_retries,
        "usageAvailableExecutions": usage_available,
        "usage": usage,
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
            SELECT execution_id, status, usage_json, attempts, elapsed_ms, error
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
