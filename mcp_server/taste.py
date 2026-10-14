"""Pure-Python preference ('taste') model.

Learns which candidate Ricardo would *keep* from his own pairwise best/worst
judgments, then reranks future candidates. This is the layer his ear has been
asking for: the style model says what his music *looks like*; this says which
plausible option *sounds like something he'd keep*.

A logistic / Bradley-Terry model over interpretable features:
    P(a preferred over b) = sigmoid(w · (f_a − f_b))
trained by plain-Python batch gradient descent. No numpy/sklearn — deterministic,
inspectable, instant at this scale. `core.py` supplies the feature vectors (so the
math here stays dependency-free); this module never imports core.
"""
from __future__ import annotations

import math


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


def train(pairs: list[tuple[list[float], list[float]]], dim: int,
          epochs: int = 500, lr: float = 0.5, l2: float = 1e-3) -> list[float]:
    """Fit preference weights from (winner_features, loser_features) pairs.

    Deterministic (zero init, full-batch GD). Returns a weight vector of length
    ``dim``; all-zero when there are no pairs (→ a no-op reranker).
    """
    w = [0.0] * dim
    if not pairs:
        return w
    n = len(pairs)
    for _ in range(epochs):
        grad = [0.0] * dim
        for fw, fl in pairs:
            diff = [fw[i] - fl[i] for i in range(dim)]
            p = sigmoid(sum(w[i] * diff[i] for i in range(dim)))
            err = 1.0 - p  # d/dscore of log P(winner > loser)
            for i in range(dim):
                grad[i] += err * diff[i]
        for i in range(dim):
            w[i] += lr * (grad[i] / n - l2 * w[i])
    return [round(x, 6) for x in w]


def score(features: list[float], weights: list[float]) -> float:
    """Taste score for one candidate (higher = more his-keeper). 0.0 if no model."""
    if not weights:
        return 0.0
    return sum(features[i] * weights[i] for i in range(min(len(features), len(weights))))


def accuracy(pairs: list[tuple[list[float], list[float]]], weights: list[float]) -> float:
    """Fraction of training pairs the model ranks correctly (winner scored higher)."""
    if not pairs:
        return 0.0
    correct = sum(1 for fw, fl in pairs if score(fw, weights) > score(fl, weights))
    return correct / len(pairs)
