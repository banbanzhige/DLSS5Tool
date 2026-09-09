import threading
import unittest
from unittest import mock

from dlss5tool import app_settings
from dlss5tool import guidance_client
from dlss5tool.guidance_execution import execution_contract
from dlss5tool.guidance_worker import Models


class ExecutionPolicyTests(unittest.TestCase):
    def test_profiles_always_use_original_raft(self):
        self.assertEqual(execution_contract({'guidance_mode': 3})['raft_output'], 'all')
        for mode, effective in ((0, 'serial'), (1, 'serial'), (2, 'serial'), (3, 'raft_streams')):
            settings = {'guidance_mode': mode, 'guidance_execution': 'raft_streams'}
            result = execution_contract(settings, 'cuda')
            self.assertEqual(result['execution'], effective)
            self.assertEqual(result['raft_output'], 'all' if mode in (1, 3) else 'off')
            self.assertEqual(result['schedule'], 'dual_stream' if mode == 3 else 'serial')
            if mode != 3:
                self.assertEqual(execution_contract(settings, 'cpu'), result)
        for mode in range(4):
            legacy = execution_contract({'guidance_mode': mode, 'guidance_execution': 'raft_final'}, 'cuda')
            serial = execution_contract({'guidance_mode': mode, 'guidance_execution': 'serial'}, 'cuda')
            self.assertEqual(legacy, serial)
        with self.assertRaisesRegex(ValueError, 'streams_cuda'):
            execution_contract({'guidance_mode': 3, 'guidance_execution': 'raft_streams'}, 'cpu')
        with self.assertRaises(ValueError):
            execution_contract({'guidance_execution': 'wrong'})

    def test_handshake_cannot_silently_ignore_dual_stream(self):
        settings = {'guidance_mode': 3, 'guidance_execution': 'raft_streams'}
        ready = {'device': 'cuda', **execution_contract(settings, 'cuda')}
        guidance_client.check_execution_handshake(settings, ready, 'en_US')
        for key in ('execution', 'raft_output', 'schedule'):
            bad = {k: v for k, v in ready.items() if k != key}
            with self.assertRaisesRegex(RuntimeError, 'Original serial'):
                guidance_client.check_execution_handshake(settings, bad, 'en_US')
        guidance_client.check_execution_handshake({'guidance_mode': 3}, {}, 'en_US')
        guidance_client.check_execution_handshake(
            {'guidance_mode': 3, 'guidance_execution': 'raft_final'}, {}, 'en_US')
        with self.assertRaises(RuntimeError):
            guidance_client.check_execution_handshake({'guidance_mode': 3}, ready, 'en_US')

    def test_settings_and_contract(self):
        for profile in ('serial', 'raft_streams'):
            self.assertEqual(app_settings.validate({'guidance_execution': profile})['guidance_execution'], profile)
        self.assertEqual(app_settings.validate({'guidance_execution': 'raft_final'})['guidance_execution'], 'serial')
        self.assertEqual(app_settings.validate({})['guidance_execution'], 'raft_streams')
        self.assertEqual(app_settings.validate({'guidance_execution': 'wrong'})['guidance_execution'], 'raft_streams')
        self.assertNotEqual(guidance_client.contract({'guidance_execution': 'serial'}),
                            guidance_client.contract({'guidance_execution': 'raft_streams'}))
        with self.assertRaisesRegex(ValueError, 'CUDA'):
            guidance_client.validate({'guidance_mode': 3, 'guidance_device': 'cpu',
                'guidance_execution': 'raft_streams', 'ui_language': 'en_US'})

    def model_stub(self):
        model = Models.__new__(Models)
        model._closed = model._failed = False
        model._process_lock = threading.Lock()
        model.flow_stream, model.depth_stream = mock.Mock(), mock.Mock()
        model.prev = model.prev_thumb = model.depth_range = object()
        model.flow = model.depth = object()
        return model

    def test_close_drains_both_streams_even_after_failure_and_is_idempotent(self):
        model = self.model_stub()
        model.flow_stream.synchronize.side_effect = RuntimeError('CUDA failure')
        model.close()
        model.close()
        model.flow_stream.synchronize.assert_called_once()
        model.depth_stream.synchronize.assert_called_once()
        self.assertIsNone(model.flow)
        self.assertIsNone(model.prev)

    def test_failure_invalidates_model_and_does_not_allow_reentry(self):
        model = self.model_stub()
        model._process_frame = mock.Mock(side_effect=RuntimeError('inference failure'))
        previous = model.prev
        with self.assertRaisesRegex(RuntimeError, 'inference failure'):
            model.process(None, False)
        self.assertIs(model.prev, previous)
        self.assertFalse(model._process_lock.locked())
        with self.assertRaisesRegex(RuntimeError, 'new session'):
            model.process(None, False)
        model._process_frame.assert_called_once()

    def test_rejects_two_frames_in_one_stateful_raft(self):
        model = self.model_stub()
        model._process_lock.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, 'Concurrent'):
                model.process(None, False)
        finally:
            model._process_lock.release()
        self.assertFalse(model._failed)

if __name__ == '__main__':
    unittest.main()
