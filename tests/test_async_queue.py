from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from model_processing_engine.async_queue import PersistentAsyncExecutor
from model_processing_engine.contracts import ExecutionRequest, ResultEnvelope

from tests.helpers import execution_request, task_definition


class FakeQueueStore:
    def __init__(self, jobs: list[tuple[str, dict]] | None = None) -> None:
        self._lock = threading.Lock()
        self.jobs = list(jobs or [])
        self.claim_count = 0
        self.finished: list[str] = []
        self.failed: list[str] = []

    def recover_async_executions(self) -> dict[str, int]:
        return {"recovered": 0, "orphaned": 0}

    def claim_next_async_execution(self) -> tuple[str, dict] | None:
        with self._lock:
            self.claim_count += 1
            return self.jobs.pop(0) if self.jobs else None

    def enqueue_async_execution(
        self,
        *,
        envelope: dict,
        request: dict,
        capacity: int,
    ) -> None:
        del capacity
        with self._lock:
            self.jobs.append((str(envelope["executionId"]), request))

    def finish_async_execution(self, execution_id: str) -> None:
        self.finished.append(execution_id)

    def fail_async_execution(self, execution_id: str, *, error: str) -> None:
        del error
        self.failed.append(execution_id)

    def async_queue_stats(self) -> dict[str, int]:
        with self._lock:
            return {"queued": len(self.jobs), "running": 0, "total": len(self.jobs)}

    def has_queued_async_executions(self) -> bool:
        raise AssertionError("idle workers must not run a second queue probe")


class RecordingEngine:
    def __init__(self) -> None:
        self.executed = threading.Event()

    def reserve(
        self,
        request: ExecutionRequest,
        *,
        persist: bool,
    ) -> ResultEnvelope:
        del request, persist
        return ResultEnvelope.model_validate(
            {
                "executionId": "queued-execution",
                "status": "queued",
                "task": {"namespace": "test", "id": "task", "version": "1"},
            }
        )

    def execute(
        self,
        request: ExecutionRequest,
        *,
        execution_id: str,
    ) -> ResultEnvelope:
        del request
        self.executed.set()
        return ResultEnvelope.model_validate(
            {
                "executionId": execution_id,
                "status": "succeeded",
                "task": {"namespace": "test", "id": "task", "version": "1"},
            }
        )


class BlockingEngine(RecordingEngine):
    def __init__(self, expected_calls: int) -> None:
        super().__init__()
        self.expected_calls = expected_calls
        self.release = threading.Event()
        self.all_started = threading.Event()
        self._lock = threading.Lock()
        self._calls = 0

    def execute(
        self,
        request: ExecutionRequest,
        *,
        execution_id: str,
    ) -> ResultEnvelope:
        with self._lock:
            self._calls += 1
            if self._calls == self.expected_calls:
                self.all_started.set()
        self.release.wait(timeout=3)
        return super().execute(request, execution_id=execution_id)


class PersistentAsyncExecutorTests(unittest.TestCase):
    def test_idle_recovery_probe_is_owned_by_one_worker(self) -> None:
        store = FakeQueueStore()
        executor = PersistentAsyncExecutor(
            engine=RecordingEngine(),
            store=store,
            worker_count=4,
            capacity=10,
        )
        with patch(
            "model_processing_engine.async_queue.RECOVERY_POLL_INTERVAL_SECONDS",
            0.02,
        ):
            executor.start()
            time.sleep(0.09)
            self.assertTrue(executor.stop(timeout_seconds=1))

        self.assertGreater(store.claim_count, 4)
        self.assertLessEqual(store.claim_count, 10)

    def test_enqueue_notifies_waiting_worker_without_poll_delay(self) -> None:
        store = FakeQueueStore()
        engine = RecordingEngine()
        executor = PersistentAsyncExecutor(
            engine=engine,
            store=store,
            worker_count=1,
            capacity=10,
        )
        request = execution_request(task_definition(), async_mode=True)
        with patch(
            "model_processing_engine.async_queue.RECOVERY_POLL_INTERVAL_SECONDS",
            60.0,
        ):
            executor.start()
            executor.enqueue(request)
            self.assertTrue(engine.executed.wait(timeout=0.5))
            self.assertTrue(executor.stop(timeout_seconds=1))

        self.assertEqual(store.finished, ["queued-execution"])

    def test_stop_uses_one_deadline_and_retains_live_threads(self) -> None:
        request = execution_request(task_definition(), async_mode=True)
        raw_request = request.model_dump(by_alias=True)
        jobs = [(f"execution-{index}", raw_request) for index in range(3)]
        store = FakeQueueStore(jobs)
        engine = BlockingEngine(expected_calls=3)
        executor = PersistentAsyncExecutor(
            engine=engine,
            store=store,
            worker_count=3,
            capacity=10,
        )
        executor.start()
        self.assertTrue(engine.all_started.wait(timeout=1))

        started = time.monotonic()
        self.assertFalse(executor.stop(timeout_seconds=0.1))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.25)
        self.assertEqual(len(executor._threads), 3)
        with self.assertRaises(RuntimeError):
            executor.start()

        engine.release.set()
        self.assertTrue(executor.stop(timeout_seconds=1))
        self.assertEqual(sorted(store.finished), [f"execution-{index}" for index in range(3)])


if __name__ == "__main__":
    unittest.main()
