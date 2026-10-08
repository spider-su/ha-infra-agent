"""Shared safe configuration and execution errors."""


class ConfigError(ValueError):
    """Configuration or bounded runtime error safe to include in task results."""
