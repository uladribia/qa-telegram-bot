# SPDX-License-Identifier: MIT
"""Tiny explicit runtime smoke operation for authorized deployments."""

import hashlib
from dataclasses import dataclass

from knowledge_bot.ports.clock import Clock
from knowledge_bot.ports.embedder import Embedder
from knowledge_bot.ports.generator import GenerationRequest, Generator
from knowledge_bot.ports.index import SearchProjectionRepository
from knowledge_bot.ports.vector_store import VectorRecord, VectorStore


@dataclass(frozen=True, slots=True)
class RuntimeSmokeService:
    """Exercise one embedding, vector, and generation path with cleanup."""

    embedder: Embedder
    generator: Generator
    vectors: VectorStore
    manifest: SearchProjectionRepository
    clock: Clock

    async def run(self) -> dict[str, object]:
        """Run the tiny smoke and always remove its temporary projection."""
        token = hashlib.sha256(str(self.clock.now()).encode()).hexdigest()[:12]
        vector_id = f"smoke:{token}"
        await self.manifest.reserve(vector_id, "smoke", token, None, self.clock.now())
        try:
            values = (await self.embedder.embed(["runtime smoke"]))[0]
            await self.vectors.upsert(
                [VectorRecord(vector_id, values, {"kind": "smoke", "object_id": token})]
            )
            await self.manifest.mark_active(vector_id, None, self.clock.now())
            matches = await self.vectors.query(
                values, top_k=1, filters={"kind": "smoke"}
            )
            result = await self.generator.generate(
                GenerationRequest(
                    question="Return insufficient.",
                    evidence=[],
                )
            )
            return {"vector_matches": len(matches), "generation_mode": result.status}
        finally:
            await self.vectors.delete([vector_id])
            await self.manifest.delete([vector_id])
