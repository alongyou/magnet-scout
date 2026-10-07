import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import magnet_api
from fastapi import BackgroundTasks, HTTPException


HASH = "0123456789abcdef0123456789abcdef01234567"


class MagnetApiTests(unittest.TestCase):
    def batch(self):
        return magnet_api.MagnetBatch.model_validate({
            "schema": "magnet-probe/v3",
            "enable_dht": False,
            "items": [{
                "info_hash": HASH,
                "magnet": f"magnet:?xt=urn:btih:{HASH}",
                "trackers": ["udp://one:80", "udp://two:80"],
            }],
        })

    def test_tracker_file_is_independent_of_working_directory(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trackers.txt"
            path.write_text("# test fixture\nudp://one:80\nudp://two:80\n")
            with patch.object(magnet_api, "TRACKER_FILE", path):
                expected = magnet_api.load_global_trackers()
                self.assertEqual(len(expected), 2)
                try:
                    os.chdir(directory)
                    self.assertEqual(magnet_api.load_global_trackers(), expected)
                finally:
                    os.chdir(original)

    def test_counts_use_maximum_and_deduplicate_trackers(self):
        def probe(tracker, ih, timeout):
            self.assertEqual(ih, bytes.fromhex(HASH))
            return {
                "status": "ok",
                "seeders": 23 if tracker == "udp://one:80" else 12,
                "leechers": 8 if tracker == "udp://two:80" else 2,
            }

        with patch.object(magnet_api, "load_global_trackers", return_value=["udp://one:80"]), \
                patch.object(magnet_api, "probe_tracker", side_effect=probe) as mocked:
            response = magnet_api.probe(self.batch())
        self.assertTrue(response["ok"])
        self.assertEqual(mocked.call_count, 2)
        result = response["results"][0]
        self.assertEqual(result["info_hash"], HASH.upper())
        self.assertEqual(result["max_seeders"], 23)
        self.assertEqual(result["max_leechers"], 8)
        self.assertEqual(result["active_trackers"], 2)
        self.assertEqual(result["trackers_responded"], 2)

    def test_tracker_failure_returns_a_result(self):
        with patch.object(magnet_api, "load_global_trackers", return_value=[]), \
                patch.object(magnet_api, "probe_tracker", side_effect=OSError("timeout")):
            result = magnet_api.probe(self.batch())["results"][0]
        self.assertEqual(result["trackers_tested"], 2)
        self.assertEqual(result["trackers_responded"], 0)
        self.assertEqual(result["max_seeders"], 0)

    def test_job_publishes_each_completion_including_failures(self):
        release = threading.Event()
        def fake_probe(tracker, ih, timeout):
            if tracker == "udp://two:80":
                if not release.wait(3):
                    raise AssertionError("test never released second tracker")
                raise OSError("tracker timeout")
            return {"status": "ok", "seeders": 8, "leechers": 2}

        with patch.object(magnet_api, "load_global_trackers", return_value=[]), \
                patch.object(magnet_api, "probe_tracker", side_effect=fake_probe):
            background = BackgroundTasks()
            initial = magnet_api.create_job(self.batch(), background)
            self.assertEqual(initial["trackers_completed"], 0)
            self.assertEqual(initial["trackers_total"], 2)
            task = background.tasks[0]
            worker = threading.Thread(target=task.func, args=task.args, kwargs=task.kwargs)
            worker.start()
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    partial = magnet_api.job_status(initial["job_id"])
                    if partial["trackers_completed"] == 1:
                        break
                    time.sleep(.01)
                self.assertEqual(partial["trackers_completed"], 1)
                self.assertFalse(partial["done"])
                self.assertEqual(partial["results"][0]["max_seeders"], 8)
                self.assertFalse(partial["results"][0]["done"])
                partial["results"][0]["max_seeders"] = 999
                self.assertEqual(magnet_api.job_status(initial["job_id"])["results"][0]["max_seeders"], 8)
            finally:
                release.set()
                worker.join(3)
            final = magnet_api.job_status(initial["job_id"])
            self.assertTrue(final["done"])
            self.assertEqual(final["trackers_completed"], 2)
            self.assertTrue(final["results"][0]["done"])
            self.assertEqual(final["results"][0]["trackers_responded"], 1)
            self.assertEqual(initial["trackers_completed"], 0)

    def test_no_trackers_finishes_immediately(self):
        batch = self.batch()
        batch.items[0].trackers = []
        with patch.object(magnet_api, "load_global_trackers", return_value=[]):
            background = BackgroundTasks()
            result = magnet_api.create_job(batch, background)
        self.assertTrue(result["done"])
        self.assertEqual(result["trackers_total"], 0)
        self.assertEqual(background.tasks, [])

    def test_duplicate_hashes_merge_trackers(self):
        batch = self.batch()
        duplicate = batch.items[0].model_copy(deep=True)
        duplicate.info_hash = duplicate.info_hash.upper()
        duplicate.trackers = ["udp://three:80"]
        batch.items.append(duplicate)
        with patch.object(magnet_api, "load_global_trackers", return_value=[]):
            tasks, snapshot = magnet_api.prepare(batch)
        self.assertEqual(len(snapshot["results"]), 1)
        self.assertEqual(len(tasks), 3)

    def test_tracker_finishes_before_dht_but_job_waits_for_both(self):
        release = threading.Event()
        batch = self.batch()
        batch.enable_dht = True
        def fake_dht(ih, publish):
            if not release.wait(3):
                raise AssertionError("DHT test never released")
            result = {"status": "ok", "done": True, "queries_sent": 2, "queries_completed": 2,
                      "nodes_responded": 2, "peer_count": 1, "seeders_estimate": None, "error": ""}
            publish(result)
            return result
        with patch.object(magnet_api, "load_global_trackers", return_value=[]), \
                patch.object(magnet_api, "probe_tracker", return_value={"status": "ok", "seeders": 3}), \
                patch.object(magnet_api.dht_engine, "lookup", side_effect=fake_dht):
            background = BackgroundTasks()
            initial = magnet_api.create_job(batch, background)
            task = background.tasks[0]
            worker = threading.Thread(target=task.func, args=task.args, kwargs=task.kwargs)
            worker.start()
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    partial = magnet_api.job_status(initial["job_id"])
                    if partial["trackers_completed"] == 2:
                        break
                    time.sleep(.01)
                self.assertEqual(partial["trackers_completed"], 2)
                self.assertEqual(partial["results"][0]["max_seeders"], 3)
                self.assertFalse(partial["done"])
                self.assertFalse(partial["results"][0]["done"])
                self.assertEqual(partial["dht_total"], 1)
            finally:
                release.set()
                worker.join(3)
            final = magnet_api.job_status(initial["job_id"])
            self.assertTrue(final["done"])
            self.assertEqual(final["dht_completed"], 1, "final callback and future must not double-count")
            self.assertEqual(final["results"][0]["dht"]["peer_count"], 1)
            self.assertEqual(final["results"][0]["max_seeders"], 3)

    def test_dht_failure_preserves_tracker_results(self):
        batch = self.batch()
        batch.enable_dht = True
        with patch.object(magnet_api, "load_global_trackers", return_value=[]), \
                patch.object(magnet_api, "probe_tracker", return_value={"status": "ok", "seeders": 4}), \
                patch.object(magnet_api.dht_engine, "lookup", side_effect=OSError("UDP blocked")):
            result = magnet_api.probe(batch)
        self.assertTrue(result["done"])
        self.assertEqual(result["dht_completed"], 1)
        self.assertEqual(result["results"][0]["max_seeders"], 4)
        self.assertEqual(result["results"][0]["dht"]["status"], "error")

    def test_invalid_hash_is_not_silently_skipped(self):
        batch = self.batch()
        batch.items[0].info_hash = "bad"
        with self.assertRaises(HTTPException) as raised:
            magnet_api.prepare(batch)
        self.assertEqual(raised.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
