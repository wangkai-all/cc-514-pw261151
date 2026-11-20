"""HTTP-level tests: routing, validation, and the local-time DST guard."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, service
from app.server import build_server


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = tempfile.mktemp(suffix=".db", prefix="sparsets-api-")
        conn = db.connect(cls.path)
        service.seed_registry(conn)
        cls.server = build_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        conn = db.connect(cls.path)
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(cls.path + suffix):
                os.remove(cls.path + suffix)

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_demo_series_and_gap_endpoint(self):
        status, body = self.call("POST", "/api/demo", {"scenario": "dst_spring"})
        self.assertEqual(status, 200)
        status, gaps = self.call("GET", "/api/series/demo_dst_spring/gaps")
        self.assertEqual(status, 200)
        self.assertTrue(any(g["tz"]["crosses_transition"] for g in gaps["gaps"]))

    def test_local_time_in_dst_hole_is_rejected(self):
        self.call("POST", "/api/demo", {"scenario": "dst_spring"})
        status, body = self.call(
            "POST", "/api/series/demo_dst_spring/ingest-local",
            {"points": [{"local": "2026-03-08T02:30:00", "value": 21.0}]},
        )
        self.assertEqual(status, 400)
        self.assertIn("不存在", body["error"])

    def test_ambiguous_local_time_is_accepted_and_reported(self):
        self.call("POST", "/api/demo", {"scenario": "dst_fall"})
        status, body = self.call(
            "POST", "/api/series/demo_dst_fall/ingest-local",
            {"points": [{"local": "2026-10-25T02:30:00", "value": 400.0}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["resolved"][0]["status"], "ambiguous")

    def test_unknown_series_is_404_and_bad_numbers_are_400(self):
        status, body = self.call("GET", "/api/series/does-not-exist")
        self.assertEqual(status, 404)
        self.call("POST", "/api/demo", {"scenario": "dst_spring"})
        status, body = self.call(
            "POST", "/api/series/demo_dst_spring/ingest",
            {"points": [{"ts": "abc", "value": 1}]},
        )
        self.assertEqual(status, 400)
        status, body = self.call(
            "POST", "/api/series/demo_dst_spring/ingest",
            {"points": [{"ts": 1772773200000, "value": "x"}]},
        )
        self.assertEqual(status, 400)

    def test_evaluate_and_compare_endpoints(self):
        self.call("POST", "/api/demo", {"scenario": "dst_spring"})
        _, runs = self.call("GET", "/api/series/demo_dst_spring/runs")
        ids = [r["id"] for r in runs["runs"][:2]]
        status, ev = self.call("GET", f"/api/runs/{ids[0]}/evaluate?holdout=0.15&seed=3")
        self.assertEqual(status, 200)
        self.assertIn("original", ev["sample_split"])
        status, cmp = self.call(
            "GET", f"/api/compare?series_id=demo_dst_spring&runs={ids[0]},{ids[1]}")
        self.assertEqual(status, 200)
        self.assertEqual(len(cmp["runs"]), 2)

    def test_replay_endpoint(self):
        self.call("POST", "/api/demo", {"scenario": "dst_spring"})
        _, runs = self.call("GET", "/api/series/demo_dst_spring/runs")
        run_id = runs["runs"][0]["id"]
        status, rep = self.call("POST", f"/api/runs/{run_id}/replay",
                                {"as_of": 4102444800000})
        self.assertEqual(status, 200)
        self.assertTrue(rep["identical"])

    def test_rejected_write_does_not_poison_the_shared_connection(self):
        # The whole server shares one SQLite connection.  A request that fails
        # after the route opened a transaction must roll it back, or the next
        # write request inherits stale locks and fails for an unrelated reason.
        self.call("POST", "/api/demo", {"scenario": "dst_spring"})
        status, _ = self.call(
            "POST", "/api/series/demo_dst_spring/ingest",
            {"points": [{"ts": 1772773200000, "value": "not-a-number"}]},
        )
        self.assertEqual(status, 400)
        status, body = self.call("POST", "/api/demo",
                                 {"scenario": "dst_spring", "reset": True})
        self.assertEqual(status, 200, msg=body)
        _, gap_body = self.call("GET", "/api/series/demo_dst_spring/gaps")
        gap_ts = gap_body["gaps"][0]["start_ts"]
        status, body = self.call(
            "POST", "/api/series/demo_dst_spring/ingest",
            {"points": [{"ts": gap_ts, "value": 21.5}]},
        )
        self.assertEqual(status, 200, msg=body)
        self.assertEqual(body["changed_slots"], 1)
        self.assertGreater(body["rows_written"], 0)

    def test_static_files_and_path_traversal(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        with urllib.request.urlopen(req) as resp:
            self.assertIn(b"<html", resp.read().lower())
        status, _ = self.call("GET", "/../README.md")
        self.assertIn(status, (403, 404))


if __name__ == "__main__":
    unittest.main(verbosity=2)
