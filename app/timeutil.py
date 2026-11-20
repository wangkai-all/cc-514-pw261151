"""Time-grid construction and timezone-boundary auditing.

All instants are integer milliseconds since the Unix epoch (UTC).  Wall-clock
labels are derived only for display and auditing; they are never used as the
grid key, so DST transitions cannot silently drop or duplicate data.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
MAX_GRID_SLOTS = 20_000


def to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return int(round(dt.timestamp() * 1000))


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def local_dt(ms: int, tz_name: str) -> datetime:
    return from_ms(ms).astimezone(ZoneInfo(tz_name))


def local_iso(ms: int, tz_name: str) -> str:
    return local_dt(ms, tz_name).isoformat()


def parse_local(text: str, tz_name: str) -> datetime:
    """Parse an ISO local wall-clock string (no offset) for display helpers."""
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        dt = dt.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)
    return dt


def resolve_local(naive: datetime, tz_name: str) -> tuple[int, str]:
    """Resolve a wall-clock time to an instant.

    Returns (instant_ms, status) where status is one of:
      "exists"       - unambiguous wall time
      "ambiguous"    - repeated wall time (DST fall-back); earlier instant used
      "nonexistent"  - skipped wall time (DST spring-forward); the first valid
                       instant of that day is used and the caller is told so.
    """
    tz = ZoneInfo(tz_name)
    candidate = naive.replace(tzinfo=tz, fold=0)
    other = naive.replace(tzinfo=tz, fold=1)
    roundtrip = candidate.astimezone(UTC).astimezone(tz)
    valid = (
        roundtrip.replace(tzinfo=None) == naive
        and roundtrip.utcoffset() == candidate.utcoffset()
    )
    ambiguous = valid and candidate.utcoffset() != other.utcoffset()
    if valid:
        return to_ms(candidate), ("ambiguous" if ambiguous else "exists")

    # Spring-forward: walk forward minute by minute to the first wall time that
    # actually exists (transitions are at most a couple of hours).
    probe = naive
    for _ in range(4 * 60):
        probe += timedelta(minutes=1)
        dt = probe.replace(tzinfo=tz, fold=0)
        back = dt.astimezone(UTC).astimezone(tz)
        if back.replace(tzinfo=None) == probe and back.utcoffset() == dt.utcoffset():
            return to_ms(dt), "nonexistent"
    raise ValueError(f"cannot resolve local time {naive} in {tz_name}")


def _entry(ms: int, tz_name: str, flags: list[str]) -> dict:
    dt = local_dt(ms, tz_name)
    return {
        "ts": ms,
        "local": dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "local_iso": dt.isoformat(),
        "offset_minutes": int(dt.utcoffset().total_seconds() // 60),
        "flags": flags,
    }


def build_grid(series: dict) -> list[dict]:
    """Build the analysis grid for a series.

    fixed: evenly spaced instants in UTC from window_start (inclusive) to
           window_end (exclusive).  Local labels shift across DST naturally.
    daily: one slot per local calendar date, anchored at local midnight.  A
           midnight skipped by DST is anchored at the first valid instant and
           flagged "anchor_shifted".
    """
    tz_name = series["timezone"]
    start = int(series["window_start"])
    end = int(series["window_end"])
    kind = series.get("interval_kind", "fixed")
    step = series.get("interval_seconds") or 3600

    if start >= end:
        raise ValueError("window_start must be before window_end")

    if kind == "daily":
        tz = ZoneInfo(tz_name)
        first_date = from_ms(start).astimezone(tz).date()
        last_exclusive = from_ms(end).astimezone(tz).date()
        slots = []
        d = first_date
        while d < last_exclusive:
            naive = datetime.combine(d, time(0, 0))
            ms, status = resolve_local(naive, tz_name)
            flags = []
            if status == "nonexistent":
                flags.append("anchor_shifted")
            if status == "ambiguous":
                flags.append("anchor_ambiguous")
            if ms >= end:
                break
            slots.append(_entry(ms, tz_name, flags))
            d += timedelta(days=1)
        if len(slots) > MAX_GRID_SLOTS:
            raise ValueError("grid too large")
        return slots

    if step <= 0:
        raise ValueError("interval_seconds must be positive")
    step_ms = int(step) * 1000
    count = math.ceil((end - start) / step_ms)
    if count > MAX_GRID_SLOTS:
        raise ValueError("grid too large; narrow the window")
    return [_entry(start + i * step_ms, tz_name, []) for i in range(count)]


def step_ms(series: dict) -> int:
    """Nominal step used for span arithmetic (daily handled separately)."""
    if series.get("interval_kind") == "daily":
        return 86_400_000
    return int(series.get("interval_seconds") or 3600) * 1000


def gap_span_ms(series: dict, gap: dict, grid: list[dict]) -> int:
    """UTC duration covered by a gap, including the final missing slot."""
    step = step_ms(series)
    if gap["end_idx"] + 1 < len(grid):
        return grid[gap["end_idx"] + 1]["ts"] - grid[gap["start_idx"]]["ts"]
    return grid[gap["end_idx"]]["ts"] + step - grid[gap["start_idx"]]["ts"]


def wall_span_minutes(series: dict, gap: dict, grid: list[dict]) -> int:
    """Wall-clock duration shown on a local calendar (differs across DST)."""
    tz_name = series["timezone"]
    start_local = local_dt(grid[gap["start_idx"]]["ts"], tz_name).replace(tzinfo=None)
    end_idx = min(gap["end_idx"] + 1, len(grid) - 1)
    end_local = local_dt(grid[end_idx]["ts"], tz_name).replace(tzinfo=None)
    if gap["end_idx"] + 1 >= len(grid):
        end_local += timedelta(milliseconds=step_ms(series))
    return int((end_local - start_local).total_seconds() // 60)


def offset_minutes(ms: int, tz_name: str) -> int:
    return int(local_dt(ms, tz_name).utcoffset().total_seconds() // 60)


def find_transitions(series: dict) -> list[dict]:
    """Locate UTC offset changes inside the window, to the minute."""
    tz_name = series["timezone"]
    start = int(series["window_start"])
    end = int(series["window_end"])
    probe = start
    prev_off = offset_minutes(start, tz_name)
    transitions = []
    guard = 0
    minute = 60_000
    while probe < end and guard < MAX_GRID_SLOTS:
        guard += 1
        # Probe in whole minutes so every sampled instant is a real boundary
        # candidate; offsets only change on minute boundaries.
        nxt = min(probe + 6 * 3600 * 1000, end)
        nxt = min(-(-nxt // minute) * minute, end)
        off = offset_minutes(nxt, tz_name)
        if off != prev_off:
            lo, hi = probe, nxt
            while hi - lo > minute:
                mid = -(((lo + hi) // 2) // minute) * minute
                if mid <= lo:
                    mid = lo + minute
                if offset_minutes(mid, tz_name) == prev_off:
                    lo = mid
                else:
                    hi = mid
            hi = -(-hi // minute) * minute
            before = local_dt(hi - 60_000, tz_name)
            after = local_dt(hi, tz_name)
            transitions.append(
                {
                    "ts": hi,
                    "offset_before": prev_off,
                    "offset_after": off,
                    "delta_minutes": off - prev_off,
                    "kind": "spring_forward" if off > prev_off else "fall_back",
                    "local_before": before.isoformat(),
                    "local_after": after.isoformat(),
                }
            )
            prev_off = off
        probe = nxt
    return transitions


def annotate_gap(series: dict, gap: dict, grid: list[dict], transitions: list[dict]) -> dict:
    tz_name = series["timezone"]
    start_ts = grid[gap["start_idx"]]["ts"]
    end_ts = grid[gap["end_idx"]]["ts"]
    crossing = [t for t in transitions if start_ts <= t["ts"] <= end_ts + step_ms(series)]
    off_a = offset_minutes(start_ts, tz_name)
    off_b = offset_minutes(end_ts, tz_name)
    return {
        "utc_minutes": gap_span_ms(series, gap, grid) // 60_000,
        "wall_minutes": wall_span_minutes(series, gap, grid),
        "offset_start": off_a,
        "offset_end": off_b,
        "crosses_transition": bool(crossing),
        "transitions": crossing,
    }


def tz_audit(series: dict, grid: list[dict], gaps: list[dict]) -> dict:
    """Validation report shown in the UI's timezone panel."""
    tz_name = series["timezone"]
    transitions = find_transitions(series)
    checks = []

    # Repeated wall-clock labels: DST fall-back makes an hour occur twice.  The
    # grid is keyed on instants, so both are retained; surface the duplication.
    by_local: dict[str, list[dict]] = {}
    for slot in grid:
        by_local.setdefault(slot["local"], []).append(slot)
    duplicates = {k: v for k, v in by_local.items() if len(v) > 1}
    if duplicates:
        sample = sorted(duplicates.items())[:3]
        checks.append(
            {
                "status": "warn",
                "code": "duplicate_local_time",
                "title": "存在重复的本地时刻",
                "detail": (
                    f"{len(duplicates)} 个本地时刻在回拨窗口中出现两次，"
                    "采样以 UTC 瞬时为准，两条记录均保留。"
                ),
                "evidence": [
                    {"local": k, "offsets": [s["offset_minutes"] for s in v]}
                    for k, v in sample
                ],
            }
        )

    # Skipped wall-clock labels: spring-forward hours do not exist locally.
    if series.get("interval_kind") != "daily":
        step_min = int((series.get("interval_seconds") or 3600) / 60)
        labels = set(by_local)
        missing_labels = []
        offset_labels = {(s["local"], s["offset_minutes"]) for s in grid}
        for t in transitions:
            if t["kind"] != "spring_forward":
                continue
            # Enumerate the local labels the jump skipped, then keep only those
            # that fall on this grid's own cadence (backward from the boundary).
            after_local = datetime.fromisoformat(t["local_after"]).replace(tzinfo=None)
            jump = t["delta_minutes"]
            tz = ZoneInfo(tz_name)
            skipped = []
            # Labels in [local_after - jump, local_after) were jumped over.
            # Keep those on the grid cadence and verify they are truly absent.
            for k in range(jump, 0, -1):
                cand_dt = after_local - timedelta(minutes=k)
                if (after_local - cand_dt).total_seconds() % (step_min * 60) != 0:
                    continue
                cand = cand_dt.strftime("%Y-%m-%dT%H:%M:%S")
                if cand in labels:
                    continue
                probe = cand_dt.replace(tzinfo=tz, fold=0)
                back = probe.astimezone(UTC).astimezone(tz)
                if back.replace(tzinfo=None) != cand_dt:
                    skipped.append(cand)
            if skipped:
                missing_labels.extend(skipped[:4])
        if missing_labels:
            checks.append(
                {
                    "status": "pass",
                    "code": "skipped_local_time",
                    "title": "春季前跳时段已正确跳过",
                    "detail": "不存在的本地时刻没有伪造采样点，网格按 UTC 等间隔推进。",
                    "evidence": [{"local": s} for s in missing_labels[:4]],
                }
            )

    shifted = [s for s in grid if "anchor_shifted" in s["flags"]]
    if shifted:
        checks.append(
            {
                "status": "warn",
                "code": "anchor_shifted",
                "title": "日界锚点因 DST 平移",
                "detail": "该日的本地零点不存在，锚点取当日第一个有效瞬时并标记。",
                "evidence": [{"local": s["local"], "local_iso": s["local_iso"]} for s in shifted],
            }
        )

    ambiguous_anchors = [s for s in grid if "anchor_ambiguous" in s["flags"]]
    if ambiguous_anchors:
        checks.append(
            {
                "status": "warn",
                "code": "anchor_ambiguous",
                "title": "日界锚点存在两次",
                "detail": "本地零点在回拨窗口内重复，锚点取较早的瞬时（fold=0）。",
                "evidence": [{"local_iso": s["local_iso"]} for s in ambiguous_anchors],
            }
        )

    gap_rows = []
    for g in gaps:
        info = annotate_gap(series, g, grid, transitions)
        gap_rows.append({**g, "tz": info})

    crossing = [g for g in gap_rows if g["tz"]["crosses_transition"]]
    if crossing:
        checks.append(
            {
                "status": "pass",
                "code": "gap_across_transition",
                "title": "跨 DST 的连续缺口已按 UTC 连续计数",
                "detail": (
                    "缺口槽位按 UTC 等间隔计算；跨转换时本地时长会比 UTC 时长少/多一段，"
                    "面板同时给出两种时长。"
                ),
                "evidence": [
                    {
                        "gap": f"{grid[g['start_idx']]['local_iso']} → {grid[g['end_idx']]['local_iso']}",
                        "slots": g["count"],
                        "utc_minutes": g["tz"]["utc_minutes"],
                        "wall_minutes": g["tz"]["wall_minutes"],
                    }
                    for g in crossing
                ],
            }
        )

    if not transitions:
        checks.append(
            {
                "status": "pass",
                "code": "no_transition",
                "title": "窗口内无时区转换",
                "detail": "所有槽位处于同一 UTC 偏移。",
                "evidence": [],
            }
        )

    return {
        "timezone": tz_name,
        "transitions": transitions,
        "checks": checks,
        "duplicate_local_count": len(duplicates),
        "shifted_anchor_count": len(shifted),
        "gaps": gap_rows,
    }
