# SPDX-License-Identifier: MIT
"""Central Loguru configuration: the only place sinks are configured."""

import sys

from loguru import logger

_configured = False


def configure_logging(*, json_logs: bool = False) -> None:
    """Configure the process-wide Loguru sink.

    Args:
        json_logs: Emit structured JSON (production) instead of human text
            (development).
    """
    global _configured
    if _configured:
        return
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO" if json_logs else "DEBUG",
        serialize=json_logs,
        backtrace=False,
        diagnose=False,
    )
    _configured = True
