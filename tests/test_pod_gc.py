import unittest
from datetime import datetime, timezone

from pod_gc import (
    ContainerState,
    ContainerStatus,
    OrphanDirectory,
    Pod,
    Volume,
    pod_from_dict,
)
from pod_gc.gc import (
    CLEANABLE_IMAGE,
    CLEANABLE_POD_DIRECTORY,
    CLEANABLE_VOLUME_DIRECTORY,
    REASON_MIRROR_MISSING,
    REASON_NO_DELETION_TIMESTAMP,
    REASON_POD_RUNNING,
    REASON_SHARED_VOLUME,
    REASON_UNKNOWN_CONTAINER,
    REASON_VOLUME_LEAK,
    evaluate_garbage_collection,
)


DELETED_AT = datetime(2026, 11, 25, 1, 2, 3, tzinfo=timezone.utc)


def terminated_pod(uid, volumes=None, image="registry.example/app:1", **kwargs):
    return Pod(
        uid=uid,
        containers=[ContainerStatus("app", ContainerState.TERMINATED, image)],
        volumes=volumes or [],
        deletion_timestamp=DELETED_AT,
        **kwargs,
    )


def running_pod(uid, volumes=None, image="registry.example/app:1", **kwargs):
    return Pod(
        uid=uid,
        containers=[ContainerStatus("app", ContainerState.RUNNING, image)],
        volumes=volumes or [],
        **kwargs,
    )


class SharedVolumeTest(unittest.TestCase):
    def test_image_volume_referenced_by_new_pod_blocks_cleanup(self):
        shared_path = "/var/lib/kubelet/pods/old-uid/volumes/kubernetes.io~image/img"
        shared_volume = Volume(
            name="img",
            path=shared_path,
            referenced_by=["old-uid", "new-uid"],
        )
        old_pod = terminated_pod("old-uid", volumes=[shared_volume])
        new_pod = running_pod(
            "new-uid",
            volumes=[Volume("img", shared_path, referenced_by=["old-uid", "new-uid"])],
        )

        report = evaluate_garbage_collection([old_pod, new_pod])

        retained_ids = {(item.kind, item.identifier) for item in report.retained}
        self.assertIn(
            (CLEANABLE_VOLUME_DIRECTORY, shared_path),
            retained_ids,
        )
        shared = next(
            item
            for item in report.retained
            if item.identifier == shared_path
        )
        self.assertEqual(shared.reason, REASON_SHARED_VOLUME)
        self.assertIn("new-uid", shared.evidence)

        # The terminating pod's directory must not be deleted while a shared
        # volume inside it is still mounted.
        cleanable_ids = {item.identifier for item in report.cleanable}
        self.assertNotIn(old_pod.directory, cleanable_ids)
        self.assertNotIn(shared_path, cleanable_ids)

        # The new pod stays because it is running.
        self.assertIn(
            new_pod.directory,
            {item.identifier for item in report.retained},
        )

        # No leak: a live pod still references the directory.
        self.assertEqual(report.volume_leaks, [])

    def test_unshared_volume_of_terminated_pod_is_cleanable(self):
        volume_path = "/var/lib/kubelet/pods/old-uid/volumes/kubernetes.io~empty-dir/tmp"
        old_pod = terminated_pod(
            "old-uid",
            volumes=[Volume("tmp", volume_path, referenced_by=["old-uid"])],
            image="registry.example/old:1",
        )

        report = evaluate_garbage_collection([old_pod])
        cleanable_ids = {item.identifier for item in report.cleanable}

        self.assertIn(old_pod.directory, cleanable_ids)
        self.assertIn(volume_path, cleanable_ids)
        self.assertIn("registry.example/old:1", cleanable_ids)
        self.assertEqual(report.retained, [])
        self.assertEqual(report.volume_leaks, [])

        pod_item = next(
            item for item in report.cleanable
            if item.identifier == old_pod.directory
        )
        self.assertEqual(pod_item.deletion_timestamp, DELETED_AT.isoformat())

    def test_shared_image_is_retained_while_referenced_by_running_pod(self):
        old_pod = terminated_pod("old-uid", image="registry.example/shared:2")
        new_pod = running_pod("new-uid", image="registry.example/shared:2")

        report = evaluate_garbage_collection([old_pod, new_pod])

        retained_images = {
            item.identifier for item in report.retained if item.kind == CLEANABLE_IMAGE
        }
        cleanable_images = {
            item.identifier for item in report.cleanable
            if item.kind == CLEANABLE_IMAGE
        }
        self.assertIn("registry.example/shared:2", retained_images)
        self.assertNotIn("registry.example/shared:2", cleanable_images)


class StaticMirrorTest(unittest.TestCase):
    def test_static_pod_survives_mirror_pod_disappearance(self):
        static_pod = Pod(
            uid="static-uid",
            containers=[
                ContainerStatus("app", ContainerState.TERMINATED, "img:1")
            ],
            is_static=True,
            mirror_pod_exists=False,
            mirror_pod_uid="mirror-uid",
            deletion_timestamp=DELETED_AT,
        )

        report = evaluate_garbage_collection([static_pod])

        self.assertEqual(
            [item.identifier for item in report.cleanable],
            [],
        )
        retained = report.retained[0]
        self.assertEqual(retained.pod_uid, "static-uid")
        self.assertEqual(retained.reason, REASON_MIRROR_MISSING)
        self.assertIn("mirror-uid", retained.evidence)
        # Its image must not be collected either.
        self.assertTrue(
            all(item.identifier != "img:1" for item in report.cleanable)
        )

    def test_static_pod_with_live_mirror_follows_normal_rules(self):
        static_pod = terminated_pod(
            "static-uid",
            is_static=True,
            mirror_pod_exists=True,
            mirror_pod_uid="mirror-uid",
        )
        report = evaluate_garbage_collection([static_pod])
        cleanable_ids = {item.identifier for item in report.cleanable}
        self.assertIn(static_pod.directory, cleanable_ids)
        reasons = {item.reason for item in report.retained}
        self.assertNotIn(REASON_MIRROR_MISSING, reasons)


