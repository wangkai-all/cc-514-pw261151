"use strict";

/* ------------------------------------------------------------------ helpers */
const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
};
const svgEl = (tag, attrs = {}) => {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    node.setAttribute(k, String(v));
  }
  return node;
};
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
// Math.min(...arr) blows the call stack on long series; fold instead.
const extent = (arr, initial = [Infinity, -Infinity]) =>
  arr.reduce(([lo, hi], v) => [Math.min(lo, v), Math.max(hi, v)], initial);
const fmtNum = (v, digits = 2) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1000) return v.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
  if (abs >= 100) return v.toFixed(1);
  return v.toFixed(digits);
};
const fmtCompact = (v) => {
  if (v === null || v === undefined) return "—";
  if (Math.abs(v) >= 10000) return (v / 1000).toFixed(1) + "K";
  return fmtNum(v);
};
const fmtLocal = (iso) => {
  if (!iso) return "—";
  return iso.replace("T", " ").replace(/([+-]\d\d:\d\d|Z)$/, "").slice(0, 16);
};
const pct = (v) => (v === null || v === undefined ? "—" : v.toFixed(1) + "%");
let CSS = {};
function readTokens() {
  const s = getComputedStyle(document.documentElement);
  const get = (name) => s.getPropertyValue(name).trim();
  CSS = {
    surface: get("--surface"), ink: get("--ink"), ink2: get("--ink-2"),
    muted: get("--ink-muted"), grid: get("--grid"), axis: get("--axis"),
    s1: get("--series-1"), s2: get("--series-2"), s3: get("--series-3"),
    s4: get("--series-4"), shade: get("--shade"), shadeWarn: get("--shade-warn"),
    good: get("--good"), warn: get("--warning"), critical: get("--critical"),
  };
}
if (window.matchMedia) {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    readTokens(); if (State.payload) renderAll();
  });
}

/* ------------------------------------------------------------------- state */
const State = {
  scenarios: [], seriesList: [], payload: null, strategies: [],
  runId: null, yMode: "value", evalData: null, compareData: null,
  highlightGap: null,
  // Bumped on every series/run switch; async loaders drop responses whose
  // token is stale so a slow request cannot repaint the panel with old data.
  viewToken: 0,
};

/* --------------------------------------------------------------------- API */
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body = null;
  try { body = await res.json(); } catch (_) { /* empty */ }
  if (!res.ok) throw new Error((body && body.error) || `请求失败 (${res.status})`);
  return body;
}

function showError(message) {
  const banner = $("#error-banner");
  if (!message) { banner.hidden = true; return; }
  banner.textContent = message;
  banner.hidden = false;
}
let toastTimer = null;
function toast(message) {
  const t = $("#toast");
  t.textContent = message;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
}
const busy = (btn, fn) => async (...args) => {
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "处理中…";
  try { return await fn(...args); }
  catch (err) { showError(err.message); }
  finally { btn.disabled = false; btn.textContent = old; }
};

/* ------------------------------------------------------------ chart scales */
function niceTicks(min, max, count = 5) {
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  const raw = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm >= 5 ? 10 : norm >= 2.5 ? 5 : norm >= 1.2 ? 2 : 1) * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step * 0.5; v += step) ticks.push(+v.toFixed(10));
  return { ticks, lo, hi };
}

function makeScale(domain, range) {
  const [d0, d1] = domain, [r0, r1] = range;
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  return (v) => r0 + (v - d0) * k;
}


const tooltip = () => {
  let node = $(".tooltip");
  if (!node) { node = el("div", { class: "tooltip" }); document.body.append(node); }
  return node;
};
function showTip(evt, html) {
  const tip = tooltip();
  tip.innerHTML = html;
  tip.classList.add("visible");
  const pad = 14;
  const rect = tip.getBoundingClientRect();
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + rect.width > window.innerWidth - 8) x = evt.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight - 8) y = evt.clientY - rect.height - pad;
  tip.style.left = Math.max(8, x) + "px";
  tip.style.top = Math.max(8, y) + "px";
}
function hideTip() { tooltip().classList.remove("visible"); }

/* ================================================================== charts */
/**
 * Main time-series chart: observed dots, completion line, gap shading.
 * One y-axis only.  Hover crosshair + tooltip; keyboard accessible focus.
 */
