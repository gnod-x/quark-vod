import pathlib
import subprocess
import sys
import tempfile
import unittest


class SyncCheckTests(unittest.TestCase):
    def test_drift_returns_failure_and_does_not_modify(self):
        with tempfile.TemporaryDirectory() as t:
            source, dest = pathlib.Path(t)/'src', pathlib.Path(t)/'dst'
            source.mkdir(); dest.mkdir()
            (source/'a.py').write_text('a=1')
            (dest/'a.py').write_text('a=2')
            cmd = [sys.executable, str(pathlib.Path(__file__).with_name('check_sync.py')), '--source', str(source), '--destination', str(dest)]
            r = subprocess.run(cmd, capture_output=True)
            self.assertEqual(r.returncode, 1)
            self.assertEqual((dest/'a.py').read_text(), 'a=2')
            (dest/'a.py').write_text('a=1')
            self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            (dest/'extra.py').write_text('')
            self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
