# SPDX-License-Identifier: MIT
"""The one grounded-answer prompt, shared by every generator adapter.

The Workers AI and local Ollama adapters used to carry identical private
copies, which is how the two drift apart. Both import from here, so a rule
change is a single edit and the local adapter exercises the production prompt.

The evidence lines carry each item's retrieval similarity, so the model can
weigh a weak match instead of treating every candidate as equally good. The
floor in ``AnswerPolicy`` stays the only hard cutoff: the score is advice, not
a gate, and a high score never excuses text that does not answer the question.
"""

from knowledge_bot.ports.generator import GenerationRequest

SYSTEM_PROMPT = """You answer questions using ONLY the evidence below.

Rules:
1. Do not add facts not supported by evidence.
2. If evidence is insufficient or materially contradictory, return insufficient.
3. Prefer higher-authority evidence when sources conflict.
4. Each item carries a similarity score from 0 to 1: how closely it matched the
   question. A low score is a weak match and a weak match is usually not the
   answer. If the text does not directly answer the question, return
   insufficient however high the score.
5. Never guess or extrapolate.
6. Return only JSON matching the schema.
7. source_ids must contain only supplied evidence ids.
8. Answer in the language of the user's question.

Return JSON:
{"status": "answered" | "insufficient", "answer": "...", "source_ids": ["..."]}"""


def render_user(request: GenerationRequest) -> str:
    """Render the question and its scored evidence for the model."""
    evidence = "\n".join(
        f"[{item.source_id}] ({item.label}, authority={item.authority},"
        f" similarity={item.similarity:.2f}) {item.text}"
        for item in request.evidence
    )
    return f"QUESTION:\n{request.question}\n\nEVIDENCE:\n{evidence or '(no evidence)'}"
