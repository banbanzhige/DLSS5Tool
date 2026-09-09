import unittest
import numpy as np
import cv2
from scripts.flow_quality_probe import resize_flow


class FlowQualityTests(unittest.TestCase):
    def test_resize_displacement_axes(self):
        flow=np.full((16,24,2),(2.,-3.),dtype=np.float32)
        actual=resize_flow(flow,72,32)
        np.testing.assert_allclose(actual,np.broadcast_to((6.,-6.),actual.shape))
        np.testing.assert_allclose(flow,np.broadcast_to((2.,-3.),flow.shape))

    def test_backward_warp_translation(self):
        rng=np.random.default_rng(42)
        prev=rng.integers(0,255,(40,56,3),dtype=np.uint8)
        cur=cv2.warpAffine(prev,np.float32([[1,0,4],[0,1,2]]),(56,40))
        yy,xx=np.mgrid[:40,:56].astype(np.float32)
        warped=cv2.remap(prev,xx-4,yy-2,cv2.INTER_LINEAR)
        np.testing.assert_array_equal(cur[4:-4,6:-6],warped[4:-4,6:-6])


if __name__=='__main__':
    unittest.main()
