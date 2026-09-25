# SPDX-License-Identifier: MIT
"""Load the exported linear classifier head.

Local and dev runtimes read ``data/classifier/model.json`` directly. A Python
Worker isolate cannot read repository-relative data files, so the Cloudflare
runtime uses the same export embedded as a generated module
(``classifier_head_data``, written by ``scripts/train_classifier.py``).
"""

import json
from pathlib import Path

from knowledge_bot.application.classifier import ClassifierHead
from knowledge_bot.domain.enums import IntentLabel


class ClassifierModelMissingError(RuntimeError):
    """Raised when the exported classifier head cannot be loaded."""


def _artifact(path: str | Path) -> object:
    """Return the JSON artifact, or the embedded copy when unreadable."""
    location = Path(path)
    try:
        if location.exists():
            return json.loads(location.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    from knowledge_bot.infrastructure import classifier_head_data

    return classifier_head_data.HEAD_DATA


def _rows(value: object) -> tuple[tuple[float, ...], ...]:
    """Validate one coefficient matrix."""
    if not isinstance(value, list):
        raise TypeError
    return tuple(_row(item) for item in value)


def _row(value: object) -> tuple[float, ...]:
    """Validate one coefficient row."""
    if not isinstance(value, list):
        raise TypeError
    return tuple(float(item) for item in value if isinstance(item, int | float | str))


def _labels(value: object) -> tuple[IntentLabel, ...]:
    """Validate the label order."""
    if not isinstance(value, list):
        raise TypeError
    return tuple(IntentLabel(str(label)) for label in value)


def load_classifier_head(path: str | Path) -> ClassifierHead:
    """Build the runtime head from the artifact, or from its embedded copy.

    Args:
        path: Path to the exported model artifact.

    Returns:
        The linear head ready for classification.

    Raises:
        ClassifierModelMissingError: When neither artifact is usable.
    """
    payload = _artifact(path)
    if not isinstance(payload, dict):
        raise ClassifierModelMissingError(  # noqa: TRY003
            f"malformed classifier head artifact: {path}"
        )
    try:
        labels = _labels(payload.get("labels"))
        coef = _rows(payload.get("coef"))
        intercept = _row(payload.get("intercept"))
    except (TypeError, ValueError) as error:
        raise ClassifierModelMissingError(  # noqa: TRY003
            f"malformed classifier head artifact: {path}"
        ) from error
    if len(labels) != len(coef) or len(labels) != len(intercept):
        raise ClassifierModelMissingError(  # noqa: TRY003
            f"classifier head label order is inconsistent: {path}"
        )
    return ClassifierHead(labels=labels, coef=coef, intercept=intercept)
