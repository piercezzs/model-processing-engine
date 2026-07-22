from __future__ import annotations

import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from model_processing_engine.exceptions import (
    ConfigurationError,
    ProviderEmptyContentError,
    ProviderError,
)

from .base import ProviderCallResult, ProviderConfig


class OpenAICompatibleProvider:
    def __init__(
        self,
        config: ProviderConfig,
        *,
        credential_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self.config = config
        self._credential_resolver = credential_resolver or _environment_credential

    def call_json(
        self,
        *,
        model: str,
        system_prompt: str,
        input_payload: dict[str, Any],
        output_schema: dict[str, Any],
        temperature: float,
        max_tokens: int | None,
        extra_body: dict[str, Any] | None = None,
    ) -> ProviderCallResult:
        del output_schema
        api_key = self._credential_resolver(self.config.api_key_env).strip()
        if not self.config.api_key_env or not api_key:
            raise ConfigurationError(
                f"Missing provider credential environment variable: {self.config.api_key_env or '<unset>'}"
            )
        resolved_model = (model or self.config.default_model).strip()
        if not resolved_model:
            raise ConfigurationError(f"Provider {self.config.id} requires a model")
        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(input_payload, ensure_ascii=False)},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if extra_body:
            protected = {"model", "messages", "temperature", "response_format", "max_tokens"}
            collisions = protected.intersection(extra_body)
            if collisions:
                raise ConfigurationError(
                    "Provider request options cannot override protected fields: "
                    + ", ".join(sorted(collisions))
                )
            payload.update(extra_body)
        request = urllib.request.Request(
            self._url(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "model-processing-engine/0.1",
            },
            method="POST",
        )
        started = time.perf_counter()
        response, attempts = self._request_json(request)
        content = _parse_json_content(_message_content(response))
        return ProviderCallResult(
            content=content,
            usage=_normalize_usage(response.get("usage")),
            attempts=attempts,
            elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
        )

    def list_models(self, *, models_path: str = "/models") -> dict[str, Any]:
        api_key = self._credential_resolver(self.config.api_key_env).strip()
        if not self.config.api_key_env or not api_key:
            raise ConfigurationError(
                f"Missing provider credential environment variable: {self.config.api_key_env or '<unset>'}"
            )
        request = urllib.request.Request(
            self._url_for(models_path),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "model-processing-engine/0.1",
            },
            method="GET",
        )
        started = time.perf_counter()
        response, attempts = self._request_json(request)
        data = response.get("data")
        if not isinstance(data, list):
            raise ProviderError("Provider model list has no data array")
        models: list[str] = []
        seen: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if not model_id or len(model_id) > 256 or model_id in seen:
                continue
            seen.add(model_id)
            models.append(model_id)
            if len(models) >= 512:
                break
        if not models:
            raise ProviderError("Provider returned no usable model IDs")
        return {
            "models": models,
            "attempts": attempts,
            "elapsedMs": max(0, int((time.perf_counter() - started) * 1000)),
        }

    def _url(self) -> str:
        return self._url_for(self.config.chat_completions_path)

    def _url_for(self, path: str) -> str:
        return self.config.base_url.rstrip("/") + "/" + path.lstrip("/")

    def _request_json(self, request: urllib.request.Request) -> tuple[dict[str, Any], int]:
        attempts = self.config.transport_retries + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                with _open_without_redirects(
                    request,
                    timeout=self.config.timeout_seconds,
                ) as response:
                    body = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                finally:
                    exc.close()
                if exc.code == 429 or 500 <= exc.code < 600:
                    last_error = exc
                    if attempt < attempts:
                        time.sleep(0.1 * attempt)
                        continue
                raise ProviderError(f"Provider request failed with HTTP {exc.code}: {_preview(body)}") from exc
            except _retryable_errors() as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(0.1 * attempt)
                    continue
                break
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ProviderError(f"Provider returned invalid response JSON: {_preview(body)}") from exc
            if not isinstance(parsed, dict):
                raise ProviderError("Provider response JSON is not an object")
            return parsed, attempt
        raise ProviderError(
            f"Provider transport failed after {attempts} attempts: "
            f"{last_error.__class__.__name__ if last_error else 'unknown error'}"
        ) from last_error


def _message_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError("Provider response has no choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ProviderError("Provider response has no message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderEmptyContentError("Provider response content is empty")
    return content.strip()


def _parse_json_content(content: str) -> dict[str, Any]:
    candidates = [content.strip()]
    lines = content.strip().splitlines()
    if len(lines) >= 3 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        candidates.append("\n".join(lines[1:-1]).strip())
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ProviderError(f"Provider returned non-JSON content: {_preview(content)}")


def _normalize_usage(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    input_tokens = _integer(raw.get("prompt_tokens"))
    output_tokens = _integer(raw.get("completion_tokens"))
    total_tokens = _integer(raw.get("total_tokens")) or input_tokens + output_tokens
    details = raw.get("prompt_tokens_details") if isinstance(raw.get("prompt_tokens_details"), dict) else {}
    return {
        "available": bool(raw),
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": total_tokens,
        "cacheReadInputTokens": _integer(details.get("cached_tokens")),
        "source": "provider_response" if raw else "provider_response_without_usage",
    }


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _retryable_errors() -> tuple[type[Exception], ...]:
    return (
        http.client.IncompleteRead,
        urllib.error.URLError,
        TimeoutError,
        socket.timeout,
        ConnectionError,
    )


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _open_without_redirects(request: urllib.request.Request, *, timeout: int) -> Any:
    opener = urllib.request.build_opener(_RejectRedirectHandler())
    return opener.open(request, timeout=timeout)


def _preview(value: str) -> str:
    return " ".join(value.split())[:300] or "<empty>"


def _environment_credential(name: str) -> str:
    return os.environ.get(name, "") if name else ""
