import unittest
from fractions import Fraction as F

from dlss5tool.frame_timeline import (
    ColorContract, ProcessingPlan, SourceFrame, frame_group, iter_timeline,
    require_frame_group,
)


class FrameTimelineTests(unittest.TestCase):
    def frames(self, n=3, rate=F(30000, 1001), epoch=0):
        return [SourceFrame(i, F(i) / rate, 1 / rate, epoch=epoch) for i in range(n)]

    def test_exact_cfr_count_duration_and_endpoint(self):
        for m in (1, 2, 3, 4, 6):
            with self.subTest(multiplier=m):
                out = list(iter_timeline(self.frames(), m))
                self.assertEqual(len(out), 3 * m)
                self.assertEqual(sum(x.duration for x in out), F(3003, 30000))
                self.assertEqual(sum(x.kind == 'generated' for x in out), 2 * (m - 1))
                self.assertEqual(sum(x.kind == 'endpoint_hold' for x in out), m - 1)
                self.assertEqual([x.pts for x in out], [F(i * 1001, 30000 * m) for i in range(3 * m)])

    def test_vfr_uses_pts_not_average_fps(self):
        frames = [SourceFrame(0, F(3, 10), F(1, 24)),
                  SourceFrame(1, F(3, 10) + F(1, 24), F(1, 60))]
        out = list(iter_timeline(frames, 2))
        self.assertEqual([x.duration for x in out], [F(1, 48)] * 2 + [F(1, 120)] * 2)
        self.assertEqual(out[-1].pts + out[-1].duration, frames[-1].pts + frames[-1].duration)

    def test_cut_is_not_a_generated_frame(self):
        left, right, _ = self.frames()
        right = SourceFrame(right.index, right.pts, right.duration, scene=1)
        out = frame_group(left, right, 4)
        self.assertEqual([x.kind for x in out], ['real'] + ['cut_hold'] * 3)
        require_frame_group(out, 0, False, epoch=0)

    def test_segment_half_open_ownership_has_no_duplicates(self):
        frames = self.frames(7)
        full = list(iter_timeline(frames, 4))
        first = list(iter_timeline(frames[:4], 4, owned=(frames[0].pts, frames[3].pts)))
        second = list(iter_timeline(frames[2:], 4, owned=(frames[3].pts, frames[-1].pts + frames[-1].duration), final_segment=True))
        self.assertEqual(first + second, full)

    def test_nonfinal_segment_cannot_fabricate_endpoint_hold(self):
        frames = self.frames(4)
        with self.assertRaises(ValueError):
            list(iter_timeline(frames[:3], 4, owned=(frames[0].pts, frames[3].pts)))

    def test_owned_boundary_cannot_truncate_sample(self):
        frames = self.frames(4)
        for start, end in ((F(1, 10000), frames[2].pts), (frames[0].pts, frames[2].pts + F(1, 10000))):
            with self.subTest(interval=(start, end)), self.assertRaises(ValueError):
                list(iter_timeline(frames, 4, owned=(start, end)))

    def test_epoch_and_missing_frames_are_errors(self):
        a, b, c = self.frames()
        for other in (c, SourceFrame(1, b.pts, b.duration, epoch=1),
                      SourceFrame(1, b.pts + F(1, 100), b.duration)):
            with self.subTest(other=other), self.assertRaises(ValueError):
                frame_group(a, other, 2)

    def test_group_validation_is_strict(self):
        out = frame_group(*self.frames()[:2], 4)
        require_frame_group(out, 3, True, epoch=0)
        for count, valid, epoch in ((2, True, 0), (3, False, 0), (3, True, 1), (3, 1, 0)):
            with self.subTest(args=(count, valid, epoch)), self.assertRaises(ValueError):
                require_frame_group(out, count, valid, epoch=epoch)
        for malformed in (out[:2], out[::-1], out[1:]):
            with self.assertRaises(ValueError):
                require_frame_group(malformed, 3, True, epoch=0)

    def test_single_frame_and_empty_stream(self):
        self.assertEqual(list(iter_timeline([], 4)), [])
        self.assertEqual([x.kind for x in iter_timeline(self.frames(1), 3)], ['real', 'endpoint_hold', 'endpoint_hold'])

    def test_reject_invalid_values(self):
        for value in (True, 2.5, 0, 5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                list(iter_timeline(self.frames(), value))
        with self.assertRaises(ValueError):
            SourceFrame(0, .1, F(1, 30))
        with self.assertRaises(ValueError):
            SourceFrame(0, 0, 0)


class ProcessingPlanTests(unittest.TestCase):
    color = ColorContract('rgba8', 'srgb', 'bt709')

    def test_spatial_and_temporal_scales_are_independent(self):
        plan = ProcessingPlan((1920, 1080), 2, 4, self.color, source_rate=F(30000, 1001))
        self.assertEqual(plan.output_size, (3840, 2160))
        self.assertEqual(plan.output_rate, F(120000, 1001))
        self.assertLess(plan.stages.index('super_resolve_real_frames'), plan.stages.index('enhance_real_frames'))
        self.assertLess(plan.stages.index('enhance_real_frames'), plan.stages.index('dlssg_frame_group'))

    def test_resize_is_explicit_and_before_fg(self):
        plan = ProcessingPlan((1920, 1080), 2, 3, self.color, final_size=(2560, 1440))
        self.assertEqual(plan.upscaled_size, (3840, 2160))
        self.assertEqual(plan.output_size, (2560, 1440))
        self.assertLess(plan.stages.index('explicit_final_resize'), plan.stages.index('dlssg_frame_group'))

    def test_no_silent_8k_or_hdr_downgrade(self):
        hdr = ColorContract('rgba16f', 'pq', 'bt2020')
        plan = ProcessingPlan((1920, 1080), 4, 4, hdr)
        self.assertEqual(plan.output_size, (7680, 4320))
        with self.assertRaises(ValueError):
            plan.require_backend(sizes=[(3840, 2160)], multipliers=[2, 3, 4], colors=[hdr], video_inputs_verified=True)
        with self.assertRaises(ValueError):
            plan.require_backend(sizes=[plan.output_size], multipliers=[2, 3, 4], colors=[self.color], video_inputs_verified=True)

    def test_only_quality_verified_capabilities_allowed(self):
        plan = ProcessingPlan((1920, 1080), 1, 4, self.color)
        kwargs = dict(sizes=[plan.output_size], multipliers=[2, 3, 4], colors=[self.color])
        with self.assertRaises(ValueError):
            plan.require_backend(**kwargs, video_inputs_verified=False)
        plan.require_backend(**kwargs, video_inputs_verified=True)
        kwargs['multipliers'] = [2]
        with self.assertRaises(ValueError):
            plan.require_backend(**kwargs, video_inputs_verified=True)

    def test_invalid_contract_and_implicit_float_rate_rejected(self):
        with self.assertRaises(ValueError):
            ColorContract('rgba16f', 'linear', 'bt709')
        with self.assertRaises(ValueError):
            ProcessingPlan((1920, 1080), 2, 4, self.color, source_rate=29.97)
        with self.assertRaises(ValueError):
            ProcessingPlan((1920, 1080), 3, 4, self.color)


if __name__ == '__main__':
    unittest.main()
