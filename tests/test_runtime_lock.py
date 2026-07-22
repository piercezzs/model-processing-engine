from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from model_processing_engine.exceptions import ServiceManagerError
from model_processing_engine.runtime_lock import exclusive_runtime_lock
from model_processing_engine.settings import Settings


def _settings(root: Path) -> Settings:
    return Settings(
        root=root,
        provider_config_path=root / "providers.json",
        data_dir=root / "data",
        host="127.0.0.1",
        port=18787,
        allow_remote=False,
        max_provider_concurrency=8,
        max_request_bytes=2 * 1024 * 1024,
    )


class RuntimeLockTests(unittest.TestCase):
    def test_second_service_cannot_own_same_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir))
            with exclusive_runtime_lock(settings):
                with self.assertRaises(ServiceManagerError):
                    with exclusive_runtime_lock(settings):
                        self.fail("A second runtime lock was unexpectedly acquired")

    def test_lock_is_released_after_context_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(Path(temp_dir))
            with exclusive_runtime_lock(settings):
                pass
            with exclusive_runtime_lock(settings):
                self.assertTrue((settings.data_dir / "service.lock").is_file())


if __name__ == "__main__":
    unittest.main()
