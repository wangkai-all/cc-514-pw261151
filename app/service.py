"""Application service: ingest, gap detection, run lifecycle, replay, evaluation."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time

from . import db, strategies, timeutil



# --------------------------------------------------------------------------- #
# Registry & series
# --------------------------------------------------------------------------- #

def seed_registry(conn: sqlite3.Connection) -> None:
    for (strategy_id, version), spec in strategies.VERSION_SPECS.items():
        conn.execute(
            """INSERT OR IGNORE INTO strategy_registry
               (strategy_id, version, label, params_json, changelog, created_ts)
               VALUES (?,?,?,?,?,?)""",
            (
                strategy_id,
                version,
                spec["label"],
                json.dumps(spec.get("defaults", {})),
                spec.get("changelog", ""),
                db.now_ms(),
            ),
        )
    conn.commit()


def list_strategies(conn) -> list[dict]:
    reg = {
        (r["strategy_id"], r["version"]): r
        for r in conn.execute("SELECT * FROM strategy_registry")
    }
    out = []
    for (strategy_id, version), spec in sorted(strategies.VERSION_SPECS.items()):
        row = reg.get((strategy_id, version))
        if row is None:
            continue
        base = strategies.STRATEGIES.get(strategy_id, spec)
        out.append(
            {
                "key": strategies.strategy_key(strategy_id, version),
                "strategy_id": strategy_id,
                "version": version,
                "label": row["label"],
                "description": spec.get("description", base.get("description", "")),
                "changelog": row["changelog"],
                "needs_right_context": spec.get(
                    "needs_right_context", base.get("needs_right_context", True)
                ),
                "param_schema": spec.get("param_schema", base.get("param_schema", [])),
                "defaults": spec.get("defaults", strategies.DEFAULT_PARAMS.get(strategy_id, {})),
                "is_latest": version == max(v for (s, v) in strategies.VERSION_SPECS if s == strategy_id),
            }
        )
    return out


def get_series(conn, series_id: str) -> dict:
    row = conn.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    if row is None:
        raise KeyError(f"series {series_id} not found")
    return dict(row)


def list_series(conn) -> list[dict]:
    out = []
    for row in conn.execute("SELECT * FROM series ORDER BY created_ts"):
        s = dict(row)
        counts = conn.execute(
            """SELECT COUNT(DISTINCT ts) AS n FROM observation_versions WHERE series_id=?""",
            (s["id"],),
        ).fetchone()
        s["observed_slots_known"] = counts["n"]
        out.append(s)
    return out


def create_series(conn, payload: dict) -> dict:
    sid = payload.get("id") or "s_" + hashlib.sha256(
        (payload.get("name", "") + str(db.now_ms())).encode()
    ).hexdigest()[:8]
    tz_name = payload.get("timezone", "UTC")
    timeutil.ZoneInfo(tz_name)  # validate
    kind = payload.get("interval_kind", "fixed")
    step = int(payload.get("interval_seconds") or 3600)
    if kind == "daily":
        step = None
    conn.execute(
        """INSERT INTO series
           (id,name,unit,timezone,interval_kind,interval_seconds,window_start,window_end,
            baseline,description,created_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            sid,
            payload["name"],
            payload.get("unit", ""),
            tz_name,
            kind,
            step,
            int(payload["window_start"]),
            int(payload["window_end"]),
            float(payload.get("baseline", 0.0)),
            payload.get("description", ""),
            db.now_ms(),
        ),
    )
    conn.commit()
    return get_series(conn, sid)


# --------------------------------------------------------------------------- #
# Grid / observation state
# --------------------------------------------------------------------------- #

def observed_state(conn, series: dict, cutoff_ms: int | None = None):
    """Latest observation version per slot, optionally as of a cutoff.

    Returns (observed_by_index, observation_rows, off_grid_rows).
    """
    grid = timeutil.build_grid(series)
    index_of = {slot["ts"]: i for i, slot in enumerate(grid)}
    sql = "SELECT * FROM observation_versions WHERE series_id=?"
    args: list = [series["id"]]
    if cutoff_ms is not None:
        sql += " AND received_ts<=?"
        args.append(int(cutoff_ms))
    sql += " ORDER BY seq"
    latest: dict[int, dict] = {}
    off_grid = []
    for row in conn.execute(sql, args):
        if row["ts"] in index_of:
            latest[row["ts"]] = dict(row)
        else:
            off_grid.append(dict(row))
    observed = [None] * len(grid)
    for ts, row in latest.items():
        observed[index_of[ts]] = row["value"]
    return grid, index_of, observed, latest, off_grid


