# SPDX-License-Identifier: MIT
"""Answering a private question from every group the asker shares with the bot.

Someone in three groups who writes to the bot privately is asking three
questions at once: what does the club say, what has my group settled, and what
has the other one. This service answers that person once per group they are
part of, and shows them what actually came back.

Two decisions shape the result.

**One round per group, not one round per group plus a global pass.** Every
round already searches global knowledge alongside the group's own, so a
separate global round would be an extra model call repeating what the first
group's round already found. What each answer is *made of* is read from the
scopes of the evidence behind it, not from the scope that was searched, so the
label a reader sees is the honest one: a block headed "Global" says the
knowledge was global, whichever group happened to be asked.

**Identical answers collapse, different ones do not.** Two rounds that came
back with the same words and the same sources are one answer, and repeating it
per group would be noise. Two rounds that differ in wording, or that cite
different sources, stay separate even when they read alike: a group's local
correction and the global answer are different facts, and hiding one behind the
other is how a reader loses the ability to correct it.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from knowledge_bot.application.answer_question import AnswerService, scoped_answer_id
from knowledge_bot.domain.entities import ChannelBinding
from knowledge_bot.domain.scope import GLOBAL_SCOPE, scope_for_space
from knowledge_bot.models.messages import NormalizedMessage
from knowledge_bot.models.questions import AskQuestionResponse

GLOBAL_LABEL = "\U0001f310 Global"
GROUP_LABEL = "\U0001f465 {name}"
SCOPE_SEPARATOR = " \u00b7 "


@dataclass(frozen=True, slots=True)
class AnswerBundle:
    """One distinct answer to a private question, ready to deliver.

    Attributes:
        text: Exactly what to send, labelled when there is more than one.
        representative_answer_id: The answer the feedback button points at.
        answer_ids: Every answer this message covers, including the ones
            collapsed into it. A webhook retry records a receipt for each, so
            a collapsed answer is never sent twice.
    """

    text: str
    representative_answer_id: str
    answer_ids: tuple[str, ...]


@dataclass(slots=True)
class _Collected:
    """One distinct answer while it is still being collected."""

    response: AskQuestionResponse
    scopes: tuple[str, ...]
    is_local: bool
    answer_ids: list[str] = field(default_factory=list)

    def absorb(
        self, response: AskQuestionResponse, scopes: tuple[str, ...], is_local: bool
    ) -> None:
        """Merge an identical answer found for another scope into this one."""
        self.answer_ids.append(response.answer_id)
        self.scopes = tuple(dict.fromkeys((*self.scopes, *scopes)))
        # A group-local answer is the one worth flagging: a reader correcting
        # "what my group said" should land on the group's own record, not on
        # the global one that happened to be worded the same.
        if is_local and not self.is_local:
            self.is_local = True
            self.response = response


@dataclass(frozen=True, slots=True)
class DirectAnswerService:
    """Produce the distinct answers to one private message."""

    answer: AnswerService

    async def bundles_for(
        self,
        message: NormalizedMessage,
        *,
        served: Sequence[ChannelBinding],
    ) -> tuple[AnswerBundle, ...]:
        """Answer one private message in every space the asker belongs to.

        Args:
            message: The private message to answer.
            served: The groups the asker shares with the bot, already ordered
                for display. Empty for an allowlisted asker with no group: the
                question is then answered from global knowledge only.

        Returns:
            One bundle per distinct answer, global knowledge first and then by
            group name. Empty when the message holds no question.
        """
        labels = _scope_labels(served)
        order = {scope: index for index, scope in enumerate(labels)}
        targets: Sequence[str | None] = [binding.space_id for binding in served] or [
            None
        ]
        collected: list[_Collected] = []
        by_answer: dict[tuple[object, ...], _Collected] = {}
        for space_id in targets:
            response = await self.answer.answer_message(
                message,
                space_id=space_id,
                answer_id=scoped_answer_id(message.id, space_id),
            )
            if response is None:
                continue
            scopes = _cited_scopes(response, order)
            key = _same_answer_key(response)
            existing = by_answer.get(key)
            if existing is None:
                item = _Collected(
                    response=response,
                    scopes=scopes,
                    is_local=space_id is not None,
                    answer_ids=[response.answer_id],
                )
                by_answer[key] = item
                collected.append(item)
            else:
                existing.absorb(response, scopes, space_id is not None)
        collected.sort(key=lambda item: _display_position(item, order))
        return tuple(
            _render(item, labels, labelled=len(collected) > 1) for item in collected
        )


def _scope_labels(served: Sequence[ChannelBinding]) -> dict[str, str]:
    """Return the display label of every scope a private answer may draw on.

    Keyed by scope key, because that is the identity the evidence carries, not
    the space id. Global comes first and the groups keep the order the caller
    resolved them in, so a reader sees the same sequence on every question.
    """
    labels = {GLOBAL_SCOPE: GLOBAL_LABEL}
    for binding in served:
        labels[scope_for_space(binding.space_id)] = GROUP_LABEL.format(
            name=binding.title or binding.space_id
        )
    return labels


def _cited_scopes(
    response: AskQuestionResponse, order: dict[str, int]
) -> tuple[str, ...]:
    """Return the scopes of the evidence behind an answer, in display order.

    An abstention cites nothing, so it has no scope at all. That is the honest
    result: "I could not answer this" is a statement about the question, not
    about a group.
    """
    scopes = {source.scope_key or GLOBAL_SCOPE for source in response.sources}
    return tuple(
        sorted(scopes, key=lambda scope: (order.get(scope, len(order)), scope))
    )


def _display_position(
    item: _Collected, order: dict[str, int]
) -> tuple[tuple[int, ...], str]:
    """Return where one collected answer belongs in the rendered sequence.

    The key is the whole list of scope positions, not just the first one: two
    blocks that both lean on global knowledge are then separated by the group
    that actually differs between them, which is the order the caller resolved
    and the reader expects.

    An answer with no scope sorts last: a group heading on an abstention would
    claim a group had an opinion about a question it knew nothing about.
    """
    if not item.scopes:
        return ((len(order),), item.response.answer_id)
    return (
        tuple(order.get(scope, len(order)) for scope in item.scopes),
        item.response.answer_id,
    )


def _same_answer_key(response: AskQuestionResponse) -> tuple[object, ...]:
    """Return what makes two answers the same answer.

    Whitespace and the set of cited sources, nothing else. A difference in
    either is a difference a reader can act on, and collapsing it would hide a
    provenance they cannot otherwise see.
    """
    return (
        " ".join(response.answer.split()),
        str(response.mode),
        tuple(sorted(source.source_id for source in response.sources)),
    )


def _render(
    item: _Collected, labels: dict[str, str], *, labelled: bool
) -> AnswerBundle:
    """Render one collected answer into a deliverable bundle."""
    text = item.response.rendered_text
    if labelled and item.scopes:
        heading = SCOPE_SEPARATOR.join(
            labels.get(scope, scope) for scope in item.scopes
        )
        text = f"{heading}\n{text}"
    return AnswerBundle(
        text=text,
        representative_answer_id=item.response.answer_id,
        answer_ids=tuple(item.answer_ids),
    )