function timeSeriesChart(host, payload, runId, yMode) {
  host.innerHTML = "";
  const grid = payload.grid;
  const runs = payload.runs;
  const run = runs.find((r) => r.id === runId) || null;
  const completions = run ? (payload.completions[String(run.id)] || []) : [];
  const obs = payload.observations;
  const gaps = payload.gaps;
  if (!grid.length) { host.append(el("p", { class: "empty", text: "没有可绘制的槽位" })); return; }

  const width = Math.max(560, host.clientWidth || 900);
  const height = 320;
  const M = { top: 16, right: 22, bottom: 30, left: 54 };
  const iw = width - M.left - M.right;
  const ih = height - M.top - M.bottom;

  const compByIdx = new Map(completions.map((c) => [c.idx, c]));
  const obsByIdx = new Map(obs.map((o) => [o.idx, o]));
  const evalSamples = State.evalData && State.evalData.samples
    ? new Map(State.evalData.samples.map((s) => [s.idx, s])) : new Map();

  let yVals = [];
  if (yMode === "error") {
    for (const c of completions) {
      const o = obsByIdx.get(c.idx);
      if (o) yVals.push(c.value - o.value);
    }
    for (const s of evalSamples.values()) if (s.error !== null) yVals.push(s.error);
  } else {
    yVals = obs.map((o) => o.value).concat(completions.map((c) => c.value));
  }
  if (!yVals.length) yVals = [0, 1];
  let [minY, maxY] = extent(yVals);
  if (yMode === "error") {
    const m = Math.max(Math.abs(minY), Math.abs(maxY), 0.1) * 1.15;
    minY = -m; maxY = m;
  } else {
    const padY = (maxY - minY) * 0.08 || 1;
    minY -= padY; maxY += padY;
  }
  const { ticks, lo, hi } = niceTicks(minY, maxY, yMode === "error" ? 4 : 5);
  const x = makeScale([0, grid.length - 1], [M.left, M.left + iw]);
  const y = makeScale([lo, hi], [M.top + ih, M.top]);

  const svg = svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`, role: "img",
    "aria-label": `时间序列图，${grid.length} 个槽位，${gaps.length} 段缺测`,
  });

  // gridlines (hairline, solid, recessive)
  for (const t of ticks) {
    svg.append(svgEl("line", { x1: M.left, x2: M.left + iw, y1: y(t), y2: y(t), stroke: CSS.grid, "stroke-width": 1 }));
  }
  // zero rule for error mode
  if (yMode === "error" && lo <= 0 && hi >= 0) {
    svg.append(svgEl("line", {
      x1: M.left, x2: M.left + iw, y1: y(0), y2: y(0),
      stroke: CSS.axis, "stroke-width": 1,
    }));
  }

  // gap shading (+ optional highlight of one gap)
  for (const g of gaps) {
    const x0 = x(g.start_idx), x1 = x(g.end_idx);
    const w = Math.max(2, x1 - x0);
    const highlighted = State.highlightGap === g.key;
    svg.append(svgEl("rect", {
      x: x0, y: M.top, width: w, height: ih,
      fill: g.tz.crosses_transition ? CSS.shadeWarn : CSS.shade,
      stroke: highlighted ? CSS.s2 : "none", "stroke-width": highlighted ? 1.5 : 0,
      opacity: highlighted ? 1 : 0.85,
    }));
    if (w > 70) {
      const label = svgEl("text", {
        x: x0 + w / 2, y: M.top + 13, "text-anchor": "middle",
        fill: CSS.ink2, "font-size": 10.5,
      });
      label.textContent = `缺口 ${g.count}`;
      svg.append(label);
    }
  }

  // completion line (2px, series-2 for completed values)
  const compSorted = completions.slice().sort((a, b) => a.idx - b.idx);
  if (compSorted.length > 1) {
    const d = compSorted.map((c, i) => `${i === 0 ? "M" : "L"}${x(c.idx).toFixed(1)},${y(c.value).toFixed(1)}`).join(" ");
    svg.append(svgEl("path", {
      d, fill: "none", stroke: CSS.s2,
      "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round",
      opacity: 0.9,
    }));
  }

  // observed line (thin underlay so the series reads as continuous where dense)
  if (yMode === "value" && obs.length > 1) {
    const d = obs.map((o, i) => `${i === 0 ? "M" : "L"}${x(o.idx).toFixed(1)},${y(o.value).toFixed(1)}`).join(" ");
    svg.append(svgEl("path", {
      d, fill: "none", stroke: CSS.s1, "stroke-width": 1.4, opacity: 0.55,
      "stroke-linejoin": "round",
    }));
  }

  // markers: observed = filled; completed = hollow ring w/ 2px surface ring
  const markerStep = Math.max(1, Math.ceil(grid.length / 160));
  const markers = svgEl("g");
  if (yMode === "value") {
    obs.forEach((o) => {
      if (o.idx % markerStep !== 0 && o.idx !== obs[obs.length - 1].idx) return;
      const c = svgEl("circle", {
        cx: x(o.idx), cy: y(o.value), r: 3.2, fill: CSS.s1,
        stroke: CSS.surface, "stroke-width": 1.2,
      });
      attachPointEvents(c, o.idx);
      markers.append(c);
    });
    completions.forEach((c) => {
      const cx = x(c.idx), cy = y(c.value);
      const node = svgEl("circle", {
        cx, cy, r: 3.6, fill: CSS.surface,
        stroke: c.quality === "fallback" ? CSS.warn : CSS.s2, "stroke-width": 2,
      });
      attachPointEvents(node, c.idx);
      markers.append(node);
    });
  } else {
    // error bars: completion minus observation where both exist
    for (const c of completions) {
      const o = obsByIdx.get(c.idx);
      if (!o) continue;
      const err = c.value - o.value;
      const node = svgEl("circle", {
        cx: x(c.idx), cy: y(err), r: 3.2, fill: CSS.s2,
        stroke: CSS.surface, "stroke-width": 1.2,
      });
      attachPointEvents(node, c.idx);
      markers.append(node);
    }
    // evaluation holdout errors (justified by real truth)
    for (const s of evalSamples.values()) {
      if (s.error === null) continue;
      const node = svgEl("rect", {
        x: x(s.idx) - 2.4, y: y(s.error) - 2.4, width: 4.8, height: 4.8,
        fill: CSS.s1, stroke: CSS.surface, "stroke-width": 1.2,
      });
      attachPointEvents(node, s.idx);
      markers.append(node);
    }
  }
  svg.append(markers);

  // axes
  svg.append(svgEl("line", {
    x1: M.left, x2: M.left + iw, y1: M.top + ih, y2: M.top + ih,
    stroke: CSS.axis, "stroke-width": 1,
  }));
  for (const t of ticks) {
    const label = svgEl("text", {
      x: M.left - 8, y: y(t) + 3.5, "text-anchor": "end",
      fill: CSS.muted, "font-size": 10.5, style: "font-variant-numeric: tabular-nums",
    });
    label.textContent = yMode === "error" ? t.toFixed(1) : fmtCompact(t);
    svg.append(label);
  }
  // x ticks: 5 evenly spaced local labels
  const xCount = width < 700 ? 4 : 6;
  for (let i = 0; i < xCount; i++) {
    const idx = Math.round((i / (xCount - 1)) * (grid.length - 1));
    const slot = grid[idx];
    const label = svgEl("text", {
      x: x(idx), y: M.top + ih + 18,
      "text-anchor": i === 0 ? "start" : i === xCount - 1 ? "end" : "middle",
      fill: CSS.muted, "font-size": 10.5,
    });
    label.textContent = slot.local.slice(5, 16);
    svg.append(label);
  }
  // axis title
  const unit = payload.series.unit ? ` (${payload.series.unit})` : "";
  const yTitle = svgEl("text", {
    x: M.left, y: M.top - 4, fill: CSS.muted, "font-size": 10.5,
  });
  yTitle.textContent = yMode === "error" ? "补全误差 = 补全值 − 真实值" : `数值${unit}`;
  svg.append(yTitle);

  // hover hit layer
  const hit = svgEl("rect", {
    x: M.left, y: M.top, width: iw, height: ih, fill: "transparent",
    tabindex: 0, role: "application", "aria-label": "悬停或使用左右方向键查看槽位",
  });
  const cross = svgEl("line", {
    x1: 0, x2: 0, y1: M.top, y2: M.top + ih, stroke: CSS.axis,
    "stroke-width": 1, opacity: 0,
  });
  svg.append(cross);
  hit.addEventListener("mousemove", (e) => {
    const rect = svg.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * width;
    const idx = clamp(Math.round(((px - M.left) / iw) * (grid.length - 1)), 0, grid.length - 1);
    cross.setAttribute("x1", x(idx)); cross.setAttribute("x2", x(idx));
    cross.setAttribute("opacity", 1);
    showSlotTip(e, idx);
  });
  hit.addEventListener("mouseleave", () => { cross.setAttribute("opacity", 0); hideTip(); });
  let keyIdx = 0;
  hit.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    keyIdx = clamp(keyIdx + (e.key === "ArrowRight" ? 1 : -1), 0, grid.length - 1);
    cross.setAttribute("x1", x(keyIdx)); cross.setAttribute("x2", x(keyIdx));
    cross.setAttribute("opacity", 1);
    const r = hit.getBoundingClientRect();
    showTip({ clientX: r.left + (x(keyIdx) / width) * r.width, clientY: r.top + r.height / 2 },
      slotTipHtml(keyIdx));
  });
  svg.append(hit);

  function attachPointEvents(node, idx) {
    node.style.cursor = "crosshair";
    node.addEventListener("mousemove", (e) => showSlotTip(e, idx));
    node.addEventListener("mouseleave", hideTip);
  }

  function showSlotTip(e, idx) {
    showTip(e, slotTipHtml(idx));
  }

  function slotTipHtml(idx) {
    const slot = grid[idx];
    const o = obsByIdx.get(idx);
    const c = compByIdx.get(idx);
    const gap = gaps.find((g) => idx >= g.start_idx && idx <= g.end_idx);
    const tzTransition = State.payload.timezone_audit.transitions.find(
      (t) => Math.abs(t.ts - slot.ts) < 60 * 60 * 1000
    );
    const sample = evalSamples.get(idx);
    const rows = [];
    if (o) rows.push(`<div class="tt-row"><span>观测</span><b>${fmtNum(o.value, 3)}</b></div>`);
    if (c) rows.push(`<div class="tt-row"><span>补全${c.quality === "fallback" ? "（回退）" : ""}</span><b>${fmtNum(c.value, 3)}</b></div>`);
    if (o && c) rows.push(`<div class="tt-row"><span>补全 − 观测</span><b>${fmtNum(c.value - o.value, 3)}</b></div>`);
    if (sample && sample.error !== null) rows.push(`<div class="tt-row"><span>掩蔽误差</span><b>${fmtNum(sample.error, 3)}</b></div>`);
    const tags = [];
    if (gap) tags.push(`缺口 ${gap.count} 槽` + (gap.tz.crosses_transition ? " · 跨时区转换" : ""));
    if (tzTransition) tags.push(tzTransition.kind === "spring_forward" ? "时区前跳点" : "时区回拨点");
    if (slot.flags && slot.flags.includes("anchor_shifted")) tags.push("日界锚点平移");
    return `<div class="tt-title">${fmtLocal(slot.local_iso)}</div>${rows.join("") || '<div class="tt-row"><span>无数据</span></div>'}
      <div class="tt-tag">UTC 偏移 ${slot.offset_minutes >= 0 ? "+" : ""}${(slot.offset_minutes / 60).toFixed(1)}h${tags.length ? " · " + tags.join(" · ") : ""}</div>`;
  }

  host.append(svg);

  // legend
  const legend = $("#chart-legend");
  legend.innerHTML = "";
  const items = [
    { type: "dot", color: CSS.s1, label: "原始观测" },
    { type: "dot-hollow", color: CSS.s2, label: run ? `补全值 · ${run.key}` : "补全值" },
    { type: "swatch", color: CSS.shade, label: "缺口区间", border: true },
  ];
  if (gaps.some((g) => g.tz.crosses_transition)) {
    items.push({ type: "swatch", color: CSS.shadeWarn, label: "跨时区转换缺口", border: true });
  }
  for (const it of items) {
    const key = it.type === "dot" ? el("span", { class: "key-dot", style: `background:${it.color}` })
      : it.type === "dot-hollow" ? el("span", { class: "key-dot hollow", style: `color:${it.color}` })
      : el("span", { class: "key-swatch", style: `background:${it.color};${it.border ? "border:1px solid var(--border)" : ""}` });
    legend.append(el("span", { class: "item" }, [key, it.label]));
  }
}

/* ------------------------------------------------ horizontal comparison bars */
function comparisonChart(host, rows) {
  host.innerHTML = "";
  if (!rows || !rows.length) { host.append(el("p", { class: "empty", text: "暂无比较数据" })); return; }
  const width = Math.max(440, host.clientWidth || 640);
  const labelW = 116, valueW = 58;
  const rowH = 46, padTop = 18, padBottom = 26;
  const height = padTop + rows.length * rowH + padBottom;
  const iw = width - labelW - valueW;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "各策略 MAE 比较" });

  const values = rows.map((r) => r.mae).filter((v) => v !== null && v !== undefined);
  const maxV = Math.max(...values, 0.0001) * 1.12;
  const x = makeScale([0, maxV], [labelW, labelW + iw]);

  // axis ticks
  const { ticks } = niceTicks(0, maxV, 4);
  for (const t of ticks) {
    if (t > maxV * 1.02) continue;
    svg.append(svgEl("line", { x1: x(t), x2: x(t), y1: padTop - 6, y2: height - padBottom + 4, stroke: CSS.grid, "stroke-width": 1 }));
    const lbl = svgEl("text", { x: x(t), y: height - padBottom + 18, "text-anchor": "middle", fill: CSS.muted, "font-size": 10.5, style: "font-variant-numeric: tabular-nums" });
    lbl.textContent = t.toFixed(2);
    svg.append(lbl);
  }
  const best = Math.min(...values);
  rows.forEach((row, i) => {
    const cy = padTop + i * rowH + rowH / 2;
    const label = svgEl("text", { x: labelW - 10, y: cy + 4, "text-anchor": "end", fill: CSS.ink2, "font-size": 12 });
    label.textContent = row.label;
    svg.append(label);
    if (row.mae === null || row.mae === undefined) {
      const none = svgEl("text", { x: labelW + 6, y: cy + 4, fill: CSS.muted, "font-size": 11.5 });
      none.textContent = "无可用样本";
      svg.append(none);
      return;
    }
    const barH = 16;
    const bar = svgEl("rect", {
      x: labelW, y: cy - barH / 2, width: Math.max(2, x(row.mae) - labelW), height: barH,
      rx: 4, fill: CSS.s1,
    });
    // square at baseline, rounded at data end: overlay a small square at left
    const cap = svgEl("rect", { x: labelW, y: cy - barH / 2, width: Math.min(4, x(row.mae) - labelW), height: barH, fill: CSS.s1 });
    svg.append(bar); svg.append(cap);
    const val = svgEl("text", {
      x: x(row.mae) + 8, y: cy + 4, fill: CSS.ink, "font-size": 12,
      style: "font-variant-numeric: tabular-nums; font-weight:600",
    });
    val.textContent = fmtNum(row.mae, 3);
    svg.append(val);
    if (Math.abs(row.mae - best) < 1e-9) {
      const tag = svgEl("text", { x: labelW + 4, y: cy - 10, fill: CSS.ink2, "font-size": 10.5 });
      tag.textContent = "最优";
      svg.append(tag);
    }
    const hit = svgEl("rect", { x: labelW, y: cy - rowH / 2, width: iw, height: rowH, fill: "transparent" });
    hit.addEventListener("mousemove", (e) => {
      showTip(e, `<div class="tt-title">${row.label}</div>
        <div class="tt-row"><span>MAE</span><b>${fmtNum(row.mae, 3)}</b></div>
        <div class="tt-row"><span>RMSE</span><b>${fmtNum(row.rmse, 3)}</b></div>
        <div class="tt-row"><span>样本</span><b>${row.n ?? "—"}</b></div>
        <div class="tt-tag">原始样本（掩蔽真实观测后计分）</div>`);
    });
    hit.addEventListener("mouseleave", hideTip);
    svg.append(hit);
  });
  // axis rule
  svg.append(svgEl("line", { x1: labelW, x2: labelW, y1: padTop - 6, y2: height - padBottom + 4, stroke: CSS.axis, "stroke-width": 1 }));
  // table twin
  const table = $("#compare-table");
  table.innerHTML = "";
  const thead = el("thead", {}, [el("tr", {}, [
    el("th", { text: "策略" }), el("th", { class: "num", text: "MAE" }),
    el("th", { class: "num", text: "RMSE" }), el("th", { class: "num", text: "偏差" }),
    el("th", { class: "num", text: "样本" }), el("th", { class: "num", text: "回退值" }),
  ])]);
  const tbody = el("tbody", {}, rows.map((r) => el("tr", {}, [
    el("td", { text: r.label }),
    el("td", { class: "num", text: fmtNum(r.mae, 3) }),
    el("td", { class: "num", text: fmtNum(r.rmse, 3) }),
    el("td", { class: "num", text: fmtNum(r.bias, 3) }),
    el("td", { class: "num", text: r.n ?? "—" }),
    el("td", { class: "num", text: r.fallback ?? "—" }),
  ])));
  table.append(el("table", {}, [thead, tbody]));
  host.append(svg);
}

/* ------------------------------------------------ per-slot version diff bars */
function versionDiffChart(host, diffs) {
  host.innerHTML = "";
  if (!diffs || !diffs.length) { host.append(el("p", { class: "empty", text: "两个版本在所有槽位上一致" })); return; }
  const width = Math.max(560, host.clientWidth || 820);
  const height = 180;
  const M = { top: 14, right: 16, bottom: 26, left: 48 };
  const iw = width - M.left - M.right, ih = height - M.top - M.bottom;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "两版本补全值差异" });
  const maxIdx = extent(diffs.map((d) => d.idx))[1];
  const maxDiff = Math.max(extent(diffs.map((d) => d.diff), [0, 0])[1], 0.0001);
  const { ticks, lo, hi } = niceTicks(0, maxDiff, 4);
  const x = makeScale([0, maxIdx], [M.left, M.left + iw]);
  const y = makeScale([lo, hi], [M.top + ih, M.top]);
  for (const t of ticks) {
    svg.append(svgEl("line", { x1: M.left, x2: M.left + iw, y1: y(t), y2: y(t), stroke: CSS.grid, "stroke-width": 1 }));
    const lbl = svgEl("text", { x: M.left - 8, y: y(t) + 3.5, "text-anchor": "end", fill: CSS.muted, "font-size": 10.5, style: "font-variant-numeric: tabular-nums" });
    lbl.textContent = fmtCompact(t);
    svg.append(lbl);
  }
  const barW = clamp(iw / Math.max(1, diffs.length) * 0.6, 2, 16);
  for (const d of diffs) {
    const rect = svgEl("rect", {
      x: x(d.idx) - barW / 2, y: y(d.diff), width: barW, height: Math.max(1, M.top + ih - y(d.diff)),
      rx: Math.min(3, barW / 2), fill: CSS.s2,
    });
    rect.addEventListener("mousemove", (e) => showTip(e,
      `<div class="tt-title">${fmtLocal(d.local_iso)}</div>
       <div class="tt-row"><span>A</span><b>${fmtNum(d.a, 3)}</b></div>
       <div class="tt-row"><span>B</span><b>${fmtNum(d.b, 3)}</b></div>
       <div class="tt-row"><span>差异</span><b>${fmtNum(d.diff, 3)}</b></div>`));
    rect.addEventListener("mouseleave", hideTip);
    svg.append(rect);
  }
  svg.append(svgEl("line", { x1: M.left, x2: M.left + iw, y1: M.top + ih, y2: M.top + ih, stroke: CSS.axis, "stroke-width": 1 }));
  host.append(svg);
}

/* ================================================================== render */
function renderAll() {
  readTokens();
  if (!State.payload) return;
  renderStats(); renderMainChart(); renderGaps(); renderTzAudit();
  renderRunsSelectors(); renderBatches(); renderEval(); renderCompare();
  renderVersionControls();
}

function renderStats() {
  const p = State.payload;
  const row = $("#stats-row");
  row.innerHTML = "";
  const stats = p.stats;
  const run = p.runs.find((r) => r.id === State.runId);
  const completed = run ? run.completion_count : 0;
  const tiles = [
    { label: "网格槽位", value: stats.slots, hint: `${p.series.interval_kind === "daily" ? "每日" : p.series.interval_seconds + "s"} · ${p.series.timezone}` },
    { label: "原始观测", value: stats.observed, hint: `${pct(stats.observed / stats.slots * 100)} 覆盖` },
    { label: "缺测槽位", value: stats.missing, hint: `${stats.gaps} 段区间 · 最长 ${stats.longest_gap}` },
    { label: "补全值", value: completed, hint: run ? `${run.key} · 回退 ${run.fallback_count}` : "未选择策略", hero: true },
    { label: "补全后覆盖", value: pct((stats.observed + completed) / Math.max(1, stats.slots) * 100), hint: "观测 + 补全" },
  ];
  for (const t of tiles) {
    row.append(el("div", { class: "stat" }, [
      el("div", { class: "label", text: t.label }),
      el("div", { class: "value" + (String(t.value).length > 6 ? " sm" : ""), text: String(t.value) }),
      el("div", { class: "hint", text: t.hint }),
    ]));
  }
}

function renderMainChart() {
  const p = State.payload;
  if (!State.runId && p.runs.length) State.runId = p.runs[0].id;
  const host = $("#main-chart");
  timeSeriesChart(host, p, State.runId, State.yMode);
  $("#chart-sub").textContent =
    `实心点为原始观测，空心为补全值，阴影为缺口区间。${p.series.name} · ${p.series.description}`;
  // table view
  const tableHost = $("#series-table");
  tableHost.innerHTML = "";
  const run = p.runs.find((r) => r.id === State.runId);
  const comps = new Map((p.completions[String(State.runId)] || []).map((c) => [c.idx, c]));
  const obs = new Map(p.observations.map((o) => [o.idx, o]));
  const rows = p.grid.map((slot, idx) => ({ slot, idx, o: obs.get(idx), c: comps.get(idx) }));
  const limit = 600;
  const tbody = el("tbody", {}, rows.slice(0, limit).map(({ slot, idx, o, c }) => el("tr", {}, [
    el("td", { text: fmtLocal(slot.local_iso) }),
    el("td", { class: "num", text: o ? fmtNum(o.value, 3) : "—" }),
    el("td", { class: "num", text: c ? fmtNum(c.value, 3) : "—" }),
    el("td", {}, [o ? el("span", { class: "pill original", text: "原始" })
      : c ? el("span", { class: `pill ${c.quality}`, text: c.quality === "fallback" ? "补全·回退" : "补全" })
      : el("span", { class: "pill", text: "缺测" })]),
  ])));
  tableHost.append(el("table", {}, [
    el("thead", {}, [el("tr", {}, [el("th", { text: "本地时间" }), el("th", { class: "num", text: "观测值" }), el("th", { class: "num", text: "补全值" }), el("th", { text: "类别" })])]),
    tbody,
  ]));
  if (rows.length > limit) tableHost.append(el("p", { class: "chart-note", text: `仅显示前 ${limit} 行，共 ${rows.length} 槽位` }));
}

function renderGaps() {
  const p = State.payload;
  const host = $("#gaps-list");
  host.innerHTML = "";
  if (!p.gaps.length) { host.append(el("p", { class: "empty", text: "没有缺测区间" })); return; }
  for (const g of p.gaps) {
    const chips = [];
    const wallDiffers = g.tz.utc_minutes !== g.tz.wall_minutes;
    chips.push(el("span", { class: "chip", text: `UTC ${g.tz.utc_minutes} 分` }));
    if (wallDiffers) {
      chips.push(el("span", { class: "chip warn", text: `本地 ${g.tz.wall_minutes} 分` }));
    }
    if (g.tz.crosses_transition) {
      const t = g.tz.transitions[0];
      chips.push(el("span", { class: "chip warn", text: t.kind === "spring_forward" ? "跨前跳" : "跨回拨" }));
    } else {
      chips.push(el("span", { class: "chip good", text: "无时区偏移" }));
    }
    if (g.boundary) chips.push(el("span", { class: "chip info", text: g.boundary === "leading" ? "窗口起始边界" : "窗口末尾边界" }));
    const card = el("div", {
      class: "gap-card" + (g.boundary ? " boundary" : ""),
      tabindex: 0, role: "button",
      "aria-label": `缺口 ${g.count} 槽，${fmtLocal(g.start_local)} 至 ${fmtLocal(g.end_local)}`,
      onclick: () => {
        State.highlightGap = State.highlightGap === g.key ? null : g.key;
        renderGaps(); renderMainChart();
      },
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.target.click(); } },
    }, [
      el("div", { class: "gc-count", html: `${g.count}<span> 槽</span>` }),
      el("div", {}, [
        el("div", { class: "gc-main", text: `${fmtLocal(g.start_local)} → ${fmtLocal(g.end_local)}` }),
        el("div", { class: "gc-idx", text: `槽位 ${g.start_idx}–${g.end_idx} · 本地偏移 ${g.tz.offset_start >= 0 ? "+" : ""}${(g.tz.offset_start / 60).toFixed(1)}h → ${g.tz.offset_end >= 0 ? "+" : ""}${(g.tz.offset_end / 60).toFixed(1)}h` }),
        el("div", { class: "chips" }, chips),
      ]),
      el("div", { class: "gc-idx", text: State.highlightGap === g.key ? "● 已在图中高亮" : "点击高亮" }),
    ]);
    host.append(card);
  }
}

function renderTzAudit() {
  const a = State.payload.timezone_audit;
  const host = $("#tz-audit");
  host.innerHTML = "";
  $("#tz-badge").textContent = a.timezone;
  const transitionChips = a.transitions.map((t) => {
    const label = t.kind === "spring_forward" ? "春季前跳" : "秋季回拨";
    return el("span", { class: "chip warn", text: `${label} ${fmtLocal(t.local_before)} → ${fmtLocal(t.local_after)}` });
  });
  if (transitionChips.length) host.append(el("div", { class: "chips", style: "margin-bottom:6px" }, transitionChips));
  for (const c of a.checks) {
    host.append(el("div", { class: "audit-item" }, [
      el("div", { class: "ai-head" }, [
        el("span", { class: `status-dot ${c.status}`, "aria-hidden": "true" }),
        c.title,
      ]),
      el("div", { class: "ai-detail", text: c.detail }),
      c.evidence && c.evidence.length
        ? el("div", { class: "ai-evidence", text: c.evidence.slice(0, 4).map((e) => Object.entries(e).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join("/") : v}`).join(" ")).join(" · ") })
        : null,
    ]));
  }
}

