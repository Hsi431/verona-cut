"""Repeatable before/after table for the generated packing fixture cases."""
import argparse
import importlib.util
import statistics
import time

from PySide6.QtWidgets import QApplication

from verona.artwork import item_bounds
from verona.editor_actions import cutting_frame
from verona.packing import _material_area, pack_items
from tests.fixtures.nesting_cases import scenarios


def _measure(function, items, metadata, parameters, repeats):
    samples = []
    frame = cutting_frame(metadata)
    for _ in range(repeats):
        start = time.perf_counter()
        result, selection, details = function(items, metadata, list(range(len(items))), parameters)
        elapsed = time.perf_counter()-start
        boxes = [item_bounds(result[index]) for index in selection]
        height = max((box.bottom() for box in boxes), default=0)-min((box.top() for box in boxes), default=0)
        area = frame.width()*frame.height()
        usage = 100*_material_area([result[index] for index in selection])/area
        samples.append((details["copies"], height, usage, elapsed))
    return (statistics.median(sample[0] for sample in samples),
            statistics.median(sample[1] for sample in samples),
            statistics.median(sample[2] for sample in samples),
            statistics.median(sample[3] for sample in samples),
            max(sample[3] for sample in samples))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-source")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    QApplication.instance() or QApplication([])
    baseline = None
    if args.baseline_source:
        spec = importlib.util.spec_from_file_location("verona.packing_baseline", args.baseline_source)
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
    print("| Scenario | Copies before → after | Height mm before → after | Usage % before → after | Time median / worst s before → after |")
    print("|---|---:|---:|---:|---:|")
    for name, items, metadata, parameters in scenarios():
        after = _measure(pack_items, items, metadata, parameters, args.repeats)
        if baseline:
            before = _measure(baseline.pack_items, items, metadata, parameters, args.repeats)
            copies, height, usage, timing = (
                f"{before[0]:g} → {after[0]:g}", f"{before[1]:.1f} → {after[1]:.1f}",
                f"{before[2]:.1f} → {after[2]:.1f}",
                f"{before[3]:.3f}/{before[4]:.3f} → {after[3]:.3f}/{after[4]:.3f}")
        else:
            copies, height, usage, timing = (
                f"{after[0]:g}", f"{after[1]:.1f}", f"{after[2]:.1f}",
                f"{after[3]:.3f}/{after[4]:.3f}")
        print(f"| {name} | {copies} | {height} | {usage} | {timing} |")


if __name__ == "__main__":
    main()
