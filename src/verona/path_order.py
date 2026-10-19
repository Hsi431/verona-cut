"""Explicit job-level ordering; original contour points and artwork stay intact."""
import math
import time

import numpy as np

from .artwork import contour_path
from .i18n import tr
from .layers import cuttable


def _entries(items):
    return [(item_index, contour_index, contour)
            for item_index, item in enumerate(items) if cuttable(item)
            for contour_index, contour in enumerate(item["contours"]) if len(contour) >= 2]


def _anchors(contours, metadata):
    registration = metadata.get("print_cut", {})
    if registration.get("enabled") and registration.get("bounds"):
        left, _, _, bottom = registration["bounds"]
        dx, dy = registration.get("offset", (0, 0))
        return (left-dx, bottom-dy), (left-dx, min(y for contour in contours for _, y in contour))
    return (0., 0.), (0., 0.)


def _distance(contours, order, start, finish):
    current = start
    total = 0.
    for index in order:
        contour = contours[index]
        total += math.dist(current, contour[0])
        current = contour[-1]
    return total + math.dist(current, finish)


def analyze_path_order(items, metadata, enabled=True):
    if type(enabled) is not bool:
        raise ValueError("Path ordering enabled must be a boolean.")
    entries = _entries(items)
    contours = [entry[2] for entry in entries]
    count = len(contours)
    original = list(range(count))
    if not count:
        return [], {"enabled": enabled, "contours": 0, "travel_before_mm": 0.,
                    "travel_after_mm": 0., "travel_saved_mm": 0., "order": [],
                    "changed": False, "inside_first_enforced": enabled, "containment_relations": 0,
                    "travel_increased_for_inside_first": False, "comparison_reference": "document contour order"}
    start, finish = _anchors(contours, metadata)
    before = _distance(contours, original, start, finish)
    chosen, relations, baseline_valid = original, 0, True
    if enabled:
        if count > 4000:
            raise ValueError(tr("自動排序最多處理 4000 條輪廓；可關閉排序使用原順序。"))
        paths = [contour_path([contour]) for contour in contours]
        bounds = np.array([[path.boundingRect().left(), path.boundingRect().top(),
                            path.boundingRect().right(), path.boundingRect().bottom()] for path in paths])
        areas = (bounds[:, 2]-bounds[:, 0])*(bounds[:, 3]-bounds[:, 1])
        children = [0]*count
        parents = [[] for _ in contours]
        deadline = time.monotonic()+10
        for outer, contour in enumerate(contours):
            if len(contour) < 4 or contour[0] != contour[-1]:
                continue
            box = bounds[outer]
            candidates = np.flatnonzero((bounds[:, 0] >= box[0]) & (bounds[:, 1] >= box[1])
                & (bounds[:, 2] <= box[2]) & (bounds[:, 3] <= box[3]) & (areas < areas[outer]-1e-10))
            for inner_value in candidates:
                if time.monotonic() > deadline or relations >= 100000:
                    raise ValueError(tr("輪廓包含關係超過本次排序上限；可關閉排序使用原順序。"))
                inner = int(inner_value)
                if paths[outer].contains(paths[inner]):
                    children[outer] += 1
                    parents[inner].append(outer)
                    relations += 1
                    if inner > outer:
                        baseline_valid = False
        starts = np.array([contour[0] for contour in contours])

        def candidate_order(nearest):
            remaining = children[:]
            available = np.array([value == 0 for value in remaining])
            order, current = [], np.array(start)
            for _ in contours:
                eligible = np.flatnonzero(available)
                if nearest:
                    delta = starts[eligible]-current
                    index = int(eligible[np.argmin(np.hypot(delta[:, 0], delta[:, 1]))])
                else:
                    index = int(eligible[0])
                available[index] = False
                order.append(index)
                current = np.array(contours[index][-1])
                for parent in parents[index]:
                    remaining[parent] -= 1
                    if not remaining[parent]:
                        available[parent] = True
            return order

        candidates = ([original] if baseline_valid else []) + [candidate_order(False), candidate_order(True)]
        chosen = min(candidates, key=lambda order: _distance(contours, order, start, finish))
    after = _distance(contours, chosen, start, finish)
    details = {"enabled": enabled, "contours": count, "changed": chosen != original,
        "comparison_reference": "document contour order",
        "inside_first_enforced": enabled, "containment_relations": relations,
        "travel_before_mm": before, "travel_after_mm": after, "travel_saved_mm": before-after,
        "travel_increased_for_inside_first": enabled and after > before+1e-9,
        "metric": "estimated pen-up distance including logical start/return; excludes registration search and real motion timing",
        "order": [{"item_index": entries[index][0], "contour_index": entries[index][1]} for index in chosen]}
    return [contours[index] for index in chosen], details


def cut_contours(items, metadata):
    enabled = metadata.get("optimize_paths", False)
    if type(enabled) is not bool:
        raise ValueError("Invalid path-order setting.")
    if enabled:
        return analyze_path_order(items, metadata, True)[0]
    return [entry[2] for entry in _entries(items)]
