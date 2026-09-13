"""Ensure diagnostic build splitting does not mix the two candidate changes."""
import unittest
from scripts.native_isolation_probe import once, variant_source


class NativeIsolationSourceTests(unittest.TestCase):
    def test_fail_closed_when_source_shape_changes(self):
        with self.assertRaises(RuntimeError):
            once('one one', 'one', 'two')
        self.assertEqual(once('one', 'one', 'two'), 'two')

    def test_depth_only_preserves_three_slots_and_omits_queue_budget(self):
        source = variant_source('depth')
        self.assertIn('constexpr int kMaxSlots = 3;', source)
        self.assertIn('bool UsesDepth()', source)
        self.assertNotIn('g_budget_adapter', source)
        self.assertNotIn('dlssnr_queue_capacity', source)

    def test_queue_only_omits_optional_depth(self):
        source = variant_source('queue')
        self.assertIn('host_queue::kMaxSlots', source)
        self.assertIn('dlssnr_queue_capacity', source)
        self.assertNotIn('bool UsesDepth()', source)
        self.assertNotIn('result |= 8;', source)

    def test_wait_control_changes_only_submission_wait(self):
        source, waiting = variant_source('combined'), variant_source('wait')
        self.assertEqual(source.replace('    if (!SubmitCommands(slot, false))\n',
                                        '    if (!SubmitCommands(slot, true))\n'), waiting)


if __name__ == '__main__':
    unittest.main()
