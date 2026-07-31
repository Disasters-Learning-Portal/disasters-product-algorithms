"""Logging utilities for Black Marble pipeline.

This module provides a consistent way to get loggers across the codebase.
Each module gets its own namespaced logger for selective filtering.
"""

import logging


def get_logger(name: str) -> logging.Logger:
    """Get a logger for the given module name.

    Args:
        name: Module name (typically __name__)

    Returns:
        Logger instance with NullHandler attached
    """
    logger = logging.getLogger(name)
    # Add NullHandler to prevent "No handler" warnings
    # Actual handlers are configured by the application (CLI)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger
