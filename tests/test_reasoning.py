from __future__ import annotations

import unittest

from model_processing_engine.exceptions import ConfigurationError
from model_processing_engine.reasoning import (
    reasoning_capability,
    resolve_reasoning_effort,
)


class ReasoningCapabilityTests(unittest.TestCase):
    def test_gpt_56_and_gpt_55_have_distinct_supported_values(self) -> None:
        gpt_56 = reasoning_capability("openai_compatible", "gpt-5.6-sol")
        gpt_55 = reasoning_capability("openai_compatible", "gpt-5.5")
        gpt_55_pro = reasoning_capability("openai_compatible", "gpt-5.5-pro")

        self.assertEqual(
            gpt_56.supported_efforts,
            ("none", "low", "medium", "high", "xhigh", "max"),
        )
        self.assertEqual(gpt_55.supported_efforts, ("none", "low", "medium", "high", "xhigh"))
        self.assertEqual(gpt_55_pro.supported_efforts, ("medium", "high", "xhigh"))
        self.assertEqual(gpt_55_pro.model_default, "high")

    def test_unknown_and_claude_models_are_auto_only_for_chat_completions(self) -> None:
        self.assertFalse(
            reasoning_capability("openai_compatible", "claude-opus-4-6").configurable
        )
        self.assertFalse(
            reasoning_capability("openai_compatible", "future-model").configurable
        )

    def test_runtime_precedence_and_auto_omission(self) -> None:
        runtime = resolve_reasoning_effort(
            provider_type="openai_compatible",
            model="gpt-5.6-sol",
            runtime_setting="low",
            task_setting="high",
            provider_setting="xhigh",
        )
        automatic = resolve_reasoning_effort(
            provider_type="openai_compatible",
            model="gpt-5.6-sol",
            runtime_setting=None,
            task_setting=None,
            provider_setting="auto",
        )

        self.assertEqual(runtime.effective, "low")
        self.assertEqual(runtime.source, "runtime")
        self.assertIsNone(automatic.effective)
        self.assertIsNone(automatic.wire_parameter)

    def test_unsupported_effort_is_rejected_without_downgrade(self) -> None:
        with self.assertRaises(ConfigurationError):
            resolve_reasoning_effort(
                provider_type="openai_compatible",
                model="gpt-5.5",
                runtime_setting="max",
                task_setting=None,
                provider_setting="auto",
            )


if __name__ == "__main__":
    unittest.main()
