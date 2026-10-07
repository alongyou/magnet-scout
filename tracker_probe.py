#!/usr/bin/env python3
"""
tracker_probe.py

Read JSON exported by magnet_collector.user.js and query HTTP/HTTPS/UDP
BitTorrent tracker scrape endpoints for each info_hash.

This is a swarm visibility probe. It does NOT download torrent payload data.
Seeder/leecher counts from different trackers can overlap and must not be summed
as if they were unique peers.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import random
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import median
from typing import Any


UDP_PROTOCOL_ID = 0x41727101980
USER_AGENT = "MagnetScout/4.1.1"


class BencodeError(ValueError):
    pass


def bdecode(data: bytes) -> Any:
    i = 0

    def parse():
        nonlocal i
        if i >= len(data):
            raise BencodeError("unexpected end of data")

        c = data[i:i+1]

        if c == b"i":
            i += 1
            end = data.index(b"e", i)
            n = int(data[i:end])
            i = end + 1
            return n

        if c == b"l":
            i += 1
            out = []
            while data[i:i+1] != b"e":
                out.append(parse())
            i += 1
            return out

        if c == b"d":
            i += 1
            out = {}
            while data[i:i+1] != b"e":
                k = parse()
                v = parse()
                out[k] = v
            i += 1
            return out

        if c.isdigit():
            colon = data.index(b":", i)
            length = int(data[i:colon])
            i = colon + 1
            out = data[i:i+length]
            i += length
            return out

        raise BencodeError(f"invalid bencode at offset {i}")

    result = parse()
    return result


def info_hash_bytes(value: str) -> bytes:
    value = value.strip()
    if len(value) == 40:
        return bytes.fromhex(value)
    if len(value) == 32:
        return base64.b32decode(value.upper())
    raise ValueError(f"unsupported BTIH length: {value!r}")


def trackers_from_file(path: str | None) -> list[str]:
    if not path:
        return []

    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def unique_keep_order(values):
    seen = set()
    out = []
    for value in values:
        value = (value or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def http_scrape_url(tracker: str, ih: bytes) -> str:
    p = urllib.parse.urlsplit(tracker)
    path = p.path

    idx = path.lower().rfind("announce")
    if idx < 0:
        raise ValueError("HTTP tracker URL has no announce path")

    scrape_path = path[:idx] + "scrape" + path[idx + len("announce"):]
    ih_param = "info_hash=" + urllib.parse.quote_from_bytes(ih, safe="")
    query = p.query + ("&" if p.query else "") + ih_param

    return urllib.parse.urlunsplit(
        (p.scheme, p.netloc, scrape_path, query, p.fragment)
    )


def probe_http(tracker: str, ih: bytes, timeout: float) -> dict:
    started = time.monotonic()
    try:
        url = http_scrape_url(tracker, ih)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Connection": "close",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(1024 * 1024)

        decoded = bdecode(body)
        if not isinstance(decoded, dict):
            raise BencodeError("root is not a dictionary")

        if b"failure reason" in decoded:
            reason = decoded[b"failure reason"].decode("utf-8", "replace")
            raise RuntimeError(reason)

        files = decoded.get(b"files")
        if not isinstance(files, dict):
            raise BencodeError("missing files dictionary")

        stats = files.get(ih)
        if not isinstance(stats, dict):
            raise BencodeError("requested info_hash missing in response")

        if any(not isinstance(stats.get(key), int) or stats[key] < 0
               for key in (b"complete", b"incomplete")):
            raise BencodeError("invalid scrape counts")

        return {
            "tracker": tracker,
            "scheme": urllib.parse.urlsplit(tracker).scheme.lower(),
            "status": "ok",
            "seeders": int(stats.get(b"complete", 0)),
            "leechers": int(stats.get(b"incomplete", 0)),
            "completed": int(stats.get(b"downloaded", 0)),
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "error": "",
        }

    except urllib.error.HTTPError as e:
        return {
            "tracker": tracker,
            "scheme": urllib.parse.urlsplit(tracker).scheme.lower(),
            "status": "http_error",
            "seeders": None,
            "leechers": None,
            "completed": None,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "error": f"HTTP {e.code}",
        }
    except Exception as e:
        return {
            "tracker": tracker,
            "scheme": urllib.parse.urlsplit(tracker).scheme.lower(),
            "status": "error",
            "seeders": None,
            "leechers": None,
            "completed": None,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "error": str(e),
        }


def udp_exchange(sock: socket.socket, payload: bytes, timeout: float) -> bytes:
    sock.settimeout(timeout)
    sock.send(payload)
    return sock.recv(65535)


def probe_udp(tracker: str, ih: bytes, timeout: float) -> dict:
    started = time.monotonic()
    p = urllib.parse.urlsplit(tracker)
    host = p.hostname
    port = p.port or 80

    if not host:
        return {
            "tracker": tracker,
            "scheme": "udp",
            "status": "error",
            "seeders": None,
            "leechers": None,
            "completed": None,
            "latency_ms": 0,
            "error": "missing hostname",
        }

    last_error = None

    try:
        addrinfos = socket.getaddrinfo(
            host, port, socket.AF_UNSPEC, socket.SOCK_DGRAM
        )
    except Exception as e:
        addrinfos = []
        last_error = e

    for family, socktype, proto, _, sockaddr in addrinfos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            sock.connect(sockaddr)

            tx1 = random.getrandbits(32)
            req1 = struct.pack("!QII", UDP_PROTOCOL_ID, 0, tx1)
            resp1 = udp_exchange(sock, req1, timeout)

            if len(resp1) < 16:
                raise RuntimeError("short UDP connect response")

            action1, rtx1, connection_id = struct.unpack("!IIQ", resp1[:16])
            if action1 == 3:
                raise RuntimeError(resp1[8:].decode("utf-8", "replace"))
            if action1 != 0 or rtx1 != tx1:
                raise RuntimeError("invalid UDP connect response")

            tx2 = random.getrandbits(32)
            req2 = struct.pack("!QII20s", connection_id, 2, tx2, ih)
            resp2 = udp_exchange(sock, req2, timeout)

            if len(resp2) < 8:
                raise RuntimeError("short UDP scrape response")

            action2, rtx2 = struct.unpack("!II", resp2[:8])
            if action2 == 3:
                raise RuntimeError(resp2[8:].decode("utf-8", "replace"))
            if action2 != 2 or rtx2 != tx2:
                raise RuntimeError("invalid UDP scrape response")
            if len(resp2) < 20:
                raise RuntimeError("short UDP scrape stats")

            seeders, completed, leechers = struct.unpack("!III", resp2[8:20])

            return {
                "tracker": tracker,
                "scheme": "udp",
                "status": "ok",
                "seeders": seeders,
                "leechers": leechers,
                "completed": completed,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "error": "",
            }

        except Exception as e:
            last_error = e
        finally:
            if sock is not None:
                sock.close()

    return {
        "tracker": tracker,
        "scheme": "udp",
        "status": "error",
        "seeders": None,
        "leechers": None,
        "completed": None,
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
        "error": str(last_error or "resolution failed"),
    }


def probe_tracker(tracker: str, ih: bytes, timeout: float) -> dict:
    scheme = urllib.parse.urlsplit(tracker).scheme.lower()
    if scheme in ("http", "https"):
        return probe_http(tracker, ih, timeout)
    if scheme == "udp":
        return probe_udp(tracker, ih, timeout)

    return {
        "tracker": tracker,
        "scheme": scheme,
        "status": "unsupported",
        "seeders": None,
        "leechers": None,
        "completed": None,
        "latency_ms": None,
        "error": f"unsupported scheme: {scheme}",
    }


def summarize(results: list[dict]) -> dict:
    ok = [r for r in results if r["status"] == "ok"]
    max_seeders = max((r["seeders"] or 0 for r in ok), default=0)
    max_leechers = max((r["leechers"] or 0 for r in ok), default=0)
    active_trackers = sum(
        1 for r in ok if (r["seeders"] or 0) > 0 or (r["leechers"] or 0) > 0
    )
    latencies = [r["latency_ms"] for r in ok if r["latency_ms"] is not None]

    # Do not sum peers from different trackers as unique peers; the same peer
    # may be registered with multiple trackers.
    if max_seeders >= 20 and active_trackers >= 2:
        grade = "A"
        verdict = "strong"
    elif max_seeders >= 5:
        grade = "B"
        verdict = "good"
    elif max_seeders >= 1:
        grade = "C"
        verdict = "seeded"
    elif max_leechers > 0:
        grade = "D"
        verdict = "active_no_reported_seed"
    elif ok:
        grade = "E"
        verdict = "tracker_responded_no_swarm"
    else:
        grade = "U"
        verdict = "unknown_or_unreachable"

    return {
        "grade": grade,
        "verdict": verdict,
        "trackers_tested": len(results),
        "trackers_responded": len(ok),
        "active_trackers": active_trackers,
        "max_seeders_on_one_tracker": max_seeders,
        "max_leechers_on_one_tracker": max_leechers,
        "median_latency_ms": round(median(latencies), 1) if latencies else None,
    }


def load_export(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    raise ValueError("JSON must be an array or contain an 'items' array")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="JSON exported by magnet_collector.user.js")
    ap.add_argument(
        "--trackers",
        help="Optional text file containing one additional tracker URL per line",
    )
    ap.add_argument("--timeout", type=float, default=4.0)
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument(
        "--max-trackers",
        type=int,
        default=40,
        help="Maximum trackers tested per info_hash; embedded trackers are first",
    )
    ap.add_argument("--output", default="tracker-report.json")
    ap.add_argument("--csv", default="tracker-report.csv")
    args = ap.parse_args()

    items = load_export(args.input)
    global_trackers = trackers_from_file(args.trackers)
    report = []

    csv_rows = []

    for index, item in enumerate(items, 1):
        raw_hash = item.get("info_hash")
        if not raw_hash:
            print(f"[{index}/{len(items)}] skip: missing info_hash")
            continue

        try:
            ih = info_hash_bytes(raw_hash)
        except Exception as e:
            print(f"[{index}/{len(items)}] skip {raw_hash}: {e}")
            continue

        trackers = unique_keep_order(
            list(item.get("trackers") or []) + global_trackers
        )
        if args.max_trackers > 0:
            trackers = trackers[: args.max_trackers]

        title = item.get("title") or raw_hash
        print(
            f"[{index}/{len(items)}] {title[:70]} "
            f"hash={raw_hash} trackers={len(trackers)}"
        )

        results = []
        if trackers:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {
                    pool.submit(probe_tracker, tr, ih, args.timeout): tr
                    for tr in trackers
                }
                for fut in as_completed(futures):
                    r = fut.result()
                    results.append(r)

        results.sort(
            key=lambda r: (
                r["status"] != "ok",
                -(r["seeders"] or 0),
                -(r["leechers"] or 0),
                r["latency_ms"] if r["latency_ms"] is not None else 10**9,
            )
        )

        summary = summarize(results)
        print(
            f"  -> grade={summary['grade']} "
            f"max_seeders={summary['max_seeders_on_one_tracker']} "
            f"max_leechers={summary['max_leechers_on_one_tracker']} "
            f"responded={summary['trackers_responded']}/{summary['trackers_tested']}"
        )

        entry = {
            "title": title,
            "info_hash": raw_hash,
            "magnet": item.get("magnet"),
            "source_url": item.get("source_url"),
            "summary": summary,
            "trackers": results,
        }
        report.append(entry)

        for r in results:
            csv_rows.append(
                {
                    "title": title,
                    "info_hash": raw_hash,
                    "grade": summary["grade"],
                    "verdict": summary["verdict"],
                    "tracker": r["tracker"],
                    "scheme": r["scheme"],
                    "status": r["status"],
                    "seeders": r["seeders"],
                    "leechers": r["leechers"],
                    "completed": r["completed"],
                    "latency_ms": r["latency_ms"],
                    "error": r["error"],
                }
            )

    Path(args.output).write_text(
        json.dumps(
            {
                "schema": "tracker-probe/v1",
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "items": report,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    fields = [
        "title",
        "info_hash",
        "grade",
        "verdict",
        "tracker",
        "scheme",
        "status",
        "seeders",
        "leechers",
        "completed",
        "latency_ms",
        "error",
    ]
    with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(csv_rows)

    print(f"Wrote {args.output}")
    print(f"Wrote {args.csv}")


if __name__ == "__main__":
    main()
