"""Input model for the offline Pod GC validator.

All inputs are plain snapshots (UID, statuses, volume references, images,
static/mirror markers, orphan directories and deletion time). The model is
deliberately independent of kubernetes client types so the validator runs
without any cluster connection.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


KUBELET_ROOT = "/var/lib/kubelet"


class ContainerState(str, Enum):
    """Container lifecycle states understood by the GC simulation."""

    RUNNING = "running"
    WAITING = "waiting"
    TERMINATED = "terminated"
    UNKNOWN = "unknown"

    @property
    def is_known_terminal(self) -> bool:
        return self is ContainerState.TERMINATED


@dataclass
class ContainerStatus:
    """One container's observed state and the image it references."""

    name: str
    state: ContainerState
    image: str

    @classmethod
    def from_dict(cls, raw):
        state = raw["state"]
        if isinstance(state, str):
            state = ContainerState(state)
        return cls(name=raw["name"], state=state, image=raw["image"])


@dataclass
class Volume:
    """A volume directory attached to a pod.

    ``path`` is the directory kubelet would manage under the pods directory.
    ``referenced_by`` lists the UIDs of every pod (including this pod) that
    still mounts the volume, which is how image volumes get shared.
    """

    name: str
    path: str
    referenced_by: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw):
        return cls(
            name=raw["name"],
            path=raw["path"],
            referenced_by=list(raw.get("referenced_by", [])),
        )


@dataclass
class Pod:
    """Snapshot of one pod as seen by kubelet's PLEG."""

    uid: str
    containers: List[ContainerStatus]
    volumes: List[Volume] = field(default_factory=list)
    is_static: bool = False
    mirror_pod_exists: bool = True
    mirror_pod_uid: Optional[str] = None
    deletion_timestamp: Optional[datetime] = None
    directory: Optional[str] = None

    def __post_init__(self):
        if not self.uid:
            raise ValueError("pod uid must not be empty")
        if self.directory is None:
            self.directory = "%s/pods/%s" % (KUBELET_ROOT, self.uid)

    @property
    def has_active_mirror(self) -> bool:
        # Static pods are managed through their mirror pod; only static pods
        # care about the mirror marker at all.
        return (not self.is_static) or self.mirror_pod_exists

    @property
    def unknown_containers(self) -> List[str]:
        return [
            container.name
            for container in self.containers
            if container.state is ContainerState.UNKNOWN
        ]

    @property
    def all_containers_terminated(self) -> bool:
        if not self.containers:
            return False
        return all(
            container.state.is_known_terminal for container in self.containers
        )

    @property
    def images(self) -> List[str]:
        return [container.image for container in self.containers]


@dataclass
class OrphanDirectory:
    """A directory found on disk whose owning pod UID is gone from config.

    ``owning_pod_uid`` is set when the path can still be attributed to a pod
    present in the input; otherwise it is ``None`` and the path is a genuine
    orphan.
    """

    path: str
    owning_pod_uid: Optional[str] = None


def pod_from_dict(raw) -> Pod:
    """Build a :class:`Pod` from a plain dictionary (e.g. parsed JSON)."""

    deletion_timestamp = raw.get("deletion_timestamp")
    if isinstance(deletion_timestamp, str) and deletion_timestamp:
        deletion_timestamp = datetime.fromisoformat(deletion_timestamp)
    return Pod(
        uid=raw["uid"],
        containers=[ContainerStatus.from_dict(item) for item in raw["containers"]],
        volumes=[Volume.from_dict(item) for item in raw.get("volumes", [])],
        is_static=raw.get("is_static", False),
        mirror_pod_exists=raw.get("mirror_pod_exists", True),
        mirror_pod_uid=raw.get("mirror_pod_uid"),
        deletion_timestamp=deletion_timestamp,
        directory=raw.get("directory"),
    )