function renderRunsSelectors() {
  const p = State.payload;
  const runSel = $("#run-select");
  runSel.innerHTML = "";
  for (const r of p.runs) {
    runSel.append(el("option", { value: r.id, selected: r.id === State.runId }, [`${r.key} · ${r.params ? JSON.stringify(r.params) : ""}`]));
  }
  runSel.onchange = () => {
    State.viewToken += 1;
    State.runId = Number(runSel.value);
    State.evalData = null;
    renderStats(); renderMainChart();
    loadEval();
  };
  // ingest gap select
  const gapSel = $("#ingest-gap");
  gapSel.innerHTML = "";
  for (const g of p.gaps) {
    gapSel.append(el("option", { value: g.key }, [
      `${g.count} 槽 · ${fmtLocal(g.start_local)}${g.boundary ? "（" + (g.boundary === "leading" ? "起始" : "末尾") + "）" : ""}`,
    ]));
  }
  // replay run select: keep the prior choice only if it exists in this series;
  // setting a value with no matching option would blank the control.
  const repSel = $("#replay-run");
  const prior = repSel.value;
  repSel.innerHTML = "";
  for (const r of p.runs) repSel.append(el("option", { value: r.id }, [`${r.key} #${r.id}`]));
  if (prior && p.runs.some((r) => String(r.id) === prior)) repSel.value = prior;
  else if (p.runs.length) repSel.value = String(p.runs[0].id);
}

