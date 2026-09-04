from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_processing_engine.exceptions import ConfigurationError
from model_processing_engine.project_environment import (
    discover_project_dir,
    load_project_environment,
    read_env_file,
    update_project_environment,
)


class ProjectEnvironmentTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        (root / "pyproject.toml").write_text(
            '[project]\nname = "model-processing-engine"\n',
            encoding="utf-8",
        )
        return root

    def test_discovers_an_explicit_mpe_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir))
            self.assertEqual(discover_project_dir(explicit=project), project.resolve())

    def test_rejects_an_invalid_explicit_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ConfigurationError):
                discover_project_dir(explicit=temp_dir)

    def test_load_preserves_existing_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir))
            (project / ".env").write_text(
                'MPE_AI_API_KEY="file-secret"\nMPE_PORT=9000\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MPE_AI_API_KEY": "process-secret"}, clear=True):
                loaded = load_project_environment(project)
                self.assertEqual(loaded["MPE_AI_API_KEY"], "file-secret")
                self.assertEqual(os.environ["MPE_AI_API_KEY"], "process-secret")
                self.assertEqual(os.environ["MPE_PORT"], "9000")

    def test_update_is_allowlisted_atomic_and_preserves_comments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir))
            (project / ".env").write_text(
                '# local settings\nMPE_PORT=8787\n',
                encoding="utf-8",
            )
            values = update_project_environment(
                project,
                {
                    "MPE_PORT": "8989",
                    "MPE_PROVIDER_OPENAI_API_KEY": "key with spaces=#value",
                },
            )
            self.assertEqual(values["MPE_PORT"], "8989")
            self.assertEqual(values["MPE_PROVIDER_OPENAI_API_KEY"], "key with spaces=#value")
            content = (project / ".env").read_text(encoding="utf-8")
            self.assertIn("# local settings", content)
            if os.name != "nt":
                mode = stat.S_IMODE((project / ".env").stat().st_mode)
                self.assertEqual(mode, 0o600)

    def test_rejects_non_mpe_keys_and_duplicate_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir))
            with self.assertRaises(ConfigurationError):
                update_project_environment(project, {"PATH": "unsafe"})
            (project / ".env").write_text("MPE_PORT=1\nMPE_PORT=2\n", encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                read_env_file(project / ".env")

    def test_update_can_remove_an_allowlisted_key_without_touching_other_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir))
            (project / ".env").write_text(
                '# local settings\nMPE_PORT=8787\nMPE_PROVIDER_UNUSED_API_KEY="secret"\n',
                encoding="utf-8",
            )

            values = update_project_environment(
                project,
                {},
                removals=("MPE_PROVIDER_UNUSED_API_KEY",),
            )

            self.assertEqual(values, {"MPE_PORT": "8787"})
            content = (project / ".env").read_text(encoding="utf-8")
            self.assertIn("# local settings", content)
            self.assertNotIn("MPE_PROVIDER_UNUSED_API_KEY", content)

    def test_update_rejects_overlapping_update_and_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir))
            with self.assertRaisesRegex(ConfigurationError, "updated and removed"):
                update_project_environment(
                    project,
                    {"MPE_PORT": "8787"},
                    removals=("MPE_PORT",),
                )


if __name__ == "__main__":
    unittest.main()
