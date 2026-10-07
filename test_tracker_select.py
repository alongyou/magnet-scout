import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tracker_select as selector
import tracker_probe


class TrackerSelectTests(unittest.TestCase):
    def test_magnet_hashes_are_normalized_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'magnets.txt'
            path.write_text('# examples\n' + '00' * 20 + '\nmagnet:?xt=urn:btih:' + 'A' * 32 + '\n')
            self.assertEqual(selector.load_hashes(path), [bytes(20)])
            path.write_text('not-a-hash\n')
            with self.assertRaisesRegex(ValueError, ':1:'):
                selector.load_hashes(path)

    def test_info_hash_uses_original_bytes(self):
        # Intentionally unsorted keys: re-encoding would change the hash.
        info = b'd4:name4:test6:lengthi1ee'
        torrent = b'd8:announce3:udp4:info' + info + b'e'
        self.assertEqual(selector.torrent_info_bytes(torrent), info)
        self.assertEqual(hashlib.sha1(selector.torrent_info_bytes(torrent)).hexdigest(), hashlib.sha1(info).hexdigest())

    def test_quality_depends_on_reliability_and_latency(self):
        hashes = [bytes(20), bytes([1]) * 20]
        ok = {'status': 'ok', 'seeders': 0, 'leechers': 0, 'latency_ms': 100}
        with patch.object(selector, 'probe_tracker', return_value=ok):
            result = selector.benchmark('udp://example:80', hashes, 2, 3, .8, 2000)
        self.assertTrue(result['qualified'], 'zero swarm counts do not mean a bad tracker')
        self.assertEqual(len(result['trials']), 4)
        with patch.object(selector, 'probe_tracker', side_effect=[ok, ok, ok, {'status': 'error'}]):
            self.assertFalse(selector.benchmark('udp://example:80', hashes, 2, 3, .8, 2000)['qualified'])
        with patch.object(selector, 'probe_tracker', return_value={**ok, 'latency_ms': 2500}):
            self.assertFalse(selector.benchmark('udp://example:80', hashes, 2, 3, .8, 2000)['qualified'])
        self.assertEqual(selector.benchmark('wss://example', hashes, 2, 3, .8, 2000)['reason'], 'unsupported protocol')

    def test_downloads_all_lists_from_same_commit_and_tests_all_only(self):
        entries = [{'name': name, 'type': 'file'} for name in ('trackers_all.txt', 'trackers_best.txt', 'README.md')]
        def download(url):
            if '/commits/' in url:
                return b'{"sha":"pinned"}'
            if '/contents?' in url:
                return json.dumps(entries).encode()
            self.assertIn('/pinned/', url)
            return b'udp://all:80\n' if url.endswith('trackers_all.txt') else b'udp://best:80\n'
        with tempfile.TemporaryDirectory() as directory, patch.object(selector, 'download', side_effect=download):
            values, source = selector.fetch_lists(Path(directory))
            self.assertEqual(values, ['udp://all:80'])
            self.assertEqual(source['files'], ['trackers_all.txt', 'trackers_best.txt'])
            self.assertTrue((Path(source['directory']) / 'trackers_best.txt').exists())

    def test_empty_selection_preserves_output_and_good_selection_backs_up(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'trackers.txt'
            output.write_text('udp://original:80\n')
            with self.assertRaises(ValueError):
                selector.save_selection(output, [], 'test')
            self.assertEqual(output.read_text(), 'udp://original:80\n')
            selector.save_selection(output, [{'tracker': 'udp://new:80'}], 'test')
            self.assertEqual(output.read_text(), 'udp://new:80\n')
            self.assertEqual((output.parent / 'trackers.txt.test.bak').read_text(), 'udp://original:80\n')

    def test_http_scrape_rejects_wrong_hash(self):
        # A tracker must not report a different torrent as the requested one.
        data = b'd5:filesd20:' + b'x' * 20 + b'd8:completei3e10:incompletei4eeee'
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return data
        with patch.object(tracker_probe.urllib.request, 'urlopen', return_value=Response()):
            result = tracker_probe.probe_http('http://example/announce', bytes(20), 1)
        self.assertEqual(result['status'], 'error')
        self.assertIn('info_hash missing', result['error'])


if __name__ == '__main__':
    unittest.main()
