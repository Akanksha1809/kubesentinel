"""Pure backoff math, kept separate from any networking so it's trivially unit-testable."""

from __future__ import annotations


def compute_backoff_seconds(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Exponential backoff with a hard cap. attempt is 0-indexed (0 => base seconds)."""
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    return min(base * (2**attempt), cap)
