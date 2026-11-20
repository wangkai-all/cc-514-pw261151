"""Focused tests for gap detection, DST boundaries, incremental updates, replay."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, demo, service, strategies, timeutil  # noqa: E402


def fresh_conn():
    path = tempfile.mktemp(suffix=".db", prefix="sparsets-test-")
    conn = db.connect(path)
    service.seed_registry(conn)
    return conn, path


def cleanup(conn, path):
    conn.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


def make_series(conn, tz="UTC", start="2026-01-01", hours=48, interval=3600,
                kind="fixed", baseline=10.0, sid="t"):
    start_ms = timeutil.to_ms(datetime.fromisoformat(start).replace(tzinfo=timeutil.ZoneInfo(tz)))
    end_ms = start_ms + hours * 3600 * 1000
    return service.create_series(conn, {
        "id": sid, "name": sid, "timezone": tz, "interval_kind": kind,
        "interval_seconds": None if kind == "daily" else interval,
        "window_start": start_ms, "window_end": end_ms, "baseline": baseline,
    })


def point(ts, value):
    return {"ts": ts, "value": value}


def slots(series, conn, points):
    return service.ingest(conn, series, [point(ts, v) for ts, v in points], kind="test")


class TimezoneTests(unittest.TestCase):
    def test_spring_forward_local_time_is_rejected_as_nonexistent(self):
        ms, status = timeutil.resolve_local(
            datetime(2026, 3, 8, 2, 30), "America/New_York"
        )
        self.assertEqual(status, "nonexistent")
        local = timeutil.local_dt(ms, "America/New_York")
        self.assertEqual((local.hour, local.minute), (3, 0))

    def test_fall_back_local_time_is_ambiguous_and_earlier_instant_wins(self):
        earlier, status = timeutil.resolve_local(
            datetime(2026, 10, 25, 2, 30), "Europe/Berlin"
        )
        self.assertEqual(status, "ambiguous")
        local = timeutil.local_dt(earlier, "Europe/Berlin")
        self.assertEqual(local.utcoffset().total_seconds(), 2 * 3600)
        later = earlier + 3600 * 1000
        self.assertEqual(
            timeutil.local_dt(later, "Europe/Berlin").strftime("%H:%M"), "02:30"
        )

    def test_fixed_grid_keeps_every_instant_across_fall_back(self):
        series = {
            "timezone": "Europe/Berlin", "interval_kind": "fixed",
            "interval_seconds": 3600, "window_start": 0, "window_end": 0,
        }
        start = timeutil.to_ms(datetime(2026, 10, 25, 0, 0, tzinfo=timeutil.ZoneInfo("Europe/Berlin")))
        series["window_start"] = start
        series["window_end"] = start + 4 * 3600 * 1000
        grid = timeutil.build_grid(series)
        self.assertEqual(len(grid), 4)
        locals_ = [g["local"][11:16] for g in grid]
        self.assertEqual(locals_, ["00:00", "01:00", "02:00", "02:00"])
        self.assertEqual([g["offset_minutes"] for g in grid], [120, 120, 120, 60])

    def test_daily_grid_shifted_anchor_when_midnight_does_not_exist(self):
        series = {
            "timezone": "America/Santiago", "interval_kind": "daily",
            "interval_seconds": None, "window_start": 0, "window_end": 0,
        }
        start = timeutil.to_ms(datetime(2026, 9, 5, tzinfo=timeutil.ZoneInfo("America/Santiago")))
        series["window_start"] = start
        series["window_end"] = start + 3 * 86_400_000
        grid = timeutil.build_grid(series)
        shifted = [g for g in grid if "anchor_shifted" in g["flags"]]
        self.assertEqual(len(shifted), 1)
        self.assertEqual(shifted[0]["local_iso"][:19], "2026-09-06T01:00:00")
        self.assertEqual(len({g["local_iso"][:10] for g in grid}), len(grid))


class GapTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_conn()

    def tearDown(self):
        cleanup(self.conn, self.path)

    def test_gaps_include_leading_interior_and_trailing_boundaries(self):
        series = make_series(self.conn, hours=10, sid="g1")
        grid = timeutil.build_grid(series)
        # leave slot 0 missing, 3-4 missing, 8-9 missing
        points = [(grid[i]["ts"], float(i)) for i in (1, 2, 5, 6, 7)]
        slots(series, self.conn, points)
        payload = service.series_payload(self.conn, service.get_series(self.conn, "g1"))
        gaps = payload["gaps"]
        self.assertEqual([(g["start_idx"], g["end_idx"]) for g in gaps], [(0, 0), (3, 4), (8, 9)])
        self.assertEqual(gaps[0]["boundary"], "leading")
        self.assertEqual(gaps[2]["boundary"], "trailing")
        self.assertIsNone(gaps[1]["boundary"])

    def test_continuous_missing_run_is_one_gap_not_many(self):
        series = make_series(self.conn, hours=30, sid="g2")
        grid = timeutil.build_grid(series)
        slots(series, self.conn, [(grid[i]["ts"], float(i)) for i in (0, 29)])
        payload = service.series_payload(self.conn, service.get_series(self.conn, "g2"))
        self.assertEqual(len(payload["gaps"]), 1)
        self.assertEqual(payload["gaps"][0]["count"], 28)

    def test_gap_across_spring_forward_reports_utc_and_wall_durations(self):
        out = demo.seed_demo(self.conn, "dst_spring")
        payload = service.series_payload(self.conn, service.get_series(self.conn, out["series_id"]))
        crossing = [g for g in payload["gaps"] if g["tz"]["crosses_transition"]]
        self.assertEqual(len(crossing), 1)
        g = crossing[0]
        self.assertEqual(g["count"], 14)
        self.assertEqual(g["tz"]["utc_minutes"], 840)
        self.assertEqual(g["tz"]["wall_minutes"], 900)

    def test_gap_across_fall_back_reports_shorter_wall_duration(self):
        out = demo.seed_demo(self.conn, "dst_fall")
        payload = service.series_payload(self.conn, service.get_series(self.conn, out["series_id"]))
        crossing = [g for g in payload["gaps"] if g["tz"]["crosses_transition"]]
        self.assertEqual(len(crossing), 1)
        self.assertEqual(crossing[0]["tz"]["utc_minutes"], 360)
        self.assertEqual(crossing[0]["tz"]["wall_minutes"], 300)
        self.assertEqual(payload["timezone_audit"]["duplicate_local_count"], 1)

    def test_off_grid_points_are_reported_not_dropped_silently(self):
        series = make_series(self.conn, hours=10, sid="g3")
        grid = timeutil.build_grid(series)
        slots(series, self.conn, [(grid[0]["ts"], 1.0)])
        res = slots(series, self.conn, [(grid[1]["ts"] + 17_000, 2.0)])
        self.assertEqual(res["ignored_off_grid"], 1)


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_conn()

    def tearDown(self):
        cleanup(self.conn, self.path)

    def test_linear_interpolates_exactly_and_marks_fallback_without_next(self):
        series = make_series(self.conn, hours=8, sid="s1")
        grid = timeutil.build_grid(series)
        slots(series, self.conn, [(grid[1]["ts"], 10.0), (grid[5]["ts"], 50.0)])
        run = service.get_or_create_run(self.conn, series, "linear", 1, {})
        comps = {r["idx"]: dict(r) for r in self.conn.execute(
            "SELECT * FROM completions WHERE run_id=?", (run["id"],))}
        self.assertAlmostEqual(comps[3]["value"], 30.0)
        self.assertEqual(comps[3]["quality"], "estimated")
        # A leading gap has no left anchor and is reported, never fabricated.
        self.assertNotIn(0, comps)
        blocked = strategies.complete_series(
            series, grid, [None, 10.0, None, None, None, 50.0, None, None],
            strategies.detect_gaps(grid, {1, 5}, len(grid)), "linear", 1, {}, {"step_ms": 3600_000, "run_ts": 0},
        )[1]
        self.assertIn("no_left_anchor", [b["reason"] for b in blocked])
        # trailing gap 6-7 has no right anchor -> constant, flagged fallback
        self.assertEqual(comps[7]["quality"], "fallback")

    def test_ffill_is_history_only_and_blocked_without_left_anchor(self):
        series = make_series(self.conn, hours=8, sid="s2")
        grid = timeutil.build_grid(series)
        slots(series, self.conn, [(grid[0]["ts"], 4.0), (grid[3]["ts"], 7.0)])
        run = service.get_or_create_run(self.conn, series, "ffill", 1, {})
        comps = {r["idx"]: r["value"] for r in self.conn.execute(
            "SELECT idx, value FROM completions WHERE run_id=?", (run["id"],))}
        self.assertEqual(comps[1], 4.0)
        self.assertEqual(comps[2], 4.0)
        self.assertEqual(comps[4], 7.0)

    def test_same_strategy_version_params_are_deduplicated(self):
        series = make_series(self.conn, hours=8, sid="s3")
        grid = timeutil.build_grid(series)
        slots(series, self.conn, [(grid[0]["ts"], 1.0)])
        a = service.get_or_create_run(self.conn, series, "seasonal", 1, {"blend": 0.5})
        b = service.get_or_create_run(self.conn, series, "seasonal", 1, {"blend": 0.5})
        c = service.get_or_create_run(self.conn, series, "seasonal", 1, {"blend": 0.6})
        self.assertEqual(a["id"], b["id"])
        self.assertNotEqual(a["id"], c["id"])
        # different versions never collapse
        d = service.get_or_create_run(self.conn, series, "seasonal", 2, {"blend": 0.5})
        self.assertNotEqual(a["id"], d["id"])

    def test_params_are_clamped_and_unknown_rejected(self):
        p = strategies.validate_params("localmean", {"neighbors": 999}, version=1)
        self.assertEqual(p["neighbors"], 50)
        with self.assertRaises(ValueError):
            strategies.validate_params("linear", {"nope": 1}, version=1)
        with self.assertRaises(ValueError):
            strategies.validate_params("linear", {}, version=99)


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_conn()

    def tearDown(self):
        cleanup(self.conn, self.path)

    def test_late_arrival_recomputes_only_affected_window(self):
        series = make_series(self.conn, hours=24, sid="i1")
        grid = timeutil.build_grid(series)
        # missing runs: 3-5 and 15-17
        observed = [i for i in range(24) if i not in {3, 4, 5, 15, 16, 17}]
        slots(series, self.conn, [(grid[i]["ts"], 100.0 + i) for i in observed])
        run = service.get_or_create_run(self.conn, series, "linear", 1, {})
        run2 = service.get_or_create_run(self.conn, series, "ffill", 1, {})
        before = {r["idx"]: r["computed_at"] for r in self.conn.execute(
            "SELECT idx, computed_at FROM completions WHERE run_id=?", (run["id"],))}
        self.assertEqual(sorted(before), [3, 4, 5, 15, 16, 17])

        res = service.ingest(self.conn, series, [point(grid[16]["ts"], 116.0)], kind="late")
        # two runs, affected slots {15, 17} + newly-observed 16 -> 2 * 2 = 4 rows
        self.assertEqual(res["rows_written"], 4)
        after = {r["idx"]: dict(r) for r in self.conn.execute(
            "SELECT * FROM completions WHERE run_id=?", (run["id"],))}
        # gap 3-5 untouched, gap 15-17 shrinks to 15,17
        self.assertEqual(sorted(after), [3, 4, 5, 15, 17])
        for idx in (3, 4, 5):
            self.assertEqual(after[idx]["computed_at"], before[idx])
        self.assertEqual(
            [(r["start_idx"], r["end_idx"]) for r in res["affected_ranges"][0]["sub_ranges"]],
            [(15, 17)],
        )
        self.assertEqual(res["runs_updated"], 2)

    def test_noop_batch_does_not_touch_completions(self):
        series = make_series(self.conn, hours=12, sid="i2")
        grid = timeutil.build_grid(series)
        slots(series, self.conn, [(grid[i]["ts"], 5.0) for i in (0, 11)])
        run = service.get_or_create_run(self.conn, series, "linear", 1, {})
        before = [dict(r) for r in self.conn.execute(
            "SELECT * FROM completions WHERE run_id=?", (run["id"],))]
        res = service.ingest(self.conn, series, [point(grid[0]["ts"], 5.0)])
        self.assertEqual(res["changed_slots"], 0)
        after = [dict(r) for r in self.conn.execute(
            "SELECT * FROM completions WHERE run_id=?", (run["id"],))]
        self.assertEqual(before, after)

    def test_correction_updates_estimate_and_keeps_separate_observation(self):
        series = make_series(self.conn, hours=12, sid="i3")
        grid = timeutil.build_grid(series)
        slots(series, self.conn, [(grid[i]["ts"], 5.0) for i in (0, 11)])
        res = service.ingest(self.conn, series, [point(grid[5]["ts"], 5.0)])
        self.assertEqual(res["changed_slots"], 1)
        rows = self.conn.execute(
            "SELECT value, received_ts FROM observation_versions WHERE series_id='i3' AND ts=?",
            (grid[5]["ts"],)).fetchall()
        self.assertEqual([r["value"] for r in rows], [5.0])


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_conn()

    def tearDown(self):
        cleanup(self.conn, self.path)

    def test_eval_separates_original_holdout_and_completed_slots(self):
        series = make_series(self.conn, hours=48, sid="e1")
        grid = timeutil.build_grid(series)
        slots(series, self.conn, [(grid[i]["ts"], 10.0 + (i % 6)) for i in range(48)
                                  if i not in {10, 11, 30}])
        run = service.get_or_create_run(self.conn, series, "linear", 1, {})
        ev = service.evaluate(self.conn, series, run["id"], holdout_fraction=0.25, seed=3)
        orig = ev["sample_split"]["original"]
        self.assertGreater(orig["slot_count"], 0)
        self.assertEqual(orig["evaluable"], orig["slot_count"] - orig["unfilled"])
        self.assertEqual(orig["metrics"]["n"], orig["evaluable"])
        classes = {s["sample_class"] for s in ev["samples"]}
        self.assertEqual(classes, {"original", "completed"})
        completed = [s for s in ev["samples"] if s["sample_class"] == "completed"]
        self.assertTrue(all(s["truth"] is None for s in completed))
        self.assertTrue(all(s["error"] is None for s in completed))
        self.assertTrue(all(s["truth"] is not None for s in ev["samples"]
                            if s["sample_class"] == "original" and s["error"] is not None))

    def test_holdout_is_deterministic_for_same_seed(self):
        series = make_series(self.conn, hours=20, sid="e2")
        grid = timeutil.build_grid(series)
        obs_idx = [i for i in range(20) if i % 4]
        slots(series, self.conn, [(grid[i]["ts"], 3.0) for i in obs_idx])
        m1 = service.holdout_mask(series, grid, obs_idx, 0.3, 5)
        m2 = service.holdout_mask(series, grid, obs_idx, 0.3, 5)
        m3 = service.holdout_mask(series, grid, obs_idx, 0.3, 6)
        self.assertEqual(m1, m2)
        self.assertNotEqual(m1, m3)

    def test_correction_revises_late_truth_not_estimate(self):
        series = make_series(self.conn, hours=12, sid="e4")
        grid = timeutil.build_grid(series)
        slots(series, self.conn, [(grid[i]["ts"], 5.0) for i in (0, 11)])
        run = service.get_or_create_run(self.conn, series, "linear", 1, {})
        service.ingest(self.conn, series, [point(grid[5]["ts"], 40.0)], kind="late")
        mark = self.conn.execute(
            "SELECT * FROM gap_eval_marks WHERE run_id=? AND ts=?",
            (run["id"], grid[5]["ts"])).fetchone()
        self.assertEqual(mark["truth"], 40.0)
        estimate = mark["estimate"]
        service.ingest(self.conn, series, [point(grid[5]["ts"], 44.0)], kind="correct")
        mark2 = self.conn.execute(
            "SELECT * FROM gap_eval_marks WHERE run_id=? AND ts=?",
            (run["id"], grid[5]["ts"])).fetchone()
        self.assertEqual(mark2["truth"], 44.0)
        self.assertEqual(mark2["estimate"], estimate)  # estimate pinned

    def test_compare_marks_pairwise_disagreement(self):
        series = make_series(self.conn, hours=24, sid="e3")
        grid = timeutil.build_grid(series)
        slots(series, self.conn, [(grid[i]["ts"], float(i)) for i in
                                  (0, 1, 2, 20, 21, 22, 23)])
        a = service.get_or_create_run(self.conn, series, "linear", 1, {})
        b = service.get_or_create_run(self.conn, series, "ffill", 1, {})
        cmp = service.compare(self.conn, series, [a["id"], b["id"]], 0.2, 9)
        self.assertEqual(len(cmp["runs"]), 2)
        self.assertGreater(cmp["pairwise"][0]["mean_abs_diff"], 0)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_conn()

    def tearDown(self):
        cleanup(self.conn, self.path)

    def test_replay_is_exact_without_later_data(self):
        out = demo.seed_demo(self.conn, "dst_spring")
        sid = out["series_id"]
        series = service.get_series(self.conn, sid)
        as_of = self.conn.execute(
            "SELECT MIN(received_ts) AS t FROM observation_versions WHERE series_id=?",
            (sid,)).fetchone()["t"]
        runs = service.series_runs(self.conn, sid)
        for run in runs:
            rep = service.replay(self.conn, run["id"], as_of + 1)
            self.assertTrue(rep["identical"], msg=f"{run['key']}: {rep['detail'][:2]}")
            self.assertEqual(rep["mismatched"], 0)

    def test_replay_after_late_arrival_shows_what_was_known(self):
        out = demo.seed_demo(self.conn, "dst_spring")
        sid = out["series_id"]
        series = service.get_series(self.conn, sid)
        as_of = self.conn.execute(
            "SELECT MIN(received_ts) AS t FROM observation_versions WHERE series_id=?",
            (sid,)).fetchone()["t"]
        payload = service.series_payload(self.conn, series)
        trail = payload["gaps"][-1]
        run = payload["runs"][0]
        service.ingest(self.conn, series, [point(trail["start_ts"], 25.0)], kind="late")
        rep = service.replay(self.conn, run["id"], as_of + 1)
        self.assertFalse(rep["identical"])
        self.assertGreater(rep["only_replay"], 0)
        # replaying now must equal the stored state
        now = db.now_ms() + 1_000
        self.assertTrue(service.replay(self.conn, run["id"], now)["identical"])

    def test_version_diff_flags_slots_near_transition(self):
        out = demo.seed_demo(self.conn, "dst_spring")
        sid = out["series_id"]
        runs = service.series_runs(self.conn, sid)
        v1 = next(r for r in runs if r["key"] == "seasonal@1")
        v2 = next(r for r in runs if r["key"] == "seasonal@2")
        diff = service.version_diff(self.conn, v1["id"], v2["id"])
        self.assertGreater(diff["changed"], 0)
        self.assertGreaterEqual(diff["near_transition"], 0)
        self.assertLessEqual(diff["changed"], diff["only_a"] + diff["only_b"] + diff["changed"])

    def test_timezone_audit_flags_duplicate_and_skipped_labels(self):
        spring = demo.seed_demo(self.conn, "dst_spring")
        audit = service.series_payload(
            self.conn, service.get_series(self.conn, spring["series_id"]))["timezone_audit"]
        codes = {c["code"] for c in audit["checks"]}
        self.assertIn("skipped_local_time", codes)
        fall = demo.seed_demo(self.conn, "dst_fall")
        audit = service.series_payload(
            self.conn, service.get_series(self.conn, fall["series_id"]))["timezone_audit"]
        codes = {c["code"] for c in audit["checks"]}
        self.assertIn("duplicate_local_time", codes)
        self.assertEqual(audit["duplicate_local_count"], 1)


class DemoResetTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_conn()

    def tearDown(self):
        cleanup(self.conn, self.path)

    def test_reset_is_repeatable_after_late_data_and_replay(self):
        # First seed, then mutate every child table that hangs off runs/series:
        # completions (cascade), gap_eval_marks (cascade), replay_log (no
        # cascade), ingest_batches, off-grid points and a second series.
        demo.seed_demo(self.conn, "dst_spring")
        other = demo.seed_demo(self.conn, "dst_fall")
        series = service.get_series(self.conn, "demo_dst_spring")
        run = service.series_runs(self.conn, "demo_dst_spring")[0]
        payload = service.series_payload(self.conn, series)
        service.ingest(self.conn, series, [point(payload["gaps"][0]["start_ts"], 21.0)], kind="late")
        service.replay(self.conn, run["id"], payload["gaps"][0]["start_ts"])
        other_obs = self.conn.execute(
            "SELECT COUNT(*) AS n FROM observation_versions WHERE series_id=?",
            (other["series_id"],)).fetchone()["n"]
        for _ in range(3):
            out = demo.seed_demo(self.conn, "dst_spring")
            self.assertEqual(out["series_id"], "demo_dst_spring")
            self.assertTrue(service.series_runs(self.conn, "demo_dst_spring"))
            # The other scenario is never touched.
            self.assertEqual(
                service.get_series(self.conn, other["series_id"])["id"], "demo_dst_fall")
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) AS n FROM observation_versions WHERE series_id=?",
                    (other["series_id"],)).fetchone()["n"],
                other_obs)

    def test_failed_write_rolls_back_shared_connection(self):
        demo.seed_demo(self.conn, "dst_spring")
        series = service.get_series(self.conn, "demo_dst_spring")
        payload = service.series_payload(self.conn, series)
        ts = payload["gaps"][0]["start_ts"]
        # A batch with a duplicate timestamp raises after the first INSERT has
        # been staged; without a rollback the connection keeps the write lock
        # and the reset below fails with "database is locked" / FK errors.
        with self.assertRaises(ValueError):
            service.ingest(self.conn, series, [point(ts, 1.0), point(ts, 2.0)], kind="late")
        self.conn.rollback()
        demo.seed_demo(self.conn, "dst_spring")
        self.assertTrue(service.series_runs(self.conn, "demo_dst_spring"))
        run = service.series_runs(self.conn, "demo_dst_spring")[0]
        service.delete_run(self.conn, run["id"])
        self.assertNotIn(run["id"], [r["id"] for r in service.series_runs(self.conn, "demo_dst_spring")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
