# SPDX-License-Identifier: MIT
"""Load the exported linear classifier head from its JSON artifact."""

import json
from pathlib import Path

from knowledge_bot.application.classifier import ClassifierHead
from knowledge_bot.domain.enums import IntentLabel


class ClassifierModelMissingError(RuntimeError):
    """Raised when the exported classifier head cannot be loaded."""


def load_classifier_head(path: str | Path) -> ClassifierHead:
    """Read ``data/classifier/model.json`` and build the runtime head.

    Args:
        path: Path to the exported model artifact.

    Returns:
        The linear head ready for classification.

    Raises:
        ClassifierModelMissingError: When the artifact is absent or malformed.
    """
    location = Path(path)
    if not location.exists():
        raise ClassifierModelMissingError(  # noqa: TRY003
            "classifier head not found at"
            f" {location}; run scripts/generate_classifier_dataset.py"
            " and scripts/train_classifier.py"
        )
    try:
        payload = json.loads(location.read_text(encoding="utf-8"))
        labels = tuple(IntentLabel(label) for label in payload["labels"])
        coef = tuple(tuple(float(value) for value in row) for row in payload["coef"])
        intercept = tuple(float(value) for value in payload["intercept"])
    except (ValueError, KeyError, TypeError) as error:
        raise ClassifierModelMissingError(  # noqa: TRY003
            f"malformed classifier head artifact: {location}"
        ) from error
    if len(labels) != len(coef) or len(labels) != len(intercept):
        raise ClassifierModelMissingError(  # noqa: TRY003
            f"classifier head label order is inconsistent: {location}"
        )
    return ClassifierHead(labels=labels, coef=coef, intercept=intercept)
