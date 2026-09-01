from __future__ import annotations

import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator

from model_processing_engine.exceptions import (
    ConfigurationError,
    ProviderEmptyContentError,
    ProviderError,
    ProviderNonJsonContentError,
)
from model_processing_engine.reasoning import ReasoningEffort

from .base import (
    ProviderCallResult,
    ProviderConfig,
    ProviderTextCompleted,
    ProviderTextDelta,
    ProviderTextEvent,
    ProviderTextResult,
)


MAX_PROVIDER_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PROVIDER_STREAM_LINE_BYTES = 1024 * 1024


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
        reasoning_effort: ReasoningEffort | None = None,
        repair_feedback: str | None = None,
        previous_output: dict[str, Any] | str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> ProviderCallResult:
        api_key = self._credential_resolver(self.config.api_key_env).strip()
        if not self.config.api_key_env or not api_key:
            raise ConfigurationError(
                f"Missing provider credential environment variable: {self.config.api_key_env or '<unset>'}"
            )
        resolved_model = (model or self.config.default_model).strip()
        if not resolved_model:
            raise ConfigurationError(f"Provider {self.config.id} requires a model")
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(input_payload, ensure_ascii=False)},
        ]
        if repair_feedback:
            if previous_output is not None:
                previous_content = (
                    _bounded_text(previous_output, maximum=50_000)
                    if isinstance(previous_output, str)
                    else _bounded_json(previous_output, maximum=50_000)
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": previous_content,
                    }
                )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "The previous output failed the declared JSON contract. "
                        f"Validation error: {_preview(repair_feedback)}. "
                        "Return one corrected JSON object only. Preserve the task meaning "
                        "and do not add fields outside the declared schema."
                    ),
                }
            )
        response_format: dict[str, Any]
        if "native_json_schema" in self.config.capabilities:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "mpe_result",
                    "strict": True,
                    "schema": output_schema,
                },
            }
        else:
            response_format = {"type": "json_object"}
        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "temperature": temperature,
            "response_format": response_format,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        if extra_body:
            protected = {
                "model",
                "messages",
                "temperature",
                "response_format",
                "max_tokens",
                "reasoning_effort",
            }
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
        try:
            response, attempts = self._request_json(request)
        except ProviderError as exc:
            raise _provider_call_error(
                exc,
                usage=exc.usage,
                attempts=exc.attempts,
                elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
            ) from exc
        usage = _normalize_usage(response.get("usage"))
        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
        try:
            content = _parse_json_content(_message_content(response))
        except ProviderError as exc:
            raise _provider_call_error(
                exc,
                usage=usage,
                attempts=attempts,
                elapsed_ms=elapsed_ms,
            ) from exc
        return ProviderCallResult(
            content=content,
            usage=usage,
            attempts=attempts,
            elapsed_ms=elapsed_ms,
        )

    def stream_text(
        self,
        *,
        model: str,
        system_prompt: str,
        input_payload: dict[str, Any],
        temperature: float,
        max_tokens: int | None,
        reasoning_effort: ReasoningEffort | None = None,
        repair_feedback: str | None = None,
        previous_output: str | None = None,
    ) -> Iterator[ProviderTextEvent]:
        api_key = self._credential_resolver(self.config.api_key_env).strip()
        if not self.config.api_key_env or not api_key:
            raise ConfigurationError(
                f"Missing provider credential environment variable: {self.config.api_key_env or '<unset>'}"
            )
        resolved_model = (model or self.config.default_model).strip()
        if not resolved_model:
            raise ConfigurationError(f"Provider {self.config.id} requires a model")
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(input_payload, ensure_ascii=False)},
        ]
        if repair_feedback:
            if previous_output is not None:
                messages.append(
                    {
                        "role": "assistant",
                        "content": _bounded_text(previous_output, maximum=50_000),
                    }
                )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "The previous plain-text answer failed the declared output contract. "
                        f"Validation error: {_preview(repair_feedback)}. "
                        "Return one corrected plain-text answer only, with no JSON wrapper."
                    ),
                }
            )
        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        request = urllib.request.Request(
            self._url(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "User-Agent": "model-processing-engine/0.1",
            },
            method="POST",
        )
        started = time.perf_counter()
        maximum_attempts = self.config.transport_retries + 1
        last_error: Exception | None = None
        for attempt in range(1, maximum_attempts + 1):
            emitted_content = False
            text_parts: list[str] = []
            usage = _normalize_usage(None)
            try:
                with _open_without_redirects(
                    request,
                    timeout=self.config.timeout_seconds,
                ) as response:
                    for data in _iter_sse_data(response, attempt=attempt):
                        if data == "[DONE]":
                            break
                        try:
                            event_payload = json.loads(data)
                        except json.JSONDecodeError as exc:
                            raise ProviderError(
                                f"Provider returned invalid SSE JSON: {_preview(data)}",
                                attempts=attempt,
                                audit_message="Provider returned invalid SSE JSON",
                            ) from exc
                        if not isinstance(event_payload, dict):
                            raise ProviderError(
                                "Provider SSE data is not a JSON object",
                                attempts=attempt,
                                audit_message="Provider SSE data is not a JSON object",
                            )
                        if isinstance(event_payload.get("error"), dict):
                            error_value = event_payload["error"]
                            raise ProviderError(
                                f"Provider stream failed: {_preview(str(error_value.get('message') or 'unknown error'))}",
                                attempts=attempt,
                                audit_message="Provider stream returned an error event",
                            )
                        if event_payload.get("usage") is not None:
                            usage = _normalize_usage(event_payload.get("usage"))
                        delta = _stream_delta_content(event_payload)
                        if delta:
                            emitted_content = True
                            text_parts.append(delta)
                            yield ProviderTextDelta(delta)
            except urllib.error.HTTPError as exc:
                try:
                    body = _read_provider_body(
                        exc,
                        attempt=attempt,
                        decode_errors="replace",
                    )
                finally:
                    exc.close()
                if not emitted_content and (exc.code == 429 or 500 <= exc.code < 600):
                    last_error = exc
                    if attempt < maximum_attempts:
                        time.sleep(0.1 * attempt)
                        continue
                raise ProviderError(
                    f"Provider request failed with HTTP {exc.code}: {_preview(body)}",
                    attempts=attempt,
                    elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
                    audit_message=f"Provider request failed with HTTP {exc.code}",
                ) from exc
            except ProviderError as exc:
                raise _provider_call_error(
                    exc,
                    usage=exc.usage or usage,
                    attempts=exc.attempts or attempt,
                    elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
                ) from exc
            except _retryable_errors() as exc:
                last_error = exc
                if not emitted_content and attempt < maximum_attempts:
                    time.sleep(0.1 * attempt)
                    continue
                raise ProviderError(
                    "Provider stream transport failed"
                    + (" after content was emitted" if emitted_content else ""),
                    attempts=attempt,
                    elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
                    audit_message="Provider stream transport failed",
                ) from exc
            text = "".join(text_parts)
            elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
            if not text:
                raise ProviderEmptyContentError(
                    "Provider response content is empty",
                    usage=usage,
                    attempts=attempt,
                    elapsed_ms=elapsed_ms,
                )
            yield ProviderTextCompleted(
                ProviderTextResult(
                    text=text,
                    usage=usage,
                    attempts=attempt,
                    elapsed_ms=elapsed_ms,
                )
            )
            return
        raise ProviderError(
            f"Provider stream transport failed after {maximum_attempts} attempts: "
            f"{last_error.__class__.__name__ if last_error else 'unknown error'}",
            attempts=maximum_attempts,
            elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
            audit_message="Provider stream transport failed",
        ) from last_error

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
                    body = _read_provider_body(response, attempt=attempt)
            except urllib.error.HTTPError as exc:
                try:
                    body = _read_provider_body(
                        exc,
                        attempt=attempt,
                        decode_errors="replace",
                    )
                finally:
                    exc.close()
                if exc.code == 429 or 500 <= exc.code < 600:
                    last_error = exc
                    if attempt < attempts:
                        time.sleep(0.1 * attempt)
                        continue
                raise ProviderError(
                    f"Provider request failed with HTTP {exc.code}: {_preview(body)}",
                    attempts=attempt,
                    audit_message=f"Provider request failed with HTTP {exc.code}",
                ) from exc
            except _retryable_errors() as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(0.1 * attempt)
                    continue
                break
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    f"Provider returned invalid response JSON: {_preview(body)}",
                    attempts=attempt,
                    audit_message="Provider returned invalid response JSON",
                ) from exc
            if not isinstance(parsed, dict):
                raise ProviderError(
                    "Provider response JSON is not an object",
                    attempts=attempt,
                    audit_message="Provider response JSON is not an object",
                )
            return parsed, attempt
        raise ProviderError(
            f"Provider transport failed after {attempts} attempts: "
            f"{last_error.__class__.__name__ if last_error else 'unknown error'}",
            attempts=attempts,
            audit_message=f"Provider transport failed after {attempts} attempts",
        ) from last_error


