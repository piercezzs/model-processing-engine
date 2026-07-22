from __future__ import annotations

import re
import unittest
from pathlib import Path

from model_processing_engine.constants import VERSION


class MetadataTests(unittest.TestCase):
    def test_runtime_version_matches_project_metadata(self) -> None:
        pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version = "([^"]+)"$', pyproject, flags=re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), VERSION)


if __name__ == "__main__":
    unittest.main()