function renderBatches() {
  const p = State.payload;
  const token = State.viewToken;
  api(`/api/series/${encodeURIComponent(p.series.id)}/batches`).then(({ batches }) => {
    if (token !== State.viewToken) return;  // series changed mid-flight
    const host = $("#batch-list");
    host.innerHTML = "";
    if (!batches.length) { host.append(el("p", { class: "empty", text: "尚无数据投递记录" })); return; }
    for (const b of batches) {
      host.append(el("div", { class: "batch-row" }, [
        el("div", {}, [
          el("div", { text: `#${b.id} · ${b.kind} · ${b.value_count} 点` }),
          el("div", { class: "br-meta", text: `${b.note || "无备注"} · ${b.duration_ms}ms` }),
        ]),
        el("div", { class: "br-range", text: b.affected_start_local ? `受影响 ${fmtLocal(b.affected_start_local)} → ${fmtLocal(b.affected_end_local)}` : "—" }),
        el("div", { class: "br-meta", text: `重算 ${b.rows_written} 槽 · 保留 ${b.rows_preserved} 历史估计 · ${b.gaps_touched} 缺口` }),
      ]));
    }
  }).catch(() => {});
}

/* --------------------------------------------------------------- eval view */
async function loadEval() {
  const p = State.payload;
  if (!State.runId) return;
  const token = State.viewToken;
  try {
    const data = await api(`/api/runs/${State.runId}/evaluate?holdout=0.15&seed=7`);
    if (token !== State.viewToken) return;  // series/run changed mid-flight
    State.evalData = data;
    renderEval();
    renderMainChart();
  } catch (err) {
    if (token === State.viewToken) showError(err.message);
  }
}

