"""Research-only candidates: no production path is enabled by these tests."""
import unittest
import importlib.util
from scripts.gpu_pipeline_candidates import ResidentInputs


class Input:
    def __init__(self, value):
        self.value = value

    def to(self, device):
        return (self.value, device)


class ResidentInputTests(unittest.TestCase):
    def test_reuses_previous_in_both_directions(self):
        for forward in (False, True):
            cache = ResidentInputs('test-device')
            frames = [Input(i) for i in range(5)]
            for previous, current in zip(frames, frames[1:]):
                pair = (previous, current) if forward else (current, previous)
                self.assertEqual(cache.pair(pair), tuple((v.value, 'test-device') for v in pair))
                self.assertEqual(len(cache.entries), 2)
            self.assertEqual(cache.uploads, 5)
            self.assertEqual(cache.hits, 3)

    def test_different_objects_and_skips_do_not_reuse_stale_data(self):
        cache = ResidentInputs('test-device')
        a, b, c, d = (Input(i) for i in range(4))
        cache.pair((a, b))
        self.assertEqual(cache.pair((c, d)), ((2, 'test-device'), (3, 'test-device')))
        # Equal values do not authorize reuse; identity is the contract.
        cache.pair((Input(2), Input(3)))
        self.assertEqual(cache.uploads, 6)
        cache.clear()
        self.assertEqual(cache.entries, [])
        cache.pair((c, d))
        self.assertEqual(cache.uploads, 8)

    def test_same_frame_pair_only_uploads_once(self):
        cache, a = ResidentInputs('test-device'), Input(1)
        self.assertEqual(cache.pair((a, a)), ((1, 'test-device'),) * 2)
        self.assertEqual(cache.uploads, 1)
        self.assertEqual(len(cache.entries), 1)


@unittest.skipUnless(importlib.util.find_spec('torch'), 'existing CUDA environment required')
class ResidentTensorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import torch
        if not torch.cuda.is_available():raise unittest.SkipTest('CUDA required')
        cls.torch=torch

    def test_bitwise_reuse_shapes_direction_and_clear(self):
        torch=self.torch
        cache=ResidentInputs('cuda')
        previous=torch.arange(3*8*16,dtype=torch.float32).reshape(1,3,8,16)/255
        for shape in ((1,3,8,16),(1,3,8,16),(1,3,16,8),(1,3,8,16)):
            current=torch.arange(3*8*16,dtype=torch.float32).reshape(shape)/127
            for sources in ((current,previous),(previous,current)):
                actual=cache.pair(sources)
                for src,gpu in zip(sources,actual):
                    self.assertTrue(gpu.is_cuda)
                    self.assertEqual(gpu.dtype,torch.float32)
                    self.assertTrue(torch.equal(src,gpu.cpu()))
            previous=current
        cache.clear()
        self.assertEqual(cache.entries,[])

    def test_hdr_endpoints_do_not_recompute(self):
        from scripts.gpu_pipeline_candidates import compose_hdr
        torch=self.torch
        a=torch.linspace(0,1,256,device='cuda').half().reshape(8,8,4)
        b=1-a
        for profile in ('hdr10_pq','hdr10_hlg'):
            self.assertIs(compose_hdr(a,b,0,profile),a)
            self.assertIs(compose_hdr(a,b,1,profile),b)


if __name__ == '__main__':
    unittest.main()
