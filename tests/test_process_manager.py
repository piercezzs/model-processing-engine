from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_processing_engine import process_manager
from model_processing_engine.constants import API_VERSION, APP_ID
from model_processing_engine.exceptions import ServiceManagerError
from model_processing_engine.process_manager import (
    HealthProbe,
    ServiceRecord,
    _prepare_runtime_directories,
    service_status,
    start_service,
    stop_service,
)
from model_processing_engine.settings import Settings


class _FakeProcess:
    def __init__(self, pid: int, *, exit_code: int | None = None) -> None:
        self.pid = pid
        self.exit_code = exit_code

    def poll(self) -> int | None:
        return self.exit_code


def _settings(root: Path, *, port: int = 18787) -> Settings:
    return Settings(
        root=root,
        provider_config_path=root / "providers.json",
        data_dir=root / "data",
        host="127.0.0.1",
        port=port,
        allow_remote=False,
        max_provider_concurrency=8,
        max_request_bytes=2 * 1024 * 1024,
        log_dir=root / "logs",
        run_dir=root / "run",
    )


def _health(settings: Settings, pid: int) -> HealthProbe:
    return HealthProbe(
        reachable=True,
        payload={
            "status": "ok",
            "app": APP_ID,
            "apiVersion": API_VERSION,
            "instanceId": settings.instance_id,
            "processId": pid,
        },
    )


def _record(settings: Settings, pid: int) -> None:
    settings.service_record_path.parent.mkdir(parents=True, exist_ok=True)
    settings.service_record_path.write_text(
        json.dumps(
            ServiceRecord(
                pid=pid,
                instance_id=settings.instance_id,
                root=str(settings.root),
                host=settings.host,
                port=settings.port,
                started_at="2026-01-01T00:00:00+00:00",
            ).as_dict()
        ),
        encoding="utf-8",
    )


class ProcessManagerTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not a Windows security boundary")
    def test_runtime_directories_are_private_without_restricting_explicit_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project-root"
            root.mkdir(mode=0o755)
            os.chmod(root, 0o755)
            settings = _settings(root)

            _prepare_runtime_directories(settings)

            self.assertEqual(root.stat().st_mode & 0o777, 0o755)
            self.assertEqual(settings.data_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(settings.service_log_path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(settings.service_record_path.parent.stat().st_mode & 0o777, 0o700)

    def test_status_reports_stopped_when_port_is_free(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir))
            with patch(
                "model_processing_engine.process_manager._probe_health",
                return_value=HealthProbe(reachable=False),
            ), patch(
                "model_processing_engine.process_manager._port_is_open",
                return_value=False,
            ):
                status = service_status(settings)
            self.assertEqual(status["status"], "stopped")

    def test_status_detects_matching_unmanaged_service(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir))
            with patch(
                "model_processing_engine.process_manager._probe_health",
                return_value=_health(settings, 43210),
            ):
                status = service_status(settings)
            self.assertEqual(status["status"], "running_unmanaged")
            self.assertFalse(status["managed"])

    def test_status_detects_foreign_listener_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir))
            with patch(
                "model_processing_engine.process_manager._probe_health",
                return_value=HealthProbe(
                    reachable=True,
                    payload={"status": "ok", "app": "different-service"},
                ),
            ):
                status = service_status(settings)
            self.assertEqual(status["status"], "conflict")

    def test_start_writes_record_and_waits_for_verified_health(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir))
            process = _FakeProcess(43210)
            probes = [
                HealthProbe(reachable=False),
                _health(settings, process.pid),
                _health(settings, process.pid),
            ]
            with patch(
                "model_processing_engine.process_manager._probe_health",
                side_effect=probes,
            ), patch(
                "model_processing_engine.process_manager._port_is_open",
                return_value=False,
            ), patch(
                "model_processing_engine.process_manager._spawn_service",
                return_value=process,
            ), patch(
                "model_processing_engine.process_manager._process_exists",
                return_value=True,
            ):
                result = start_service(settings, timeout_seconds=1)
            self.assertEqual(result["status"], "running")
            record = json.loads(settings.service_record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["pid"], process.pid)
            self.assertTrue(settings.service_log_path.is_file())

    def test_duplicate_start_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir))
            _record(settings, 43210)
            with patch(
                "model_processing_engine.process_manager._probe_health",
                return_value=_health(settings, 43210),
            ), patch(
                "model_processing_engine.process_manager._process_exists",
                return_value=True,
            ), patch("model_processing_engine.process_manager._spawn_service") as spawn:
                result = start_service(settings, timeout_seconds=1)
            self.assertTrue(result["alreadyRunning"])
            spawn.assert_not_called()

    def test_windows_venv_launcher_records_the_health_process_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir))
            launcher = _FakeProcess(43210)
            service_pid = 54321
            probes = [
                HealthProbe(reachable=False),
                _health(settings, service_pid),
                _health(settings, service_pid),
            ]
            with patch(
                "model_processing_engine.process_manager._is_windows",
                return_value=True,
            ), patch(
                "model_processing_engine.process_manager._probe_health",
                side_effect=probes,
            ), patch(
                "model_processing_engine.process_manager._port_is_open",
                return_value=False,
            ), patch(
                "model_processing_engine.process_manager._spawn_service",
                return_value=launcher,
            ), patch(
                "model_processing_engine.process_manager._process_exists",
                return_value=True,
            ):
                result = start_service(settings, timeout_seconds=1)
            self.assertEqual(result["status"], "running")
            self.assertEqual(result["pid"], service_pid)
            record = json.loads(settings.service_record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["pid"], service_pid)

    def test_start_failure_removes_new_service_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir))
            process = _FakeProcess(43210, exit_code=7)
            with patch(
                "model_processing_engine.process_manager._probe_health",
                return_value=HealthProbe(reachable=False),
            ), patch(
                "model_processing_engine.process_manager._port_is_open",
                return_value=False,
            ), patch(
                "model_processing_engine.process_manager._spawn_service",
                return_value=process,
            ):
                with self.assertRaises(ServiceManagerError):
                    start_service(settings, timeout_seconds=1)
            self.assertFalse(settings.service_record_path.exists())

    def test_os_kill_errors_are_treated_as_not_running(self) -> None:
        with patch(
            "model_processing_engine.process_manager._is_windows",
            return_value=False,
        ):
            with patch(
                "model_processing_engine.process_manager.os.kill",
                side_effect=OSError(87, "The parameter is incorrect"),
            ):
                self.assertFalse(process_manager._process_exists(43210))
            with patch(
                "model_processing_engine.process_manager.os.kill",
                side_effect=SystemError("kill returned a result with an exception set"),
            ):
                self.assertFalse(process_manager._process_exists(43210))

    def test_windows_process_existence_uses_native_query(self) -> None:
        with patch(
            "model_processing_engine.process_manager._is_windows",
            return_value=True,
        ), patch(
            "model_processing_engine.process_manager._windows_process_exists",
            return_value=True,
        ) as native_query, patch(
            "model_processing_engine.process_manager.os.kill",
        ) as kill:
            self.assertTrue(process_manager._process_exists(43210))
        native_query.assert_called_once_with(43210)
        kill.assert_not_called()

    def test_stop_refuses_to_signal_unverified_live_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir))
            _record(settings, os.getpid())
            with patch(
                "model_processing_engine.process_manager._probe_health",
                return_value=HealthProbe(reachable=False, error="connection refused"),
            ), patch(
                "model_processing_engine.process_manager._process_exists",
                return_value=True,
            ), patch("model_processing_engine.process_manager.os.kill") as kill:
                with self.assertRaises(ServiceManagerError):
                    stop_service(settings, timeout_seconds=1)
            kill.assert_not_called()

    def test_stop_signals_verified_process_and_removes_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir))
            _record(settings, 43210)
            running = {
                "status": "running",
                "managed": True,
                "pid": 43210,
            }
            with patch(
                "model_processing_engine.process_manager.service_status",
                return_value=running,
            ), patch(
                "model_processing_engine.process_manager._process_exists",
                return_value=False,
            ), patch("model_processing_engine.process_manager.os.kill") as kill:
                result = stop_service(settings, timeout_seconds=1)
            kill.assert_called_once()
            self.assertEqual(result["status"], "stopped")
            self.assertFalse(settings.service_record_path.exists())

    def test_active_control_lock_blocks_competing_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir))
            settings.service_lock_path.parent.mkdir(parents=True, exist_ok=True)
            settings.service_lock_path.write_text(
                json.dumps({"pid": os.getpid(), "token": "other"}),
                encoding="utf-8",
            )
            with self.assertRaises(ServiceManagerError):
                start_service(settings, timeout_seconds=1)


if __name__ == "__main__":
    unittest.main()
