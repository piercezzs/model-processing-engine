from __future__ import annotations

import threading
from typing import Any

from pydantic import ValidationError

from .cache import SQLiteRuntimeStore
from .contracts import ExecutionRequest, ResultEnvelope
from .engine import ModelProcessingEngine


class PersistentAsyncExecutor:
    def __init__(
        self,
        *,
        engine: ModelProcessingEngine,
        store: SQLiteRuntimeStore,
        worker_count: int,
        capacity: int,
    ) -> None:
        self.engine = engine
        self.store = store
        self.worker_count = max(1, worker_count)
        self.capacity = max(1, capacity)
        self._condition = threading.Condition()
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started = False

    def start(self) -> None:
        with self._condition:
            if self._started:
                return
            self.store.recover_async_executions()
            self._stop_event.clear()
            self._threads = [
                threading.Thread(
                    target=self._worker,
                    name=f"mpe-async-worker-{index + 1}",
                    daemon=True,
                )
                for index in range(self.worker_count)
            ]
            self._started = True
            for thread in self._threads:
                thread.start()
            self._condition.notify_all()

    def stop(self, *, timeout_seconds: float = 2.0) -> None:
        with self._condition:
            if not self._started:
                return
            self._stop_event.set()
            self._condition.notify_all()
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=max(0.0, timeout_seconds))
        with self._condition:
            self._threads = []
            self._started = False

    def enqueue(self, request: ExecutionRequest) -> ResultEnvelope:
        reserved = self.engine.reserve(request, persist=False)
        self.store.enqueue_async_execution(
            envelope=reserved.model_dump(by_alias=True),
            request=request.model_dump(by_alias=True),
            capacity=self.capacity,
        )
        with self._condition:
            self._condition.notify()
        return reserved

    def stats(self) -> dict[str, Any]:
        return {
            **self.store.async_queue_stats(),
            "capacity": self.capacity,
            "workers": self.worker_count,
        }

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            job = self.store.claim_next_async_execution()
            if job is None:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._stop_event.is_set()
                        or self.store.has_queued_async_executions(),
                        timeout=0.5,
                    )
                continue
            execution_id, raw_request = job
            try:
                request = ExecutionRequest.model_validate(raw_request)
            except ValidationError as exc:
                self.store.fail_async_execution(
                    execution_id,
                    error=f"Persisted async request is invalid: {exc.__class__.__name__}",
                )
                continue
            try:
                self.engine.execute(request, execution_id=execution_id)
            except Exception as exc:
                self.store.fail_async_execution(
                    execution_id,
                    error=f"Async worker failed: {exc.__class__.__name__}",
                )
            else:
                self.store.finish_async_execution(execution_id)
