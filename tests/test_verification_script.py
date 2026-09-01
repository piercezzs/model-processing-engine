from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "verify_project.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("mpe_verify_project", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load verification script")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class VerificationScriptContractTests(unittest.TestCase):
    def test_deterministic_contract_covers_project_verification(self) -> None:
        module = _load_script()
        with tempfile.TemporaryDirectory() as pycache_dir:
            environment = module._project_environment(
                {"PYTHONPYCACHEPREFIX": pycache_dir}
            )
            steps = module._deterministic_steps(environment)

        commands = [" ".join(step.command) for step in steps]
        self.assertEqual(
            [step.label for step in steps],
            [
                "Python unit tests",
                "Python compile check",
                "Locked dependency resolution",
                "GitHub YAML parsing",
                "Admin JavaScript syntax",
                "Git whitespace and conflict markers",
                "Example Task Pack contract",
            ],
        )
        self.assertTrue(any("unittest discover" in command for command in commands))
        self.assertTrue(any("compileall" in command for command in commands))
        self.assertTrue(any("--check-github-yaml" in command for command in commands))
        self.assertTrue(any("task validate" in command for command in commands))
        self.assertEqual(steps[0].command[0], sys.executable)

        commands_by_label = {step.label: step.command for step in steps}
        for label, executable, arguments in (
            ("Locked dependency resolution", "uv", ("lock", "--check")),
            (
                "Admin JavaScript syntax",
                "node",
                ("--check", "src/model_processing_engine/admin_ui/app.js"),
            ),
            ("Git whitespace and conflict markers", "git", ("diff", "--check")),
        ):
            command = commands_by_label[label]
            self.assertEqual(Path(command[0]).stem.casefold(), executable)
            self.assertEqual(command[1:], arguments)

    def test_github_yaml_parser_accepts_mappings_and_rejects_malformed_yaml(self) -> None:
        module = _load_script()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            workflow = workflows / "ci.yml"
            workflow.write_text("name: CI\non: [push]\njobs: {}\n", encoding="utf-8")
            dependabot = root / ".github" / "dependabot.yaml"
            dependabot.write_text("version: 2\nupdates: []\n", encoding="utf-8")

            module._verify_github_yaml(root)

            workflow.write_text("name: [\n", encoding="utf-8")
            with self.assertRaisesRegex(module.VerificationError, "Could not parse YAML"):
                module._verify_github_yaml(root)

    def test_runtime_mode_restarts_and_checks_the_managed_service(self) -> None:
        module = _load_script()
        steps = module._runtime_steps(module._project_environment())
        commands = [" ".join(step.command) for step in steps]

        self.assertTrue(
            any("model_processing_engine.cli restart" in item for item in commands)
        )
        self.assertTrue(
            any("model_processing_engine.cli status" in item for item in commands)
        )
        self.assertEqual(steps[0].command[0], sys.executable)
        self.assertIn("historyProviderCacheTokens", module.ADMIN_SCRIPT_MARKERS)
        self.assertIn("historyTransportRetries", module.ADMIN_SCRIPT_MARKERS)
        self.assertIn("nativeJsonSchema", module.ADMIN_SCRIPT_MARKERS)
        self.assertIn("modelReasoningCapabilities", module.ADMIN_SCRIPT_MARKERS)
        self.assertIn("defaultReasoningEffort", module.ADMIN_SCRIPT_MARKERS)


if __name__ == "__main__":
    unittest.main()
