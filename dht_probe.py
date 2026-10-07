#!/usr/bin/env python3
"""Bounded read-only Mainline DHT lookup with optional BEP 33 seed estimates."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import struct
import tempfile
from threading import Lock
import time
import urllib.parse

from tracker_probe import bdecode, info_hash_bytes
from paths import DATA_DIR

ROOT = Path(__file__).resolve().parent
DEFAULT_BOOTSTRAP = DATA_DIR / 'dht_bootstrap.txt'
DEFAULT_CACHE = DATA_DIR / 'dht_nodes.json'


def bencode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b':' + value
    if isinstance(value, int):
        return b'i' + str(value).encode() + b'e'
    if isinstance(value, list):
        return b'l' + b''.join(bencode(item) for item in value) + b'e'
    if isinstance(value, dict):
        return b'd' + b''.join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b'e'
    raise TypeError('unsupported bencode value')


def valid_endpoint(host, port, allow_private=False):
    try:
        address = ipaddress.ip_address(host)
        return (isinstance(port, int) and 1 <= port <= 65535 and
                not address.is_multicast and not address.is_unspecified and
                (allow_private or address.is_global))
    except ValueError:
        return False


def compact_endpoint(value, allow_private=False):
    if not isinstance(value, bytes) or len(value) not in (6, 18):
        return None
    host = str(ipaddress.ip_address(value[:-2]))
    port = struct.unpack('!H', value[-2:])[0]
    return (host, port) if valid_endpoint(host, port, allow_private) else None


def decode_contacts(data, ipv6=False, allow_private=False):
    width = 38 if ipv6 else 26
    if not isinstance(data, bytes) or len(data) % width:
        return []
    output = []
    for offset in range(0, len(data), width):
        node_id = data[offset:offset + 20]
        endpoint = compact_endpoint(data[offset + 20:offset + width], allow_private)
        if endpoint:
            output.append((endpoint, node_id))
    return output


def bloom_estimate(bloom):
    """BEP 33 estimate for a union of 2048-bit filters; no estimate if saturated."""
    if bloom is None:
        return None
    if len(bloom) != 256:
        raise ValueError('BEP 33 filters must be 256 bytes')
    ones = sum(byte.bit_count() for byte in bloom)
    if ones == 0:
        return 0
    if ones == 2048:
        return None
    estimate = math.log((2048 - ones) / 2048) / (2 * math.log(1 - 1 / 2048))
    return round(estimate) if estimate <= 6000 else None


def bootstrap_entries(path):
    entries = []
    for number, line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(), 1):
        value = line.strip()
        if not value or value.startswith('#'):
            continue
        parsed = urllib.parse.urlsplit('//' + value)
        try:
            if not parsed.hostname or not parsed.port or parsed.path:
                raise ValueError('expected host:port or [IPv6]:port')
            entries.append((parsed.hostname, parsed.port))
        except ValueError as exc:
            raise ValueError(f'{path}:{number}: invalid bootstrap endpoint') from exc
    return list(dict.fromkeys(entries))


class DHTProbe:
    def __init__(self, bootstrap_file=DEFAULT_BOOTSTRAP, cache_file=DEFAULT_CACHE,
                 timeout=12, query_timeout=1.5, max_queries=64, workers=8, allow_private=False):
        if timeout <= 0 or query_timeout <= 0 or max_queries < 1 or workers < 1:
            raise ValueError('DHT query limits must be positive')
        self.bootstrap_file = Path(bootstrap_file)
        self.cache_file = Path(cache_file) if cache_file else None
        self.timeout, self.query_timeout = timeout, query_timeout
        self.max_queries, self.workers = max_queries, workers
        self.allow_private = allow_private
        self.node_id = os.urandom(20)
        self.lock = Lock()
        self.contacts = {}
        self.resolved = {}
        if self.cache_file and self.cache_file.exists():
            try:
                saved = json.loads(self.cache_file.read_text(encoding='utf-8'))
                if time.time() - saved['saved_at'] < 86400:
                    for item in saved['nodes'][:256]:
                        endpoint = (item['host'], item['port'])
                        node_id = bytes.fromhex(item['id'])
                        if len(node_id) == 20 and valid_endpoint(*endpoint, allow_private):
                            self.contacts[endpoint] = node_id
            except (OSError, ValueError, TypeError, KeyError):
                pass

    def resolve(self, endpoint):
        with self.lock:
            cached = self.resolved.get(endpoint)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        addresses = []
        for _, _, _, _, addr in socket.getaddrinfo(*endpoint, socket.AF_UNSPEC, socket.SOCK_DGRAM):
            candidate = (addr[0], addr[1])
            # Explicit bootstrap configuration may resolve through a local UDP proxy
            # or refer to a private test node; only discovered contacts require global IPs.
            if valid_endpoint(*candidate, True) and candidate not in addresses:
                addresses.append(candidate)
        with self.lock:
            self.resolved[endpoint] = (time.monotonic(), addresses)
        return addresses

    def query(self, endpoint, info_hash, timeout):
        transaction = os.urandom(4)
        request = bencode({b't': transaction, b'y': b'q', b'q': b'get_peers', b'ro': 1,
                           b'a': {b'id': self.node_id, b'info_hash': info_hash, b'scrape': 1,
                                  b'want': [b'n4', b'n6']}})
        family = socket.AF_INET6 if ':' in endpoint[0] else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            # Connected UDP accepts packets only from the requested endpoint.
            sock.connect(endpoint)
            sock.send(request)
            data = sock.recv(65535)
        response = bdecode(data)
        if not isinstance(response, dict) or response.get(b't') != transaction:
            raise ValueError('invalid DHT transaction')
        if response.get(b'y') != b'r':
            raise ValueError('DHT node returned an error or non-response')
        value = response.get(b'r')
        if not isinstance(value, dict) or not isinstance(value.get(b'id'), bytes) or len(value[b'id']) != 20:
            raise ValueError('invalid DHT response node ID')
        return value

    def remember(self, contacts):
        with self.lock:
            self.contacts.update(contacts)
            self.contacts = dict(list(self.contacts.items())[-256:])
            if not self.cache_file:
                return ''
            payload = {'saved_at': time.time(), 'nodes': [
                {'host': host, 'port': port, 'id': node_id.hex()}
                for (host, port), node_id in self.contacts.items()]}
            temporary = None
            try:
                self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                                 dir=self.cache_file.parent, delete=False) as file:
                    temporary = Path(file.name)
                    json.dump(payload, file)
                temporary.replace(self.cache_file)
                return ''
            except OSError as exc:
                if temporary:
                    temporary.unlink(missing_ok=True)
                return str(exc)

    def lookup(self, info_hash, publish=lambda progress: None):
        if len(info_hash) != 20:
            raise ValueError('DHT lookup requires a 20-byte BTIH')
        started = time.monotonic()
        progress = {'status': 'checking', 'done': False, 'queries_sent': 0, 'queries_completed': 0,
                    'nodes_responded': 0, 'peer_count': 0, 'seeders_estimate': None,
                    'scrape_nodes': 0, 'elapsed_ms': 0, 'max_queries': self.max_queries, 'error': ''}
        peers, visited, good_contacts = set(), set(), {}
        seed_filter = None
        with self.lock:
            candidates = dict(self.contacts)
        def notify():
            progress['elapsed_ms'] = round((time.monotonic() - started) * 1000)
            publish(dict(progress))
        notify()
        resolution_errors = []
        # Resolve configured entry points. Normal DNS resolution may exceed the UDP query budget.
        for endpoint in bootstrap_entries(self.bootstrap_file):
            try:
                for address in self.resolve(endpoint):
                    candidates.setdefault(address, None)
            except OSError as exc:
                resolution_errors.append(str(exc))
        deadline = time.monotonic() + self.timeout
        target = int.from_bytes(info_hash, 'big')
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            while progress['queries_sent'] < self.max_queries and time.monotonic() < deadline:
                available = [(endpoint, node_id) for endpoint, node_id in candidates.items() if endpoint not in visited]
                if not available:
                    break
                available.sort(key=lambda item: int.from_bytes(item[1], 'big') ^ target if item[1] else -1)
                batch = available[:min(self.workers, self.max_queries - progress['queries_sent'])]
                futures = {}
                for endpoint, _ in batch:
                    visited.add(endpoint)
                    timeout = min(self.query_timeout, max(.01, deadline - time.monotonic()))
                    futures[executor.submit(self.query, endpoint, info_hash, timeout)] = endpoint
                    progress['queries_sent'] += 1
                for future in as_completed(futures):
                    progress['queries_completed'] += 1
                    try:
                        value = future.result()
                        progress['nodes_responded'] += 1
                        good_contacts[futures[future]] = value[b'id']
                        for endpoint, node_id in (decode_contacts(value.get(b'nodes', b''), allow_private=self.allow_private) +
                                                  decode_contacts(value.get(b'nodes6', b''), ipv6=True, allow_private=self.allow_private)):
                            if len(candidates) < 1024:
                                candidates.setdefault(endpoint, node_id)
                        values = value.get(b'values', [])
                        if isinstance(values, list):
                            for packed in values[:512]:
                                endpoint = compact_endpoint(packed, self.allow_private)
                                if endpoint and len(peers) < 512:
                                    peers.add(endpoint)
                        bloom = value.get(b'BFsd')
                        if isinstance(bloom, bytes) and len(bloom) == 256:
                            progress['scrape_nodes'] += 1
                            if seed_filter is None:
                                seed_filter = bytearray(256)
                            for index, byte in enumerate(bloom):
                                seed_filter[index] |= byte
                    except (OSError, ValueError, TypeError, KeyError, RecursionError):
                        pass
                    progress['peer_count'] = len(peers)
                    progress['seeders_estimate'] = bloom_estimate(seed_filter)
                    notify()
        warning = self.remember(good_contacts) if good_contacts else ''
        progress.update(done=True, status='ok' if progress['nodes_responded'] else 'no_response',
                        peer_limit_reached=len(peers) >= 512,
                        budget_exhausted=time.monotonic() >= deadline or progress['queries_sent'] >= self.max_queries)
        if not progress['nodes_responded']:
            progress['error'] = 'DHT 无有效响应；请检查 UDP 网络和引导节点'
            if resolution_errors:
                progress['error'] += '；DNS: ' + '; '.join(resolution_errors)
        if warning:
            progress['cache_warning'] = warning
        notify()
        return dict(progress)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('magnet_or_hash')
    parser.add_argument('--bootstrap', type=Path, default=DEFAULT_BOOTSTRAP)
    parser.add_argument('--cache', type=Path, default=DEFAULT_CACHE)
    parser.add_argument('--timeout', type=float, default=12)
    parser.add_argument('--max-queries', type=int, default=64)
    args = parser.parse_args()
    value = args.magnet_or_hash
    if value.lower().startswith('magnet:?'):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(value).query)
        value = next((xt[9:] for xt in query.get('xt', []) if xt.lower().startswith('urn:btih:')), '')
    engine = DHTProbe(args.bootstrap, args.cache, timeout=args.timeout, max_queries=args.max_queries)
    result = engine.lookup(info_hash_bytes(value))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
