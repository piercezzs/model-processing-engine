from __future__ import annotations

import threading
import time
from typing import Any

from pydantic import ValidationError

from .cache import SQLiteRuntimeStore
from .contracts import ExecutionRequest, ResultEnvelope
from .engine import ModelProcessingEngine


RECOVERY_POLL_INTERVAL_SECONDS = 5.0


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
        self._wake_generation = 0

    def start(self) -> None:
        with self._condition:
            if self._started:
                self._threads = [thread for thread in self._threads if thread.is_alive()]
                if self._threads:
                    if self._stop_event.is_set():
                        raise RuntimeError("Persistent async executor is still stopping")
                    return
                self._started = False
            self.store.recover_async_executions()
            self._stop_event.clear()
            self._threads = [
                threading.Thread(
                    target=self._worker,
                    args=(index,),
                    name=f"mpe-async-worker-{index + 1}",
                    daemon=True,
                )
                for index in range(self.worker_count)
            ]
            self._started = True
            for thread in self._threads:
                thread.start()
            self._condition.notify_all()

    def stop(self, *, timeout_seconds: float = 2.0) -> bool:
        with self._condition:
            if not self._started:
                return True
            self._stop_event.set()
            self._condition.notify_all()
            threads = list(self._threads)
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        with self._condition:
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            if not self._threads:
                self._started = False
            return not self._threads

    def enqueue(self, request: ExecutionRequest) -> ResultEnvelope:
        reserved = self.engine.reserve(request, persist=False)
        self.store.enqueue_async_execution(
            envelope=reserved.model_dump(by_alias=True),
            request=request.model_dump(by_alias=True),
            capacity=self.capacity,
        )
        with self._condition:
            self._wake_generation += 1
            self._condition.notify()
        return reserved

    def stats(self) -> dict[str, Any]:
        return {
            **self.store.async_queue_stats(),
            "capacity": self.capacity,
            "workers": self.worker_count,
        }

    def _worker(self, index: int) -> None:
        try:
            while not self._stop_event.is_set():
                with self._condition:
                    observed_generation = self._wake_generation
                job = self.store.claim_next_async_execution()
                if job is None:
                    with self._condition:
                        self._condition.wait_for(
                            lambda: self._stop_event.is_set()
                            or self._wake_generation != observed_generation,
                            timeout=(
                                RECOVERY_POLL_INTERVAL_SECONDS
                                if index == 0
                                else None
                            ),
                        )
                    continue
                if self._stop_event.is_set():
                    return
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
        finally:
            current = threading.current_thread()
            with self._condition:
                self._threads = [thread for thread in self._threads if thread is not current]
                if not self._threads:
                    self._started = False
                self._condition.notify_all()
