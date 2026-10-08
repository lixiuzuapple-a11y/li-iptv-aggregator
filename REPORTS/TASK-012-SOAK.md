# TASK-012 · SOAK 与资源增长报告

> §22 / §23 / §24 产出。**所有数字为 2026-10-07 生产实测**。
> 工具：`tools/soak_observe_t012.py`（只读）、`tools/prod_growth_t012.py`（只读）。
> 采样时刻：`2026-10-07T12:42:00Z`（= 20:42 CST）。
> 增长口径经 `tools/patch_retention_t012.py` 修正后在生产复验。

---

## 一句话

生产已连续运行 **21 个 scheduler 轮次**（远超 §22 最低要求 3 个周期），
期间 **0 次重启、0 僵尸进程、0 临时文件残留、0 资源泄漏迹象**；
数据增长稳态 **11,262 行/天**，外推一年 **228.0 MiB** ⇒ **不需要 retention**。

---

## §22 Soak 观测

### 采样时的真实状态

| 观测项 | 实测值 | 判读 |
|---|---|---|
| `systemctl is-active` | `active` | ✅ |
| `NRestarts` | **0** | ✅ 全程未重启 |
| `MemoryCurrent` | 29,442,048 B ≈ **28.1 MiB** | ✅ 2G 内存机，无压力 |
| `TasksCurrent` | 3 | ✅ 无线程膨胀 |
| `CPUUsageNSec` | 283,520,616,000 ≈ 283.5 s | 累计值，见下 |
| open fd | **4** | ✅ 极低 |
| 僵尸进程 | **0** | ✅ 无 ffprobe 残留 |
| ffprobe 子进程 | **0** | ✅ 测活已收敛，无堆积 |
| 磁盘 | 50G 用 6.6G（**14%**） | ✅ |
| SQLite | 2,961,408 B ≈ **2.82 MiB** | ✅ |
| journal_mode | `delete` | 见下 |
| DB size / page | 723 页 × 4096 B | — |

**关于 CPUUsageNSec**：283.5 秒是**服务自 13:15 启动以来**的累计 CPU 时间。
换算成平均占用极低。这不是「不算就没事」——它说明 22～24 分钟一轮的
调度间隔下，ffprobe 是**短时突发**而非持续占用。

### Scheduler 轮次历史

```
R #16  degraded  10:46:43 → 10:46:43  publish=DEGRADED_DYNAMIC_PARTIAL
R #17  ok        11:08:48 → 11:08:48  publish=OK
R #18  ok        11:31:44 → 11:31:44  publish=OK
R #19  ok        11:55:41 → 11:55:41  publish=OK
R #20  ok        12:19:48 → 12:19:48  publish=OK
R #21  ok        12:43:21 → 12:43:21  publish=OK
```

- 最近 5 轮**全部 `ok`**，轮间隔 **22～24 分钟**，高度稳定。
- #16 的 `degraded` 是 `DEGRADED_DYNAMIC_PARTIAL` —— 动态源部分失败、
  已成功源照常发布（`isolate` 策略的正确行为），不是事故。
- `started_at == finished_at` 说明状态记录里的时间戳只到秒级，
  单轮耗时不可从中读出（真实耗时要用 cadence 模块的推断路径，见
  `OPERATIONS/RUNTIME-CADENCE.md`）。

### §22 明令要求的观测项，逐条对照

任务书说「不能只看服务还活着」，逐项确认：

| 观测项 | 结论 |
|---|---|
| service restart | ✅ `NRestarts=0` |
| scheduler lock | ✅ 无异常（journal 无锁相关报错） |
| stale lock | ✅ 未出现 |
| temp 文件 | ✅ `*.tmp` / `*.tmp.*` / `live.*.m3u.tmp` **全为 0**；`tmp/` 目录**空** |
| previous | ✅ `live.previous.m3u` 存在（56,984 B） |
| publish summary | ✅ 7,968 B，覆盖式更新 |
| runtime status | ✅ 35,386 B，覆盖式更新 |
| epg status | ✅ 785 B，`error=None` |
| SQLite size | ✅ 2.82 MiB |
| journal error | ⚠️ 见下 |
| memory / CPU / disk | ✅ 28.1 MiB / 极低 / 14% |
| open fd | ✅ 4 |
| ffprobe child / zombie | ✅ 0 / 0 |
| repeated exception | ✅ 无 |

### ⚠️ journal 里的 3 条 warning（如实报告，非本轮引入）

```
Oct 04 13:32:24 li-iptv.service: Start request repeated too quickly.
Oct 04 13:32:24 li-iptv.service: Failed with result 'start-limit-hit'.
Oct 04 13:32:24 systemd[1]: Failed to start li-iptv.service ...
```

