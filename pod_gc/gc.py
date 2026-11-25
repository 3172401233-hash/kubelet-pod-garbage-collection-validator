"""Pure-Python simulation of kubelet pod/volume/image garbage collection.

The function :func:`evaluate_garbage_collection` consumes in-memory snapshots
and returns three reports:

* ``cleanable``  - resources a real kubelet GC pass may delete now
* ``retained``   - resources that must be kept, including the safety reason
* ``volume_leaks`` - volume directories left on disk with no owner and no
  pod reference, which GC can neither safely delete nor attribute

Safety rules enforced here:

1. A directory whose volume still appears in another pod's references is
   never cleanable.
2. A container in an unknown state is never treated as terminated, so the
   owning pod is not garbage collected.
3. A static pod whose mirror pod disappeared is retained; missing mirror
   state alone does not terminate the static pod.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .model import (
    ContainerState,
    OrphanDirectory,
    Pod,
)

# Retention / cleanup reason constants, kept stable for assertions.
REASON_SHARED_VOLUME = "volume_still_referenced"
REASON_UNKNOWN_CONTAINER = "unknown_container_state"
REASON_MIRROR_MISSING = "static_pod_mirror_missing"
REASON_POD_RUNNING = "pod_not_terminated"
REASON_NO_DELETION_TIMESTAMP = "no_deletion_timestamp"
REASON_IMAGE_IN_USE = "image_still_in_use"
REASON_VOLUME_LEAK = "orphan_volume_without_reference"
REASON_UNATTRIBUTED_ORPHAN = "unattributed_orphan_directory"

CLEANABLE_POD_DIRECTORY = "pod_directory"
CLEANABLE_VOLUME_DIRECTORY = "volume_directory"
CLEANABLE_IMAGE = "image"


@dataclass
class CleanableItem:
    """A resource that is safe to collect in this GC pass."""

    kind: str
    identifier: str
    pod_uid: Optional[str]
    deletion_timestamp: Optional[str] = None
    detail: str = ""


@dataclass
class RetainedEvidence:
    """A resource that must survive this GC pass, with proof of why."""

    kind: str
    identifier: str
    pod_uid: Optional[str]
    reason: str
    evidence: str


@dataclass
class VolumeLeak:
    """An on-disk volume directory with neither owner pod nor references."""

    path: str
    pod_uid: Optional[str]
    reason: str
    evidence: str


@dataclass
class GCReport:
    cleanable: List[CleanableItem] = field(default_factory=list)
    retained: List[RetainedEvidence] = field(default_factory=list)
    volume_leaks: List[VolumeLeak] = field(default_factory=list)


def _iso(value):
    return None if value is None else value.isoformat()


def _volume_referencers(volume, current_pod: Pod, pods_by_uid: Dict[str, Pod]):
    """Return UIDs of live pods that still reference a volume.

    The pod owning the directory counts only while it is still present; the
    important case for image-volume sharing is a *different* pod holding a
    reference.
    """

    referencers = []
    for uid in volume.referenced_by:
        if uid == current_pod.uid:
            continue
        other = pods_by_uid.get(uid)
        if other is not None:
            referencers.append(uid)
    return referencers


def _live_referencers(volume, pods_by_uid: Dict[str, Pod]):
    """All UIDs in ``referenced_by`` that resolve to pods in the snapshot."""

    return [uid for uid in volume.referenced_by if uid in pods_by_uid]


def _classify_pod(pod: Pod) -> Optional[str]:
    """Return a retention reason, or ``None`` when the pod is a GC candidate.

    Order matters: the strongest safety evidence is reported first.
    """

    # Rule 3: a static pod must not be terminated merely because its mirror
    # pod is gone. The static config source still owns the pod.
    if pod.is_static and not pod.mirror_pod_exists:
        return REASON_MIRROR_MISSING

    # Rule 2: unknown state is not evidence of termination.
    if pod.unknown_containers:
        return REASON_UNKNOWN_CONTAINER

    if not pod.all_containers_terminated:
        return REASON_POD_RUNNING

    if pod.deletion_timestamp is None:
        return REASON_NO_DELETION_TIMESTAMP

    return None


def _looks_like_volume_path(path: str) -> bool:
    return "/volumes/" in path or path.endswith("/volumes")


def evaluate_garbage_collection(
    pods: Sequence[Pod],
    orphan_directories: Optional[Sequence[OrphanDirectory]] = None,
) -> GCReport:
    """Evaluate one kubelet GC pass against supplied snapshots.

    Nothing on disk is touched and no network call is made; the returned
    report describes the decision a kubelet would make.
    """

    orphan_directories = list(orphan_directories or [])
    report = GCReport()
    pods_by_uid = {pod.uid: pod for pod in pods}

    # Index every volume path so orphan classification can decide whether a
    # surviving pod still references the directory.
    all_volumes = {}
    for pod in pods:
        for volume in pod.volumes:
            all_volumes[volume.path] = (pod, volume)

    # ---- Pod-level classification -----------------------------------------
    terminated_pod_uids = set()
    for pod in pods:
        reason = _classify_pod(pod)
        if reason is not None:
            evidence = _retention_evidence(pod, reason)
            report.retained.append(
                RetainedEvidence(
                    kind=CLEANABLE_POD_DIRECTORY,
                    identifier=pod.directory,
                    pod_uid=pod.uid,
                    reason=reason,
                    evidence=evidence,
                )
            )
            continue

        terminated_pod_uids.add(pod.uid)

        # Rule 1: even for an otherwise terminating pod, each volume that is
        # still referenced by another pod blocks directory removal.
        shared_volume = _first_shared_volume(pod, pods_by_uid)
        if shared_volume is not None:
            volume, referencers = shared_volume
            report.retained.append(
                RetainedEvidence(
                    kind=CLEANABLE_VOLUME_DIRECTORY,
                    identifier=volume.path,
                    pod_uid=pod.uid,
                    reason=REASON_SHARED_VOLUME,
                    evidence=(
                        "volume %r referenced by pods %s"
                        % (volume.name, ",".join(sorted(referencers)))
                    ),
                )
            )
        else:
            report.cleanable.append(
                CleanableItem(
                    kind=CLEANABLE_POD_DIRECTORY,
                    identifier=pod.directory,
                    pod_uid=pod.uid,
                    deletion_timestamp=_iso(pod.deletion_timestamp),
                    detail="all containers terminated and deletion timestamp set",
                )
            )
            for volume in pod.volumes:
                report.cleanable.append(
                    CleanableItem(
                        kind=CLEANABLE_VOLUME_DIRECTORY,
                        identifier=volume.path,
                        pod_uid=pod.uid,
                        deletion_timestamp=_iso(pod.deletion_timestamp),
                        detail="volume %r has no remaining reference" % volume.name,
                    )
                )

    # ---- Image GC ----------------------------------------------------------
    report.cleanable.extend(_collect_images(pods, terminated_pod_uids, report))

    # ---- On-disk orphan directories ---------------------------------------
    _classify_orphans(orphan_directories, pods_by_uid, all_volumes, report)

    return report


def _retention_evidence(pod: Pod, reason: str) -> str:
    if reason == REASON_MIRROR_MISSING:
        mirror_uid = pod.mirror_pod_uid or "<unset>"
        return (
            "static pod %s mirror_pod_exists=False mirror_pod_uid=%s; "
            "missing mirror is not a termination signal" % (pod.uid, mirror_uid)
        )
    if reason == REASON_UNKNOWN_CONTAINER:
        return (
            "containers with unknown state: %s"
            % ",".join(pod.unknown_containers)
        )
    if reason == REASON_POD_RUNNING:
        states = ",".join(
            "%s=%s" % (container.name, container.state.value)
            for container in pod.containers
        )
        return "non-terminated container states: %s" % states
    if reason == REASON_NO_DELETION_TIMESTAMP:
        return "all containers terminated but deletion_timestamp is None"
    return reason


def _first_shared_volume(pod: Pod, pods_by_uid: Dict[str, Pod]):
    for volume in pod.volumes:
        referencers = _volume_referencers(volume, pod, pods_by_uid)
        if referencers:
            return volume, referencers
    return None


def _collect_images(pods, terminated_pod_uids, report: GCReport):
    """An image is collectable only when no retained pod still uses it."""

    usage: Dict[str, List[Pod]] = {}
    for pod in pods:
        for image in pod.images:
            usage.setdefault(image, []).append(pod)

    cleanable = []
    for image, users in sorted(usage.items()):
        retained_users = [pod for pod in users if pod.uid not in terminated_pod_uids]
        if retained_users:
            report.retained.append(
                RetainedEvidence(
                    kind=CLEANABLE_IMAGE,
                    identifier=image,
                    pod_uid=None,
                    reason=REASON_IMAGE_IN_USE,
                    evidence="referenced by retained pods: %s"
                    % ",".join(sorted(pod.uid for pod in retained_users)),
                )
            )
            continue
        cleanable.append(
            CleanableItem(
                kind=CLEANABLE_IMAGE,
                identifier=image,
                pod_uid=None,
                detail="only referenced by terminated pods: %s"
                % ",".join(sorted(pod.uid for pod in users)),
            )
        )
    return cleanable


def _classify_orphans(orphan_directories, pods_by_uid, all_volumes, report: GCReport):
    seen = set()
    for orphan in orphan_directories:
        path = orphan.path
        if path in seen:
            continue
        seen.add(path)

        # Directory still belongs to a pod present in the snapshot.
        owner = pods_by_uid.get(orphan.owning_pod_uid or "")
        if owner is not None:
            volume = _volume_of_owner(owner, path)
            if volume is not None:
                referencers = _live_referencers(volume, pods_by_uid)
                if referencers:
                    report.retained.append(
                        RetainedEvidence(
                            kind=CLEANABLE_VOLUME_DIRECTORY,
                            identifier=path,
                            pod_uid=owner.uid,
                            reason=REASON_SHARED_VOLUME,
                            evidence="referenced by pods %s"
                            % ",".join(sorted(referencers)),
                        )
                    )
                    continue
            # Owned by a surviving pod: GC must leave the directory alone.
            report.retained.append(
                RetainedEvidence(
                    kind="orphan_directory",
                    identifier=path,
                    pod_uid=owner.uid,
                    reason=REASON_POD_RUNNING,
                    evidence="owning pod %s still exists" % owner.uid,
                )
            )
            continue

        # No owning pod in the snapshot.
        indexed = all_volumes.get(path)
        if indexed is not None:
            owner_pod, volume = indexed
            referencers = _live_referencers(volume, pods_by_uid)
            if referencers:
                report.retained.append(
                    RetainedEvidence(
                        kind=CLEANABLE_VOLUME_DIRECTORY,
                        identifier=path,
                        pod_uid=None,
                        reason=REASON_SHARED_VOLUME,
                        evidence="dangling directory still referenced by pods %s"
                        % ",".join(sorted(referencers)),
                    )
                )
                continue

        if _looks_like_volume_path(path):
            # A volume-shaped directory with no owner and zero references is
            # a leak: record it instead of silently deleting it.
            report.volume_leaks.append(
                VolumeLeak(
                    path=path,
                    pod_uid=orphan.owning_pod_uid,
                    reason=REASON_VOLUME_LEAK,
                    evidence="no owning pod in snapshot and no volume references",
                )
            )
            continue

        # Non-volume leftovers with no attributable owner are retained as
        # evidence rather than deleted; the caller decides out of band.
        report.retained.append(
            RetainedEvidence(
                kind="orphan_directory",
                identifier=path,
                pod_uid=None,
                reason=REASON_UNATTRIBUTED_ORPHAN,
                evidence="directory exists on disk without an owning pod UID",
            )
        )


def _volume_of_owner(owner: Pod, path: str):
    for volume in owner.volumes:
        if volume.path == path:
            return volume
    return None
