"""Missing-interval detection and completion strategies.

Values are stored exactly as observed (``kind = 'observed'``) or as estimated
(``kind = 'estimated'``) and are never merged into one series.  Every strategy
is a pure function of (grid, observed points, params), so evaluating the same
strategy version twice is deterministic and replayable.

Strategies (``strategy_id@version``):
  linear         linear interpolation between bracketing observations
  ffill          forward fill from the previous observation
  seasonal       same-slot-of-day (or slot-1-day for daily grids) with a
                 fade toward the neighbouring observed mean over long gaps
  localmean      mean of the k nearest observations on each side
  drift          forward fill plus a linear drift estimated from the k
                 observations before and the k after the gap

Every strategy declares ``needs_right_context``: when False a completion can
be produced from history alone (streaming-friendly); when True the gap is
"blocked" until an observation after it arrives.
"""

from __future__ import annotations

import hashlib
import json
import math

STRATEGIES: dict[str, dict] = {
    "linear": {
        "version": 1,
        "label": "线性插值",
        "needs_right_context": True,
        "description": "用缺口两侧最近观测做直线插值；单侧不可用则退化为常数保持。",
        "param_schema": [],
    },
    "ffill": {
        "version": 1,
        "label": "前向填充",
        "needs_right_context": False,
        "description": "沿用缺口前最后一个观测值，直到下一次观测到达。",
        "param_schema": [],
    },
    "seasonal": {
        "version": 1,
        "label": "季节模型补全",
        "needs_right_context": False,
        "description": "取上一周期同槽观测（逐时=前一日，日值=上周同日）；长缺口衰减。",
        "param_schema": [
            {"name": "blend", "label": "季节权重", "type": "number", "min": 0, "max": 1, "step": 0.05, "default": 0.85},
            {"name": "fade_after", "label": "衰减起始(槽)", "type": "integer", "min": 1, "max": 2000, "default": 48},
        ],
    },
    "localmean": {
        "version": 1,
        "label": "局部均值模型",
        "needs_right_context": True,
        "description": "缺口左右各取 k 个最近观测求均值。",
        "param_schema": [
            {"name": "neighbors", "label": "单侧邻居数", "type": "integer", "min": 1, "max": 50, "default": 3},
        ],
    },
    "drift": {
        "version": 1,
        "label": "漂移模型",
        "needs_right_context": True,
        "description": "前向填值叠加由缺口两侧斜率估计的漂移，斜率截断以防发散。",
        "param_schema": [
            {"name": "window", "label": "斜率窗口", "type": "integer", "min": 1, "max": 50, "default": 3},
            {"name": "max_slope", "label": "斜率上限/槽", "type": "number", "min": 0, "max": 1000, "step": 0.1, "default": 5},
        ],
    },
}

DEFAULT_PARAMS = {
    "linear": {},
    "ffill": {},
    "seasonal": {"blend": 0.85, "fade_after": 48},
    "localmean": {"neighbors": 3},
    "drift": {"window": 3, "max_slope": 5},
}

# Registered strategy versions.  A version bump is a new immutable identity;
# existing runs keep pointing at the version they were computed with, and the
# replay endpoint refuses to mix versions.
VERSION_SPECS: dict[tuple[str, int], dict] = {
    (sid, spec["version"]): {
        "label": spec["label"],
        "description": spec["description"],
        "needs_right_context": spec["needs_right_context"],
        "param_schema": spec["param_schema"],
        "defaults": DEFAULT_PARAMS[sid],
        "changelog": "初始版本",
    }
    for sid, spec in STRATEGIES.items()
}
VERSION_SPECS.update({
    ("seasonal", 2): {
        "label": "季节模型补全 v2",
        "description": "降低季节权重、提前衰减；长缺口更保守，短缺口更贴近日周期。",
        "needs_right_context": False,
        "param_schema": [
            {"name": "blend", "label": "季节权重", "type": "number", "min": 0, "max": 1, "step": 0.05, "default": 0.7},
            {"name": "fade_after", "label": "衰减起始(槽)", "type": "integer", "min": 1, "max": 2000, "default": 24},
        ],
        "defaults": {"blend": 0.7, "fade_after": 24},
        "changelog": "blend 0.85→0.70；fade_after 48→24；补全值不再依赖右侧观测，可在数据到达前产出。",
    },
    ("linear", 2): {
        "label": "线性插值 v2",
        "description": "带单侧外推限制的线性插值：缺口过长时退化为端点常数，避免长直线虚构趋势。",
        "needs_right_context": True,
        "param_schema": [
            {"name": "max_span", "label": "最大插值跨度(槽)", "type": "integer", "min": 1, "max": 5000, "default": 24},
        ],
        "defaults": {"max_span": 24},
        "changelog": "新增 max_span：超过跨度的缺口不再插直线，改为端点保持。",
    },
})