def _read_provider_body(
    response: Any,
    *,
    attempt: int,
    decode_errors: str = "strict",
) -> str:
    raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
        raise ProviderError(
            f"Provider response exceeded {MAX_PROVIDER_RESPONSE_BYTES} bytes",
            attempts=attempt,
            audit_message="Provider response exceeded the byte limit",
        )
    try:
        return raw.decode("utf-8", errors=decode_errors)
    except UnicodeDecodeError as exc:
        raise ProviderError(
            "Provider response is not valid UTF-8",
            attempts=attempt,
            audit_message="Provider response is not valid UTF-8",
        ) from exc


def _iter_sse_data(response: Any, *, attempt: int) -> Iterator[str]:
    total_bytes = 0
    data_lines: list[str] = []
    while True:
        raw_line = response.readline(MAX_PROVIDER_STREAM_LINE_BYTES + 1)
        if not raw_line:
            if data_lines:
                yield "\n".join(data_lines)
            return
        total_bytes += len(raw_line)
        if total_bytes > MAX_PROVIDER_RESPONSE_BYTES:
            raise ProviderError(
                f"Provider stream exceeded {MAX_PROVIDER_RESPONSE_BYTES} bytes",
                attempts=attempt,
                audit_message="Provider stream exceeded the byte limit",
            )
        if len(raw_line) > MAX_PROVIDER_STREAM_LINE_BYTES:
            raise ProviderError(
                f"Provider stream line exceeded {MAX_PROVIDER_STREAM_LINE_BYTES} bytes",
                attempts=attempt,
                audit_message="Provider stream line exceeded the byte limit",
            )
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProviderError(
                "Provider stream is not valid UTF-8",
                attempts=attempt,
                audit_message="Provider stream is not valid UTF-8",
            ) from exc
        line = line.rstrip("\r\n")
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line == "data":
            data_lines.append("")
        elif line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)


