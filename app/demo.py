"""Deterministic demo scenarios.

Each scenario builds a series whose grid deliberately contains:
  * a short interior gap
  * a long continuous gap (the "continuous missing" case)
  * a trailing gap at the window edge (boundary case)
  * for DST scenarios, a gap straddling the spring-forward or fall-back
    transition and a full transition window around it
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from . import db, service, strategies, timeutil


def _wave(ts: int, period_h: float, amp: float, phase: float) -> float:
    hours = ts / 3_600_000.0
    return amp * math.sin(2 * math.pi * hours / period_h + phase)


def _local_window(tz_name: str, start_date: str, days: int) -> tuple[int, int]:
    tz = timeutil.ZoneInfo(tz_name)
    start = datetime.fromisoformat(start_date).replace(tzinfo=tz)
    end = (datetime.fromisoformat(start_date) + timedelta(days=days)).replace(tzinfo=tz)
    return timeutil.to_ms(start), timeutil.to_ms(end)


SCENARIOS = {
    "dst_spring": {
        "name": "纽约春季前跳 · 逐时流",
        "timezone": "America/New_York",
        "start": "2026-03-06",
        "days": 4,
        "interval_kind": "fixed",
        "interval_seconds": 3600,
        "unit": "µg/m³",
        "baseline": 22.0,
        "description": (
            "逐时空气质量读数，窗口跨 2026-03-08 春季前跳（02:00 跳过）。"
            "含短缺口、跨转换的长缺口与窗口末尾缺口。"
        ),
        "gaps": [
            # (local naive start, number of slots)
            ("2026-03-06T18:00:00", 3),
            ("2026-03-07T22:00:00", 14),   # straddles spring forward
            ("2026-03-09T15:00:00", 6),
        ],
        "trailing": 5,
    },
    "dst_fall": {
        "name": "柏林秋季回拨 · 逐时流",
        "timezone": "Europe/Berlin",
        "start": "2026-10-24",
        "days": 3,
        "interval_kind": "fixed",
        "interval_seconds": 3600,
        "unit": "kW",
        "baseline": 410.0,
        "description": (
            "逐时负荷，窗口跨 2026-10-25 秋季回拨（02:00 重复一次）。"
            "本地 02:00–03:00 出现两个不同 UTC 瞬时，采样必须保留两条。"
        ),
        "gaps": [
            ("2026-10-24T09:00:00", 2),
            ("2026-10-25T00:00:00", 6),    # straddles the repeated hour
            ("2026-10-25T20:00:00", 4),
        ],
        "trailing": 3,
    },
    "daily_utc": {
        "name": "UTC 日值 · 连续缺失",
        "timezone": "UTC",
        "start": "2026-01-01",
        "days": 60,
        "interval_kind": "daily",
        "interval_seconds": None,
        "unit": "mm",
        "baseline": 12.0,
        "description": "每日降雨量，含 21 天连续缺测与 12 天窗口末尾缺测，用于检验连续缺失与边界。",
        "gaps": [
            ("2026-01-09T00:00:00", 2),
            ("2026-01-20T00:00:00", 21),
            ("2026-02-22T00:00:00", 7),
        ],
        "trailing": 12,
    },
    "santiago_daily": {
        "name": "圣地亚哥日值 · DST 日界",
        "timezone": "America/Santiago",
        "start": "2026-09-03",
        "days": 6,
        "interval_kind": "daily",
        "interval_seconds": None,
        "unit": "°C",
        "baseline": 14.5,
        "description": "每日均温，2026-09-06 春季前跳使本地零点不存在，日界锚点需平移并标记。",
        "gaps": [
            ("2026-09-05T00:00:00", 2),
            ("2026-09-06T00:00:00", 1),
        ],
        "trailing": 0,
    },
}


def _series_values(ts: int, tz_name: str, baseline: float, seed: int) -> float:
    rng = random.Random(seed ^ (ts // 3_600_000))
    local = timeutil.local_dt(ts, tz_name)
    hour = local.hour + local.minute / 60.0
    day = local.timetuple().tm_yday
    diurnal = _wave(ts, 24.0, baseline * 0.22, phase=-math.pi / 2 + hour / 24.0)
    weekly = _wave(ts, 24.0 * 7, baseline * 0.05, phase=day / 7.0)
    drift = (day % 30) * baseline * 0.002
    noise = rng.gauss(0, baseline * 0.015)
    return round(max(0.0, baseline + diurnal + weekly + drift + noise), 3)


def seed_demo(conn, scenario_key: str = "dst_spring", reset: bool = True, seed: int = 11) -> dict:
    if scenario_key not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario_key}")
    spec = SCENARIOS[scenario_key]
    sid = f"demo_{scenario_key}"
    if reset:
        # Rebuild only the demo row for this scenario; other series (including
        # other demo scenarios and user-created ones) are left untouched.
        demo_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM series WHERE id=?", (sid,))]
        for sid in demo_ids:
            # replay_log has no cascade, so drop it first; deleting the runs then
            # cascades completions and gap_eval_marks via their FKs.  Doing it
            # this way avoids depending on child-table delete order.
            conn.execute("DELETE FROM replay_log WHERE run_id IN (SELECT id FROM runs WHERE series_id=?)", (sid,))
            conn.execute("DELETE FROM runs WHERE series_id=?", (sid,))
            conn.execute("DELETE FROM observation_versions WHERE series_id=?", (sid,))
            conn.execute("DELETE FROM ingest_batches WHERE series_id=?", (sid,))
            conn.execute("DELETE FROM off_grid_points WHERE series_id=?", (sid,))
            conn.execute("DELETE FROM series WHERE id=?", (sid,))
        conn.commit()

    service.seed_registry(conn)
    start, end = _local_window(spec["timezone"], spec["start"], spec["days"])
    series = service.create_series(
        conn,
        {
            "id": sid,
            "name": spec["name"],
            "unit": spec["unit"],
            "timezone": spec["timezone"],
            "interval_kind": spec["interval_kind"],
            "interval_seconds": spec["interval_seconds"],
            "window_start": start,
            "window_end": end,
            "baseline": spec["baseline"],
            "description": spec["description"],
        },
    )
    grid = timeutil.build_grid(series)
    index_of = {slot["ts"]: i for i, slot in enumerate(grid)}
    blocked: set[int] = set()
    for local_start, count in spec["gaps"]:
        naive = datetime.fromisoformat(local_start)
        ts, status = timeutil.resolve_local(naive, spec["timezone"])
        if status == "nonexistent":
            # Anchor the intended gap at the first real slot after the jump.
            ts = next((s["ts"] for s in grid if s["ts"] >= ts), ts)
        if ts not in index_of:
            continue
        start_i = index_of[ts]
        for k in range(count):
            if start_i + k in range(len(grid)):
                blocked.add(start_i + k)
    trailing = spec["trailing"]
    if trailing:
        blocked.update(range(max(0, len(grid) - trailing), len(grid)))
    # Leading boundary gap for the daily scenario: first two days missing.
    if scenario_key == "daily_utc":
        blocked.update({0, 1})

    base_received = db.now_ms() - 86_400_000
    points = []
    for i, slot in enumerate(grid):
        if i in blocked:
            continue
        value = _series_values(slot["ts"], spec["timezone"], spec["baseline"], seed)
        points.append(
            {
                "ts": slot["ts"],
                "value": value,
                "received_ts": base_received + i * 60_000,
            }
        )

    # Assign received_ts via a direct write path so late-arrival scoring has a
    # realistic history to replay against.
    batch = service.ingest(
        conn, series, [{"ts": p["ts"], "value": p["value"]} for p in points],
        kind="initial_load", source="seed", note="演示初始数据",
        received_ts=base_received,
    )
    conn.execute(
        "UPDATE observation_versions SET received_ts=? WHERE series_id=? AND batch_id=?",
        (base_received, sid, batch["batch_id"]),
    )
    conn.commit()

    # Baseline completions from three strategies, all pinned to v1 or v2.
    created = []
    for strategy_id, version, params in [
        ("linear", 1, {}),
        ("linear", 2, {}),
        ("ffill", 1, {}),
        ("seasonal", 1, {}),
        ("seasonal", 2, {}),
    ]:
        run = service.get_or_create_run(conn, series, strategy_id, version, params)
        created.append({"run_id": run["id"], "key": strategies.strategy_key(strategy_id, version)})

    return {
        "scenario": scenario_key,
        "series": service.get_series(conn, sid),
        "series_id": sid,
        "runs": created,
        "grid_slots": len(grid),
        "observed": len(points),
        "description": spec["description"],
    }


def scenarios_catalog() -> list[dict]:
    return [
        {
            "key": k,
            "name": v["name"],
            "timezone": v["timezone"],
            "interval_kind": v["interval_kind"],
            "description": v["description"],
            "days": v["days"],
        }
        for k, v in SCENARIOS.items()
    ]
