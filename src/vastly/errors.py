"""Structured error hierarchy for vastly."""


class VastlyError(Exception):
    """Base error for all vastly failures."""

    exit_code = 1


class ConfigError(VastlyError):
    """Invalid or unreadable configuration."""

    exit_code = 2


class APIError(VastlyError):
    """Vast.ai API unreachable or returned an error."""

    exit_code = 3
