# SPDX-License-Identifier: MIT
"""Liveness and readiness probes."""

from fastapi import APIRouter


def build_system_router() -> APIRouter:
    """Build the platform probe routes."""
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Report application liveness.

        In production this path is served by the static asset layer, so edge
        liveness does not depend on the Python interpreter starting. Use
        ``/readyz`` to check that the application itself is up.
        """
        return {"status": "ok"}

    @router.get("/readyz")
    async def readyz() -> dict[str, str]:
        """Report Worker readiness without consuming AI quota."""
        return {"status": "ok"}

    return router
