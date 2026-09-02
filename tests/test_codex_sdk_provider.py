from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from model_processing_engine.exceptions import (
    ConfigurationError,
    ProviderError,
    ProviderNonJsonContentError,
)
from model_processing_engine.providers.base import ProviderConfig, load_provider_registry_data
from model_processing_engine.providers.codex_sdk import (
    CodexSdkProvider,
    _codex_output_schema,
    _run_turn,
    _run_turn_with_timeout,
)


class _ApprovalMode:
    deny_all = "deny_all"


class _Sandbox:
    read_only = "read-only"


@dataclass
class _UsageTotals:
    input_tokens: int = 12
    output_tokens: int = 5
    total_tokens: int = 17
    cached_input_tokens: int = 3
    reasoning_output_tokens: int = 2


@dataclass
class _Usage:
    total: _UsageTotals


@dataclass
class _Result:
    final_response: str
    usage: _Usage | None = None
    items: list[object] = field(default_factory=list)


@dataclass
class _WebSearchItem:
    type: str = "webSearch"


@dataclass
class _ThreadItem:
    root: object


class _Turn:
    def __init__(self, result: _Result) -> None:
        self.result = result
        self.interrupted = False

    def run(self) -> _Result:
        return self.result

    def interrupt(self) -> None:
        self.interrupted = True


class _Thread:
    def __init__(self, turn: _Turn) -> None:
        self._turn = turn
        self.turn_kwargs: dict[str, object] = {}

    def turn(self, _prompt: str, **kwargs: object) -> _Turn:
        self.turn_kwargs = kwargs
        return self._turn


class _Codex:
    def __init__(self, thread: _Thread) -> None:
        self._thread = thread
        self.thread_kwargs: dict[str, object] = {}

    def __enter__(self) -> "_Codex":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def thread_start(self, **kwargs: object) -> _Thread:
        self.thread_kwargs = kwargs
        return self._thread


