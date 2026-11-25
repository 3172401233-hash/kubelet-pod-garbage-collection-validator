"""Offline kubelet Pod GC validator.

This package only simulates kubelet garbage-collection decisions on data
handed to it by the caller. It never connects to a kubelet, the API server
or the container runtime.
"""

from .model import (
    KUBELET_ROOT,
    ContainerState,
    ContainerStatus,
    OrphanDirectory,
    Pod,
    Volume,
    pod_from_dict,
)
from .gc import (
    CleanableItem,
    RetainedEvidence,
    VolumeLeak,
    evaluate_garbage_collection,
)

__all__ = [
    "KUBELET_ROOT",
    "ContainerState",
    "ContainerStatus",
    "OrphanDirectory",
    "Pod",
    "Volume",
    "pod_from_dict",
    "CleanableItem",
    "RetainedEvidence",
    "VolumeLeak",
    "evaluate_garbage_collection",
]