def spec_for(strategy_id: str, version: int | None = None) -> dict:
    if version is None:
        version = max(v for (s, v) in VERSION_SPECS if s == strategy_id)
    spec = VERSION_SPECS.get((strategy_id, int(version)))
    if spec is None:
        raise ValueError(f"strategy {strategy_id}@{version} is not registered")
    return spec


def strategy_key(strategy_id: str, version: int) -> str:
    return f"{strategy_id}@{version}"


def params_hash(strategy_id: str, version: int, params: dict) -> str:
    payload = json.dumps(
        {"s": strategy_id, "v": version, "p": params}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def validate_params(strategy_id: str, params: dict, version: int | None = None) -> dict:
    """Coerce and bound params against the schema; reject unknown keys."""
    spec = spec_for(strategy_id, version)
    schema = {p["name"]: p for p in spec["param_schema"]}
    unknown = set(params) - set(schema)
    if unknown:
        raise ValueError(f"unknown params for {strategy_id}: {sorted(unknown)}")
    out = dict(spec["defaults"])
    for name, spec in schema.items():
        if name not in params:
            continue
        raw = params[name]
        try:
            value = int(raw) if spec["type"] == "integer" else float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"param {name} must be numeric")
        if isinstance(raw, bool):
            raise ValueError(f"param {name} must be numeric")
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"param {name} must be finite")
        value = max(spec["min"], min(spec["max"], value))
        out[name] = value
    return out


def detect_gaps(grid: list[dict], observed_idx: set[int], total_slots: int) -> list[dict]:
    """Group consecutive missing slots into intervals with first/last UI anchors.

    A gap stores its full index range plus up to ``first``/``last`` entries so
    a large missing run does not have to be materialised client-side.
    """
    gaps = []
    i = 0
    while i < total_slots:
        if i in observed_idx:
            i += 1
            continue
        start = i
        while i < total_slots and i not in observed_idx:
            i += 1
        end = i - 1
        gaps.append(
            {
                "start_idx": start,
                "end_idx": end,
                "count": end - start + 1,
                "first": grid[start],
                "last": grid[end],
            }
        )
    return gaps


def _initial(series: dict, grid: list[dict], idx: int) -> float:
    """Neutral fallback when a gap has no usable context at all."""
    return float(series.get("baseline", 0.0))


