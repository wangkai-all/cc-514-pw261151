# 稀疏时间序列分析 (Sparse Time-Series Analysis)

识别缺测区间、补全稀疏序列、把补全值与真实观测**分开保存**，并在新数据到达时
只重算受影响窗口；评估结果区分原始样本与补全样本，策略版本变化时可回放。

零依赖：Python 3.11 标准库 + SQLite，前端为原生 HTML/CSS/JS（无构建步骤）。

## 快速开始

```bash
python3 run.py --port 8765 --seed-demo
# 打开 http://127.0.0.1:8765
```

`--seed-demo` 会生成 4 个演示场景（春季前跳、秋季回拨、UTC 连续缺失、日界平移）。
也可以只启动后点击页面顶部的「载入场景」。

```bash
python3 -m unittest discover -s tests -v     # 34 个测试
```

## 设计要点

### 1. 时间以 UTC 瞬时为准

所有槽位以 Unix 毫秒作键；本地时间只用于展示与审计。因此：

* 春季前跳不存在的本地时刻**不会**被伪造成采样点（API 会拒绝 `local` 时间）；
* 秋季回拨重复的本地时刻**两条都会保留**（偏移不同），面板显式给出警告；
* 日值序列的日界锚点在本地零点不存在时平移到当日第一个有效瞬时，并标记
  `anchor_shifted`；
* 缺口的时长同时给出 **UTC 分钟**与**本地日历分钟**：跨前跳的缺口本地更长，
  跨回拨的缺口本地更短。

`/api/series/{id}/timezone-audit` 返回上述验证结果，页面「时区边界验证」面板逐条展示。

### 2. 观测与补全分开存放

| 表 | 内容 |
|---|---|
| `observation_versions` | 只追加的观测版本（迟到修正 = 新版本，不覆盖历史） |
| `completions` | 每策略 run 的补全值，`quality ∈ {estimated, fallback}` |
| `gap_eval_marks` | 补全槽被真实数据覆盖时，归档**当时的估计值 + 到达的真值** |
| `off_grid_points` | 落在网格之外的提交，单独留存供检查 |

补全值永远不会写入观测表；同一槽位同时存在观测与补全时，页面与 API 并列显示。

### 3. 增量重算只影响受影响窗口

一次投递只会重算：变化槽位本身 + 其两侧相邻的连续缺失段。其余补全值的
`computed_at` 保持不变（测试 `test_late_arrival_recomputes_only_affected_window`
断言这一点）。响应中返回 `affected_ranges`（可能多段）、`rows_written`、
`rows_preserved`。

### 4. 评估区分原始 / 补全 / 迟到真值

* **原始样本**：按 (序列, 槽位, 种子) 确定性掩蔽真实观测 → 重新补全 → 对真值计分
  （MAE / RMSE / 偏差 / MAPE / P90）。掩蔽与策略无关，因此策略间可比。
* **补全样本**：无真值的槽位，只报告数量、均值与策略间分歧，绝不计误差。
* **迟到真值**：补全后真实数据到达的槽位，用归档的估计值对到达值计分。

`/api/runs/{id}/evaluate` 返回 `sample_split.original|completed|late_arrival`；
`/api/compare` 在同一掩蔽集上比较多个 run。

### 5. 版本化与回放

策略身份是 `strategy_id@version`，参数哈希参与 run 的唯一键。内置已注册版本：

| 策略 | v1 | v2 |
|---|---|---|
| `linear` | 两侧直线插值 | 增加 `max_span`：超跨度退化为端点保持 |
| `seasonal` | 同槽季节 + 长缺口衰减，可读右侧 | 全左向、逐槽衰减，不依赖右侧数据 |
| `ffill` / `localmean` / `drift` | 仅 v1 | — |

`POST /api/runs/{id}/replay {as_of}` 只用 `received_ts <= as_of` 的观测重算，逐槽与当前
存储比对。在数据未变化时结果完全一致（可复现）；晚到数据到达后差异即为「当时未知」
的部分。`GET /api/version-diff?a=&b=` 给出两版本逐槽差异，并标出 DST 转换附近的槽位。

## 补齐策略

| id | 说明 | 右侧上下文 |
|---|---|---|
| `linear` | 两侧最近观测直线插值 | 需要 |
| `ffill` | 前向填充 | 不需要（可流式） |
| `seasonal` | 前一日同槽（日值为前一日），长缺口衰减 | v1 需要 / v2 不需要 |
| `localmean` | 缺口两侧各 k 个观测均值 | 需要 |
| `drift` | 前向填值 + 斜率估计漂移（截断） | 需要 |

窗口起始的连续缺失没有左锚点，任何策略都无法诚实补全：这些缺口被报告为
`blocked`，不会生成数值。

## API 摘要

```
GET  /api/strategies
GET  /api/series                      GET /api/series/{id}
GET  /api/series/{id}/gaps            GET /api/series/{id}/timezone-audit
GET  /api/series/{id}/runs            GET /api/series/{id}/batches
POST /api/series/{id}/ingest          { points:[{ts,value}], kind?, received_ts? }
POST /api/series/{id}/ingest-local    { points:[{local,value}] }  ← 拒绝 DST 不存在的时刻
POST /api/runs                        { series_id, strategy_id, version?, params? }
GET  /api/runs/{id}/evaluate?holdout=0.15&seed=7
GET  /api/compare?series_id=&runs=1,2,3&holdout=0.15
GET  /api/version-diff?a=&b=
POST /api/runs/{id}/replay            { as_of }
POST /api/demo                        { scenario }
```

## 演示场景

| key | 时区 | 检验点 |
|---|---|---|
| `dst_spring` | America/New_York | 14 槽缺口跨前跳：UTC 840 分 / 本地 900 分 |
| `dst_fall` | Europe/Berlin | 缺口跨回拨：UTC 360 分 / 本地 300 分；重复本地时刻 |
| `daily_utc` | UTC | 21 天连续缺失 + 窗口末尾 12 天缺失 |
| `santiago_daily` | America/Santiago | 本地零点不存在，日界锚点平移 |

## 代码结构

```
app/timeutil.py     网格构建、DST 解析与审计
app/strategies.py   策略注册表、版本、参数校验、缺口检测与补全
app/db.py           SQLite schema（观测/补全/归档/批次）
app/service.py      投递、增量重算、评估、比较、回放
app/demo.py         确定性演示数据
app/server.py       HTTP JSON API + 静态文件
static/             前端（原生 JS，手绘 SVG 图表）
tests/test_sparse.py  策略、时区、增量、回放、演示重置
tests/test_api.py     HTTP 路由、校验、共享连接回滚
```