function renderEval() {
  const data = State.evalData;
  const host = $("#eval-summary");
  host.innerHTML = "";
  const tableHost = $("#eval-table");
  tableHost.innerHTML = "";
  const chartHost = $("#eval-chart");
  chartHost.innerHTML = "";
  $("#eval-legend").innerHTML = "";
  if (!data) { host.append(el("p", { class: "empty", text: "正在加载评估…" })); return; }
  const s = data.sample_split;
  const tiles = [
    { label: "原始样本 MAE", value: fmtNum(s.original.metrics.mae, 3), sub: `${s.original.evaluable}/${s.original.slot_count} 可评分 · 掩蔽真值` },
    { label: "原始样本 RMSE", value: fmtNum(s.original.metrics.rmse, 3), sub: "均方根误差" },
    { label: "补全样本槽位", value: s.completed.slot_count, sub: "无真值，仅报分布" },
    { label: "补全值均值", value: fmtNum(s.completed.mean, 2), sub: `观测均值 ${fmtNum(s.completed.observed_mean, 2)}` },
    { label: "迟到真值 MAE", value: fmtNum(s.late_arrival.metrics.mae, 3), sub: `${s.late_arrival.evaluable} 槽被真实数据覆盖` },
  ];
  for (const t of tiles) {
    host.append(el("div", { class: "eval-tile" }, [
      el("div", { class: "t-label", text: t.label }),
      el("div", { class: "t-value", text: String(t.value) }),
      el("div", { class: "t-sub", text: t.sub }),
    ]));
  }
  $("#eval-sub").textContent =
    `原始样本：${s.original.definition}；补全样本：${s.completed.definition}；迟到真值：${s.late_arrival.definition}。`;

  // chart: distribution of completion values vs observed, + error histogram
  const errs = data.samples.filter((x) => x.error !== null).map((x) => x.error);
  const compVals = data.samples.filter((x) => x.sample_class === "completed").map((x) => x.estimated);
  const legend = $("#eval-legend");
  legend.innerHTML = "";
  if (!errs.length) {
    // An empty histogram with a ±0.00 axis reads as broken data; say plainly
    // that this series has no held-out truth to score yet.  Clearing the
    // legend too keeps it from describing the previously selected series.
    chartHost.append(el("p", { class: "empty", text: "这一段没有可评分的原始样本（缺测段过长或样本过少），仅报告补全值分布。" }));
    if (compVals.length) {
      legend.append(el("span", { class: "item" }, [
        el("span", { class: "key-dot hollow", style: `color:${CSS.s2}` }),
        `补全样本 ${compVals.length} 槽（无真值，未计误差）`,
      ]));
    }
  } else if (errs.length || compVals.length) {
    renderEvalChart(chartHost, errs, compVals, s.completed.observed_mean);
  } else {
    chartHost.append(el("p", { class: "empty", text: "评估样本为空" }));
  }

  const thead = el("thead", {}, [el("tr", {}, [
    el("th", { text: "本地时间" }), el("th", { class: "num", text: "估计值" }),
    el("th", { class: "num", text: "真值" }), el("th", { class: "num", text: "误差" }),
    el("th", { text: "样本类别" }),
  ])]);
  const tbody = el("tbody", {}, data.samples.slice(0, 200).map((row) => el("tr", {}, [
    el("td", { text: fmtLocal(row.local_iso) }),
    el("td", { class: "num", text: fmtNum(row.estimated, 3) }),
    el("td", { class: "num", text: row.truth === null ? "—" : fmtNum(row.truth, 3) }),
    el("td", { class: "num", text: row.error === null ? "—" : fmtNum(row.error, 3) }),
    el("td", {}, [el("span", {
      class: `pill ${row.sample_class}${row.quality === "fallback" ? " fallback" : ""}`,
      text: row.sample_class === "original" ? "原始样本" : "补全样本",
    })]),
  ])));
  tableHost.append(el("table", {}, [thead, tbody]));
}

