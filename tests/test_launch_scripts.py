from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LaunchScriptContractTest(unittest.TestCase):
    def _read(self, name: str) -> str:
        return (PROJECT_ROOT / name).read_text(encoding="utf-8")

    def test_start_scripts_are_cross_platform_and_checkable(self) -> None:
        mac = self._read("start_mpe.command")
        windows = self._read("start_mpe.bat")

        for content in (mac, windows):
            self.assertIn("--check-only", content)
            self.assertIn("--status-only", content)
            self.assertIn("--start-only", content)
            self.assertIn("--no-open", content)
            self.assertIn("MPE_PROJECT_DIR", content)
            self.assertIn("pyproject.toml", content)
            self.assertIn("pip install", content)
            self.assertIn("mpe-pyproject.sha256", content)
            self.assertIn("Adopting the existing ready .venv", content)
            self.assertIn("model_processing_engine.cli restart", content)
            self.assertIn("model_processing_engine.cli start", content)
            self.assertIn("model_processing_engine.cli status", content)
            self.assertIn("model_processing_engine.cli admin", content)
            self.assertIn("sys.version_info >= (3, 10)", content)

    def test_launchers_delegate_process_identity_to_mpe(self) -> None:
        scripts = [
            self._read("start_mpe.command"),
            self._read("start_mpe.bat"),
            self._read("stop_mpe.command"),
            self._read("stop_mpe.bat"),
        ]
        for content in scripts:
            lowered = content.casefold()
            self.assertNotIn("kill -9", lowered)
            self.assertNotIn("taskkill", lowered)
            self.assertNotIn("pkill", lowered)

    def test_start_wrappers_restart_an_existing_managed_service(self) -> None:
        for name in ("start_mpe.command", "start_mpe.bat"):
            content = self._read(name)
            self.assertIn("Starting or restarting the managed service", content)
            self.assertIn("model_processing_engine.cli restart", content)

    def test_start_only_mode_keeps_idempotent_service_semantics(self) -> None:
        for name in ("start_mpe.command", "start_mpe.bat"):
            content = self._read(name)
            self.assertIn("START_ONLY", content)
            self.assertIn("Ensuring the managed service is running", content)
            self.assertIn("model_processing_engine.cli start", content)

    def test_status_only_mode_skips_environment_repair(self) -> None:
        mac = self._read("start_mpe.command")
        windows = self._read("start_mpe.bat")
        self.assertLess(
            mac.index('if [ "$STATUS_ONLY" -eq 1 ]'),
            mac.index('if [ "$CHECK_ONLY" -eq 1 ]'),
        )
        self.assertLess(
            windows.index('if "%STATUS_ONLY%"=="1"'),
            windows.index('if "%CHECK_ONLY%"=="1"'),
        )

    def test_stop_scripts_use_the_verified_manager(self) -> None:
        for name in ("stop_mpe.command", "stop_mpe.bat"):
            content = self._read(name)
            self.assertIn("model_processing_engine.cli stop", content)
            self.assertIn("model_processing_engine.cli status", content)

    def test_windows_digest_does_not_use_fragile_for_command_capture(self) -> None:
        windows = self._read("start_mpe.bat")
        self.assertIn("stamp.read_text", windows)
        self.assertIn("write_text(hashlib.sha256", windows)
        self.assertNotIn("for /f \"delims=\" %%H", windows)


if __name__ == "__main__":
    unittest.main()
