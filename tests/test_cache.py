from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_processing_engine.cache import SQLiteRuntimeStore


class CacheStoreTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