**时间：10-04 13:32**，早于本任务（10-07）。这是当时连续启动失败触发
systemd start-limit 留下的历史记录，**与 TASK-012 无关**。

当前 `is-active` = `active`、`NRestarts=0`，说明服务此后一直正常。

本轮**未**清理 journal（清日志是不可逆操作，且这些记录是审计线索）。
如需清理请单独授权。

---

## §23 资源与数据增长

### 实测速率

```
PROBE_ROWS  18388
first  2026-10-05T14:25:16+00:00
last   2026-10-07T12:19:48+00:00

DAY 2026-10-05   762      ← 冷启动日：服务当天 14:25 才起，只跑了半天
DAY 2026-10-06 11262      ← 第一个完整自然日
DAY 2026-10-07  6364      （当天未过完，20:42 采样）
```

🚨 **本报告第一版在这里算错了，修正如下。**

初版用「两天平均」= **6012 行/天** 做外推基准。这个数是错的：
10-05 是**冷启动日**（半天数据），把它算进平均会把稳态速率拉低 47%。

正确基准是「最近一个完整自然日」= **11,262 行/天**
（`retention.rate_source` 默认 `latest_complete_day`）。

### 🚨 第二个错误：每行字节被低估

初版用 `count(pageno) × page_size` 算每行字节，得 58 B/行。
这个口径把索引页/overflow/freelist 的处理搞混了。改为
`dbstat` 的 `sum(pgsize)` 后得 **131 B/行**。

### 修正后的最终数字（生产实测，已验证）

| 周期 | 行数 | 体积 |
|---|---|---|
| 30 天 | 337,860 | 18.7 MiB |
| 180 天 | 2,027,160 | 112.4 MiB |
| 365 天 | 4,110,630 | **228.0 MiB** |

对照口径（若用被污染的均值 6012）：365 天 = 2,194,380 行 / 121.7 MiB。

**两个口径同向**（都低于 512 MiB/年阈值）⇒ 结论对口径选择不敏感，稳健。

### 结论

- 稳态 **11,262 行/天**，按 44 canonical / 174 stream 算，
  每条 stream 每天约 65 次测活 —— 与 22～24 分钟一轮的调度吻合。
- 1 年外推 **228.0 MiB**，**低于** 512 MiB 阈值 ⇒
  **结论：不需要 retention**（§23 要求的「明确结论」）。
- 当前 DB 实际只有 **2.82 MiB**，50G 盘用了 14%。
- **本轮不执行任何清理**（§23 明令：默认只设计不执行）。
- `liptv/retention.py` **不含任何 `DELETE` / `VACUUM`**，由测试用 AST 检查固定。

### 方法论教训（本轮最有价值的一条）

同一份数据，取不同的基准可以得到**相反的**运维结论：

| 基准 | 365 天 | 判定 |
|---|---|---|
| 全部历史平均（3081/天） | 94.9 MiB | 不需要 retention |
| 两天平均（6012/天） | 121.7 MiB | 不需要 retention |
| **最近完整日（11262/天）** | **228.0 MiB** | **不需要 retention** |

本例三种口径**恰好同向**，所以结论没被误导。但这是运气 —— 如果阈值
恰好落在 122～228 MiB 之间，任何一种错误口径都会给出**错误结论**。

⇒ 「用最近一个完整自然日」已写成 `retention.py` 的默认行为，
并有回归测试 `test_r3_cold_start_day_does_not_drag_rate_down` 钉死。

---

## §24 日志与临时文件生命周期

| 检查项 | 结果 |
|---|---|
| `out/*.json` 覆盖式 | ✅ `runtime-status.json` / `epg-status.json` / `publish-summary.json` 均为覆盖写，体积稳定在 7～36 KB |
| dynamic temp 清理 | ✅ `tmp/` 目录**空**（0 个条目） |
| diagnostic temp 残留 | ✅ `*.tmp` 0 个、`*.tmp.*` 0 个、`live.*.m3u.tmp` 0 个 |
| debug 输出进生产 | ✅ 未发现 |
| 状态文件有界 | ✅ `status_history=5`（轮次封顶 5）、摘要只存聚合量 |
| ELK/Loki | ❌ **未引入**（任务书明令不需要） |

---

## 冻结边界

- 本轮**未重启服务、未清日志、未删任何生产数据**。
- 未修改 `journal_mode`（当前 `delete`；改 WAL 属 schema 级变更，需 Reviewer 批准）。
- 未引入定期重启来掩盖任何问题（当前 `NRestarts=0`，没有泄漏需要掩盖）。
- EV-Lab 未受任何触碰（`ev-lab-task0006.service` 不在本任务操作范围内）。
