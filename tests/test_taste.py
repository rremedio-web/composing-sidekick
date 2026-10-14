"""Unit tests for mcp_server/taste.py."""
from __future__ import annotations

import unittest

import sys
import os

# Insert repo root to allow imports from mcp_server/ and analyze/
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcp_server import taste


class TestSigmoid(unittest.TestCase):
    """Test sigmoid() edge cases and behavior."""

    def test_extreme_negative(self):
        """sigmoid(-30) → ~0.0 (numerical stability)."""
        self.assertAlmostEqual(taste.sigmoid(-30.0), 0.0, places=12)

    def test_extreme_positive(self):
        """sigmoid(30) → ~1.0 (numerical stability)."""
        self.assertAlmostEqual(taste.sigmoid(30.0), 1.0, places=12)

    def test_zero(self):
        """sigmoid(0) → 0.5."""
        self.assertEqual(taste.sigmoid(0.0), 0.5)

    def test_positive(self):
        """sigmoid(1.0) ≈ 0.731."""
        self.assertAlmostEqual(taste.sigmoid(1.0), 0.7310585786300049, places=10)

    def test_negative(self):
        """sigmoid(-1.0) ≈ 0.269."""
        self.assertAlmostEqual(taste.sigmoid(-1.0), 0.2689414213699951, places=10)


class TestTrain(unittest.TestCase):
    """Test train() determinism and convergence on a separable toy set."""

    def test_empty_pairs(self):
        """train([]) → all-zero weights (no-op)."""
        w = taste.train([], dim=5)
        self.assertEqual(w, [0.0, 0.0, 0.0, 0.0, 0.0])

    def test_single_pair(self):
        """train([(f1, f2)]) → converges to a separating hyperplane."""
        # Simple separable case: winner has higher feature 0
        pairs = [([1.0, 0.0], [0.0, 1.0])]
        w = taste.train(pairs, dim=2, epochs=1000, lr=0.5)
        # Winner (f1) should score higher than loser (f2)
        self.assertGreater(taste.score([1.0, 0.0], w), taste.score([0.0, 1.0], w))

    def test_multiple_pairs_convergence(self):
        """train([(fw, fl)...]) → improves accuracy over epochs."""
        # Linearly separable 2D case
        pairs = [
            ([1.0, 0.0], [0.0, 1.0]),  # winner has higher x
            ([1.0, 1.0], [0.0, 0.0]),  # winner has higher x
            ([0.5, 0.0], [0.0, 0.5]),  # winner has higher x
        ]
        w1 = taste.train(pairs, dim=2, epochs=100, lr=0.5)
        w2 = taste.train(pairs, dim=2, epochs=500, lr=0.5)
        # More epochs → higher accuracy
        acc1 = taste.accuracy(pairs, w1)
        acc2 = taste.accuracy(pairs, w2)
        self.assertGreaterEqual(acc2, acc1)

    def test_determinism(self):
        """train() with identical inputs → identical weights."""
        pairs = [([1.0, 0.0], [0.0, 1.0]), ([0.5, 0.0], [0.0, 0.5])]
        w1 = taste.train(pairs, dim=2, epochs=100, lr=0.5)
        w2 = taste.train(pairs, dim=2, epochs=100, lr=0.5)
        self.assertEqual(w1, w2)


class TestScore(unittest.TestCase):
    """Test score() with edge cases."""

    def test_empty_weights(self):
        """score(features, []) → 0.0."""
        self.assertEqual(taste.score([1.0, 2.0, 3.0], []), 0.0)

    def test_mismatched_lengths(self):
        """score(features, weights) → uses min(len(features), len(weights))."""
        self.assertEqual(taste.score([1.0, 2.0, 3.0], [0.1, 0.2]), 0.5)

    def test_normal_case(self):
        """score(features, weights) → dot product."""
        self.assertEqual(taste.score([1.0, 2.0], [0.1, 0.2]), 0.5)


class TestAccuracy(unittest.TestCase):
    """Test accuracy() edge cases."""

    def test_empty_pairs(self):
        """accuracy([]) → 0.0."""
        self.assertEqual(taste.accuracy([], [0.1, 0.2]), 0.0)

    def test_single_pair(self):
        """accuracy([(fw, fl)]) → 1.0 if winner scores higher, else 0.0."""
        pairs = [([1.0, 0.0], [0.0, 1.0])]
        w = taste.train(pairs, dim=2, epochs=1000)
        self.assertEqual(taste.accuracy(pairs, w), 1.0)

    def test_perfect_ranking(self):
        """accuracy() → fraction of correctly ranked pairs."""
        pairs = [
            ([1.0, 0.0], [0.0, 1.0]),  # winner has higher x
            ([1.0, 1.0], [0.0, 0.0]),  # winner has higher x
            ([0.5, 0.0], [0.0, 0.5]),  # winner has higher x
        ]
        w = taste.train(pairs, dim=2, epochs=500)
        acc = taste.accuracy(pairs, w)
        self.assertEqual(acc, 1.0)

    def test_random_ranking(self):
        """accuracy() → 0.0 for random weights."""
        pairs = [
            ([1.0, 0.0], [0.0, 1.0]),
            ([1.0, 1.0], [0.0, 0.0]),
            ([0.5, 0.0], [0.0, 0.5]),
        ]
        w = [0.0, 0.0]  # all-zero weights (no-op)
        acc = taste.accuracy(pairs, w)
        self.assertEqual(acc, 0.0)


if __name__ == "__main__":
    unittest.main()
