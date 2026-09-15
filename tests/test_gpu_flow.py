"""CPU-only protocol/lifetime coverage for production GPU flow negotiation."""
import copy
import ctypes as C
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np
from dlss5tool import app_settings, dlss_engine, guidance_client, gpu_flow


class DescriptorTests(unittest.TestCase):
    def descriptor(self):
        return dict(transport=gpu_flow.TRANSPORT,owner_pid=123,width=256,height=128,
                    flow_size=[128,128],luid=[0,1],slots=[dict(handle=55,allocation=131072)])

    def test_descriptor_and_abi(self):
        self.assertEqual(gpu_flow.validate_descriptor(self.descriptor(),256,128,(128,128)),131072)
        self.assertEqual(C.sizeof(gpu_flow.ExternalMemoryDesc),104)
        self.assertEqual(C.sizeof(gpu_flow.BufferDesc),88)

    def test_bad_descriptor(self):
        for patch in (dict(owner_pid=True),dict(owner_pid=-1),dict(slots=[]),dict(flow_size=[256,128]),
                      dict(slots=[dict(handle=0,allocation=131072)]),dict(slots=[dict(handle=3,allocation=2)]),
                      dict(luid=[False,1]),dict(transport='other')):
            with self.subTest(patch=patch),self.assertRaises(ValueError):
                gpu_flow.validate_descriptor({**self.descriptor(),**patch},256,128,(128,128))

    def test_eligibility_preserves_legacy(self):
        base=dict(guidance_mode=1)
        self.assertTrue(gpu_flow.eligible(base))
        self.assertTrue(gpu_flow.eligible({**base,'host_submission':'compatibility'}))
        for patch in (dict(guidance_mode=0),dict(guidance_mode=3),dict(guidance_device='cpu'),
                      dict(guidance_gpu_transport='off'),dict(guidance_flow_backend='nvofa'),
                      dict(host_persistent_buffers=False),dict(host_tiled_mode=True),dict(guidance_transport='pipe')):
            self.assertFalse(gpu_flow.eligible({**base,**patch}))

    def test_settings_and_contract(self):
        self.assertEqual(app_settings.validate({})['guidance_gpu_transport'],'auto')
        self.assertEqual(app_settings.validate({'guidance_gpu_transport':'off'})['guidance_gpu_transport'],'off')
        self.assertNotEqual(guidance_client.contract({'guidance_gpu_transport':'off'}),
                            guidance_client.contract({'guidance_gpu_transport':'auto'}))


class ClientTests(unittest.TestCase):
    def session(self):
        session=guidance_client.GuidanceSession.__new__(guidance_client.GuidanceSession)
        session.info={'gpu_flow_capability':gpu_flow.TRANSPORT}
        session._sequence=0
        session.width,session.height=8,6
        session.language='en_US'
        session._buffers=SimpleNamespace(rgba=np.zeros((6,8,4),np.uint8))
        session._send=mock.Mock()
        session.close=mock.Mock()
        return session

    def test_old_worker_not_contacted(self):
        s=self.session(); s.info={}
        self.assertFalse(s.enable_gpu_flow({}))
        s._send.assert_not_called()

    def test_decline_retains_cpu_session(self):
        s=self.session(); s._reply=mock.Mock(return_value={'gpu_flow_transport':None,'reason':'different GPU'})
        self.assertFalse(s.enable_gpu_flow({}))
        self.assertEqual(s.info['gpu_flow_fallback_reason'],'different GPU')
        s.close.assert_not_called()

    def test_configure_after_cpu_preview_and_gpu_ack(self):
        s=self.session(); s._sequence=4
        s._reply=mock.Mock(return_value={'gpu_flow_transport':gpu_flow.TRANSPORT})
        self.assertTrue(s.enable_gpu_flow({}))
        s._reply.return_value={'sequence':5,'gpu_slot':2,'reset':True,'metrics':{'gpu_flow':True}}
        self.assertEqual(s.process(s._buffers.rgba,copy_outputs=False,allow_missing_depth=True,gpu_slot=2),
                         (None,None,True))
        self.assertTrue(s.last_metrics['gpu_flow'])

    def test_wrong_sequence_slot_and_timeout_close(self):
        for reply in ({'sequence':2,'gpu_slot':0},{'sequence':1,'gpu_slot':1}):
            s=self.session(); s.info['gpu_flow_transport']=gpu_flow.TRANSPORT
            s._reply=mock.Mock(return_value=reply)
            with self.assertRaises(RuntimeError):
                s.process(s._buffers.rgba,copy_outputs=False,allow_missing_depth=True,gpu_slot=0)
            s.close.assert_called_once()
        s=self.session(); s.info['gpu_flow_transport']=gpu_flow.TRANSPORT
        s._reply=mock.Mock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):
            s.process(s._buffers.rgba,copy_outputs=False,allow_missing_depth=True,gpu_slot=0)
        s.close.assert_called_once()


class EngineTests(unittest.TestCase):
    def live(self):
        live=dlss_engine.Live.__new__(dlss_engine.Live)
        live._w,live._h=8,6
        live._reset_next=True;live._gpu_flow=None
        live.settings={'guidance_mode':1}; live.backend='v2';live.adapter_info={}
        live.supports_optional_depth=True
        live._lib=SimpleNamespace(dlssnr_gpu_flow_create=True)
        live._guidance=mock.Mock()
        live._guidance.info={'gpu_flow_capability':gpu_flow.TRANSPORT}
        live._guidance.enable_gpu_flow.return_value=True
        live._guidance.process.return_value=(None,None,True)
        return live

    def test_reserve_then_ack_then_arm(self):
        live=self.live(); native=mock.Mock()
        native.reserve.return_value=2
        with mock.patch.object(gpu_flow,'NativeFlow',return_value=native):
            self.assertTrue(live._prepare_guidance(np.zeros((6,8,4),np.uint8),False))
        native.reserve.assert_called_once();native.arm.assert_called_once_with(2)
        self.assertEqual(live._guidance.process.call_args.kwargs['gpu_slot'],2)

    def test_preview_stays_cpu_then_production_enables(self):
        live=self.live();native=mock.Mock();native.reserve.return_value=0
        frame=np.zeros((6,8,4),np.uint8)
        with mock.patch.object(gpu_flow,'NativeFlow',return_value=native) as factory:
            live._prepare_guidance(frame,True,allow_gpu=False)
            factory.assert_not_called()
            live._prepare_guidance(frame,True)
            factory.assert_called_once()

    def test_close_worker_before_exporter(self):
        live=self.live();events=[]
        live._guidance.close.side_effect=lambda:events.append('worker')
        live._gpu_flow=mock.Mock();live._gpu_flow.close.side_effect=lambda:events.append('exporter')
        live.close_guidance();live.close_guidance()
        self.assertEqual(events,['worker','exporter'])

    def test_inference_failure_does_not_arm(self):
        live=self.live();native=mock.Mock();native.reserve.return_value=0
        live._guidance.process.side_effect=RuntimeError('worker died')
        with mock.patch.object(gpu_flow,'NativeFlow',return_value=native),self.assertRaises(RuntimeError):
            live._prepare_guidance(np.zeros((6,8,4),np.uint8),False)
        native.arm.assert_not_called();native.close.assert_called_once()
        self.assertTrue(live._reset_next)


if __name__=='__main__':unittest.main()
