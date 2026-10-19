"""Pure geometry helpers for aligning and distributing selected item bounds."""

from __future__ import annotations

import math
from collections.abc import Sequence


_MODES = {"left", "hcenter", "right", "top", "vcenter", "bottom",
          "distribute_h", "distribute_v"}


def alignment_offsets(
    bounds: Sequence[tuple[float, float, float, float]], mode: str,
) -> list[tuple[float, float]]:
    """Return per-item offsets for alignment or equal-gap distribution.

    Each bound is ``(left, top, right, bottom)``. Results follow input order.
    """
    if mode not in _MODES:
        raise ValueError(f"unsupported alignment mode: {mode}")
    normalized: list[tuple[float, float, float, float]] = []
    for bound in bounds:
        if len(bound) != 4:
            raise ValueError("each bound must contain left, top, right, bottom")
        left, top, right, bottom = map(float, bound)
        if not all(map(math.isfinite, (left, top, right, bottom))):
            raise ValueError("bounds must be finite")
        if left > right or top > bottom:
            raise ValueError("bounds must be ordered")
        normalized.append((left, top, right, bottom))

    offsets = [(0.0, 0.0) for _ in normalized]
    if not normalized:
        return offsets
    if mode in {"distribute_h", "distribute_v"}:
        if len(normalized) < 3:
            raise ValueError("distribution requires at least three items")
        horizontal = mode == "distribute_h"
        start = 0 if horizontal else 1
        end = 2 if horizontal else 3
        indices = sorted(range(len(normalized)), key=lambda i: normalized[i][start])
        first, last = normalized[indices[0]], normalized[indices[-1]]
        gap = (last[start] - first[end] - sum(
            normalized[i][end] - normalized[i][start]
            for i in indices[1:-1]
        )) / (len(indices) - 1)
        cursor = first[end] + gap
        for index in indices[1:-1]:
            item = normalized[index]
            offsets[index] = ((cursor - item[start], 0.0) if horizontal
                              else (0.0, cursor - item[start]))
            cursor += item[end] - item[start] + gap
        return offsets

    if mode in {"left", "hcenter", "right"}:
        target = {
            "left": min(bound[0] for bound in normalized),
            "hcenter": (min(bound[0] for bound in normalized)
                        + max(bound[2] for bound in normalized)) / 2,
            "right": max(bound[2] for bound in normalized),
        }[mode]
        for i, (left, _top, right, _bottom) in enumerate(normalized):
            x = target - (left if mode == "left" else right if mode == "right"
                          else (left + right) / 2)
            offsets[i] = (x, 0.0)
    else:
        target = {
            "top": min(bound[1] for bound in normalized),
            "vcenter": (min(bound[1] for bound in normalized)
                        + max(bound[3] for bound in normalized)) / 2,
            "bottom": max(bound[3] for bound in normalized),
        }[mode]
        for i, (_left, top, _right, bottom) in enumerate(normalized):
            y = target - (top if mode == "top" else bottom if mode == "bottom"
                          else (top + bottom) / 2)
            offsets[i] = (0.0, y)
    return offsets
