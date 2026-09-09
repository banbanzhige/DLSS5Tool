import importlib.util
import threading
import unittest
from unittest import mock
import numpy as np
from dlss5tool.guidance_cache import RawGuidanceCache, frame_digest, cache_budget_mib
from dlss5tool.guidance_worker import Models


class CacheTests(unittest.TestCase):
    def test_lru_owned_arrays_and_budget(self):
        cache = RawGuidanceCache(32)
        a = np.ones(4, np.float32)
        cache.put('a', a); a.fill(99)
        np.testing.assert_array_equal(cache.get('a'), np.ones(4))
        with self.assertRaises(ValueError): cache.get('a')[0] = 4
        cache.put('b', a); cache.get('a'); cache.put('c', a)
        self.assertIsNone(cache.get('b'))
        self.assertEqual(cache.bytes, 32)
        self.assertEqual(cache.evictions, 1)
        cache.put('huge', np.zeros(100, np.float32))
        self.assertLessEqual(cache.bytes, 32)
        cache.clear(); self.assertEqual(cache.bytes, 0)

    def test_disabled_invalid_and_replacement(self):
        cache=RawGuidanceCache(0);cache.put('a',np.ones(2,np.float32))
        self.assertIsNone(cache.get('a'))
        cache=RawGuidanceCache(100)
        with self.assertRaises(ValueError):cache.put('bad',np.array([np.nan],np.float32))
        cache.put('a',np.ones(10,np.float32));cache.put('a',np.ones(2,np.float32))
        self.assertEqual(cache.bytes,8)

    def test_content_shape_dtype_and_ordered_keys(self):
        a=np.zeros((4,4,4),np.uint8);b=a.copy();b[0,0,0]=1
        self.assertEqual(frame_digest(a),frame_digest(a.copy()))
        self.assertNotEqual(frame_digest(a),frame_digest(b))
        self.assertNotEqual(frame_digest(a),frame_digest(a.reshape(2,8,4)))
        self.assertNotEqual(frame_digest(a),frame_digest(a.view(np.uint16)))
        cache=RawGuidanceCache(100)
        cache.put(('flow',frame_digest(a),frame_digest(b)),np.ones(2,np.float32))
        self.assertIsNone(cache.get(('flow',frame_digest(b),frame_digest(a))))

    def test_budget(self):
        self.assertEqual(cache_budget_mib({}),1024)
        self.assertEqual(cache_budget_mib({'guidance_cache_mb':0}),0)
        self.assertEqual(cache_budget_mib({'guidance_cache_mb':10000}),10000)
        self.assertEqual(cache_budget_mib({'guidance_cache_mb':'bad'}),1024)

    def test_gui_reserves_budget_without_tk_access(self):
        from dlss5tool.gui import App
        app=App.__new__(App)
        app._preview_runtime_settings={'preview_cache_mb':8192}
        self.assertEqual(app._preview_cache_bytes(),8192*1048576)
        app._preview_runtime_settings={'preview_cache_mb':512}
        self.assertEqual(app._preview_cache_bytes(),512*1048576)


@unittest.skipUnless(importlib.util.find_spec('torch'), 'requires Torch test environment')
class ModelCacheTests(unittest.TestCase):
    def model(self, budget=1, mode=3):
        import cv2
        import torch
        from dlss5tool.guidance_execution import execution_contract
        m=Models.__new__(Models)
        m.cv2,m.np,m.torch=cv2,np,torch
        m.settings={'guidance_mode':mode,'guidance_edge':128,'guidance_flow_direction':'backward'}
        m.mode=mode;m.device='cpu';m.flow_stream=m.depth_stream=None
        m.execution_info=execution_contract(m.settings,'cpu')
        m._closed=m._failed=False;m._process_lock=threading.Lock()
        m.prev=m.prev_thumb=m.prev_digest=m.depth_range=None
        m.raw_cache=RawGuidanceCache(budget*1048576)
        m.flow=object() if mode in (1,3) else None
        m.depth=object() if mode in (2,3) else None
        m._flow_input=mock.Mock(side_effect=lambda small:small)
        m._depth_input=mock.Mock(side_effect=lambda small,*_:small)
        m._infer_flow=mock.Mock(side_effect=lambda small:torch.full((1,2,128,128),float(small.mean()-m.prev.mean())))
        m._infer_depth=mock.Mock(side_effect=lambda small:torch.arange(128*128,dtype=torch.float32).reshape(128,128)/100+float(small.mean()))
        return m

    def test_replay_seek_partial_hit_and_eviction(self):
        frames=[np.full((128,128,4),n,np.uint8) for n in (10,20,30)]
        m=self.model(); baseline=[m.process(f,i==0) for i,f in enumerate(frames)]
        for i,f in enumerate(frames):
            result=m.process(f,i==0)
            np.testing.assert_array_equal(result[0],baseline[i][0])
            np.testing.assert_array_equal(result[1],baseline[i][1])
            self.assertEqual(m.last_metrics['depth_model_calls'],0)
            self.assertEqual(m.last_metrics['flow_model_calls'],0)
        self.assertEqual(m._infer_depth.call_count,3);self.assertEqual(m._infer_flow.call_count,2)
        # Seek can reuse raw depth but must reset normalization and clear motion.
        result=m.process(frames[2],True);control=self.model().process(frames[2],True)
        np.testing.assert_array_equal(result[1],control[1]);self.assertFalse(result[0].any())
        m.process(frames[0],False)  # new ordered pair, cached depth
        self.assertEqual(m.last_metrics['flow_model_calls'],1)
        self.assertEqual(m.last_metrics['depth_model_calls'],0)
        m.raw_cache.clear()
        m.process(frames[1],False)
        self.assertEqual(m.last_metrics['depth_model_calls'],1)
        m.close();self.assertEqual(m.raw_cache.bytes,0)

    def test_cut_inactive_mode_and_failure(self):
        for mode in (1,2,3):
            m=self.model(mode=mode)
            m.process(np.zeros((128,128,4),np.uint8),True)
            mv,dp,reset=m.process(np.full((128,128,4),255,np.uint8),False)
            self.assertTrue(reset);self.assertFalse(mv.any())
            if mode==1:self.assertFalse(dp.any())
        m=self.model();m.process(np.zeros((128,128,4),np.uint8),True)
        m._infer_depth.side_effect=RuntimeError('failure')
        with self.assertRaises(RuntimeError):m.process(np.ones((128,128,4),np.uint8),False)
        self.assertEqual(m.raw_cache.bytes,0)
        self.assertTrue(m._failed)


if __name__=='__main__':unittest.main()
