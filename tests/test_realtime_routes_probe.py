"""CPU tests for experimental raw prediction caching; no CUDA/production patch."""
import unittest
from unittest import mock
from types import SimpleNamespace
import numpy as np
from scripts.realtime_routes_probe import RawCache


class Tensor:
    def __init__(self,array): self.array=np.array(array,dtype=np.float32)
    def cpu(self): return self
    def numpy(self): return self.array


class FakeModel:
    def __init__(self):
        self.settings={'guidance_edge':720,'guidance_flow_direction':'backward','intensity':1}
        self.torch=SimpleNamespace(from_numpy=Tensor)
        self.flow_stream=self.depth_stream=None
        self.prev=None; self.bounds=None; self.depth_calls=0;self.flow_calls=0;self.prepares=0
    def _depth_input(self,frame,*unused): self.prepares+=1;return frame
    def _flow_input(self,frame): self.prepares+=1;return frame
    def _infer_depth(self,frame):
        self.depth_calls+=1
        return Tensor([frame.mean(),frame.mean()+10])
    def _infer_flow(self,frame):
        self.flow_calls+=1
        return Tensor([frame.mean()-self.prev.mean()])
    def process(self,frame,reset,outputs=None):
        reset=reset or self.prev is None
        flow=np.zeros(1) if reset else self._infer_flow(self._flow_input(frame)).numpy()
        raw=self._infer_depth(self._depth_input(frame)).numpy()
        self.bounds=raw.copy() if reset or self.bounds is None else .9*self.bounds+.1*raw
        depth=(raw-self.bounds[0])/(self.bounds[1]-self.bounds[0])
        self.prev=frame
        return flow.copy(),depth.copy(),reset


class TestRawCache(unittest.TestCase):
    def frames(self): return [np.full((8,8,4),i,dtype=np.uint8) for i in (1,4,9)]
    def test_replay_skips_network_and_preprocessing(self):
        model=FakeModel(); cache=RawCache(model); frames=self.frames()
        first=[model.process(f,i==0) for i,f in enumerate(frames)]
        self.assertEqual((model.depth_calls,model.flow_calls,model.prepares),(3,2,5))
        model.settings['intensity']=.55
        second=[model.process(f,i==0) for i,f in enumerate(frames)]
        self.assertEqual((model.depth_calls,model.flow_calls,model.prepares),(3,2,5))
        self.assertEqual(cache.hits,5)
        for a,b in zip(first,second):
            np.testing.assert_array_equal(a[0],b[0]);np.testing.assert_array_equal(a[1],b[1])
    def test_seek_recomputes_normalization_not_network(self):
        model=FakeModel(); cache=RawCache(model);frames=self.frames()
        for i,f in enumerate(frames):model.process(f,i==0)
        cached=model.process(frames[-1],True)
        control=FakeModel().process(frames[-1],True)
        np.testing.assert_array_equal(cached[0],control[0]);np.testing.assert_array_equal(cached[1],control[1])
        self.assertEqual((model.depth_calls,model.flow_calls),(3,2))
    def test_ordered_pair_and_changed_content_miss(self):
        model=FakeModel();cache=RawCache(model);a,b,c=self.frames()
        model.process(a,True);model.process(b,False)
        model.process(a,False)  # reversed pair must not reuse forward prediction
        self.assertEqual(model.flow_calls,2)
        changed=b.copy();changed[0,0,0]+=1
        model.process(changed,False)
        self.assertEqual((model.depth_calls,model.flow_calls),(3,3))
    def test_guidance_changes_reject_old_namespace(self):
        model=FakeModel();RawCache(model);model.process(self.frames()[0],True)
        model.settings['guidance_edge']=384
        with self.assertRaisesRegex(RuntimeError,'new model/cache'):model.process(self.frames()[0],True)
    def test_uncached_control_runs_models(self):
        model=FakeModel();cache=RawCache(model);frames=self.frames()
        for i,f in enumerate(frames):model.process(f,i==0)
        cache.enabled=False
        for i,f in enumerate(frames):model.process(f,i==0)
        self.assertEqual((model.depth_calls,model.flow_calls),(6,4))
        self.assertEqual(cache.hits,0)
    def test_indexed_key_separates_sources(self):
        model=FakeModel();cache=RawCache(model,indexed=True);frame=self.frames()[0]
        model.cache_frame_token=('source-a',0);model.process(frame,True)
        model.process(frame,True)
        self.assertEqual(model.depth_calls,1)
        model.cache_frame_token=('source-b',0);model.process(frame,True)
        self.assertEqual(model.depth_calls,2)
    def test_cached_percentiles_keep_temporal_normalization(self):
        import cv2
        model=FakeModel();model.np=np;model.cv2=cv2;model.depth_range=None
        cache=RawCache(model,indexed=True);cache.current='frame'
        raw=Tensor([[1,3],[5,10]])
        first=np.empty((2,2),np.float32)
        bounds=model._finish_depth(raw,first,True,2,2)
        model.depth_range=(2.,12.)
        expected=tuple(.9*a+.1*b for a,b in zip(model.depth_range,bounds))
        output=np.empty_like(first)
        with mock.patch.object(np,'percentile',side_effect=AssertionError('Repeated percentile')):
            actual=model._finish_depth(raw,output,False,2,2)
        self.assertEqual(expected,actual)
        np.testing.assert_array_equal(output,np.clip((raw.numpy()-expected[0])/(expected[1]-expected[0]),0,1))


if __name__=='__main__': unittest.main()