class CodexSdkProviderTests(unittest.TestCase):
    def test_structured_call_uses_ephemeral_read_only_thread_and_normalizes_usage(self) -> None:
        turn = _Turn(
            _Result(
                '{"events": []}',
                _Usage(_UsageTotals()),
                [_ThreadItem(_WebSearchItem()), _ThreadItem(_WebSearchItem())],
            )
        )
        thread = _Thread(turn)
        codex = _Codex(thread)
        provider = CodexSdkProvider(
            ProviderConfig(
                id="codex-local",
                type="codex_sdk",
                default_model="gpt-5.6-sol",
                capabilities=("structured_json", "web_search"),
                web_search="live",
            ),
            codex_factory=lambda _path, _search: codex,
        )
        sdk = {"ApprovalMode": _ApprovalMode, "Sandbox": _Sandbox}

        with patch(
            "model_processing_engine.providers.codex_sdk._load_codex_sdk",
            return_value=sdk,
        ):
            result = provider.call_json(
                model="",
                system_prompt="Find bounded public events.",
                input_payload={"startDate": "2026-06-16", "endDate": "2026-06-18"},
                output_schema={"type": "object"},
                temperature=0.1,
                max_tokens=1000,
                reasoning_effort="medium",
            )

        self.assertEqual(result.content, {"events": []})
        self.assertEqual(result.usage["totalTokens"], 17)
        self.assertEqual(result.usage["cacheReadInputTokens"], 3)
        self.assertEqual(result.usage["webSearchCalls"], 2)
        self.assertTrue(codex.thread_kwargs["ephemeral"])
        self.assertEqual(codex.thread_kwargs["sandbox"], "read-only")
        self.assertEqual(thread.turn_kwargs["output_schema"], {"type": "object"})
        self.assertEqual(thread.turn_kwargs["effort"], "medium")
        self.assertTrue(Path(str(codex.thread_kwargs["cwd"])).name.startswith("mpe-codex-"))

    def test_non_json_result_is_rejected(self) -> None:
        turn = _Turn(_Result("not json"))
        thread = _Thread(turn)
        codex = _Codex(thread)
        provider = CodexSdkProvider(
            ProviderConfig(
                id="codex-local",
                type="codex_sdk",
                default_model="gpt-5.6-sol",
            ),
            codex_factory=lambda _path, _search: codex,
        )
        sdk = {"ApprovalMode": _ApprovalMode, "Sandbox": _Sandbox}

        with patch(
            "model_processing_engine.providers.codex_sdk._load_codex_sdk",
            return_value=sdk,
        ):
            with self.assertRaises(ProviderNonJsonContentError):
                provider.call_json(
                    model="",
                    system_prompt="Return JSON.",
                    input_payload={},
                    output_schema={"type": "object"},
                    temperature=0.1,
                    max_tokens=None,
                )

    def test_timeout_interrupts_turn(self) -> None:
        release = threading.Event()

        class SlowTurn:
            def __init__(self) -> None:
                self.interrupted = False

            def run(self) -> _Result:
                release.wait(1)
                return _Result("{}")

            def interrupt(self) -> None:
                self.interrupted = True
                release.set()

        turn = SlowTurn()
        with self.assertRaises(ProviderError) as raised:
            _run_turn_with_timeout(turn, 0.01)
        self.assertIn("timed out", str(raised.exception))
        self.assertTrue(turn.interrupted)

    def test_zero_timeout_runs_turn_without_wall_clock_deadline(self) -> None:
        turn = _Turn(_Result('{"status":"ok"}'))

        result = _run_turn(turn, 0)

        self.assertEqual(result.final_response, '{"status":"ok"}')
        self.assertFalse(turn.interrupted)

    def test_codex_schema_removes_unsupported_hints_only(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "topics": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "format": "uri"},
                }
            },
        }
        adapted = _codex_output_schema(schema)
        self.assertNotIn("uniqueItems", adapted["properties"]["topics"])
        self.assertNotIn("format", adapted["properties"]["topics"]["items"])
        self.assertIn("uniqueItems", schema["properties"]["topics"])
        self.assertIn("format", schema["properties"]["topics"]["items"])

    def test_provider_config_validates_web_search_mode(self) -> None:
        raw = {
            "defaultProviderId": "codex-local",
            "providers": {
                "codex-local": {
                    "type": "codex_sdk",
                    "defaultModel": "gpt-5.6-sol",
                    "capabilities": ["structured_json", "web_search"],
                    "webSearch": "live",
                }
            },
        }
        registry = load_provider_registry_data(raw, factories={"codex_sdk": CodexSdkProvider})
        provider = registry.get("codex-local")
        self.assertEqual(provider.config.web_search, "live")
        self.assertNotEqual(
            provider.config.digest,
            ProviderConfig(
                id="codex-local",
                type="codex_sdk",
                default_model="gpt-5.6-sol",
                capabilities=("structured_json", "web_search"),
                web_search="cached",
            ).digest,
        )

    def test_provider_config_allows_disabled_timeout_only_for_codex_sdk(self) -> None:
        raw = {
            "defaultProviderId": "codex-local",
            "providers": {
                "codex-local": {
                    "type": "codex_sdk",
                    "defaultModel": "gpt-5.6-sol",
                    "capabilities": ["structured_json", "web_search"],
                    "webSearch": "live",
                    "timeoutSeconds": 0,
                }
            },
        }

        registry = load_provider_registry_data(raw, factories={"codex_sdk": CodexSdkProvider})

        self.assertEqual(registry.get("codex-local").config.timeout_seconds, 0)

        raw["providers"]["codex-local"]["type"] = "openai_compatible"
        raw["providers"]["codex-local"]["baseUrl"] = "https://example.com/v1"
        with self.assertRaisesRegex(ConfigurationError, "must be 1-600"):
            load_provider_registry_data(raw, factories={"openai_compatible": CodexSdkProvider})


if __name__ == "__main__":
    unittest.main()
