import unittest
import numpy as np
from scripts.encode_recheck import compare
from scripts.encode_acceptance_fg import timeline_events


class RecheckEvidenceTests(unittest.TestCase):
    def test_hdr_error_does_not_use_8bit_psnr_peak(self):
        a=np.array([.1,.2],np.float16);b=a.copy();b[0]=.125
        result=compare(a,b)
        self.assertFalse(result['equal'])
        self.assertIsNone(result['psnr_db'])
        self.assertGreater(result['max_abs'],0)

    def test_8bit_difference_keeps_psnr(self):
        result=compare(np.array([0,255],np.uint8),np.array([1,254],np.uint8))
        self.assertEqual(result['mae'],1)
        self.assertAlmostEqual(result['psnr_db'],48.1308036087)

    def test_16_source_frames_produce_32_actual_timeline_entries(self):
        events=timeline_events(16,2,(8,))
        self.assertEqual(len(events),32)
        self.assertEqual(sum(e['kind']=='original' for e in events),16)
        self.assertEqual(sum(e['kind']=='generated' for e in events),14)
        self.assertEqual(sum(e['kind']=='hold' for e in events),2)


if __name__=='__main__':unittest.main()
