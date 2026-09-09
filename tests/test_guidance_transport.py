import json
import os
import subprocess
import sys
import unittest
from unittest import mock

import numpy as np

from dlss5tool import guidance_client
from dlss5tool.guidance_transport import GuidanceBuffers, TRANSPORT


class BufferTests(unittest.TestCase):
    def test_layout_attach_and_cleanup(self):
        owner = GuidanceBuffers(8, 6)
        descriptor = owner.descriptor
        peer = GuidanceBuffers(8, 6, descriptor)
        try:
            owner.rgba.fill(23)
            peer.motion.fill(-2.5)
            peer.depth.fill(0.7)
            np.testing.assert_array_equal(peer.rgba, 23)
            np.testing.assert_array_equal(owner.motion, -2.5)
            np.testing.assert_array_equal(owner.depth, np.float32(0.7))
            self.assertEqual(descriptor['size'], 8 * 6 * 16)
        finally:
            peer.close()
            owner.close()
            owner.close()
        with self.assertRaises(FileNotFoundError):
            GuidanceBuffers(8, 6, descriptor)

    def test_invalid_dimensions_and_descriptor(self):
        for width, height in ((0, 1), (1, -2), (True, 3), (1.5, 4)):
            with self.assertRaises(ValueError):
                GuidanceBuffers(width, height)
        with self.assertRaises(ValueError):
            GuidanceBuffers(8, 6, {'transport': TRANSPORT, 'name': 'unused', 'size': 1})
        owner = GuidanceBuffers(8, 6)
        try:
            with self.assertRaises(ValueError):
                GuidanceBuffers(80, 60, {**owner.descriptor, 'size': 80 * 60 * 16})
        finally:
            owner.close()