class ContainerStateTest(unittest.TestCase):
    def test_unknown_container_state_is_not_terminated(self):
        pod = Pod(
            uid="unknown-uid",
            containers=[
                ContainerStatus("sidecar", ContainerState.UNKNOWN, "img:9"),
            ],
            deletion_timestamp=DELETED_AT,
        )

        report = evaluate_garbage_collection([pod])

        self.assertEqual(report.cleanable, [])
        retained = report.retained[0]
        self.assertEqual(retained.reason, REASON_UNKNOWN_CONTAINER)
        self.assertIn("sidecar", retained.evidence)
        # An unknown state gives no evidence the image is unused.
        retained_images = {
            item.identifier for item in report.retained
            if item.kind == CLEANABLE_IMAGE
        }
        self.assertIn("img:9", retained_images)

    def test_running_pod_without_deletion_timestamp_is_retained(self):
        pod = running_pod("running-uid")
        report = evaluate_garbage_collection([pod])
        retained = report.retained[0]
        self.assertEqual(retained.reason, REASON_POD_RUNNING)

    def test_terminated_pod_without_deletion_timestamp_is_retained(self):
        pod = Pod(
            uid="stuck-uid",
            containers=[
                ContainerStatus("app", ContainerState.TERMINATED, "img:1")
            ],
        )
        report = evaluate_garbage_collection([pod])
        retained = report.retained[0]
        self.assertEqual(retained.reason, REASON_NO_DELETION_TIMESTAMP)


class OrphanAndLeakTest(unittest.TestCase):
    def test_orphan_volume_without_owner_or_reference_is_a_leak(self):
        leak_path = (
            "/var/lib/kubelet/pods/gone-uid/volumes/kubernetes.io~image/leak"
        )
        report = evaluate_garbage_collection(
            pods=[],
            orphan_directories=[OrphanDirectory(leak_path)],
        )

        self.assertEqual(len(report.volume_leaks), 1)
        leak = report.volume_leaks[0]
        self.assertEqual(leak.path, leak_path)
        self.assertEqual(leak.reason, REASON_VOLUME_LEAK)
        self.assertEqual(report.cleanable, [])

    def test_orphan_directory_referenced_by_surviving_pod_is_retained(self):
        shared_path = (
            "/var/lib/kubelet/pods/gone-uid/volumes/kubernetes.io~image/img"
        )
        survivor = running_pod(
            "new-uid",
            volumes=[Volume("img", shared_path, referenced_by=["new-uid"])],
        )
        report = evaluate_garbage_collection(
            pods=[survivor],
            orphan_directories=[
                OrphanDirectory(shared_path, owning_pod_uid="gone-uid")
            ],
        )

        self.assertEqual(report.volume_leaks, [])
        retained = next(
            item for item in report.retained if item.identifier == shared_path
        )
        self.assertEqual(retained.reason, REASON_SHARED_VOLUME)
        self.assertNotIn(shared_path, {item.identifier for item in report.cleanable})

    def test_non_volume_orphan_is_retained_as_evidence(self):
        path = "/var/lib/kubelet/pods/gone-uid/etc"
        report = evaluate_garbage_collection(
            pods=[],
            orphan_directories=[OrphanDirectory(path)],
        )
        self.assertEqual(report.volume_leaks, [])
        retained = report.retained[0]
        self.assertEqual(retained.identifier, path)


class InputParsingTest(unittest.TestCase):
    def test_pod_from_dict_accepts_snapshot_payload(self):
        payload = {
            "uid": "dict-uid",
            "containers": [
                {"name": "app", "state": "terminated", "image": "img:1"}
            ],
            "volumes": [
                {
                    "name": "img",
                    "path": "/var/lib/kubelet/pods/dict-uid/volumes/img",
                    "referenced_by": ["dict-uid"],
                }
            ],
            "is_static": True,
            "mirror_pod_exists": False,
            "mirror_pod_uid": "m-uid",
            "deletion_timestamp": "2026-11-25T01:02:03+00:00",
        }
        pod = pod_from_dict(payload)

        self.assertEqual(pod.uid, "dict-uid")
        self.assertTrue(pod.is_static)
        self.assertFalse(pod.mirror_pod_exists)
        self.assertEqual(
            pod.deletion_timestamp,
            datetime(2026, 11, 25, 1, 2, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(pod.containers[0].state, ContainerState.TERMINATED)
        self.assertEqual(pod.volumes[0].referenced_by, ["dict-uid"])

        report = evaluate_garbage_collection([pod])
        self.assertEqual(report.cleanable, [])
        self.assertEqual(report.retained[0].reason, REASON_MIRROR_MISSING)


if __name__ == "__main__":
    unittest.main()
