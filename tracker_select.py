#!/usr/bin/env python3
"""Download public tracker lists, benchmark scrape support, save ranked trackers."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from statistics import median
import sys
import tempfile
import urllib.parse
import urllib.request

from tracker_probe import bdecode, info_hash_bytes, probe_tracker, unique_keep_order

ROOT = Path(__file__).resolve().parent
REPO = "ngosang/trackerslist"
PUBLIC_RELEASES = ("https://releases.ubuntu.com/24.04/", "https://releases.ubuntu.com/26.04/")


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "MagnetScout/4.1.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError(f"download too large: {url}")
    return data


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write(text)
    temporary.replace(path)


def torrent_info_bytes(data: bytes) -> bytes:
    """Locate the original encoded info value; never hash re-encoded metadata."""
    def end_at(offset):
        token = data[offset:offset + 1]
        if token == b"i":
            return data.index(b"e", offset + 1) + 1
        if token in (b"d", b"l"):
            cursor = offset + 1
            while data[cursor:cursor + 1] != b"e":
                cursor = end_at(cursor)
            return cursor + 1
        if token.isdigit():
            colon = data.index(b":", offset)
            end = colon + 1 + int(data[offset:colon])
            if end > len(data):
                raise ValueError("truncated torrent")
            return end
        raise ValueError("invalid torrent encoding")

    if not data.startswith(b"d"):
        raise ValueError("torrent root must be a dictionary")
    cursor = 1
    while data[cursor:cursor + 1] != b"e":
        key_end = end_at(cursor)
        key = bdecode(data[cursor:key_end])
        value_end = end_at(key_end)
        if key == b"info":
            return data[key_end:value_end]
        cursor = value_end
    raise ValueError("torrent has no info dictionary")


def refresh_magnets(path: Path, cache: Path):
    lines = ["# Public Ubuntu test resources; one magnet or BTIH per line.",
             "# Generated from official .torrent metadata; no ISO payload downloaded."]
    samples = []
    for release in PUBLIC_RELEASES:
        listing = download(release).decode("utf-8")
        filenames = re.findall(r'href="(ubuntu-[0-9.]+-(?:desktop|live-server)-amd64\.iso\.torrent)"', listing)
        for flavor in ("desktop", "live-server"):
            matches = [name for name in filenames if f"-{flavor}-" in name]
            if not matches:
                raise ValueError(f"no {flavor} torrent found at {release}")
            filename = max(matches, key=lambda name: tuple(map(int, name.split("-")[1].split("."))))
            source = urllib.parse.urljoin(release, filename)
            payload = download(source)
            info = torrent_info_bytes(payload)
            ih = hashlib.sha1(info).hexdigest().upper()
            metadata = bdecode(payload)
            title = bdecode(info)[b"name"].decode("utf-8", "replace")
            trackers = []
            if b"announce" in metadata:
                trackers.append(metadata[b"announce"].decode())
            for tier in metadata.get(b"announce-list", []):
                trackers.extend(value.decode() for value in tier)
            query = [("xt", f"urn:btih:{ih}"), ("dn", title)]
            query.extend(("tr", value) for value in unique_keep_order(trackers))
            magnet = "magnet:?" + urllib.parse.urlencode(query)
            lines.extend([f"# {title}", f"# Source: {source}", magnet, ""])
            samples.append({"name": title, "info_hash": ih, "source": source})
            cache.mkdir(parents=True, exist_ok=True)
            (cache / filename).write_bytes(payload)
    atomic_write(path, "\n".join(lines).rstrip() + "\n")
    atomic_write(cache / "magnets_sources.json", json.dumps(samples, indent=2) + "\n")
    print(f"Saved {len(samples)} official test magnets to {path}", flush=True)


def load_hashes(path: Path) -> list[bytes]:
    hashes = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if value.lower().startswith("magnet:?"):
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(value).query)
            value = next((xt[9:] for xt in params.get("xt", []) if xt.lower().startswith("urn:btih:")), "")
        elif value.lower().startswith("urn:btih:"):
            value = value[9:]
        try:
            ih = info_hash_bytes(value)
            if len(ih) != 20:
                raise ValueError("expected 20 bytes")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{path}:{number}: invalid magnet or hash") from exc
        if ih not in hashes:
            hashes.append(ih)
    if not hashes:
        raise ValueError(f"no test hashes in {path}")
    return hashes


def fetch_lists(cache: Path) -> tuple[list[str], dict]:
    commit = json.loads(download(f"https://api.github.com/repos/{REPO}/commits/master"))["sha"]
    entries = json.loads(download(f"https://api.github.com/repos/{REPO}/contents?ref={commit}"))
    names = [entry["name"] for entry in entries if entry["type"] == "file"
             and entry["name"].startswith("trackers_") and entry["name"].endswith(".txt")]
    if "trackers_all.txt" not in names:
        raise ValueError("repository has no trackers_all.txt")
    destination = cache / commit
    destination.mkdir(parents=True, exist_ok=True)
    texts = {}
    # All lists come from the same commit; partial downloads never update the output.
    for name in sorted(names):
        text = download(f"https://raw.githubusercontent.com/{REPO}/{commit}/{name}").decode("utf-8")
        atomic_write(destination / name, text)
        texts[name] = text
    values = unique_keep_order(line for line in texts["trackers_all.txt"].splitlines()
                               if line.strip() and not line.lstrip().startswith("#"))
    return values, {"repository": REPO, "commit": commit, "files": sorted(names), "directory": str(destination)}


def benchmark(tracker: str, hashes: list[bytes], rounds: int, timeout: float,
              min_success: float, max_latency: float) -> dict:
    scheme = urllib.parse.urlsplit(tracker).scheme.lower()
    if scheme not in ("udp", "http", "https"):
        return {"tracker": tracker, "qualified": False, "reason": "unsupported protocol", "trials": []}
    trials = []
    for _ in range(rounds):
        for ih in hashes:
            try:
                result = probe_tracker(tracker, ih, timeout)
            except Exception as exc:
                result = {"status": "error", "error": str(exc)}
            trials.append({"info_hash": ih.hex().upper(), **result})
    good = [r for r in trials if r.get("status") == "ok"]
    ratio = len(good) / len(trials)
    latency = median([r["latency_ms"] for r in good]) if good else None
    active_hashes = {r["info_hash"] for r in good if (r.get("seeders") or 0) > 0 or (r.get("leechers") or 0) > 0}
    # Swarm counts are informational, not a server quality threshold.
    qualified = ratio >= min_success and latency is not None and latency <= max_latency
    reason = "qualified" if qualified else ("low success rate" if ratio < min_success else "high latency")
    return {"tracker": tracker, "qualified": qualified, "reason": reason,
            "success_rate": round(ratio, 4), "median_latency_ms": latency,
            "active_test_hashes": len(active_hashes), "trials": trials}


def save_selection(output: Path, qualified: list[dict], stamp: str):
    if not qualified:
        raise ValueError("No trackers qualified; existing trackers.txt was preserved. Inspect tracker_report.json.")
    if output.exists():
        backup = output.with_name(f"{output.name}.{stamp}.bak")
        backup.write_bytes(output.read_bytes())
    atomic_write(output, "\n".join(item["tracker"] for item in qualified) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--magnets", type=Path, default=ROOT / "magnets.txt")
    parser.add_argument("--output", type=Path, default=ROOT / "trackers.txt")
    parser.add_argument("--cache", type=Path, default=ROOT / "tracker_cache")
    parser.add_argument("--report", type=Path, default=ROOT / "tracker_report.json")
    parser.add_argument("--refresh-magnets", action="store_true", help="refresh samples from official Ubuntu torrents first")
    parser.add_argument("--refresh-magnets-only", action="store_true")
    parser.add_argument("--input", type=Path, help="use a local all-list instead of downloading the repository")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=3)
    parser.add_argument("--min-success", type=float, default=0.8)
    parser.add_argument("--max-latency-ms", type=float, default=2000)
    parser.add_argument("--limit", type=int, default=0, help="maximum output trackers; 0 saves all qualified trackers")
    args = parser.parse_args(argv)
    if (args.rounds < 1 or args.workers < 1 or args.timeout <= 0 or args.max_latency_ms <= 0
            or not 0 < args.min_success <= 1 or args.limit < 0
            or not math.isfinite(args.timeout) or not math.isfinite(args.max_latency_ms)):
        parser.error("invalid benchmark thresholds")
    if args.refresh_magnets or args.refresh_magnets_only:
        refresh_magnets(args.magnets, args.cache / "public_torrents")
    if args.refresh_magnets_only:
        return
    hashes = load_hashes(args.magnets)
    if args.input:
        candidates = unique_keep_order(line for line in args.input.read_text().splitlines()
                                       if line.strip() and not line.lstrip().startswith("#"))
        source = {"input": str(args.input)}
    else:
        candidates, source = fetch_lists(args.cache)
    if not candidates:
        raise ValueError("candidate list is empty; output preserved")
    print(f"Testing {len(candidates)} trackers × {len(hashes)} hashes × {args.rounds} rounds", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(benchmark, tracker, hashes, args.rounds, args.timeout,
                                   args.min_success, args.max_latency_ms): tracker for tracker in candidates}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"[{len(results)}/{len(candidates)}] {result['reason']}: {result['tracker']}", flush=True)
    results.sort(key=lambda r: (not r["qualified"], -r.get("success_rate", 0),
                               r.get("median_latency_ms") if r.get("median_latency_ms") is not None else float("inf"), r["tracker"]))
    qualified = [r for r in results if r["qualified"]]
    if args.limit:
        qualified = qualified[:args.limit]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "source": source,
              "test_hashes": [ih.hex().upper() for ih in hashes],
              "thresholds": {"rounds": args.rounds, "timeout": args.timeout,
                             "min_success": args.min_success, "max_latency_ms": args.max_latency_ms},
              "candidates": len(candidates), "selected": len(qualified), "results": results}
    atomic_write(args.report, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    save_selection(args.output, qualified, stamp)
    print(f"Saved {len(qualified)} trackers to {args.output}; report: {args.report}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