def _seasonal_period(series: dict, grid: list[dict], idx: int) -> int | None:
    if series.get("interval_kind") == "daily":
        # Daily data cycles weekly, not day-to-day; a 1-slot period would
        # collapse into forward fill inside a gap.
        return 7
    step = int(series.get("interval_seconds") or 3600)
    if step == 0 or 86_400 % step != 0:
        return None
    return max(1, 86_400 // step)


def complete_gap(
    series: dict,
    grid: list[dict],
    observed: list[float | None],
    gap: dict,
    strategy_id: str,
    version: int,
    params: dict,
    stats: dict,
) -> tuple[list[float], bool]:
    """Return (values, confident) for the slots of one gap.

    ``confident`` is False when the gap had no observation on at least one
    side and a fallback value was used; such values are stored with
    ``quality = 'fallback'`` so evaluation can separate them.
    """
    start, end = gap["start_idx"], gap["end_idx"]
    length = end - start + 1
    prev_idx = start - 1 if start > 0 else None
    next_idx = end + 1 if end + 1 < len(observed) else None
    prev_val = observed[prev_idx] if prev_idx is not None else None
    next_val = observed[next_idx] if next_idx is not None else None
    step = stats["step_ms"]

    if strategy_id == "ffill":
        if prev_val is None:
            return [_initial(series, grid, i) for i in range(start, end + 1)], False
        return [prev_val] * length, True

    if strategy_id == "linear":
        if prev_val is not None and next_val is not None:
            max_span = params.get("max_span")
            if max_span is not None and length > max_span:
                return [prev_val] * length, False
            span = (next_idx - prev_idx) * step
            out = []
            for i in range(start, end + 1):
                t = (i - prev_idx) * step / span
                out.append(prev_val + (next_val - prev_val) * t)
            return out, True
        anchor = prev_val if prev_val is not None else next_val
        if anchor is not None:
            return [anchor] * length, False
        return [_initial(series, grid, i) for i in range(start, end + 1)], False

    if strategy_id == "seasonal":
        period = _seasonal_period(series, grid, start)
        blend = params["blend"]
        fade_after = params["fade_after"]
        version = int(version)
        if period is None:
            # Irregular grid: no calendar periodicity; fall back to previous.
            if prev_val is None:
                return [_initial(series, grid, i) for i in range(start, end + 1)], False
            return [prev_val] * length, True
        if version == 1:
            out, confident = [], True
            for i in range(start, end + 1):
                ref = None
                j = i - period
                while j >= 0 and ref is None:
                    if observed[j] is not None:
                        ref = observed[j]
                    else:
                        j -= period
                if ref is None:
                    # No previous day yet: use trailing local window mean.
                    lo = max(0, i - period)
                    window = [v for v in observed[lo:i] if v is not None]
                    if window:
                        ref = sum(window) / len(window)
                    elif prev_val is not None:
                        ref = prev_val
                        confident = False
                    else:
                        ref = _initial(series, grid, i)
                        confident = False
                if prev_val is None or next_val is None:
                    confident = False
                fade = 1.0
                if length > fade_after:
                    fade = max(0.0, min(1.0, fade_after / length))
                base = prev_val if prev_val is not None else ref
                out.append(base + (ref - base) * blend * fade)
            return out, confident
        # v2: fully left-context only, fade applied per slot, no right anchor.
        out, confident = [], prev_val is not None
        for offset, i in enumerate(range(start, end + 1)):
            ref = None
            j = i - period
            while j >= 0 and ref is None:
                if observed[j] is not None:
                    ref = observed[j]
                else:
                    j -= period
            if ref is None:
                ref = prev_val
            if ref is None:
                ref = _initial(series, grid, i)
                confident = False
            fade = 1.0 if offset < fade_after else fade_after / (offset + 1)
            base = prev_val if prev_val is not None else ref
            out.append(base + (ref - base) * blend * fade)
        return out, confident

    if strategy_id in {"localmean", "drift"}:
        k = params.get("neighbors") or params.get("window") or 3
        left = [v for v in observed[max(0, start - k):start] if v is not None]
        right = [v for v in observed[end + 1:end + 1 + k] if v is not None]
        if strategy_id == "localmean":
            vals = left + right
            if not vals:
                return [_initial(series, grid, i) for i in range(start, end + 1)], False
            mean = sum(vals) / len(vals)
            return [mean] * length, bool(left and right)
        # drift
        if prev_val is None:
            anchor = next_val if next_val is not None else _initial(series, grid, start)
            return [anchor] * length, False
        slope = 0.0
        if left and right:
            left_mean = sum(left) / len(left)
            right_mean = sum(right) / len(right)
            slope = (right_mean - left_mean) / (length + 1)
            cap = params.get("max_slope", 5)
            slope = max(-cap, min(cap, slope))
        out = [prev_val + slope * (i - start + 1) for i in range(start, end + 1)]
        return out, bool(left and right)

    raise ValueError(f"unknown strategy {strategy_id}")


def complete_series(
    series: dict,
    grid: list[dict],
    observed: list[float | None],
    gaps: list[dict],
    strategy_id: str,
    version: int,
    params: dict,
    stats: dict,
) -> tuple[list[dict], list[dict]]:
    """Complete every gap; returns (estimated rows, blocked gaps).

    Blocked gaps are those with no left anchor at all (data begins in a gap),
    which no strategy can fill honestly; they are reported, not fabricated.
    """
    rows, blocked = [], []
    first_obs = next((i for i, v in enumerate(observed) if v is not None), None)
    for gap in gaps:
        # A leading gap has no previous observation, so forward-looking
        # strategies cannot produce an honest value and must not invent one.
        if gap["start_idx"] == 0 and first_obs is not None:
            blocked.append({**gap, "reason": "no_left_anchor"})
            continue
        values, confident = complete_gap(
            series, grid, observed, gap, strategy_id, version, params, stats
        )
        for i, value in zip(range(gap["start_idx"], gap["end_idx"] + 1), values):
            rows.append(
                {
                    "ts": grid[i]["ts"],
                    "idx": i,
                    "value": round(float(value), 6),
                    "quality": "estimated" if confident else "fallback",
                    "run_ts": stats["run_ts"],
                }
            )
    return rows, blocked