def _stream_delta_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
    )


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
    raise ProviderNonJsonContentError(
        f"Provider returned non-JSON content: {_preview(content)}",
        content=content,
        audit_message="Provider returned non-JSON content",
    )


def _normalize_usage(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    input_tokens = _integer(raw.get("prompt_tokens"))
    output_tokens = _integer(raw.get("completion_tokens"))
    total_tokens = _integer(raw.get("total_tokens")) or input_tokens + output_tokens
    details_value = raw.get("prompt_tokens_details")
    details = details_value if isinstance(details_value, dict) else {}
    cache_read_input_tokens = max(
        _integer(details.get("cached_tokens")),
        _integer(raw.get("prompt_cache_hit_tokens")),
    )
    return {
        "available": bool(raw),
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": total_tokens,
        "cacheReadInputTokens": cache_read_input_tokens,
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


def _bounded_json(value: dict[str, Any], *, maximum: int) -> str:
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return serialized if len(serialized) <= maximum else serialized[:maximum]


def _bounded_text(value: str, *, maximum: int) -> str:
    return value if len(value) <= maximum else value[:maximum]


def _provider_call_error(
    error: ProviderError,
    *,
    usage: dict[str, Any],
    attempts: int,
    elapsed_ms: int,
) -> ProviderError:
    if isinstance(error, ProviderNonJsonContentError):
        return ProviderNonJsonContentError(
            str(error),
            content=error.content,
            usage=usage,
            attempts=attempts,
            elapsed_ms=elapsed_ms,
            audit_message=error.audit_message,
            audit_calls=error.audit_calls,
        )
    error_type = ProviderEmptyContentError if isinstance(error, ProviderEmptyContentError) else ProviderError
    return error_type(
        str(error),
        usage=usage,
        attempts=attempts,
        elapsed_ms=elapsed_ms,
        audit_message=error.audit_message,
        audit_calls=error.audit_calls,
    )


def _environment_credential(name: str) -> str:
    return os.environ.get(name, "") if name else ""
