"""Native ownership across worker exit, reuse, failure and explicit cleanup."""
import gc
import threading
import unittest
from unittest import mock

import numpy as np

from dlss5tool import appearance, residual_composition as rc
from dlss5tool.preview_jobs import LatestPreviewJob


class NativeComposerLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.created, self.destroyed = [], []
        self.library = mock.Mock()
        self.library.dlss5_quality_create.side_effect = self.allocate
        self.library.dlss5_quality_destroy.side_effect = self.destroyed.append
        self.library.dlss5_quality_compose.return_value = 1
        for patch in (
                mock.patch.object(rc, '_native_state', threading.local()),
                mock.patch.object(rc, '_native_library_path', return_value='composer.dll'),
                mock.patch.object(rc.ctypes, 'CDLL', return_value=self.library)):
            value = patch.start()
            self.addCleanup(patch.stop)
            if patch.attribute == '_native_library_path':
                self.path_mock = value
        self.addCleanup(rc.release_native_composer)

    def allocate(self):
        handle = len(self.created) + 1
        self.created.append(handle)
        return handle

    def test_repeated_short_preview_threads_destroy_every_handle(self):
        job = LatestPreviewJob('composer-lifetime-test')
        for cycle in range(24):
            # Return pixels/data, never the resource owner into a retained result.
            started, workers = threading.Event(), []
            def compute():
                workers.append(threading.current_thread())
                started.set()
                return rc._native_bridge() is not None
            job.request(cycle, compute)
            self.assertTrue(started.wait(3))
            workers[0].join(3)
            self.assertFalse(workers[0].is_alive())
            self.assertTrue(job.request(cycle, lambda: None))
            job.invalidate()
            gc.collect()
            self.assertEqual(self.created, self.destroyed)
        self.assertEqual(len(self.created), 24)

    def test_same_thread_reuses_and_explicit_release_is_idempotent(self):
        self.assertTrue(rc._native_bridge()._release.atexit)
        for _ in range(24):
            self.assertIsNotNone(rc._native_bridge())
        self.assertEqual(self.created, [1])
        self.assertEqual(self.destroyed, [])
        rc.release_native_composer()
        rc.release_native_composer()
        gc.collect()
        self.assertEqual(self.destroyed, [1])
        self.assertIsNotNone(rc._native_bridge())
        self.assertEqual(self.created, [1, 2])

    def test_library_change_and_disable_release_previous_handle(self):
        rc._native_bridge()
        self.path_mock.return_value = 'replacement.dll'
        rc._native_bridge()
        self.assertEqual(self.destroyed, [1])
        self.path_mock.return_value = None
        self.assertIsNone(rc._native_bridge())
        self.assertEqual(self.destroyed, [1, 2])

    def test_missing_or_failed_initialization_allocates_no_owner(self):
        self.library.dlss5_quality_create.side_effect = None
        self.library.dlss5_quality_create.return_value = 0
        self.assertIsNone(rc._native_bridge())
        self.assertEqual(self.destroyed, [])
        self.library.dlss5_quality_create.side_effect = OSError('unavailable')
        self.assertIsNone(rc._native_bridge())
        self.assertEqual(self.destroyed, [])

    def test_owner_construction_failure_releases_native_allocation(self):
        with mock.patch.object(rc, '_NativeBridge', side_effect=MemoryError('owner allocation')):
            with self.assertRaises(MemoryError):
                rc._native_bridge()
        self.assertEqual(self.created, [1])
        self.assertEqual(self.destroyed, [1])

    def test_dispatch_failure_releases_once_and_allows_retry(self):
        image = np.zeros((4, 4, 3), np.uint8)
        values = appearance.preflight({'nr_quality': {'total': .75}})
        with mock.patch.object(rc, 'NATIVE_COMPOSITION_MIN_PIXELS', 1):
            for failure in (0, OSError('device lost')):
                self.library.dlss5_quality_compose.side_effect = (
                    failure if isinstance(failure, Exception) else None)
                self.library.dlss5_quality_compose.return_value = 0
                self.assertIsNone(rc._native_compose(image, image, values, order='rgb'))
                self.assertEqual(self.created, self.destroyed)
            self.library.dlss5_quality_compose.side_effect = None
            self.library.dlss5_quality_compose.return_value = 1
            self.assertIsNotNone(rc._native_compose(image, image, values, order='rgb'))
        rc.release_native_composer()
        self.assertEqual(self.created, self.destroyed)

    def test_array_allocation_failure_releases_even_while_traceback_is_retained(self):
        image = np.zeros((4, 4, 3), np.uint8)
        values = appearance.preflight({'nr_quality': {'total': .75}})
        with mock.patch.object(rc, 'NATIVE_COMPOSITION_MIN_PIXELS', 1), \
                mock.patch.object(rc.np, 'empty_like', side_effect=MemoryError('frame allocation')):
            try:
                rc._native_compose(image, image, values, order='rgb')
            except MemoryError as error:
                retained_error = error
            else:
                self.fail('allocation should fail')
        self.assertIsNotNone(retained_error.__traceback__)
        self.assertEqual(self.created, self.destroyed)

    def test_release_on_another_thread_never_destroys_active_owner(self):
        ready, finish = threading.Event(), threading.Event()
        atexit_flags = []
        def compute():
            bridge = rc._native_bridge()
            atexit_flags.append(bridge._release.atexit)
            ready.set()
            finish.wait(3)
        worker = threading.Thread(target=compute)
        worker.start()
        try:
            self.assertTrue(ready.wait(3))
            self.assertEqual(atexit_flags, [False])
            rc.release_native_composer()
            self.assertEqual(self.destroyed, [])
        finally:
            finish.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.created, self.destroyed)
