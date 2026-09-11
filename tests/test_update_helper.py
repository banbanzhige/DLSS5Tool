import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from dlss5tool import delta_update as delta, update_helper as helper


@unittest.skipUnless(os.name == 'nt', 'Windows helper integration')
class WindowsHelperTests(unittest.TestCase):
    def test_mutex_blocks_another_process(self):
        with tempfile.TemporaryDirectory() as folder:
            code = ('from dlss5tool.update_helper import installation_lock; '
                    'import sys;\nwith installation_lock(sys.argv[1]): pass')
            with helper.installation_lock(folder):
                result = subprocess.run([sys.executable, '-B', '-c', code, folder],
                                        capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'Another DLSS5Tool', result.stderr)
            result = subprocess.run([sys.executable, '-B', '-c', code, folder],
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_wait_parent_does_not_terminate_it(self):
        process = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(0.5)'])
        try:
            with self.assertRaises(delta.DeltaError):
                helper.wait_for_parent(process.pid, timeout_ms=1)
            self.assertIsNone(process.poll())
            helper.wait_for_parent(process.pid, timeout_ms=5000)
        finally:
            process.wait(timeout=10)

    def test_parent_timeout_returns_prepared_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'DLSS5Tool.exe').touch()
            (root / delta.TRANSACTION).mkdir()
            delta.set_state(root, 'ready', target='v3.0.1')
            with (mock.patch.object(helper, 'wait_for_parent', side_effect=delta.DeltaError('timeout')),
                  mock.patch.object(helper, 'notify')):
                self.assertEqual(helper.main(['--root', folder, '--parent-pid', '99999']), 1)
            self.assertEqual(delta.status(root)['phase'], 'ready')
            self.assertEqual(delta.status(root)['target'], 'v3.0.1')


if __name__ == '__main__':
    unittest.main()
