from __future__ import annotations


class ModelProcessingError(Exception):
    """Base exception for expected engine failures."""


class ContractValidationError(ModelProcessingError):
    """A task, input, or provider output violated a declared contract."""


class ProviderError(ModelProcessingError):
    """A provider could not complete a model request."""

    def __init__(
        self,
        message: str,
        *,
        usage: dict[str, object] | None = None,
        attempts: int = 0,
        elapsed_ms: int = 0,
        audit_message: str = "",
        audit_calls: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = dict(usage or {})
        self.attempts = max(0, int(attempts))
        self.elapsed_ms = max(0, int(elapsed_ms))
        self.audit_message = (audit_message or " ".join(str(message).split()))[:240]
        self.audit_calls = list(audit_calls or [])


class ProviderEmptyContentError(ProviderError):
    """A provider completed a request but returned no assistant content."""


class ProviderNonJsonContentError(ProviderError):
    """A structured-output request returned bounded, non-JSON assistant content."""

    def __init__(self, message: str, *, content: str, **kwargs: object) -> None:
        super().__init__(message, **kwargs)
        self.content = str(content)[:50_000]


class ConfigurationError(ModelProcessingError):
    """Local runtime configuration is invalid or incomplete."""


class ProviderConfigurationNotFoundError(ConfigurationError):
    """A requested local Provider configuration does not exist."""


class ExecutionNotFoundError(ModelProcessingError):
    """An execution record does not exist."""


class AsyncQueueFullError(ModelProcessingError):
    """The persistent asynchronous execution queue has reached its capacity."""


class ServiceManagerError(ModelProcessingError):
    """The managed local service could not be controlled safely."""
