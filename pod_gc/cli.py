"""Command line entry point for offline GC report generation.

Usage::

    python -m pod_gc.cli snapshot.json

The snapshot file contains the same data kubelet would observe (pod UIDs,
container states, volume references, images, static/mirror markers, orphan
directories and deletion timestamps), but the process never opens a socket
or touches the directories named in the input.
"""

import argparse
import json
import sys

from .gc import evaluate_garbage_collection
from .model import OrphanDirectory, pod_from_dict


def _load_snapshot(path):
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    pods = [pod_from_dict(raw) for raw in payload.get("pods", [])]
    orphans = [
        OrphanDirectory(
            path=raw["path"],
            owning_pod_uid=raw.get("owning_pod_uid"),
        )
        for raw in payload.get("orphan_directories", [])
    ]
    return pods, orphans


def _print_section(title, items, formatter):
    print(title)
    if not items:
        print("  (none)")
        return
    for item in items:
        print("  - " + formatter(item))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline kubelet Pod GC validator (no kubelet connection)."
    )
    parser.add_argument("snapshot", help="path to a JSON pod snapshot file")
    args = parser.parse_args(argv)

    pods, orphans = _load_snapshot(args.snapshot)
    report = evaluate_garbage_collection(pods, orphans)

    _print_section(
        "Cleanable resources:",
        report.cleanable,
        lambda item: "[%s] %s (pod=%s)%s"
        % (
            item.kind,
            item.identifier,
            item.pod_uid or "-",
            (" deleted_at=%s" % item.deletion_timestamp)
            if item.deletion_timestamp
            else "",
        ),
    )
    _print_section(
        "Retained evidence:",
        report.retained,
        lambda item: "[%s] %s reason=%s | %s"
        % (item.kind, item.identifier, item.reason, item.evidence),
    )
    _print_section(
        "Volume leaks:",
        report.volume_leaks,
        lambda item: "%s (pod=%s) | %s"
        % (item.path, item.pod_uid or "-", item.evidence),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