function renderEvalChart(host, errs, compVals, observedMean) {
  const width = Math.max(380, host.clientWidth || 520);
  const height = 200;
  const M = { top: 16, right: 16, bottom: 28, left: 44 };
  const iw = width - M.left - M.right, ih = height - M.top - M.bottom;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "误差分布与补全值分布" });
  const maxAbs = Math.max(0.0001, ...errs.map((e) => Math.abs(e)));
  const bins = 13;
  const counts = new Array(bins).fill(0);
  for (const e of errs) {
    const b = clamp(Math.floor(((e + maxAbs) / (2 * maxAbs)) * bins), 0, bins - 1);
    counts[b] += 1;
  }
  const maxCount = Math.max(1, ...counts);
  const x = makeScale([-maxAbs, maxAbs], [M.left, M.left + iw]);
  const y = makeScale([0, maxCount], [M.top + ih, M.top]);
  // gridlines
  const { ticks } = niceTicks(0, maxCount, 3);
  for (const t of ticks) {
    if (t > maxCount) continue;
    svg.append(svgEl("line", { x1: M.left, x2: M.left + iw, y1: y(t), y2: y(t), stroke: CSS.grid, "stroke-width": 1 }));
    const l = svgEl("text", { x: M.left - 8, y: y(t) + 3.5, "text-anchor": "end", fill: CSS.muted, "font-size": 10.5, style: "font-variant-numeric: tabular-nums" });
    l.textContent = String(t); svg.append(l);
  }
  const bw = iw / bins;
  counts.forEach((c, i) => {
    if (!c) return;
    const bx = M.left + i * bw;
    const by = y(c);
    svg.append(svgEl("rect", {
      x: bx + 1, y: by, width: Math.max(1, bw - 2), height: M.top + ih - by,
      rx: 2, fill: CSS.s1,
    }));
  });
  // zero rule
  svg.append(svgEl("line", { x1: x(0), x2: x(0), y1: M.top, y2: M.top + ih, stroke: CSS.axis, "stroke-width": 1 }));
  svg.append(svgEl("line", { x1: M.left, x2: M.left + iw, y1: M.top + ih, y2: M.top + ih, stroke: CSS.axis, "stroke-width": 1 }));
  const title = svgEl("text", { x: M.left, y: M.top - 4, fill: CSS.muted, "font-size": 10.5 });
  title.textContent = "原始样本误差分布 (补全 − 真值)";
  svg.append(title);
  const left = svgEl("text", { x: M.left, y: height - 6, "text-anchor": "start", fill: CSS.muted, "font-size": 10.5 });
  left.textContent = `−${fmtNum(maxAbs, 2)}`;
  const right = svgEl("text", { x: M.left + iw, y: height - 6, "text-anchor": "end", fill: CSS.muted, "font-size": 10.5 });
  right.textContent = `+${fmtNum(maxAbs, 2)}`;
  const mid = svgEl("text", { x: x(0), y: height - 6, "text-anchor": "middle", fill: CSS.muted, "font-size": 10.5 });
  mid.textContent = "0";
  svg.append(left, mid, right);
  const hover = svgEl("rect", { x: M.left, y: M.top, width: iw, height: ih, fill: "transparent" });
  hover.addEventListener("mousemove", (e) => {
    const rect = svg.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * width;
    const i = clamp(Math.floor((px - M.left) / bw), 0, bins - 1);
    const lo = -maxAbs + (i / bins) * 2 * maxAbs;
    const hi = -maxAbs + ((i + 1) / bins) * 2 * maxAbs;
    showTip(e, `<div class="tt-title">误差区间</div>
      <div class="tt-row"><span>范围</span><b>${fmtNum(lo, 2)} … ${fmtNum(hi, 2)}</b></div>
      <div class="tt-row"><span>样本</span><b>${counts[i]}</b></div>`);
  });
  hover.addEventListener("mouseleave", hideTip);
  svg.append(hover);
  host.append(svg);

  const legend = $("#eval-legend");
  legend.innerHTML = "";
  legend.append(el("span", { class: "item" }, [
    el("span", { class: "key-swatch", style: `background:${CSS.s1}` }),
    `原始样本误差 · n=${errs.length}`,
  ]));
  if (compVals.length) {
    legend.append(el("span", { class: "item" }, [
      el("span", { class: "key-dot hollow", style: `color:${CSS.s2}` }),
      `补全样本 ${compVals.length} 槽（无真值，未计误差）`,
    ]));
  }
}

/* ------------------------------------------------------------- compare view */
async function loadCompare() {
  const p = State.payload;
  const ids = p.runs.slice(0, 4).map((r) => r.id);
  if (ids.length < 2) return;
  const token = State.viewToken;
  try {
    const data = await api(`/api/compare?series_id=${encodeURIComponent(p.series.id)}&runs=${ids.join(",")}&holdout=0.15&seed=7`);
    if (token !== State.viewToken) return;  // series changed mid-flight
    State.compareData = data;
    renderCompare();
  } catch (err) {
    if (token === State.viewToken) showError(err.message);
  }
}

function renderCompare() {
  const host = $("#compare-chart");
  const p = State.payload;
  const data = State.compareData;
  if (!data) { host.innerHTML = ""; host.append(el("p", { class: "empty", text: "正在比较…" })); return; }
  const rows = data.runs.map((r) => ({
    label: `${r.key}`,
    mae: r.holdout.mae, rmse: r.holdout.rmse, bias: r.holdout.bias,
    n: r.holdout.n, fallback: r.coverage.fallback,
  }));
  comparisonChart(host, rows);
  const note = el("p", { class: "chart-note", text: `同一掩蔽集：${data.holdout.masked_slots} 个真实观测被掩蔽后补全；横轴为 MAE，越短越好。` });
  host.append(note);
  if (data.pairwise.length) {
    const pw = data.pairwise.map((x) => {
      const a = data.runs.find((r) => r.run_id === x.run_a);
      const b = data.runs.find((r) => r.run_id === x.run_b);
      return `${a.key} ↔ ${b.key}: 共享 ${x.shared_slots} 槽，平均差 ${fmtNum(x.mean_abs_diff, 3)}`;
    });
    host.append(el("p", { class: "chart-note", text: "补全样本一致性 — " + pw.join("；") }));
  }
}

