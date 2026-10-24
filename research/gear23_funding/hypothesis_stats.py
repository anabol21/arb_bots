"""Shared correlation helpers for gear 2.3 funding hypotheses.

Stdlib only. Thresholds live in METHOD.md and are not applied here.
"""

from __future__ import annotations

import math


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def mad(values: list[float]) -> float | None:
    center = median(values)
    if center is None:
        return None
    return median([abs(value - center) for value in values])


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n != len(ys) or n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    numer = sum(a * b for a, b in zip(dx, dy))
    var_x = sum(a * a for a in dx)
    var_y = sum(b * b for b in dy)
    if var_x <= 0.0 or var_y <= 0.0:
        return None
    return numer / math.sqrt(var_x * var_y)


def ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    out = [0.0] * len(values)
    i = 0
    n = len(values)
    while i < n:
        j = i
        anchor = values[order[i]]
        while j + 1 < n and values[order[j + 1]] == anchor:
            j += 1
        average = (i + j) / 2.0
        for k in range(i, j + 1):
            out[order[k]] = average
        i = j + 1
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    return pearson(ranks(xs), ranks(ys))


def share_positive(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(1 for value in values if value > 0.0) / len(values)


def within_correlation(
    pairs_by_coin: dict[str, list[tuple[float, float]]],
    min_n: int = 30,
    min_pooled: int = 200,
) -> dict:
    """Per-coin correlations and a pooled correlation after within-coin demeaning.

    A coin enters the per-coin summary only when it has at least ``min_n`` pairs.
    Undefined correlations (zero variance) are left out of the median and the
    positive-sign share. The pooled series subtracts each coin's own mean on
    each variable and is estimated only when the pooled pair count is at least
    ``min_pooled``.
    """
    per_pearson: list[float] = []
    per_spearman: list[float] = []
    pooled_x: list[float] = []
    pooled_y: list[float] = []
    n_coins_eligible = 0
    n_coins_with_pairs = 0
    for pairs in pairs_by_coin.values():
        n = len(pairs)
        if n == 0:
            continue
        n_coins_with_pairs += 1
        xs = [pair[0] for pair in pairs]
        ys = [pair[1] for pair in pairs]
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        pooled_x.extend(x - mean_x for x in xs)
        pooled_y.extend(y - mean_y for y in ys)
        if n < min_n:
            continue
        n_coins_eligible += 1
        r_p = pearson(xs, ys)
        r_s = spearman(xs, ys)
        if r_p is not None:
            per_pearson.append(r_p)
        if r_s is not None:
            per_spearman.append(r_s)
    n_points = len(pooled_x)
    estimable = n_points >= min_pooled
    return {
        "n_points": n_points,
        "n_coins_with_pairs": n_coins_with_pairs,
        "per_coin": {
            "min_n": min_n,
            "n_coins": n_coins_eligible,
            "n_pearson": len(per_pearson),
            "n_spearman": len(per_spearman),
            "median_pearson": median(per_pearson),
            "median_spearman": median(per_spearman),
            "share_pearson_positive": share_positive(per_pearson),
            "share_spearman_positive": share_positive(per_spearman),
        },
        "pooled": {
            "n": n_points,
            "min_n": min_pooled,
            "estimable": estimable,
            "pearson": pearson(pooled_x, pooled_y) if estimable else None,
            "spearman": spearman(pooled_x, pooled_y) if estimable else None,
        },
    }


def self_check() -> dict:
    identical = [1.0, 2.0, 3.0, 4.0]
    reversed_series = [4.0, 3.0, 2.0, 1.0]
    pearson_identical = pearson(identical, identical)
    spearman_reversed = spearman(identical, reversed_series)
    median_even = median([1.0, 2.0, 3.0, 4.0])
    mad_odd = mad([1.0, 2.0, 3.0, 4.0, 5.0])
    ok = (
        pearson_identical is not None
        and abs(pearson_identical - 1.0) < 1e-12
        and spearman_reversed is not None
        and abs(spearman_reversed - (-1.0)) < 1e-12
        and median_even == 2.5
        and mad_odd == 1.0
    )
    return {
        "ok": ok,
        "pearson_identical": pearson_identical,
        "spearman_reversed": spearman_reversed,
        "median_1_2_3_4": median_even,
        "mad_1_2_3_4_5": mad_odd,
    }