def missing_ranges(gaps: list[dict]) -> set[int]:
    out: set[int] = set()
    for g in gaps:
        out.update(range(g["start_idx"], g["end_idx"] + 1))
    return out


def prepare(conn, series: dict, cutoff_ms: int | None = None):
    grid, index_of, observed, latest, off_grid = observed_state(conn, series, cutoff_ms)
    obs_idx = {i for i, v in enumerate(observed) if v is not None}
    gaps = strategies.detect_gaps(grid, obs_idx, len(grid))
    stats = {"step_ms": timeutil.step_ms(series), "run_ts": db.now_ms()}
    return grid, observed, latest, off_grid, gaps, stats


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #

def _run_row(conn, run_id: int) -> dict:
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(f"run {run_id} not found")
    return dict(row)


def get_or_create_run(conn, series: dict, strategy_id: str, version: int, params: dict) -> dict:
    spec = strategies.VERSION_SPECS.get((strategy_id, int(version)))
    if spec is None:
        raise ValueError(f"strategy {strategy_id}@{version} is not registered")
    params = strategies.validate_params(strategy_id, params, version=int(version))
    p_hash = strategies.params_hash(strategy_id, int(version), params)
    row = conn.execute(
        """SELECT * FROM runs WHERE series_id=? AND strategy_id=? AND version=? AND params_hash=?""",
        (series["id"], strategy_id, int(version), p_hash),
    ).fetchone()
    if row is not None:
        return dict(row)
    ts = db.now_ms()
    cur = conn.execute(
        """INSERT INTO runs
           (series_id,strategy_id,version,params_json,params_hash,created_ts,
            last_updated_ts,status)
           VALUES (?,?,?,?,?,?,?, 'active')""",
        (series["id"], strategy_id, int(version), json.dumps(params, sort_keys=True),
         p_hash, ts, ts),
    )
    run_id = cur.lastrowid
    conn.commit()
    grid, observed, latest, off_grid, gaps, stats = prepare(conn, series)
    recompute_run(conn, run_id, series, grid, observed, gaps, stats, batch_id=None,
                  only_ranges=None)
    conn.commit()
    return _run_row(conn, run_id)


def recompute_run(
    conn,
    run_id: int,
    series: dict,
    grid: list[dict],
    observed: list[float | None],
    gaps: list[dict],
    stats: dict,
    batch_id: int | None,
    only_ranges: list[tuple[int, int]] | None,
) -> dict:
    """Recompute completions, optionally limited to inclusive index ranges.

    Only slots inside the ranges are deleted and rewritten; estimates outside
    are left untouched.  Slots inside the ranges that became observed have
    their previous estimate archived into ``gap_eval_marks`` before removal,
    so the estimate remains scoreable against the newly arrived truth.
    """
    run = _run_row(conn, run_id)
    params = strategies.validate_params(run["strategy_id"], json.loads(run["params_json"]), version=run["version"])
    if only_ranges is None:
        ranges = [(0, len(grid) - 1)]
    else:
        ranges = []
        for lo, hi in only_ranges:
            lo = max(0, lo)
            hi = min(len(grid) - 1, hi)
            if lo <= hi:
                ranges.append((lo, hi))
        if not ranges:
            return {"run_id": run_id, "ranges": [], "gaps_touched": 0,
                    "rows_written": 0, "rows_preserved": 0, "blocked": 0}

    archived_count = 0
    now = stats["run_ts"]
    for lo, hi in ranges:
        archived = conn.execute(
            """SELECT * FROM completions WHERE run_id=? AND idx BETWEEN ? AND ?""",
            (run_id, lo, hi),
        ).fetchall()
        for row in archived:
            if observed[row["idx"]] is not None:
                conn.execute(
                    """INSERT OR REPLACE INTO gap_eval_marks
                       (run_id, ts, truth, estimate, origin, noted_ts)
                       VALUES (?,?,?,?,?,?)""",
                    (run_id, row["ts"], observed[row["idx"]], row["value"],
                     "late_arrival", now),
                )
                archived_count += 1
        conn.execute(
            "DELETE FROM completions WHERE run_id=? AND idx BETWEEN ? AND ?",
            (run_id, lo, hi),
        )

    target_gaps = [
        g for g in gaps
        if any(g["end_idx"] >= lo and g["start_idx"] <= hi for lo, hi in ranges)
    ]
    estimates, blocked = strategies.complete_series(
        series, grid, observed, target_gaps, run["strategy_id"], run["version"], params, stats
    )
    for est in estimates:
        conn.execute(
            """INSERT OR REPLACE INTO completions
               (run_id,ts,idx,value,quality,computed_at,context_start_idx,context_end_idx,batch_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                est["ts"],
                est["idx"],
                est["value"],
                est["quality"],
                now,
                max(0, est["idx"] - 1),
                min(len(grid) - 1, est["idx"] + 1),
                batch_id,
            ),
        )
    conn.execute(
        "UPDATE runs SET last_updated_ts=?, last_batch_id=COALESCE(?, last_batch_id) WHERE id=?",
        (now, batch_id, run_id),
    )
    return {
        "run_id": run_id,
        "ranges": [list(r) for r in ranges],
        "gaps_touched": len(target_gaps),
        "rows_written": len(estimates),
        "rows_preserved": archived_count,
        "blocked": len(blocked),
    }


def series_runs(conn, series_id: str) -> list[dict]:
    out = []
    for row in conn.execute(
        "SELECT * FROM runs WHERE series_id=? ORDER BY created_ts, id", (series_id,)
    ):
        r = dict(row)
        r["params"] = json.loads(r["params_json"])
        r["key"] = strategies.strategy_key(r["strategy_id"], r["version"])
        r["label"] = strategies.VERSION_SPECS.get(
            (r["strategy_id"], r["version"]), {}
        ).get("label", r["strategy_id"])
        c = conn.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN quality='fallback' THEN 1 ELSE 0 END) AS fb
               FROM completions WHERE run_id=?""",
            (r["id"],),
        ).fetchone()
        r["completion_count"] = c["n"]
        r["fallback_count"] = c["fb"] or 0
        marks = conn.execute(
            "SELECT COUNT(*) AS n FROM gap_eval_marks WHERE run_id=?", (r["id"],)
        ).fetchone()["n"]
        r["late_truth_count"] = marks
        out.append(r)
    return out


