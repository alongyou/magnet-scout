import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import container_entrypoint


class ContainerEntrypointTests(unittest.TestCase):
    def test_initializes_defaults_and_preserves_custom_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(container_entrypoint, 'DATA_DIR', root), \
                    patch.dict(os.environ, {'MAGNET_SCOUT_INIT_TRACKERS': 'false'}):
                container_entrypoint.initialize()
                self.assertTrue((root / 'magnets.txt').exists())
                self.assertTrue((root / 'dht_bootstrap.txt').exists())
                self.assertFalse((root / 'trackers.txt').exists())
                (root / 'magnets.txt').write_text('custom test hash\n')
                (root / 'trackers.txt').write_text('udp://custom:80\n')
                container_entrypoint.initialize()
                self.assertEqual((root / 'magnets.txt').read_text(), 'custom test hash\n')
                self.assertEqual((root / 'trackers.txt').read_text(), 'udp://custom:80\n')

    def test_download_failure_does_not_block_api_startup(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(container_entrypoint, 'DATA_DIR', Path(directory)), \
                patch.dict(os.environ, {'MAGNET_SCOUT_INIT_TRACKERS': 'true'}), \
                patch.object(container_entrypoint.urllib.request, 'urlopen', side_effect=OSError('offline')):
            container_entrypoint.initialize()
            self.assertFalse((Path(directory) / 'trackers.txt').exists())
            self.assertTrue((Path(directory) / 'dht_bootstrap.txt').exists())


if __name__ == '__main__':
    unittest.main()
