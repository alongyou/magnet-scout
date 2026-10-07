#!/usr/bin/env python3
"""Tracker probe API with synchronous compatibility and incremental jobs."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from pathlib import Path
from threading import Lock
import time
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from tracker_probe import info_hash_bytes, probe_tracker, unique_keep_order

app = FastAPI(title="Magnet Scout", version="4.1.1")
TRACKER_FILE = Path(__file__).resolve().with_name("trackers.txt")
MAX_TRACKERS = 30
TRACKER_TIMEOUT = 4
WORKERS = 64
JOB_TTL = 3600
MAX_JOBS = 128
jobs = {}
jobs_lock = Lock()


class MagnetItem(BaseModel):
    info_hash: str
    magnet: str
    title: str = ""
    trackers: list[str] = Field(default_factory=list)
    source_url: str = ""


class MagnetBatch(BaseModel):
    schema_version: str = Field(default="", alias="schema")
    page_title: str = ""
    source_url: str = ""
    items: list[MagnetItem]


def load_global_trackers():
    path = Path(TRACKER_FILE)
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def prepare(batch):
    global_trackers = load_global_trackers()
    torrents = {}
    for item in batch.items:
        try:
            ih = info_hash_bytes(item.info_hash)
            if len(ih) != 20:
                raise ValueError("BTIH must contain 20 bytes")
        except ValueError as exc:
            raise HTTPException(422, f"invalid info_hash: {item.info_hash}") from exc
        key = ih.hex().upper()
        if key not in torrents:
            torrents[key] = {"info_hash": key, "title": item.title, "trackers": [], "ih": ih}
        torrents[key]["trackers"] = unique_keep_order(torrents[key]["trackers"] + item.trackers)
    tasks, results = [], []
    for key, record in torrents.items():
        trackers = unique_keep_order(global_trackers + record["trackers"])[:MAX_TRACKERS]
        results.append({"info_hash": key, "title": record["title"], "trackers_tested": len(trackers),
                        "trackers_completed": 0, "trackers_responded": 0, "active_trackers": 0,
                        "max_seeders": 0, "max_leechers": 0, "done": not trackers})
        tasks.extend((key, record["ih"], tracker) for tracker in trackers)
    return tasks, {"ok": True, "done": not tasks, "trackers_completed": 0,
                   "trackers_total": len(tasks), "results": results}


def run_probes(tasks, snapshot, publish=lambda snapshot: None):
    torrents = {record["info_hash"]: record for record in snapshot["results"]}
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(probe_tracker, tracker, ih, TRACKER_TIMEOUT): key
                   for key, ih, tracker in tasks}
        for future in as_completed(futures):
            record = torrents[futures[future]]
            try:
                result = future.result()
            except Exception:
                result = {"status": "error"}
            # Completed includes failed/timeout probes, so progress always reaches total.
            record["trackers_completed"] += 1
            snapshot["trackers_completed"] += 1
            if result.get("status") == "ok":
                seeds, peers = result.get("seeders") or 0, result.get("leechers") or 0
                record["trackers_responded"] += 1
                record["active_trackers"] += int(seeds > 0 or peers > 0)
                record["max_seeders"] = max(record["max_seeders"], seeds)
                record["max_leechers"] = max(record["max_leechers"], peers)
            record["done"] = record["trackers_completed"] == record["trackers_tested"]
            snapshot["done"] = snapshot["trackers_completed"] == snapshot["trackers_total"]
            publish(snapshot)
    snapshot["done"] = True
    return snapshot


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/magnets/probe")
def probe(batch: MagnetBatch):
    tasks, snapshot = prepare(batch)
    return run_probes(tasks, snapshot)


def run_job(job_id, tasks, snapshot):
    def publish(value):
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["snapshot"] = deepcopy(value)
    try:
        run_probes(tasks, snapshot, publish)
    except Exception as exc:
        snapshot.update(done=True, error=str(exc))
    publish(snapshot)


@app.post("/api/magnets/jobs")
def create_job(batch: MagnetBatch, background_tasks: BackgroundTasks):
    tasks, snapshot = prepare(batch)
    job_id = uuid4().hex
    snapshot["job_id"] = job_id
    now = time.monotonic()
    with jobs_lock:
        for key, job in list(jobs.items()):
            if job["snapshot"]["done"] and now - job["created"] > JOB_TTL:
                del jobs[key]
        if len(jobs) >= MAX_JOBS:
            completed = [key for key, job in jobs.items() if job["snapshot"]["done"]]
            if not completed:
                raise HTTPException(503, "too many active probe jobs")
            del jobs[completed[0]]
        jobs[job_id] = {"created": now, "snapshot": deepcopy(snapshot)}
    if tasks:
        background_tasks.add_task(run_job, job_id, tasks, snapshot)
    return deepcopy(snapshot)


@app.get("/api/magnets/jobs/{job_id}")
def job_status(job_id: str):
    with jobs_lock:
        if job_id not in jobs:
            raise HTTPException(404, "probe job not found or expired")
        return deepcopy(jobs[job_id]["snapshot"])