def delete_run(conn, run_id: int) -> None:
    conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
    conn.commit()


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #

def _affected_ranges(changed_idx: set[int], old_obs: list, new_obs: list) -> list[tuple[int, int]]:
    """Minimal slot ranges whose completion can change.

    A changed slot affects its own slot plus the contiguous missing run on each
    side (their bracket anchors changed).  Every strategy here reads at most
    the immediate neighbours, so nothing beyond those runs can move — this is
    what keeps a late arrival from rewriting unrelated windows.
    """
    if not changed_idx:
        return []
    ranges = []
    for c in sorted(changed_idx):
        lo = hi = c
        probe = c - 1
        while probe >= 0 and new_obs[probe] is None and old_obs[probe] is None:
            lo = probe
            probe -= 1
        probe = c + 1
        while probe < len(new_obs) and new_obs[probe] is None and old_obs[probe] is None:
            hi = probe
            probe += 1
        ranges.append((lo, hi))
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def ingest(conn, series: dict, points: list[dict], kind: str = "stream", note: str = "",
           source: str = "stream", received_ts: int | None = None) -> dict:
    """Append observation versions, then recompute only affected windows."""
    if not points:
        raise ValueError("no points supplied")
    t0 = time.perf_counter()
    grid, old_obs, old_latest, off_grid, old_gaps, stats = prepare(conn, series)
    index_of = {slot["ts"]: i for i, slot in enumerate(grid)}
    now = int(received_ts) if received_ts is not None else db.now_ms()

    changed_idx: set[int] = set()
    inserted = 0
    outside = 0
    seen_ts: set[int] = set()
    for p in points:
        ts = int(p["ts"])
        value = float(p["value"])
        if ts in seen_ts:
            # A batch that repeats an instant is ambiguous; reject rather than
            # silently keeping whichever version happened to land last.
            raise ValueError(f"同一批次重复提交同一时点：{ts}")
        seen_ts.add(ts)
        if math.isnan(value) or math.isinf(value):
            raise ValueError("value must be finite")
        if ts not in index_of:
            # Keep off-grid points in their own table so the client can see
            # them; they never enter gap detection or completion.
            conn.execute(
                """INSERT INTO off_grid_points (series_id, ts, value, received_ts, source)
                   VALUES (?,?,?,?,?)""",
                (series["id"], ts, value, now, source),
            )
            outside += 1
            continue
        idx = index_of[ts]
        previous = old_obs[idx]
        conn.execute(
            """INSERT INTO observation_versions
               (series_id,ts,value,received_ts,batch_id,source,note)
               VALUES (?,?,?,?,NULL,?,?)""",
            (series["id"], ts, value, now, source, note),
        )
        inserted += 1
        if previous is None or previous != value:
            changed_idx.add(idx)

    grid, new_obs, new_latest, off_grid, new_gaps, stats = prepare(conn, series)
    stats["run_ts"] = now
    changed_idx = {i for i in changed_idx if old_obs[i] != new_obs[i]}
    if not changed_idx:
        conn.commit()
        return {
            "batch_id": None,
            "inserted": inserted,
            "ignored_off_grid": outside,
            "changed_slots": 0,
            "affected_ranges": [],
            "gaps_touched": 0,
            "runs_updated": 0,
            "rows_written": 0,
            "rows_preserved": 0,
            "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
            "message": "数据未改变任何观测槽位（重复值或窗口外）",
        }

    ranges = _affected_ranges(changed_idx, old_obs, new_obs)
    affected = (ranges[0][0], ranges[-1][1])
    cur = conn.execute(
        """INSERT INTO ingest_batches
           (series_id,created_ts,kind,value_count,min_ts,max_ts,affected_start_ts,
            affected_end_ts,note)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            series["id"], now, kind, inserted,
            min(int(p["ts"]) for p in points), max(int(p["ts"]) for p in points),
            grid[affected[0]]["ts"], grid[affected[1]]["ts"], note,
        ),
    )
    batch_id = cur.lastrowid
    conn.execute(
        "UPDATE observation_versions SET batch_id=? WHERE series_id=? AND received_ts=? AND batch_id IS NULL",
        (batch_id, series["id"], now),
    )

    totals = {"gaps_touched": 0, "rows_written": 0, "rows_preserved": 0, "runs_updated": 0}
    for run in series_runs(conn, series["id"]):
        result = recompute_run(
            conn, run["id"], series, grid, new_obs, new_gaps, stats,
            batch_id=batch_id, only_ranges=ranges,
        )
        totals["gaps_touched"] += result["gaps_touched"]
        totals["rows_written"] += result["rows_written"]
        totals["rows_preserved"] += result["rows_preserved"]
        totals["runs_updated"] += 1

    # A correction revises the truth used for late-arrival scoring: the mark
    # is evaluation data, not replay input, so the estimate stays pinned while
    # truth moves to the best-known observation.
    revised = 0
    for i in changed_idx:
        if old_obs[i] is not None and new_obs[i] is not None:
            cur2 = conn.execute(
                """UPDATE gap_eval_marks SET truth=?
                   WHERE ts=? AND run_id IN (SELECT id FROM runs WHERE series_id=?)""",
                (new_obs[i], grid[i]["ts"], series["id"]),
            )
            revised += cur2.rowcount

    # Expose the slots that were deliberately *not* rewritten so callers can
    # assert incremental behaviour rather than trusting the range report.
    untouched_lo = conn.execute(
        """SELECT MIN(idx) AS lo FROM completions WHERE run_id IN
           (SELECT id FROM runs WHERE series_id=?) AND idx < ?""",
        (series["id"], affected[0]),
    ).fetchone()["lo"]
    untouched_hi = conn.execute(
        """SELECT MAX(idx) AS hi FROM completions WHERE run_id IN
           (SELECT id FROM runs WHERE series_id=?) AND idx > ?""",
        (series["id"], affected[1]),
    ).fetchone()["hi"]
    untouched = [untouched_lo, untouched_hi] if untouched_lo is not None or untouched_hi is not None else None

    duration = (time.perf_counter() - t0) * 1000
    conn.execute(
        """UPDATE ingest_batches SET gaps_touched=?, runs_updated=?, rows_written=?,
           rows_preserved=?, duration_ms=?, affected_start_ts=?, affected_end_ts=? WHERE id=?""",
        (
            totals["gaps_touched"], totals["runs_updated"], totals["rows_written"],
            totals["rows_preserved"], round(duration, 2),
            grid[affected[0]]["ts"], grid[affected[1]]["ts"], batch_id,
        ),
    )
    conn.commit()
    return {
        "batch_id": batch_id,
        "inserted": inserted,
        "ignored_off_grid": outside,
        "changed_slots": len(changed_idx),
        "affected_ranges": [
            {
                "start_idx": affected[0],
                "end_idx": affected[1],
                "from": grid[affected[0]]["local_iso"],
                "to": grid[affected[1]]["local_iso"],
                "slot_count": affected[1] - affected[0] + 1,
                "sub_ranges": [{"start_idx": a, "end_idx": b} for a, b in ranges],
            }
        ],
        "gaps_touched": totals["gaps_touched"],
        "runs_updated": totals["runs_updated"],
        "rows_written": totals["rows_written"],
        "rows_preserved": totals["rows_preserved"],
        "marks_revised": revised,
        "untouched_completion_range": untouched,
        "duration_ms": round(duration, 2),
        "message": (
            f"更新了 {len(changed_idx)} 个槽位，"
            f"仅重算 {sum(b - a + 1 for a, b in ranges)} 个受影响槽位（{len(ranges)} 段窗口）"
        ),
    }


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #

def _metrics(pairs: list[tuple[float, float]]) -> dict:
    n = len(pairs)
    if n == 0:
        return {"n": 0, "mae": None, "rmse": None, "bias": None, "mape": None, "p90": None}
    errs = [p - t for p, t in pairs]
    abs_err = [abs(e) for e in errs]
    mape_terms = [abs(e) / abs(t) for e, (_, t) in zip(errs, pairs) if t != 0]
    return {
        "n": n,
        "mae": round(sum(abs_err) / n, 4),
        "rmse": round(math.sqrt(sum(e * e for e in errs) / n), 4),
        "bias": round(sum(errs) / n, 4),
        "mape": round(sum(mape_terms) / len(mape_terms) * 100, 3) if mape_terms else None,
        "p90": round(sorted(abs_err)[min(n - 1, int(n * 0.9))], 4),
    }


def holdout_mask(series: dict, grid: list[dict], obs_idx: list[int],
                 fraction: float, seed: int) -> set[int]:
    """Deterministic pseudo-holdout of observed slots, independent of strategy."""
    if not obs_idx:
        return set()
    fraction = max(0.0, min(0.8, float(fraction)))
    mask = set()
    for idx in obs_idx:
        h = hashlib.sha256(f"{series['id']}|{grid[idx]['ts']}|{int(seed)}".encode()).digest()
        u = int.from_bytes(h[:8], "big") / 2**64
        if u < fraction:
            mask.add(idx)
    return mask


def evaluate(conn, series: dict, run_id: int, holdout_fraction: float = 0.0,
             seed: int = 7) -> dict:
    run = _run_row(conn, run_id)
    grid, observed, latest, off_grid, gaps, stats = prepare(conn, series)
    comp_rows = {
        r["idx"]: dict(r)
        for r in conn.execute("SELECT * FROM completions WHERE run_id=? ORDER BY idx", (run_id,))
    }
    # --- late-arrival truth: estimates kept when real data landed on top ---
    marks = [dict(r) for r in conn.execute(
        "SELECT * FROM gap_eval_marks WHERE run_id=? ORDER BY ts", (run_id,)
    )]
    # Marks store the estimate that was superseded, so the score survives the
    # estimate's removal from ``completions``.
    late_pairs = [
        (m["estimate"], m["truth"]) for m in marks if m["estimate"] is not None
    ]
    marks_without_estimate = sum(1 for m in marks if m["estimate"] is None)

    # --- holdout truth: observed values, masked out, then completed ---
    obs_idx = [i for i, v in enumerate(observed) if v is not None]
    mask = holdout_mask(series, grid, obs_idx, holdout_fraction, seed)
    masked_obs = list(observed)
    for i in mask:
        masked_obs[i] = None
    masked_gaps = strategies.detect_gaps(grid, {i for i, v in enumerate(masked_obs) if v is not None}, len(grid))
    params = json.loads(run["params_json"])
    m_stats = {**stats, "run_ts": db.now_ms()}
    holdout_rows, _ = strategies.complete_series(
        series, grid, masked_obs, masked_gaps, run["strategy_id"], run["version"], params, m_stats
    )
    holdout_by_idx = {r["idx"]: r for r in holdout_rows}
    holdout_pairs = [
        (holdout_by_idx[i]["value"], observed[i]) for i in mask if i in holdout_by_idx
    ]
    holdout_unfilled = len([i for i in mask if i not in holdout_by_idx])

    # --- per-slot sample table, clearly split original vs completed ---
    sample = []
    max_rows = 400
    for idx in range(len(grid)):
        # Holdout slots are the "original" sample: known truth, estimate scored
        # against it.  These come from the masked recomputation, not storage.
        if idx in mask and idx in holdout_by_idx:
            row = {
                "idx": idx,
                "ts": grid[idx]["ts"],
                "local_iso": grid[idx]["local_iso"],
                "estimated": holdout_by_idx[idx]["value"],
                "quality": holdout_by_idx[idx]["quality"],
                "sample_class": "original",
                "truth": observed[idx],
                "error": round(holdout_by_idx[idx]["value"] - observed[idx], 4),
            }
        elif idx in comp_rows and observed[idx] is not None:
            # Defensive: a stored completion whose slot later became observed
            # is superseded, never reported as a blind "completed" estimate.
            row = {
                "idx": idx,
                "ts": grid[idx]["ts"],
                "local_iso": grid[idx]["local_iso"],
                "estimated": comp_rows[idx]["value"],
                "quality": comp_rows[idx]["quality"],
                "sample_class": "superseded",
                "truth": observed[idx],
                "error": round(comp_rows[idx]["value"] - observed[idx], 4),
            }
        elif idx in comp_rows and idx not in mask:
            row = {
                "idx": idx,
                "ts": grid[idx]["ts"],
                "local_iso": grid[idx]["local_iso"],
                "estimated": comp_rows[idx]["value"],
                "quality": comp_rows[idx]["quality"],
                "sample_class": "completed",
                "truth": None,
                "error": None,
            }
        else:
            continue
        if len(sample) < max_rows:
            sample.append(row)

    original_metrics = _metrics(holdout_pairs)
    late_metrics = _metrics(late_pairs)
    completed_values = [comp_rows[i]["value"] for i in comp_rows if i not in mask]
    orig_values = [v for v in observed if v is not None]

    coverage = {
        "slots": len(grid),
        "observed": len(obs_idx),
        "completed": len(comp_rows),
        "fallback": sum(1 for c in comp_rows.values() if c["quality"] == "fallback"),
        "unfilled": sum(1 for i in range(len(grid)) if observed[i] is None and i not in comp_rows),
        "coverage_pct": round((len(obs_idx) + len(comp_rows)) / len(grid) * 100, 2) if grid else 0,
        "observed_pct": round(len(obs_idx) / len(grid) * 100, 2) if grid else 0,
    }

    return {
        "run": {
            "id": run["id"],
            "key": strategies.strategy_key(run["strategy_id"], run["version"]),
            "params": params,
        },
        "coverage": coverage,
        "sample_split": {
            "original": {
                "definition": "掩蔽真实观测后补全，误差对真实观测计算",
                "slot_count": len(mask),
                "evaluable": len(holdout_pairs),
                "unfilled": holdout_unfilled,
                "metrics": original_metrics,
            },
            "completed": {
                "definition": "无真值的补全槽位，仅报告分布与模型间一致性",
                "slot_count": len(comp_rows) - sum(
                    1 for i in comp_rows if i in mask or observed[i] is not None
                ),
                "mean": round(sum(completed_values) / len(completed_values), 4) if completed_values else None,
                "observed_mean": round(sum(orig_values) / len(orig_values), 4) if orig_values else None,
            },
            "late_arrival": {
                "definition": "补全后被真实数据覆盖的槽位，误差对到达后的真实值计算",
                "slot_count": len(marks),
                "evaluable": len(late_pairs),
                "archived_without_estimate": marks_without_estimate,
                "metrics": late_metrics,
            },
        },
        "holdout": {"fraction": holdout_fraction, "seed": seed},
        "gaps": _gap_payload(series, gaps, grid),
        "samples": sample,
    }


def compare(conn, series: dict, run_ids: list[int], holdout_fraction: float = 0.1,
            seed: int = 7) -> dict:
    grid, observed, latest, off_grid, gaps, stats = prepare(conn, series)
    runs = [_run_row(conn, rid) for rid in run_ids]
    obs_idx = [i for i, v in enumerate(observed) if v is not None]
    mask = holdout_mask(series, grid, obs_idx, holdout_fraction, seed)
    masked_obs = list(observed)
    for i in mask:
        masked_obs[i] = None
    masked_gaps = strategies.detect_gaps(grid, {i for i, v in enumerate(masked_obs) if v is not None}, len(grid))

    table = []
    per_run_idx = {}
    for run in runs:
        params = json.loads(run["params_json"])
        rows, _ = strategies.complete_series(
            series, grid, masked_obs, masked_gaps, run["strategy_id"], run["version"], params,
            {**stats, "run_ts": db.now_ms()},
        )
        by_idx = {r["idx"]: r for r in rows}
        per_run_idx[run["id"]] = by_idx
        pairs = [(by_idx[i]["value"], observed[i]) for i in mask if i in by_idx]
        all_comp = {r["idx"]: r for r in conn.execute(
            "SELECT * FROM completions WHERE run_id=?", (run["id"],))}
        table.append(
            {
                "run_id": run["id"],
                "key": strategies.strategy_key(run["strategy_id"], run["version"]),
                "params": params,
                "holdout": _metrics(pairs),
                "coverage": {
                    "completed": len(all_comp),
                    "fallback": sum(1 for c in all_comp.values() if c["quality"] == "fallback"),
                },
            }
        )

    # Pairwise disagreement on completed-only slots.
    pairwise = []
    base_id = runs[0]["id"] if runs else None
    for run in runs[1:]:
        a = per_run_idx[base_id]
        b = per_run_idx[run["id"]]
        common = sorted(set(a) & set(b))
        diffs = [abs(a[i]["value"] - b[i]["value"]) for i in common]
        pairwise.append(
            {
                "run_a": base_id,
                "run_b": run["id"],
                "shared_slots": len(common),
                "only_a": len(set(a) - set(b)),
                "only_b": len(set(b) - set(a)),
                "mean_abs_diff": round(sum(diffs) / len(diffs), 4) if diffs else None,
                "max_abs_diff": round(max(diffs), 4) if diffs else None,
            }
        )
    return {
        "latest_run": base_id,
        "holdout": {"fraction": holdout_fraction, "seed": seed, "masked_slots": len(mask)},
        "runs": table,
        "pairwise": pairwise,
    }


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #

def replay(conn, run_id: int, as_of_ms: int) -> dict:
    run = _run_row(conn, run_id)
    series = get_series(conn, run["series_id"])
    stored = {
        r["ts"]: dict(r)
        for r in conn.execute("SELECT * FROM completions WHERE run_id=?", (run_id,))
    }
    grid, observed, latest, off_grid, gaps, stats = prepare(conn, series, cutoff_ms=as_of_ms)
    stats["run_ts"] = as_of_ms
    params = json.loads(run["params_json"])
    replayed_rows, blocked = strategies.complete_series(
        series, grid, observed, gaps, run["strategy_id"], run["version"], params, stats
    )
    replayed = {r["ts"]: r for r in replayed_rows}
    matched = mismatched = 0
    max_diff = 0.0
    detail = []
    for ts in sorted(set(stored) | set(replayed)):
        a = stored.get(ts, {}).get("value")
        b = replayed.get(ts, {}).get("value")
        if a is not None and b is not None:
            diff = abs(a - b)
            if diff <= 1e-9:
                matched += 1
            else:
                mismatched += 1
                max_diff = max(max_diff, diff)
                if len(detail) < 200:
                    detail.append(
                        {
                            "ts": ts,
                            "local_iso": timeutil.local_iso(ts, series["timezone"]),
                            "stored": a,
                            "replayed": round(b, 6),
                            "diff": round(diff, 6),
                        }
                    )
    only_stored = len(set(stored) - set(replayed))
    only_replay = len(set(replayed) - set(stored))
    n_obs = sum(1 for v in observed if v is not None)
    conn.execute(
        """INSERT INTO replay_log (run_id,created_ts,as_of_ts,matched,mismatched,only_stored,
           only_replay,max_abs_diff,detail_json) VALUES (?,?,?,?,?,?,?,?,?)""",
        (run_id, db.now_ms(), as_of_ms, matched, mismatched, only_stored, only_replay,
         round(max_diff, 6), json.dumps(detail[:50])),
    )
    conn.commit()
    return {
        "run_id": run_id,
        "key": strategies.strategy_key(run["strategy_id"], run["version"]),
        "as_of": as_of_ms,
        "as_of_local": timeutil.local_iso(as_of_ms, series["timezone"]),
        "observations_at_cutoff": n_obs,
        "observations_now": conn.execute(
            "SELECT COUNT(DISTINCT ts) AS n FROM observation_versions WHERE series_id=?", (series["id"],)
        ).fetchone()["n"],
        "matched": matched,
        "mismatched": mismatched,
        "only_stored": only_stored,
        "only_replay": only_replay,
        "blocked": len(blocked),
        "max_abs_diff": round(max_diff, 6),
        "identical": mismatched == 0 and only_stored == 0 and only_replay == 0,
        "detail": detail,
    }


# --------------------------------------------------------------------------- #
# Payload builders
# --------------------------------------------------------------------------- #

def _gap_payload(series: dict, gaps: list[dict], grid: list[dict]) -> list[dict]:
    transitions = timeutil.find_transitions(series)
    out = []
    for g in gaps:
        info = timeutil.annotate_gap(series, g, grid, transitions)
        out.append(
            {
                "key": f"{grid[g['start_idx']]['ts']}-{grid[g['end_idx']]['ts']}",
                "start_idx": g["start_idx"],
                "end_idx": g["end_idx"],
                "count": g["count"],
                "start_ts": g["first"]["ts"],
                "end_ts": g["last"]["ts"],
                "start_local": g["first"]["local_iso"],
                "end_local": g["last"]["local_iso"],
                "tz": info,
                "boundary": (
                    "leading" if g["start_idx"] == 0
                    else "trailing" if g["end_idx"] == len(grid) - 1
                    else None
                ),
            }
        )
    return out


def series_payload(conn, series: dict) -> dict:
    grid, observed, latest, off_grid, gaps, stats = prepare(conn, series)
    index_of = {slot["ts"]: i for i, slot in enumerate(grid)}
    runs = series_runs(conn, series["id"])
    comps = {}
    for run in runs:
        comps[str(run["id"])] = [
            {
                "ts": r["ts"],
                "idx": r["idx"],
                "value": r["value"],
                "quality": r["quality"],
            }
            for r in conn.execute(
                "SELECT ts, idx, value, quality FROM completions WHERE run_id=? ORDER BY idx",
                (run["id"],),
            )
        ]
    audit = timeutil.tz_audit(series, grid, gaps)
    # include observation local labels for the audit panel review
    observed_points = [
        {
            "ts": row["ts"],
            "idx": index_of[row["ts"]],
            "value": row["value"],
            "local_iso": timeutil.local_iso(row["ts"], series["timezone"]),
            "received_ts": row["received_ts"],
            "source": row["source"],
        }
        for row in latest.values()
    ]
    off_grid_rows = [dict(r) for r in conn.execute(
        "SELECT ts, value, received_ts, source FROM off_grid_points WHERE series_id=? ORDER BY ts LIMIT 50",
        (series["id"],),
    )]
    off_grid_count = conn.execute(
        "SELECT COUNT(*) AS n FROM off_grid_points WHERE series_id=?", (series["id"],)
    ).fetchone()["n"]
    return {
        "series": series,
        "grid": grid,
        "observations": sorted(observed_points, key=lambda p: p["ts"]),
        "gaps": _gap_payload(series, gaps, grid),
        "runs": runs,
        "completions": comps,
        "timezone_audit": audit,
        "off_grid": [
            {
                "ts": r["ts"],
                "value": r["value"],
                "local_iso": timeutil.local_iso(r["ts"], series["timezone"]),
                "source": r["source"],
            }
            for r in off_grid_rows
        ],
        "off_grid_count": off_grid_count,
        "stats": {
            "slots": len(grid),
            "observed": sum(1 for v in observed if v is not None),
            "missing": sum(1 for v in observed if v is None),
            "gaps": len(gaps),
            "longest_gap": max((g["count"] for g in gaps), default=0),
        },
    }


def batches(conn, series_id: str, limit: int = 50) -> list[dict]:
    out = []
    for row in conn.execute(
        "SELECT * FROM ingest_batches WHERE series_id=? ORDER BY id DESC LIMIT ?",
        (series_id, limit),
    ):
        r = dict(row)
        if r["affected_start_ts"] is not None:
            series = get_series(conn, series_id)
            r["affected_start_local"] = timeutil.local_iso(r["affected_start_ts"], series["timezone"])
            r["affected_end_local"] = timeutil.local_iso(r["affected_end_ts"], series["timezone"])
        out.append(r)
    return out


def version_diff(conn, run_a: int, run_b: int) -> dict:
    a, b = _run_row(conn, run_a), _run_row(conn, run_b)
    if a["series_id"] != b["series_id"]:
        raise ValueError("runs belong to different series")
    series = get_series(conn, a["series_id"])
    ca = {r["idx"]: dict(r) for r in conn.execute("SELECT * FROM completions WHERE run_id=?", (run_a,))}
    cb = {r["idx"]: dict(r) for r in conn.execute("SELECT * FROM completions WHERE run_id=?", (run_b,))}
    grid = timeutil.build_grid(series)
    diffs = []
    for idx in sorted(set(ca) & set(cb)):
        diff = abs(ca[idx]["value"] - cb[idx]["value"])
        if diff > 1e-9:
            diffs.append(
                {
                    "idx": idx,
                    "ts": grid[idx]["ts"],
                    "local_iso": grid[idx]["local_iso"],
                    "a": ca[idx]["value"],
                    "b": cb[idx]["value"],
                    "diff": round(diff, 6),
                }
            )
    near_transition = set()
    for t in timeutil.find_transitions(series):
        for d in diffs:
            if abs(d["ts"] - t["ts"]) <= 3 * 3600 * 1000:
                near_transition.add(d["idx"])
    return {
        "run_a": {"id": run_a, "key": strategies.strategy_key(a["strategy_id"], a["version"]), "params": json.loads(a["params_json"])},
        "run_b": {"id": run_b, "key": strategies.strategy_key(b["strategy_id"], b["version"]), "params": json.loads(b["params_json"])},
        "changed": len(diffs),
        "only_a": len(set(ca) - set(cb)),
        "only_b": len(set(cb) - set(ca)),
        "mean_abs_diff": round(sum(d["diff"] for d in diffs) / len(diffs), 6) if diffs else 0,
        "max_abs_diff": round(max((d["diff"] for d in diffs), default=0), 6),
        "near_transition": len(near_transition),
        "diffs": diffs[:300],
    }
