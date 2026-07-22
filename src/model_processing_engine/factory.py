from __future__ import annotations

from pathlib import Path

from .cache import SQLiteRuntimeStore
from .engine import ModelProcessingEngine
from .providers.base import load_provider_registry
from .providers.mock import MockProvider
from .providers.openai_compatible import OpenAICompatibleProvider
from .settings import Settings, load_settings


def build_default_engine(
    *,
    root: str | Path | None = None,
    settings: Settings | None = None,
    recover_incomplete: bool = False,
) -> ModelProcessingEngine:
    runtime = settings or load_settings(root)
    providers = load_provider_registry(
        runtime.provider_config_path,
        factories={
            "mock": MockProvider,
            "openai_compatible": OpenAICompatibleProvider,
        },
    )
    store = SQLiteRuntimeStore(runtime.database_path)
    if recover_incomplete:
        store.recover_incomplete_executions()
    return ModelProcessingEngine(
        providers=providers,
        store=store,
        max_provider_concurrency=runtime.max_provider_concurrency,
    )
