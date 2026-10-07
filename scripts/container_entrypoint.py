#!/usr/bin/env python3
"""Initialize persistent defaults, then replace this process with the command."""
import os
from pathlib import Path
import shutil
import sys
import urllib.request

# This script is also testable outside the container.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paths import DATA_DIR, ROOT


def initialize():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name in ('magnets.txt', 'dht_bootstrap.txt'):
        destination = DATA_DIR / name
        if not destination.exists():
            shutil.copyfile(ROOT / name, destination)
    tracker_file = DATA_DIR / 'trackers.txt'
    if not tracker_file.exists() and os.environ.get('MAGNET_SCOUT_INIT_TRACKERS', 'true').lower() == 'true':
        url = 'https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_all.txt'
        request = urllib.request.Request(url, headers={'User-Agent': 'MagnetScout/4.2.0'})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                content = response.read(1024 * 1024).decode('utf-8')
            from tracker_probe import unique_keep_order
            from tracker_select import atomic_write
            values = unique_keep_order(line for line in content.splitlines()
                                       if line.strip() and not line.lstrip().startswith('#'))
            if not values or any(not line.startswith(('udp://', 'http://', 'https://', 'ws://', 'wss://')) for line in values):
                raise ValueError('tracker source returned an invalid list')
            atomic_write(tracker_file, '# Initial candidates; run tracker_select.py to benchmark.\n' + '\n'.join(values) + '\n')
            print(f'Initialized {len(values)} candidate trackers at {tracker_file}', flush=True)
        except (OSError, ValueError) as exc:
            print(f'Tracker initialization skipped: {exc}. API can still use magnet trackers and DHT.', file=sys.stderr, flush=True)


if __name__ == '__main__':
    initialize()
    if len(sys.argv) < 2:
        raise SystemExit('A command is required')
    os.execvp(sys.argv[1], sys.argv[1:])
