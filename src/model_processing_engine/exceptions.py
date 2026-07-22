from __future__ import annotations


class ModelProcessingError(Exception):
    """Base exception for expected engine failures."""


class ContractValidationError(ModelProcessingError):
    """A task, input, or provider output violated a declared contract."""


class ProviderError(ModelProcessingError):
    """A provider could not complete a model request."""


class ProviderEmptyContentError(ProviderError):
    """A provider completed a request but returned no assistant content."""


class ConfigurationError(ModelProcessingError):
    """Local runtime configuration is invalid or incomplete."""


class ExecutionNotFoundError(ModelProcessingError):
    """An execution record does not exist."""


class ServiceManagerError(ModelProcessingError):
    """The managed local service could not be controlled safely."""
