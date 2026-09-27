# SPDX-License-Identifier: MIT
"""The one grounded-answer prompt, shared by every generator adapter.

The Workers AI and local Ollama adapters used to carry identical private
copies, which is how the two drift apart. Both import from here, so a rule
change is a single edit and the local adapter exercises the production prompt.

The evidence lines carry each item's provenance and its retrieval similarity,
so the model can weigh both a weak match and a weak source instead of treating
every candidate as equally good. Provenance is ``official`` for the club's own
published knowledge and ``reported`` for evidence inferred from group
conversation, which may be noise, hearsay or out of date.

The floor in ``AnswerPolicy`` stays the only hard cutoff, and provenance is not
a licence: an official, high-authority item still has to actually answer the
question. Trust says where a fact came from, never that it is relevant.

Each item is rendered as the question it answers (``Q:``) and its text (``A:``).
The question is not decoration: retrieval matches on it, so the model otherwise
has to guess what a bare assertion was in answer to, and a near-miss is
indistinguishable from an answer. Prefixing every line of the text with ``A:``
also keeps a multi-line answer from bleeding into the next item.
"""

from knowledge_bot.ports.generator import EvidenceItem, GenerationRequest

#: How each provenance tier is described to the model.
PROVENANCE_PHRASES = {
    "official": "official club knowledge",
    "reported": "unverified, inferred from group chat",
}

SYSTEM_PROMPT = """You answer questions using ONLY the evidence below.

Rules:
1. Do not add facts not supported by evidence.
2. Every item shows the question it answers, prefixed Q:, and its text, whose
   every line is prefixed A:. Answer the user's question. An item whose Q is not
   the question the user asked does not answer it, however well it matched and
   however official it is. Two items can look similar and still answer different
   questions: one asking what a fee includes does not answer how to pay it.
2. If evidence is insufficient or materially contradictory, return insufficient.
3. Every item is marked either official club knowledge or reported evidence taken
   from group conversation. Official evidence is the club's curated, published
   knowledge and is more trustworthy: prefer it over reported evidence when the
   two conflict, and prefer it as the basis for your answer when both are
   available. Reported evidence is weaker and may be casual chatter, hearsay or
   out of date, so never treat it as equal to an official source, and never
   build an answer on a single reported item when official evidence disagrees.
   A reported item's Q is the question its message replied to, not free text: it
   is a reply whose subject is whatever the group happened to be discussing, so
   judge it on whether that subject answers the user. Short casual lines such as
   "ok", "yes" or "we will be there" match many questions weakly and usually
   answer none of them.
4. Each item carries a similarity score from 0 to 1: how closely it matched the
   question. A low score is a weak match and a weak match is usually not the
   answer. If the text does not directly answer the question, return
   insufficient however high the score and however official or authoritative the
   item is. Trust describes where a fact came from; it never replaces the
   requirement that the text answer the question.
5. Never guess or extrapolate.
6. Return only JSON matching the schema.
7. source_ids must contain only supplied evidence ids, and must cite the items
   your answer actually rests on.
8. Answer in the language of the user's question.

Return JSON:
{"status": "answered" | "insufficient", "answer": "...", "source_ids": ["..."]}"""


def _provenance_phrase(item: EvidenceItem) -> str:
    """Describe one item's provenance for its evidence line.

    Only the item's own facts are rendered here. How reported evidence is
    gathered and why it is often off-topic is explained once in the system
    prompt, not repeated on every line.
    """
    phrase = PROVENANCE_PHRASES.get(item.provenance, item.provenance)
    if item.source_kind:
        return f"{phrase}, source={item.source_kind}"
    return phrase


def render_user(request: GenerationRequest) -> str:
    """Render the question and its labelled, scored evidence for the model.

    Each item becomes a header, the question it answers, and its text with every
    line prefixed, so a multi-line answer cannot be mistaken for the next item.
    """
    blocks: list[str] = []
    for item in request.evidence:
        header = (
            f"[{item.source_id}] ({_provenance_phrase(item)},"
            f" authority={item.authority}, similarity={item.similarity:.2f})"
        )
        lines = [header]
        if item.question:
            lines.append(f"  Q: {item.question}")
        lines.extend(f"  A: {line}" for line in item.text.splitlines() or [""])
        blocks.append("\n".join(lines))
    evidence = "\n".join(blocks)
    return f"QUESTION:\n{request.question}\n\nEVIDENCE:\n{evidence or '(no evidence)'}"
