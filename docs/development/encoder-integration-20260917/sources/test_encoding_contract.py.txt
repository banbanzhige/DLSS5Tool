import unittest
from dlss5tool.encoding_contract import frame_encoding_contract


class EncodingContractTests(unittest.TestCase):
    def test_sdr_keeps_existing_wire_and_yuv_conversion(self):
        c=frame_encoding_contract(321,181,30,322,182)
        self.assertEqual(c.wire_format,'bgr24')
        self.assertEqual(c.encoder_format,'yuv420p')
        self.assertEqual(c.filter_chain,'pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p')
        self.assertEqual(c.input_args(),['-f','rawvideo','-pixel_format','bgr24','-video_size','321x181',
            '-framerate','30','-i','pipe:0','-an','-vf',c.filter_chain])

    def test_resize_is_not_silently_moved_or_changed(self):
        c=frame_encoding_contract(320,180,60,640,360,True)
        self.assertEqual(c.filter_chain,'scale=640:360:flags=lanczos,format=yuv420p')

    def test_hdr_keeps_pq_hlg_metadata_and_integer_wire(self):
        for transfer in ('smpte2084','arib-std-b67'):
            meta=dict(color_transfer=transfer,color_space='bt2020nc',color_primaries='bt2020')
            for resized in (False,True):
                c=frame_encoding_contract(320,180,24,640,360,resized,meta)
                self.assertEqual((c.wire_format,c.encoder_format),('rgba64le','p010le'))
                self.assertIn(f'transferin={transfer}:transfer={transfer}',c.filter_chain)
                self.assertTrue(c.filter_chain.endswith('rangein=full:range=limited,format=p010le'))
                self.assertEqual(c.output_color_args,('-color_range','tv','-color_primaries','bt2020',
                    '-color_trc',transfer,'-colorspace','bt2020nc'))

    def test_reject_invalid_dimensions_and_rate(self):
        for rate in (0,-1,float('nan'),float('inf')):
            with self.assertRaises(ValueError):frame_encoding_contract(320,180,rate,320,180)
        with self.assertRaises(ValueError):frame_encoding_contract(0,180,24,320,180)


if __name__=='__main__':unittest.main()
