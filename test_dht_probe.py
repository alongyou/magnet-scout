import hashlib
import ipaddress
import json
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path

from dht_probe import DHTProbe, bencode, bloom_estimate, compact_endpoint, decode_contacts
from tracker_probe import bdecode, BencodeError


def packed(host, port):
    return ipaddress.ip_address(host).packed + struct.pack('!H', port)


def bloom_for(addresses):
    value = bytearray(256)
    for address in addresses:
        digest = hashlib.sha1(ipaddress.ip_address(address).packed).digest()
        for index in (int.from_bytes(digest[:2], 'little') % 2048, int.from_bytes(digest[2:4], 'little') % 2048):
            value[index // 8] |= 1 << (index % 8)
    return bytes(value)


class LocalNode:
    def __init__(self, response):
        self.response = response
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('127.0.0.1', 0))
        self.socket.settimeout(.1)
        self.endpoint = self.socket.getsockname()
        self.stop = threading.Event()
        self.requests = []
        self.worker = threading.Thread(target=self.serve)
        self.worker.start()

    def serve(self):
        while not self.stop.is_set():
            try:
                data, address = self.socket.recvfrom(65535)
            except socket.timeout:
                continue
            request = bdecode(data)
            self.requests.append(request)
            payload = self.response(request)
            self.socket.sendto(bencode(payload), address)

    def close(self):
        self.stop.set()
        self.worker.join(1)
        self.socket.close()


class DHTProbeTests(unittest.TestCase):
    def test_iterative_lookup_deduplicates_peers_and_merges_seed_filters(self):
        second = LocalNode(lambda request: {b't': request[b't'], b'y': b'r', b'r': {
            b'id': b'b' * 20, b'values': [packed('8.8.8.8', 6881), packed('8.8.8.8', 6882)],
            b'BFsd': bloom_for(['8.8.8.8', '1.1.1.1'])}})
        first = LocalNode(lambda request: {b't': request[b't'], b'y': b'r', b'r': {
            b'id': b'a' * 20, b'nodes': b'b' * 20 + packed(*second.endpoint),
            b'values': [packed('8.8.8.8', 6881), packed('8.8.8.8', 6881)],
            b'BFsd': bloom_for(['8.8.8.8'])}})
        try:
            with tempfile.TemporaryDirectory() as directory:
                bootstrap = Path(directory) / 'bootstrap.txt'
                bootstrap.write_text(f'{first.endpoint[0]}:{first.endpoint[1]}\n')
                cache = Path(directory) / 'nodes.json'
                engine = DHTProbe(bootstrap, cache, timeout=1, max_queries=8, allow_private=True)
                updates = []
                result = engine.lookup(bytes(20), updates.append)
                self.assertEqual(result['peer_count'], 2, 'same endpoint counts once, different ports count separately')
                self.assertEqual(result['nodes_responded'], 2)
                self.assertEqual(result['queries_completed'], 2)
                self.assertEqual(result['seeders_estimate'], 2, 'overlapping bloom filters are ORed, not summed')
                self.assertTrue(result['done'])
                self.assertEqual(result['status'], 'ok')
                self.assertTrue(any(value['queries_completed'] == 1 and not value['done'] for value in updates))
                self.assertEqual(first.requests[0][b'q'], b'get_peers')
                self.assertEqual(first.requests[0][b'ro'], 1)
                self.assertEqual(first.requests[0][b'a'][b'scrape'], 1)
                self.assertEqual(first.requests[0][b'a'][b'info_hash'], bytes(20))
                saved = json.loads(cache.read_text())
                self.assertEqual(len(saved['nodes']), 2)
                self.assertEqual(len(DHTProbe(bootstrap, cache, allow_private=True).contacts), 2)
        finally:
            first.close()
            second.close()

    def test_wrong_transaction_and_no_scrape_support_do_not_invent_seeds(self):
        for wrong in (True, False):
            node = LocalNode(lambda request: {b't': b'wrong' if wrong else request[b't'], b'y': b'r', b'r': {
                b'id': b'a' * 20, b'values': [packed('8.8.8.8', 6881)]}})
            try:
                with tempfile.TemporaryDirectory() as directory:
                    bootstrap = Path(directory) / 'bootstrap.txt'
                    bootstrap.write_text(f'{node.endpoint[0]}:{node.endpoint[1]}\n')
                    result = DHTProbe(bootstrap, None, timeout=.5, max_queries=1).lookup(bytes(20))
                    self.assertIsNone(result['seeders_estimate'])
                    self.assertEqual(result['peer_count'], 0 if wrong else 1)
                    self.assertEqual(result['status'], 'no_response' if wrong else 'ok')
                    self.assertEqual(result['queries_completed'], 1)
            finally:
                node.close()

    def test_timeouts_finish_and_respect_query_budget(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1', 0))
        try:
            with tempfile.TemporaryDirectory() as directory:
                bootstrap = Path(directory) / 'bootstrap.txt'
                bootstrap.write_text(f'127.0.0.1:{sock.getsockname()[1]}\n')
                result = DHTProbe(bootstrap, None, timeout=.05, query_timeout=.05, max_queries=1).lookup(bytes(20))
                self.assertTrue(result['done'])
                self.assertEqual(result['status'], 'no_response')
                self.assertEqual(result['queries_sent'], 1)
                self.assertEqual(result['queries_completed'], 1)
        finally:
            sock.close()

    def test_bep33_reference_vector_estimate(self):
        addresses = [str(ipaddress.ip_address(int(ipaddress.ip_address('192.0.2.0')) + n)) for n in range(256)]
        addresses += [str(ipaddress.ip_address(int(ipaddress.ip_address('2001:db8::')) + n)) for n in range(1000)]
        self.assertEqual(bloom_for(addresses)[:8].hex().upper(), 'F6C3F5EAA07FFD91')
        self.assertEqual(bloom_estimate(bloom_for(addresses)), 1225)
        self.assertEqual(bloom_estimate(bytes(256)), 0)
        self.assertIsNone(bloom_estimate(bytes([255]) * 256))

    def test_ipv6_and_invalid_contact_decoding(self):
        self.assertEqual(compact_endpoint(packed('2606:4700:4700::1111', 6881)), ('2606:4700:4700::1111', 6881))
        self.assertIsNone(compact_endpoint(packed('8.8.8.8', 0)))
        self.assertIsNone(compact_endpoint(packed('127.0.0.1', 6881)))
        node = b'n' * 20 + packed('2606:4700:4700::1111', 6881)
        self.assertEqual(len(decode_contacts(node, ipv6=True)), 1)
        self.assertEqual(decode_contacts(node + b'x', ipv6=True), [])

    def test_malformed_bencode_is_bounded(self):
        for data in [b'9:short', b'letrailing', b'l' * 34 + b'e' * 34, b'di1ei2ee']:
            with self.assertRaises((BencodeError, ValueError)):
                bdecode(data)


if __name__ == '__main__':
    unittest.main()
