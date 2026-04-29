"""
Simple least-squares linear regression — used by reports and history-page
depletion estimates.  Kept dependency-free (no numpy) so it works in the
standard Python container without extra packages.
"""
from __future__ import annotations


def linear_regression(xs: list[float], ys: list[float]) -> float:
    """
    Return the least-squares slope (units of y per unit of x).

    Returns 0.0 if there are fewer than 2 points or if all x values are
    identical (the denominator would be zero).
    """
    n = len(xs)
    if n < 2:
        return 0.0
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_xx = sum(x * x for x in xs)
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return 0.0
    return (n * sum_xy - sum_x * sum_y) / denom
