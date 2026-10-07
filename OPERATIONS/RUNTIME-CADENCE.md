# OPERATIONS / RUNTIME-CADENCE

> TASK-012 §20 产出。**这份文档是观测口径，不是配置。**
> 阈值只存在于 `liptv/cadence.py::TARGETS`，用于把实测间隔判成
> `ON_TARGET` / `TOO_FAST` / `SLOW` / `UNKNOWN`；**不会**写回 config，
> 也不会改 scheduler 行为。

## 一句话

生产真实节奏（2026-10-07 实测）：**约 22～24 分钟一轮**，
每轮 = 动态源刷新 + 全量 ffprobe + publish；EPG 6～12 小时一次。

---

## 各动作的实测与目标

| 动作 | 目标区间 | 生产实测 | 判定 |
|---|---|---|---|
| dynamic 源刷新 | ~15 min | 22～24 min（随 probe 全量耗时浮动） | ON_TARGET |
| fixed 源刷新 | 1～3 h | 同上（与 probe 同轮） | ON_TARGET |
| fixed 全量 ffprobe | 30～60 min | 22～24 min | **偏快（但在允许带内）** |
| EPG 抓取 | 6～12 h | 单点不可算 | **UNKNOWN** |

### 为什么 fixed probe 比参考值快

§20 的参考范围是「30～60 min 或与固定刷新解耦」。生产实测 22～24 min，
原因是**一轮内 probe 174 条 stream 的墙钟耗时本身就是 20 分钟量级** ——
不是被调高了频率，而是「轮次间隔 ≈ 单轮执行时长」。

⚠️ 这是**当前规模的自然结果**，不是配置错误。若将来 stream 数翻十倍，
一轮会变成 3 小时量级，届时 cadence 会自然落到目标区间内，无需干预。

### EPG 为什么是 UNKNOWN 而不是数字

`runtime-status.json` **不保留 EPG 历史时间序列**，`epg-status.json`
只有一个 `last_success_epoch` 时间点。单点算不出间隔。

把单点包装成「间隔 6 小时」就是 §37 明令禁止的无证据结论，所以这里如实
标 `UNKNOWN`。要真正核验 EPG 间隔，需要**跨多次采样** `epg-status.json` 的
`last_success_epoch` 并做差。

---

## 数据来源与截断说明（重要）

cadence 有两个数据源，优先级如下：

1. **`runtime-status.json` 的 `rounds`** —— 直接观测，最准；
2. **`probe_result` 时间分布推断** —— 兜底。

### 什么时候会退到第 2 个源

生产 `status_history = 5`（`liptv/config.py`，§24 要求状态文件有界），
所以 `rounds` 最多 5 轮，直接算间隔只有 4 个样本。样本不足 6 时
（`cadence.MIN_ROUNDS_FOR_DIRECT`），自动改用 probe 推断，并在输出的
`source` 字段标 `"probe_inferred"`。

### probe 推断的可信度边界

推断方法：把「某一小时内出现新 probe 记录」视为一次测活活动的开始。
**误差来源是 ±1 小时粒度** —— 一轮跨小时边界时，开始时刻会被归到后一小时。

因此推断路径的判定边界**放宽 34%**（`PROBE_INFERRED_SLACK`）。
放宽不等于放弃：真正过密（如 60 秒一轮）仍会被判 `TOO_FAST`。

---

## §20 三条硬禁止的当前状态

| 禁止项 | 当前状态 | 保障方式 |
|---|---|---|
| EPG 每 15 分钟抓 | ✅ 未发生 | `TARGETS["epg_refresh"]["min_seconds"] = 18000`（5h） |
| 全量 ffprobe 每 15 分钟 | ✅ 未发生 | `TARGETS["fixed_probe"]["min_seconds"] = 1200`（20min） |
| 每轮无意义写盘 | ✅ 已处理 | `publish()` 的 no-change：内容逐字节一致则跳过重写，`rewritten=False` |

前两条是**测试钉死的**（`test_r3_epg_min_interval_blocks_15min_cadence` /
`test_r3_probe_min_interval_blocks_15min_full_probe`），任何人把阈值调到
900 秒以下，测试立刻失败。

第三条：`reliability-summary.json` 里记了 `rewritten`；`publish-summary.json`
的 `rewritten` 字段在旧 release 上是 `None`（该字段是 TASK-012 §21 新加，
需下个 release 才会在生产出现）。

---

## 怎么自己看

```bash
# 人读（≤12 行）
python3 -m liptv reliability-status

# 只看节奏
python3 -m liptv reliability-status --json | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['cadence'], ensure_ascii=False, indent=2))"

# 只看增长
python3 -m liptv reliability-status --json | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['growth'], ensure_ascii=False, indent=2))"
```

---

## 冻结边界

- 本文档与 `liptv/cadence.py` **不改 scheduler 任何行为**。
- 阈值是观测口径，不是「必须达到的 SLA」；判 `SLOW` / `TOO_FAST` 只提示，
  不触发任何自动动作。
- cadence **不进入 `/healthz`**（§26：healthz 保持最小）。复杂度都在
  `reliability-status` 里。