/* -------------------------------------------------------------- version view */
function renderVersionControls() {
  const sel = $("#ver-strategy");
  const prior = sel.value;
  const byStrategy = {};
  for (const s of State.strategies) (byStrategy[s.strategy_id] ||= []).push(s);
  sel.innerHTML = "";
  for (const [sid, versions] of Object.entries(byStrategy)) {
    if (versions.length < 2) continue;
    sel.append(el("option", { value: sid }, [`${sid}（${versions.length} 个版本）`]));
  }
  if (prior) sel.value = prior;
  refreshVersionSelects();
}

function refreshVersionSelects() {
  const sid = $("#ver-strategy").value;
  const versions = State.strategies.filter((s) => s.strategy_id === sid).sort((a, b) => a.version - b.version);
  const aSel = $("#ver-a"), bSel = $("#ver-b");
  aSel.innerHTML = ""; bSel.innerHTML = "";
  versions.forEach((v) => {
    aSel.append(el("option", { value: v.key }, [`${v.key}`]));
    bSel.append(el("option", { value: v.key }, [`${v.key}`]));
  });
  if (versions.length) aSel.value = versions[0].key;
  if (versions.length) bSel.value = versions[versions.length - 1].key;
}

async function runVersionDiff() {
  const p = State.payload;
  const seriesId = p.series.id;
  const aKey = $("#ver-a").value, bKey = $("#ver-b").value;
  if (aKey === bKey) { showError("请选择两个不同的版本"); return; }
  let aRun = p.runs.find((r) => r.key === aKey);
  let bRun = p.runs.find((r) => r.key === bKey);
  if (!aRun || !bRun) {
    // Versions are first-class: create the missing run on demand, using the
    // registered defaults for that version.
    for (const key of [aKey, bKey]) {
      if (p.runs.some((r) => r.key === key)) continue;
      const [strategy_id, version] = key.split("@");
      await api("/api/runs", {
        method: "POST",
        body: JSON.stringify({ series_id: seriesId, strategy_id, version: Number(version), params: {} }),
      });
    }
    await refreshSeries();
    // refreshSeries awaits /api/series and keeps the same selected series, so
    // State.payload is now the refreshed payload for seriesId.
    const fresh = State.payload;
    aRun = fresh.runs.find((r) => r.key === aKey);
    bRun = fresh.runs.find((r) => r.key === bKey);
    if (!aRun || !bRun) { showError("无法创建该版本的 Run"); return; }
  }
  const data = await api(`/api/version-diff?a=${aRun.id}&b=${bRun.id}`);
  if (State.payload?.series?.id !== seriesId) return;  // user switched series
  const host = $("#version-result");
  host.innerHTML = "";
  host.append(el("div", { class: "diff-head" }, [
    el("span", {}, ["差异槽位 ", el("b", { text: String(data.changed) })]),
    el("span", {}, ["平均差 ", el("b", { text: fmtNum(data.mean_abs_diff, 3) })]),
    el("span", {}, ["最大差 ", el("b", { text: fmtNum(data.max_abs_diff, 3) })]),
    el("span", {}, ["A 独有 ", el("b", { text: String(data.only_a) }), " · B 独有 ", el("b", { text: String(data.only_b) })]),
    data.near_transition ? el("span", { class: "chip warn", text: `${data.near_transition} 槽位于 DST 转换附近` }) : null,
  ]));
  const chartHost = el("div", { class: "diff-chart" });
  host.append(chartHost);
  versionDiffChart(chartHost, data.diffs);
  host.append(el("p", { class: "chart-note", text: `${data.run_a.key} ${JSON.stringify(data.run_a.params)} → ${data.run_b.key} ${JSON.stringify(data.run_b.params)}；柱形为逐槽绝对差异。` }));
  const tbody = el("tbody", {}, data.diffs.slice(0, 120).map((d) => el("tr", {}, [
    el("td", { text: fmtLocal(d.local_iso) }),
    el("td", { class: "num", text: fmtNum(d.a, 3) }),
    el("td", { class: "num", text: fmtNum(d.b, 3) }),
    el("td", { class: "num", text: fmtNum(d.diff, 3) }),
  ])));
  const table = el("div", { class: "table-wrap compact-table", style: "margin-top:10px" }, [
    el("table", {}, [
      el("thead", {}, [el("tr", {}, [el("th", { text: "本地时间" }), el("th", { class: "num", text: "A" }), el("th", { class: "num", text: "B" }), el("th", { class: "num", text: "差异" })])]),
      tbody,
    ]),
  ]);
  host.append(table);
  await loadReplayPoints();
}

async function loadReplayPoints() {
  const p = State.payload;
  const seriesId = p.series.id;
  const token = State.viewToken;
  const runId = $("#replay-run").value;
  const sel = $("#replay-asof");
  const prior = sel.value;
  sel.innerHTML = "";
  if (!runId) return;
  const batches = (await api(`/api/series/${encodeURIComponent(seriesId)}/batches`)).batches;
  if (token !== State.viewToken) return;  // series changed mid-flight
  const options = [];
  options.push({ ts: p.series.window_start, label: "窗口起点（无数据）" });
  for (const b of [...batches].reverse()) {
    // received_ts (not created_ts) is the cut-off the replay compares against.
    options.push({ ts: b.created_ts, label: `批次 #${b.id} 到达时 · ${b.kind}` });
  }
  options.push({ ts: Date.now(), label: "现在（最新数据）" });
  options.sort((a, b) => a.ts - b.ts);
  for (const o of options) sel.append(el("option", { value: o.ts }, [o.label]));
  // An option value that is not in the new list would blank the control, so
  // only restore a timestamp that still exists; otherwise default to "now".
  if (prior && options.some((o) => String(o.ts) === prior)) sel.value = prior;
  else if (options.length) sel.value = String(options[options.length - 1].ts);
}

async function runReplay() {
  const seriesId = State.payload?.series?.id;
  const runId = $("#replay-run").value;
  const asOf = Number($("#replay-asof").value);
  if (!runId) { showError("请选择要回放的 Run"); return; }
  if (!Number.isFinite(asOf) || asOf <= 0) { showError("请选择有效的回放时点"); return; }
  const data = await api(`/api/runs/${runId}/replay`, {
    method: "POST", body: JSON.stringify({ as_of: asOf }),
  });
  if (State.payload?.series?.id !== seriesId) return;  // user switched series
  const host = $("#replay-result");
  host.innerHTML = "";
  const ok = data.identical;
  host.append(el("div", { class: `rr-verdict ${ok ? "verdict-ok" : "verdict-bad"}` }, [
    el("span", { class: `status-dot ${ok ? "pass" : "warn"}` }),
    ok ? "回放与存储结果完全一致（可复现）" : "回放与当前存储存在差异（数据晚到后已重算）",
  ]));
  host.append(el("div", { class: "kv" }, [
    el("div", {}, ["时点 ", el("b", { text: fmtLocal(data.as_of_local) })]),
    el("div", {}, ["当时观测 ", el("b", { text: String(data.observations_at_cutoff) }), " / 现在 ", el("b", { text: String(data.observations_now) })]),
    el("div", {}, ["一致 ", el("b", { text: String(data.matched) }), " · 不一致 ", el("b", { text: String(data.mismatched) })]),
    el("div", {}, ["仅存储 ", el("b", { text: String(data.only_stored) }), " · 仅回放 ", el("b", { text: String(data.only_replay) })]),
    el("div", {}, ["最大差 ", el("b", { text: fmtNum(data.max_abs_diff, 3) })]),
  ]));
  if (data.detail.length) {
    const rows = data.detail.slice(0, 60).map((d) => el("tr", {}, [
      el("td", { text: fmtLocal(d.local_iso) }),
      el("td", { class: "num", text: fmtNum(d.stored, 3) }),
      el("td", { class: "num", text: fmtNum(d.replayed, 3) }),
      el("td", { class: "num", text: fmtNum(d.diff, 3) }),
    ]));
    host.append(el("div", { class: "table-wrap compact-table" }, [el("table", {}, [
      el("thead", {}, [el("tr", {}, [el("th", { text: "本地时间" }), el("th", { class: "num", text: "存储" }), el("th", { class: "num", text: "回放" }), el("th", { class: "num", text: "差异" })])]),
      el("tbody", {}, rows),
    ])]));
  }
}

