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
            self.assertIn("--no-open", content)
            self.assertIn("MPE_PROJECT_DIR", content)
            self.assertIn("pyproject.toml", content)
            self.assertIn("pip install", content)
            self.assertIn("mpe-pyproject.sha256", content)
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

    def test_stop_scripts_use_the_verified_manager(self) -> None:
        for name in ("stop_mpe.command", "stop_mpe.bat"):
            content = self._read(name)
            self.assertIn("model_processing_engine.cli stop", content)
            self.assertIn("model_processing_engine.cli status", content)


if __name__ == "__main__":
    unittest.main()
