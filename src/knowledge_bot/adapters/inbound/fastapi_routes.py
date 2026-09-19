# SPDX-License-Identifier: MIT
"""FastAPI application and HTTP routes exposed by the Worker."""

from fastapi import FastAPI

from knowledge_bot.infrastructure.logging import configure_logging

configure_logging()

app = FastAPI(title="knowledge-bot")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Report Worker liveness."""
    return {"status": "ok"}


@app.get("/smoke/deps")
async def smoke_deps() -> dict[str, str]:
    """Temporary check that core dependencies import in the Worker runtime."""
    import fastapi
    import loguru
    import pydantic

    return {
        "fastapi": fastapi.__version__,
        "pydantic": pydantic.VERSION,
        "loguru": type(loguru.logger).__name__,
    }