/* ---------------------------------------------------------------- ingestion */
async function submitIngest(evt) {
  evt.preventDefault();
  const p = State.payload;
  const gapKey = $("#ingest-gap").value;
  const gap = p.gaps.find((g) => g.key === gapKey);
  if (!gap) { showError("请选择一个缺口"); return; }
  const count = clamp(Number($("#ingest-count").value) || 1, 1, gap.count);
  const noisy = $("#ingest-noisy").checked;
  const gridMap = new Map(p.grid.map((g) => [g.ts, g]));
  // choose observations near the gap to estimate a plausible truth
  const pts = [];
  let lastVal = null;
  for (let i = gap.start_idx - 1; i >= 0; i--) {
    const o = p.observations.find((x) => x.idx === i);
    if (o) { lastVal = o.value; break; }
  }
  if (lastVal === null) lastVal = p.series.baseline;
  for (let k = 0; k < count; k++) {
    const idx = gap.start_idx + k;
    const slot = p.grid[idx];
    const v = noisy ? Math.max(0, lastVal + (Math.random() - 0.5) * Math.abs(lastVal) * 0.06) : lastVal;
    pts.push({ ts: slot.ts, value: +v.toFixed(3) });
  }
  const res = await api(`/api/series/${encodeURIComponent(p.series.id)}/ingest`, {
    method: "POST", body: JSON.stringify({ points: pts, kind: "late", note: "补测数据" }),
  });
  await refreshSeries();
  const host = $("#ingest-result");
  host.hidden = false;
  host.innerHTML = "";
  host.append(el("div", { text: res.message }));
  host.append(el("div", { class: "ir-grid" }, [
    el("div", {}, ["写入观测 ", el("b", { text: String(res.inserted) })]),
    el("div", {}, ["受影响槽位 ", el("b", { text: String(res.changed_slots) })]),
    el("div", {}, ["重算窗口 ", el("b", { text: res.affected_ranges.map((r) => `${r.start_idx}–${r.end_idx}`).join(", ") || "—" })]),
    el("div", {}, ["重算写回 ", el("b", { text: String(res.rows_written) })]),
    el("div", {}, ["保留历史估计 ", el("b", { text: String(res.rows_preserved) })]),
    el("div", {}, ["耗时 ", el("b", { text: res.duration_ms + " ms" })]),
  ]));
  toast("补测数据已写入，仅重算受影响窗口");
}

/* ---------------------------------------------------------------- scenario */
async function loadScenarios() {
  const { scenarios } = await api("/api/scenarios");
  State.scenarios = scenarios;
  const sel = $("#scenario-select");
  sel.innerHTML = "";
  for (const s of scenarios) sel.append(el("option", { value: s.key }, [`${s.name} · ${s.timezone}`]));
}

async function loadSeriesList() {
  const { series } = await api("/api/series");
  State.seriesList = series;
  const sel = $("#series-select");
  const prior = sel.value || State.payload?.series?.id;
  sel.innerHTML = "";
  for (const s of series) sel.append(el("option", { value: s.id }, [`${s.name}`]));
  if (prior && series.some((s) => s.id === prior)) sel.value = prior;
}

async function refreshSeries() {
  const sid = State.payload?.series?.id || $("#series-select").value;
  if (!sid) return;
  State.viewToken += 1;
  State.payload = await api(`/api/series/${encodeURIComponent(sid)}`);
  const runExists = State.payload.runs.some((r) => r.id === State.runId);
  if (!runExists) State.runId = State.payload.runs[0]?.id ?? null;
  renderAll();
  loadEval(); loadCompare();
  await loadReplayPoints();
}

async function selectSeries(sid) {
  State.viewToken += 1;
  State.payload = await api(`/api/series/${encodeURIComponent(sid)}`);
  State.runId = State.payload.runs[0]?.id ?? null;
  State.evalData = null; State.compareData = null; State.highlightGap = null;
  renderAll();
  loadEval(); loadCompare();
  await loadReplayPoints();
}

async function loadDemo(scenario) {
  await api("/api/demo", { method: "POST", body: JSON.stringify({ scenario, reset: true }) });
  await loadSeriesList();
  const demoId = `demo_${scenario}`;
  if (State.seriesList.some((s) => s.id === demoId)) {
    $("#series-select").value = demoId;
    await selectSeries(demoId);
  }
  toast("演示数据已载入");
}

/* -------------------------------------------------------------- wire events */
async function init() {
  readTokens();
  try {
    const [{ strategies }] = await Promise.all([api("/api/strategies"), loadScenarios()]);
    State.strategies = strategies;
  } catch (err) { showError(err.message); return; }
  await loadSeriesList();
  if (!State.seriesList.length) {
    await loadDemo("dst_spring");
  } else {
    await selectSeries(State.seriesList[0].id);
  }

  $("#load-scenario").onclick = busy($("#load-scenario"), () => loadDemo($("#scenario-select").value));
  $("#series-select").onchange = (e) => selectSeries(e.target.value);
  $("#run-eval").onclick = busy($("#run-eval"), loadEval);
  $("#run-compare").onclick = busy($("#run-compare"), loadCompare);
  $("#ingest-form").onsubmit = busy($("#ingest-form button"), submitIngest);
  $("#toggle-table").onclick = () => {
    const t = $("#series-table");
    t.hidden = !t.hidden;
    $("#toggle-table").textContent = t.hidden ? "查看表格视图" : "隐藏表格视图";
    $("#toggle-table").setAttribute("aria-expanded", String(!t.hidden));
  };
  document.querySelectorAll(".seg-btn").forEach((btn) => {
    btn.onclick = () => {
      document.querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      State.yMode = btn.dataset.y;
      renderMainChart();
    };
  });
  $("#ver-strategy").onchange = refreshVersionSelects;
  $("#diff-versions").onclick = busy($("#diff-versions"), async () => {
    try { await runVersionDiff(); } catch (err) { showError(err.message); }
  });
  $("#replay-run").onchange = loadReplayPoints;
  $("#run-replay").onclick = busy($("#run-replay"), async () => {
    try { await runReplay(); } catch (err) { showError(err.message); }
  });
  window.addEventListener("resize", () => {
    clearTimeout(window.__resizeTimer);
    window.__resizeTimer = setTimeout(() => {
      if (!State.payload) return;
      renderMainChart();
      if (State.evalData) renderEval();
      if (State.compareData) renderCompare();
    }, 160);
  });
  $("#footer-status").textContent = `已连接 · ${State.seriesList.length} 个序列`;
}

init();
