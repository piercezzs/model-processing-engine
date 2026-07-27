from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_processing_engine.cache import SQLiteRuntimeStore
from model_processing_engine.exceptions import AsyncQueueFullError, ConfigurationError


class CacheStoreTests(unittest.TestCase):
    @staticmethod
    def _queued_envelope(execution_id: str) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "executionId": execution_id,
            "status": "queued",
            "task": {"namespace": "one", "id": "task", "version": "1"},
            "provider": {},
            "cache": {},
            "usage": {},
            "timing": {},
            "progress": {"event": "queued"},
            "result": None,
            "warnings": [],
            "error": None,
        }

    def test_cache_persists_across_store_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.sqlite"
            first = SQLiteRuntimeStore(path)
            first.put_cache(
                namespace="one",
                key="key",
                task_id="task",
                task_version="1",
                provider_id="mock",
                model="mock-v1",
                result={"summary": "saved"},
                metadata={},
                ttl_seconds=None,
            )
            second = SQLiteRuntimeStore(path)
            record = second.get_cache("one", "key")
            self.assertIsNotNone(record)
            self.assertEqual(record.result, {"summary": "saved"})

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not a Windows security boundary")
    def test_store_secures_direct_sdk_directory_database_and_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sdk" / "runtime" / "runtime.sqlite"
            previous_umask = os.umask(0o022)
            try:
                store = SQLiteRuntimeStore(path)
                with store._connect() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO cache_entries "
                        "(namespace, cache_key, task_id, task_version, provider_id, model, "
                        "result_json, metadata_json, created_at, expires_at, "
                        "last_accessed_at, hit_count) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            "one",
                            "key",
                            "task",
                            "1",
                            "mock",
                            "mock-v1",
                            "{}",
                            "{}",
                            0.0,
                            None,
                            0.0,
                            0,
                        ),
                    )
                    modes = {
                        candidate.name: stat.S_IMODE(candidate.stat().st_mode)
                        for candidate in (
                            path,
                            Path(f"{path}-wal"),
                            Path(f"{path}-shm"),
                        )
                        if candidate.exists()
                    }
            finally:
                os.umask(previous_umask)

            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(modes["runtime.sqlite"], 0o600)
            self.assertEqual(modes["runtime.sqlite-wal"], 0o600)
            self.assertEqual(modes["runtime.sqlite-shm"], 0o600)

    def test_store_rejects_symbolic_link_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target.sqlite"
            target.touch()
            link = root / "runtime.sqlite"
            try:
                link.symlink_to(target)
            except (NotImplementedError, OSError):
                self.skipTest("Symbolic links are not available")
            with self.assertRaises(ConfigurationError):
                SQLiteRuntimeStore(link)

    def test_namespaces_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteRuntimeStore(Path(temp_dir) / "runtime.sqlite")
            store.put_cache(
                namespace="one",
                key="same",
                task_id="task",
                task_version="1",
                provider_id="mock",
                model="mock-v1",
                result={"summary": "one"},
                metadata={},
                ttl_seconds=None,
            )
            self.assertIsNone(store.get_cache("two", "same"))

    def test_expired_entry_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteRuntimeStore(Path(temp_dir) / "runtime.sqlite")
            with patch("model_processing_engine.cache.time.time", return_value=100.0):
                store.put_cache(
                    namespace="one",
                    key="short",
                    task_id="task",
                    task_version="1",
                    provider_id="mock",
                    model="mock-v1",
                    result={"summary": "old"},
                    metadata={},
                    ttl_seconds=1,
                )
            with patch("model_processing_engine.cache.time.time", return_value=102.0):
                self.assertIsNone(store.get_cache("one", "short"))
            self.assertEqual(store.cache_count(), 0)

    def test_incomplete_execution_is_failed_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.sqlite"
            store = SQLiteRuntimeStore(path)
            store.save_execution(
                {
                    "schemaVersion": 1,
                    "executionId": "exec-1",
                    "status": "running",
                    "task": {"namespace": "one", "id": "task", "version": "1"},
                }
            )
            restarted = SQLiteRuntimeStore(path)
            restarted.recover_incomplete_executions()
            record = restarted.get_execution("exec-1")
            self.assertEqual(record["status"], "failed")
            self.assertIn("interrupted", record["error"])

    def test_async_queue_is_bounded_and_recovers_running_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.sqlite"
            store = SQLiteRuntimeStore(path)
            request = {"task": {"id": "task"}, "input": {"text": "hello"}}
            store.enqueue_async_execution(
                envelope=self._queued_envelope("exec-1"),
                request=request,
                capacity=1,
            )
            with self.assertRaises(AsyncQueueFullError):
                store.enqueue_async_execution(
                    envelope=self._queued_envelope("exec-2"),
                    request=request,
                    capacity=1,
                )
            claimed = store.claim_next_async_execution()
            self.assertEqual(claimed, ("exec-1", request))
            self.assertEqual(store.async_queue_stats()["running"], 1)

            restarted = SQLiteRuntimeStore(path)
            recovery = restarted.recover_async_executions()
            self.assertEqual(recovery, {"recovered": 1, "orphaned": 0})
            self.assertEqual(restarted.get_execution("exec-1")["status"], "queued")
            self.assertEqual(
                restarted.claim_next_async_execution(),
                ("exec-1", request),
            )
            restarted.finish_async_execution("exec-1")
            self.assertEqual(restarted.async_queue_stats()["total"], 0)

    def test_execution_history_is_redacted_paginated_and_aggregated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteRuntimeStore(Path(temp_dir) / "runtime.sqlite")
            store.save_execution(
                {
                    "schemaVersion": 1,
                    "executionId": "exec-1",
                    "status": "succeeded",
                    "task": {"namespace": "one", "id": "task", "version": "1"},
                    "provider": {"id": "provider", "model": "model"},
                    "cache": {"hit": False},
                    "usage": {
                        "available": True,
                        "inputTokens": 4,
                        "outputTokens": 3,
                        "totalTokens": 7,
                        "cacheReadInputTokens": 2,
                    },
                    "timing": {"providerCallCount": 1, "transportRetries": 1},
                    "result": {"private": "content"},
                }
            )
            store.save_execution(
                {
                    "schemaVersion": 1,
                    "executionId": "exec-2",
                    "status": "succeeded",
                    "task": {"namespace": "one", "id": "task", "version": "1"},
                    "provider": {"id": "provider", "model": "model"},
                    "cache": {"hit": True, "hitCount": 2},
                    "usage": {"available": False, "totalTokens": 0},
                    "timing": {"providerCallCount": 0, "transportRetries": 0},
                    "result": {"private": "cached"},
                }
            )

            history = store.execution_history(limit=1, offset=0, namespace="one")

            self.assertEqual(history["pagination"]["total"], 2)
            self.assertTrue(history["pagination"]["hasMore"])
            self.assertEqual(len(history["items"]), 1)
            self.assertNotIn("result", history["items"][0])
            self.assertEqual(history["summary"]["total"], 2)
            self.assertEqual(history["summary"]["cacheHits"], 1)
            self.assertEqual(history["summary"]["providerCacheHitExecutions"], 1)
            self.assertEqual(history["summary"]["providerCallCount"], 1)
            self.assertEqual(history["summary"]["usage"]["totalTokens"], 7)
            self.assertEqual(
                history["summary"]["usage"]["cacheReadInputTokens"],
                2,
            )

    def test_execution_history_filters_and_period_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteRuntimeStore(Path(temp_dir) / "runtime.sqlite")
            first_time = 1784772000.0
            with patch("model_processing_engine.cache.time.time", return_value=first_time):
                store.save_execution(
                    {
                        "schemaVersion": 1,
                        "executionId": "exec-deepseek",
                        "status": "succeeded",
                        "task": {
                            "namespace": "project",
                            "id": "interpret",
                            "version": "1",
                            "kind": "task",
                        },
                        "provider": {"id": "deepseek", "model": "flash"},
                        "cache": {"hit": False},
                        "usage": {},
                        "timing": {
                            "createdAt": "2026-07-23T02:00:00+00:00",
                            "completedAt": "2026-07-23T02:00:02+00:00",
                            "elapsedMs": 2000,
                        },
                    }
                )
                store.save_provider_call(
                    {
                        "callId": "call-deepseek",
                        "executionId": "exec-deepseek",
                        "purpose": "task",
                        "sequence": 1,
                        "providerId": "deepseek",
                        "model": "flash",
                        "status": "succeeded",
                        "usage": {
                            "available": True,
                            "inputTokens": 8,
                            "outputTokens": 2,
                            "totalTokens": 10,
                            "cacheReadInputTokens": 4,
                        },
                        "attempts": 1,
                        "elapsedMs": 1900,
                    }
                )
            second_time = first_time + 86400
            with patch("model_processing_engine.cache.time.time", return_value=second_time):
                store.save_execution(
                    {
                        "schemaVersion": 1,
                        "executionId": "exec-probe",
                        "status": "failed",
                        "task": {
                            "namespace": "_mpe",
                            "id": "provider_connection_test",
                            "version": "1",
                            "kind": "provider_test",
                        },
                        "provider": {"id": "mock", "model": "mock-v1"},
                        "cache": {"hit": False},
                        "usage": {},
                        "timing": {"createdAt": "2026-07-24T02:00:00+00:00"},
                    }
                )

            history = store.execution_history(
                kind="task",
                provider_id="deepseek",
                model="flash",
                created_from=first_time - 1,
                created_to=first_time + 1,
            )
            statistics = store.execution_statistics(
                period="day",
                anchor="2026-07-23",
                timezone_name="UTC",
                kind="task",
            )

            self.assertEqual(history["pagination"]["total"], 1)
            self.assertEqual(history["items"][0]["executionId"], "exec-deepseek")
            self.assertEqual(statistics["summary"]["total"], 1)
            self.assertEqual(statistics["summary"]["providerCallCount"], 1)
            self.assertEqual(statistics["summary"]["usage"]["totalTokens"], 10)
            self.assertEqual(statistics["series"][2]["executions"], 1)
            self.assertEqual(statistics["models"][0]["providerId"], "deepseek")
            self.assertEqual(statistics["models"][0]["model"], "flash")
            self.assertEqual(
                statistics["facets"]["providers"],
                [{"id": "deepseek", "models": ["flash"]}],
            )

    def test_existing_execution_records_are_backfilled_for_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.sqlite"
            envelope = {
                "executionId": "legacy",
                "status": "succeeded",
                "task": {
                    "namespace": "legacy-project",
                    "id": "legacy-task",
                    "version": "1",
                    "kind": "provider_test",
                },
                "provider": {"id": "legacy-provider", "model": "legacy-model"},
                "timing": {"createdAt": "2026-07-23T01:00:00+00:00"},
            }
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    CREATE TABLE execution_records (
                        execution_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        namespace TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        task_version TEXT NOT NULL,
                        envelope_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO execution_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy",
                        "succeeded",
                        "legacy-project",
                        "legacy-task",
                        "1",
                        json.dumps(envelope),
                        1784768400.0,
                        1784768400.0,
                    ),
                )

            store = SQLiteRuntimeStore(path)
            history = store.execution_history(kind="provider_test")

            self.assertEqual(history["pagination"]["total"], 1)
            self.assertEqual(history["items"][0]["provider"]["id"], "legacy-provider")
            with sqlite3.connect(path) as connection:
                row = connection.execute(
                    """
                    SELECT kind, provider_id, model
                    FROM execution_records
                    WHERE execution_id = 'legacy'
                    """
                ).fetchone()
            self.assertEqual(
                row,
                ("provider_test", "legacy-provider", "legacy-model"),
            )


if __name__ == "__main__":
    unittest.main()
