#!/usr/bin/env python3
"""Build installable userscript and metadata from the canonical source."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='fail if generated files are outdated')
    args = parser.parse_args()
    source = (ROOT / 'magnet_checker.js').read_text(encoding='utf-8')
    marker = '// ==/UserScript=='
    if not source.startswith('// ==UserScript==') or marker not in source:
        parser.error('source must start with a valid userscript header')
    metadata = source[:source.index(marker) + len(marker)] + '\n'
    outputs = {'magnet-scout.user.js': source, 'magnet-scout.meta.js': metadata}
    for name, content in outputs.items():
        path = ROOT / name
        if args.check:
            if not path.exists() or path.read_text(encoding='utf-8') != content:
                print(f'Outdated: {name}; run python scripts/build_userscript.py', file=sys.stderr)
                return 1
        else:
            path.write_text(content, encoding='utf-8')
    print('Userscript release files are up to date' if args.check else 'Built userscript and update metadata')
    return 0


if __name__ == '__main__':
    sys.exit(main())