@unittest.skipUnless(os.name == 'nt', 'Windows named-pipe component contract')
class SessionTests(unittest.TestCase):
    def test_old_cache_component_cannot_ignore_shared_budget(self):
        with self.assertRaisesRegex(RuntimeError,'component|组件'):
            self.session(guidance_cache_pool='Local\\DLSS5-cache-fixture')

    def session(self, transport='auto', legacy=False, **extra):
        popen = subprocess.Popen
        def launch(command, **kwargs):
            args = [sys.executable, '-m', 'tests.guidance_worker_fixture']
            if legacy:
                args.append('--legacy')
            return popen(args + command[1:], **kwargs)
        with mock.patch.object(guidance_client, 'validate', return_value={'worker': 'fixture.exe'}), \
                mock.patch.object(guidance_client.subprocess, 'Popen', side_effect=launch):
            session = guidance_client.GuidanceSession({'guidance_mode': 1,
                'guidance_transport': transport, **extra}, 8, 6)
        self.addCleanup(session.close)
        return session

    def test_shared_and_pipe_match_reset_and_noncontiguous_input(self):
        shared, pipe = self.session(), self.session('pipe')
        self.assertEqual(shared.info['transport'], TRANSPORT)
        self.assertEqual(pipe.transport, 'pipe')
        frame = np.arange(6 * 8 * 4, dtype=np.uint8).reshape(6, 8, 4)[:, ::-1]
        for reset in (True, False, False, True):
            a = shared.process(frame, reset)
            b = pipe.process(frame, reset)
            for left, right in zip(a, b):
                np.testing.assert_array_equal(left, right)

    def test_default_outputs_survive_next_frame_and_close(self):
        session = self.session()
        frame = np.full((6, 8, 4), 24, np.uint8)
        motion, depth, _ = session.process(frame)
        expected = (motion.copy(), depth.copy())
        session.process(frame * 2)
        descriptor = session._buffers.descriptor
        session.close()
        session.close()
        np.testing.assert_array_equal(motion, expected[0])
        np.testing.assert_array_equal(depth, expected[1])
        with self.assertRaises(FileNotFoundError):
            GuidanceBuffers(8, 6, descriptor)

    def test_borrowed_outputs_are_buffer_views(self):
        session = self.session()
        mv, dp, _ = session.process(np.ones((6, 8, 4), np.uint8), copy_outputs=False)
        self.assertIs(mv, session._buffers.motion)
        self.assertIs(dp, session._buffers.depth)
        session.process(np.zeros((6, 8, 4), np.uint8), True, copy_outputs=False)
        np.testing.assert_array_equal(mv, 0)
        np.testing.assert_array_equal(dp, 0)

    def test_old_component_falls_back_only_when_allowed(self):
        old = self.session(legacy=True)
        self.assertEqual(old.transport, 'pipe')
        self.assertIsNone(old._buffers)
        motion, _, _ = old.process(np.ones((6, 8, 4), np.uint8))
        np.testing.assert_array_equal(motion[..., 0], 0.5)
        with self.assertRaisesRegex(RuntimeError, 'does not support shared memory'):
            self.session(TRANSPORT, legacy=True)

    def test_old_component_cannot_silently_ignore_depth_acceleration(self):
        with self.assertRaisesRegex(RuntimeError, 'Original FP32'):
            self.session(legacy=True, guidance_mode=3, guidance_depth_encoder='vitl',
                         guidance_depth_profile='sdpa_fp16', ui_language='en_US')

    def test_old_component_cannot_silently_ignore_dual_stream(self):
        legacy_final = self.session(legacy=True, guidance_execution='raft_final', ui_language='en_US')
        self.assertEqual(legacy_final.info['execution'], None)
        with self.assertRaisesRegex(RuntimeError, 'Original serial'):
            self.session(legacy=True, guidance_mode=3,
                         guidance_execution='raft_streams', ui_language='en_US')

    def test_inference_error_and_nonfinite_release_resources(self):
        for setting, message in (('fixture_error', 'fixture inference failure'),
                                 ('fixture_nonfinite', 'NaN|Inf|有限|finite')):
            session = self.session(**{setting: True})
            descriptor = session._buffers.descriptor
            with self.assertRaisesRegex(RuntimeError if setting == 'fixture_error' else ValueError, message):
                session.process(np.zeros((6, 8, 4), np.uint8))
            self.assertIsNone(session._buffers)
            self.assertIsNone(session._process)
            with self.assertRaises(FileNotFoundError):
                GuidanceBuffers(8, 6, descriptor)

    def test_acknowledgement_mismatch_is_fatal(self):
        session = self.session()
        with mock.patch.object(session, '_reply', return_value={'ok': True, 'sequence': 999}):
            with self.assertRaisesRegex(RuntimeError, 'acknowledgement mismatch'):
                session.process(np.zeros((6, 8, 4), np.uint8))
        self.assertIsNone(session._buffers)

    def test_worker_rejects_out_of_order_frame(self):
        session = self.session()
        session._send({'sequence': 2, 'reset': False})
        with self.assertRaisesRegex(RuntimeError, 'sequence mismatch'):
            session._reply()

    def test_worker_death_releases_buffers(self):
        session = self.session()
        descriptor = session._buffers.descriptor
        session._process.kill()
        session._process.wait(timeout=5)
        with self.assertRaises((OSError, EOFError)):
            session.process(np.zeros((6, 8, 4), np.uint8))
        self.assertIsNone(session._buffers)
        with self.assertRaises(FileNotFoundError):
            GuidanceBuffers(8, 6, descriptor)

    def test_timeout_releases_buffers(self):
        session = self.session()
        descriptor = session._buffers.descriptor
        with mock.patch.object(session._connection, 'poll', return_value=False):
            with self.assertRaises(TimeoutError):
                session.process(np.zeros((6, 8, 4), np.uint8))
        with self.assertRaises(FileNotFoundError):
            GuidanceBuffers(8, 6, descriptor)


if __name__ == '__main__':
    unittest.main()
