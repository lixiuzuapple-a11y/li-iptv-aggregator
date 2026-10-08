# TASK-012 · 主报告

> 任务：**Playback Quality, Auto-Curation & Daily Reliability**
> 采样基准：**2026-10-07**（生产实测，非估算）
> 状态：**REVIEW**（不启动 TASK-013）

---

## 一句话

fixed 稳定性 **42/44 stable = 95.5%**（超过 §2 的 90% 目标，未降门槛）；
**8/8 频道**完整通过 failover + recovery 验证；**55 轮**生产 soak 零重启零残留；
全量回归 **873 passed / 0 failed**（TASK-001~011 零回归）。

> **本报告口径已按 REVIEW-01 §4 统一为最终状态**：§20 的 21 轮是**阶段快照**
> （当时只观察到 21 轮），全文最终数字一律用 **55 轮**；
> healthz 修复**已部署**到生产 release `997326580c86-20261008T010432Z`。

---

## §36 数字口径（先定口径，再看数字）

TASK-011 出现过「41/42/43 用同一个词」的混淆，本轮严格区分：

| 口径 | 值 | 含义 |
|---|---|---|
| canonical 库存 | **44** | 明确知道是哪些频道（curated 语义，§39） |
| currently published fixed | **43** | 真实写进 M3U 的固定条数 |
| stream 库存 | **174** | 所有候选线路（含未发布的） |
| stream 可用 | **144** | 已发布频道名下当前 eligible 的 |
| multi-stream 频道 | **43** | 有 ≥2 条线路 |
| single-stream 频道 | **1** | 只有 CCTV-16 奥林匹克 |
| 带 tvg-id 的条目 | **43** | 固定频道中带 tvg-id 的条数 |
| **EPG 交集命中** | **41** | 43 个 tvg-id 中在 XMLTV 存在的 |
| XMLTV `<channel>` 数 | **42** | EPG 文件里的频道总数 |
| M3U `#EXTINF` 总数 | **223** | 43 fixed + 180 动态（**随赛事变动，不是固定值**） |
| EPG `<programme>` 数 | **11,059** | — |

🚨 **「41/42/43」三个数字的口径完全不同**：
- **41** = 43 个 tvg-id 中命中 EPG 的（未命中 2 个：`France24.fr`、`FujianStraitsTV.cn`）
- **42** = XMLTV 自己的频道数
- **43** = 带 tvg-id/logo 的固定条目数

**「41/41」的分母是有 EPG 那批，不是 43。** TASK-011 §26 的教训保持有效。

---

## §2 fixed 稳定性硬目标

| 指标 | 值 | 目标 | 判定 |
|---|---|---|---|
| stable | **42** | — | — |
| degraded | 1 | — | — |
| failed | 1 | — | — |
| unknown | **0** | 不算 stable | ✅ |
| **stable_rate** | **95.5%** | ≥90% | ✅ **达标** |

**未降低任何门槛。** 三个门槛全部按 `liptv/stability.py` 的原值：
`min_samples=3`、`min_success_rate=0.8`、`max_last_success_age_hours=48`。

`unknown = 0` 是因为所有频道都攒够了样本（审计：174/174 stream 至少 6 个样本）。

**两个非 stable 频道（如实列出，不掩盖）**：

| 频道 | 状态 | 说明 |
|---|---|---|
| CCTV-5+ 体育赛事 | FAILED | 330 次探测 **322 成功** —— 见下方勘误 |
| 福建海峡卫视 | DEGRADED | 有可用线但成功率不足 |

🚨 **勘误（对比本任务早期记录）**：CCTV-5+ 早期记录是
「198 次探测 **0 成功**，全 TIMEOUT，已自然 skip」。
**2026-10-07 实测已完全改变**：330 次探测、**322 次成功**、跨 39 个小时。
它现在是**可播的**。早期结论已过期，本报告以实测为准。

这条印证了 TASK-011 的方法论：**任何可播结论都必须实测，历史记录会过期。**

---

## §9 统一错误分类

新增 `liptv/errors.py`，把 probe 的 11 类 + fetch 的 9 类**归一到 14 类统一术语**。

```
DNS_ERROR  CONNECT_ERROR  TIMEOUT
HTTP_4XX   HTTP_5XX       HTML_FAKE
INVALID_PLAYLIST  INVALID_MEDIA
SEGMENT_UNREACHABLE  SIGNED_DOWNSTREAM_UNREACHABLE
EMPTY_MEDIA  GEO_BLOCK_SUSPECTED  ENVIRONMENT_ERROR  UNKNOWN
```

关键判定：
- **HTTP 200 一律不等于 success**（`HTML_FAKE` 检出假 200）
- `403`/`451` → `GEO_BLOCK_SUSPECTED`
- 分片 404 **仅在显式 `segment=True` 时**才归 `SEGMENT_UNREACHABLE`
  （区分「源站整条挂了」与「分片索引对不上」—— TASK-011 §13 的教训）

**生产实测错误分布（近 7 日，18,562 次）**：

| 类别 | 次数 |
|---|---|
| TIMEOUT | 2,275 |
| CONNECT_ERROR | 775 |
| INVALID_MEDIA | 424 |
| HTTP_4XX | 110 |

---

## §10 / §11 稳定性派生层

新增 `liptv/stability.py`：STABLE / DEGRADED / FAILED / UNKNOWN 四态。

**冻结保证（结构性，不是靠约定）**：

```python
# selected_stream_id 直接调 select_best_stream 的真实结果
best = select_mod.select_best_stream(conn, cid, now=reference, ...)
```

派生层**结构上不可能**与 selector 分叉 —— 它没有另算一套。
测试 `test_r1_negative_selector_still_drives_selection` 钉死这一点。

**UNKNOWN 绝不算 STABLE**，由 `test_r1_negative_unknown_must_not_be_stable` 固定。

**未改动 selector 任何一行**（§4 冻结原则）。所有增强都是最小且有真实缺陷证据的。

---

## §17 dynamic preflight

见 `REPORTS/TASK-012-DYNAMIC-PREFLIGHT.md`。

**本轮修掉的最危险缺陷**：本机 FlClash 代理把「连不上的端口」改写成
`HTTP 502`。照字面判 FAIL 会让 **authoritative 源（JSNZKPG）整批删掉赛事**。
已把 502/503/504 降级为 `UNKNOWN`（永不排除），404 作为对照组不受影响。

生产机无代理，但本机有 —— 这条防护在任何本机验证中都会被踩到。

---

## §19 每日可靠性摘要

新增 `liptv/reliability.py` + CLI `reliability-status` / `stability-status` /
`dynamic-preflight`。

**生产实测人读输出**（≤13 行）：

```
[固定] 库存 44 / 已发布 43  稳定 42 退化 1 失败 1 未知 0  稳定率 95.5%
[线路] 共 174  可用 144  不可用 30  多线路频道 43
[切换] 可切频道 43（≥2 条可用线）  已恢复线路 0（窗口内失败过又成功）
[测活] 近 7 天 14978/18562 成功  中位启动 3020ms  p95 6879
[错误] TIMEOUT=2275  CONNECT_ERROR=775  INVALID_MEDIA=424  HTTP_4XX=110
[动态] jsnzkpg-sports: 抓取 75 发布 21 预检 fail -
[动态] korice-ppv: 抓取 180 发布 180 预检 fail -
[节目单] 频道 None 节目 None  末次成功 epoch 1791342438.3458035
[运行] 上次发布 2026-10-07T12:43:21+00:00  最近轮次 ...#21  结果 ok
[需关注] CCTV-5+ 体育赛事(无可用线)、福建海峡卫视(有可用线但退化)
[增长] 基准 latest_complete_day = 11262 行/天 ... 1 年外推 228.0 MiB
[节奏] 解析 5 轮，2 项偏离目标
[口径] 测活来自 shanghai-cloud，不代表家庭网络可播；UNKNOWN 不计入稳定分子。
```

**脱敏**：`redaction` 五项（stream_urls / signed_query / cookie /
authorization / vpn）全为 `false`，由测试固定。

### 🚨 本轮修的口径缺陷：会给出**相反结论**的增长估算

生产库只有两天历史：10-05（服务当天 14:25 才启动，**半天 762 行**）与
10-06（完整一天 **11,262 行**）。

| 基准 | 365 天外推 | 判定 |
|---|---|---|
| 全部历史平均（3,081/天） | 94.9 MiB | 不需要 retention |
| 两天平均（6,012/天） | 121.7 MiB | 不需要 retention |
| **最近完整日（11,262/天）** | **228.0 MiB** | **不需要 retention** |

本例三种口径**恰好同向**（都低于 512 MiB 阈值），结论没被误导 ——
**但这是运气**。若阈值落在 122～228 MiB 之间，任何错误口径都会给出错误结论。

修正为默认 `rate_source="latest_complete_day"`，并有回归测试钉死。
同时把每行字节从 `count(pageno) × page_size`（低估）改为 `dbstat` 的
`sum(pgsize)`（生产实测 58 → 131 B/行）。

**§23 结论：1 年 228.0 MiB < 512 MiB 阈值 ⇒ 不需要 retention。**
本轮**未执行任何清理**；`retention.py` 不含任何 `DELETE`/`VACUUM`（AST 测试固定）。

---

## §20 cadence

见 `OPERATIONS/RUNTIME-CADENCE.md`。

**生产实测：22～24 分钟一轮**（最近 6 轮间隔 1325s / 1376s / 1433s / 1440s / 1500s）。

⚠️ **两个数据源**：生产 `status_history=5`，`rounds` 只留 5 轮，
直接算间隔样本太少。因此 cadence 在轮次 < 6 时**自动退回
`probe_result` 时间分布推断**，并把判定边界放宽 34%（±1h 粒度误差）。
来源在输出的 `source` 字段如实标出。

**§20 三条硬禁止的当前状态**：

| 禁止项 | 状态 | 保障 |
|---|---|---|
| EPG 每 15 分钟 | ✅ 未发生 | `epg_refresh.min_seconds = 18000`（5h），测试钉死 |
| 全量 ffprobe 每 15 分钟 | ✅ 未发生 | `fixed_probe.min_seconds = 1200`（20min），测试钉死 |
| 每轮无意义写盘 | ✅ 已处理 | `publish()` 的 no-change（`rewritten=False`） |

EPG cadence **恒为 `UNKNOWN`** —— `runtime-status` 不保留 EPG 历史序列，
单点算不出间隔。编一个出来就是 §37 禁止的无证据结论。

---

## §21 publish no-change

`publish()` 现在比对新内容与现有文件：**逐字节一致则跳过重写**
（`rewritten=False`），保住真正的上一版、消除 mtime 抖动。
内容变化时照常原子重写。

**踩过的坑**：这个优化改变了「`previous` 何时产生」的既有语义，
一度让 `test_publish_replace_failure_keeps_current_and_previous` 失败。
已改为「每次发布前改内容」以覆盖真实写盘分支，并补负向验证
（关掉优化 → 4 项立即失败）。

---

## §22 / §23 / §24 soak 与资源

见 `REPORTS/TASK-012-SOAK.md`。

**55 轮 soak（最终）**：0 重启、0 僵尸、0 ffprobe 残留、0 临时文件、28.1 MiB 内存、
4 个 fd、DB 2.82 MiB、磁盘 14%。

> **阶段快照（非最终数字）**：写这份报告初稿时只观察到 **21 轮**，当时的结论是
> 「0 重启、0 残留」。后续生产持续运行到 **55 轮**，指标一致（同样 0 重启 0 残留）。
> 本节与全文一律以 **55 轮**为最终口径；21 轮仅作历史留痕。

⚠️ journal 里有 3 条 **10-04** 的 `start-limit-hit` 记录 —— **早于本任务**，
非本轮引入。本轮**未清理 journal**（清理是不可逆操作，需单独授权）。

---

## §27 failover / recovery

见 `REPORTS/TASK-012-FAILOVER.md`。

**8/8 生产频道完整通过**：selector stream id 变、publish URL 变、
canonical metadata 一字未动、恢复后**无需人工 reset** 即重新 eligible。

生产库全程 `mode=ro`，注入只在 SQLite 在线备份的副本上做。

⚠️ **A 段（生产自然换线证据）为空** —— 生产至今没出故障。
「没出过事所以没切换过」**不能**证明「能切换」，所以不作为通过依据。

---

## §29 failure smoke（F1–F10）

| # | 场景 | 覆盖 |
|---|---|---|
| F1 | fixed 源失败 → 老库存保留 | ✅ 测试 |
| F2 | selected 连续失败 → 切备用 | ✅ 测试 |
| F3 | all streams fail → canonical skip | ✅ 测试（且**不写空文件**） |
| F4 | recovery → 重新进候选 | ✅ 测试 + 生产副本验证 |
| F5 | JSNZKPG 坏 URL → authoritative 排除 | ✅ 测试 |
| F6 | KORICE 失败 → advisory only | ✅ 测试 |
| F7 | EPG 失败 → LKG | ✅ TASK-011 既有 |
| F8 | summary 写失败 → 不影响 live.m3u | ✅ 测试 |
| F9 | 环境故障 → 0 per-stream 假失败 | ✅ 测试 |
| F10 | publish 空 → LKG 不覆盖 | ✅ 测试 |

---

## §30 冻结回归 + §31 测试

**全量 873 passed / 0 failed**（5 分 46 秒，2026-10-08 收尾复跑）。

TASK-012 新增 **186 项**（`--collect-only` 实测计数）：

| 文件 | 项数 | 覆盖 |
|---|---|---|
| `test_task012.py` | 48 | §9 错误分类 / §10-11 派生 / §13 multi-stream |
| `test_task012b.py` | 62 | §17 preflight / §19 summary / §21 no-change |
| `test_task012c.py` | 76 | §20 cadence / §22-24 soak / §29 smoke / §31-32 守护 |

其中 `test_task012c.py` 的 `TestHealthzStaleRegression` 是本轮
`/healthz` 永久 stale 那个真 bug 的专项回归（含结构性负向验证）。

**负向验证**（换回旧实现必须 failed）：

| 关闭什么 | 立即失败 |
|---|---|
| cadence 判定阈值 | 2 项 |
| retention「排除今天」 | 1 项 |
| publish no-change 优化 | 4 项 |
| wall-clock 守卫 | 守卫本身被测 |

---

## 🚨 本轮修掉的 4 个真 bug

| # | Bug | 危害 | 发现方式 |
|---|---|---|---|
| 1 | **`/healthz` 永久 stale** | **运维会误判服务已死** | 生产 healthz 报 stale 但 mtime 更新 |
| 2 | **代理伪造状态码** | JSNZKPG 可能被整批删赛事 | 本机测试暴露 502 |
| 3 | **时间炸弹测试** | 7 日窗口一过就集体假失败 | 全量回归真实失败 |
| 4 | **增长口径污染** | 相反的 retention 结论 | 手算与摘要对不上 |
| 5 | **no-change 改 LKG 语义** | `previous` 不再产生 | 既有测试失败 |

**第 1 个（最严重）的详细分析见下文「本轮修掉的第 5 个真 bug」一节。**

**第 3 个的根因值得单说**：
`test_select.py` 调 `select_playlist` 没传 `now=`，用真实墙钟算 7 日窗口，
而 `conftest.NOW` 写死 `2026-09-30`。2026-10-07 恰好跨过 `+7d` 边界 ⇒ 引爆。

**防复发**：`tests/conftest.py` 新增 autouse 守卫 —— 时间敏感函数收到
`now=None` 直接 fail。守卫自身踩了三个坑（`**kwargs` 隐藏参数、
误列 `cadence.analyse`、误伤 CLI 路径），全部写成回归测试。

---

## 已知不足（如实列出，不掩盖）

1. **生产无自然换线证据** —— 未出过故障，8/8 全靠副本注入。
2. **preflight FAIL 路径无生产实证** —— 最近一轮两源都 ok。
3. **只验证了 top 8 频道** —— 43 个多线路频道里剩 35 个未逐一验证。
4. **未验证跨源切换**（唯一线路彻底消失需改 `stream.status`，属生产写）。
5. **固定 probe 22～24 分钟比 §20 参考值（30～60 min）快** ——
   原因是「轮间隔 ≈ 单轮执行时长」，非配置错误。stream 数翻十倍后
   会自然落入目标区间。
6. **`journal` 里有 10-04 的历史 warning** 未清理（清理是不可逆操作，需授权）。

---

## 🚨 本轮修掉的第 5 个真 bug：`/healthz` 永久 stale

**这是本轮最重要的发现，也是唯一一个「功能性故障」而非「显示不准」。**

### 生产现象（2026-10-07 21:38 CST 实测）

```json
{
  "status": "stale",
  "freshness": {
    "last_success_publish_at": "2026-10-07T12:43:21+00:00",
    "seconds_since_last_success": 3327,
    "stale_after_seconds": 2700,
    "is_stale": true
  },
  "playlist": { "last_modified": "2026-10-07T13:16:00+00:00" },
  "last_run": { "round_id": "2026-10-07T13:07:03+00:00#22",
                "publish_status": "DEGRADED_DYNAMIC_PARTIAL" }
}
```

**矛盾一目了然**：`last_success` 停在 12:43，但 `live.m3u` 的 mtime 是
**13:16**（新 33 分钟），round #22 在 **13:07** 刚跑过 —— 播放列表一直在正常刷新。

### 根因

`liptv/runtime.py` 的 `PUBLISHED_STATUSES` 只有两个：

```python
PUBLISHED_STATUSES = (
    publish_mod.STATUS_OK,
    publish_mod.STATUS_DEGRADED_FIXED_ONLY,
)   # ← 漏了 DEGRADED_DYNAMIC_PARTIAL
```

而 `DEGRADED_DYNAMIC_PARTIAL` 是 **TASK-008** 引入的 isolate 降级状态，
`publish.py` 明确写了它「文件已写出」「`EXIT_OK`」。

⇒ isolate 策略下**只要任一动态源失败**，每轮状态都是
`DEGRADED_DYNAMIC_PARTIAL` ⇒ `last_success_publish_at` **永不推进**
⇒ 45 分钟后 `/healthz` 永久 stale，**而服务完全正常**。

生产当时的实际情况正是如此：`jsnzkpg-sports` 抓取 75 条发布 21 条，
`korice-ppv` 抓取 180 条发布 180 条 —— 一源部分失败 ⇒ 走 isolate ⇒ 命中此 bug。

### 危害

运维看到 `/healthz` 报 stale 会**误判服务已死**，可能触发不必要的处置
（重启、restore-db 等不可逆操作）。项目红线明确禁止这类误判导致的操作。

### 修法

`PUBLISHED_STATUSES` 补上该状态，并加了一条**结构性约束**：

```python
# 测试 test_r3_published_statuses_match_exit_zero 固定：
set(PUBLISHED_STATUSES) == {所有 STATUS_EXIT == 0 且非 DRY_RUN 的状态}
```

以后 `publish.py` 新增任何 exit 0 的状态，若忘记同步，
**测试立刻失败**。这比「记得手动同步」可靠。

对照组测试 `test_r3_rejected_publish_does_not_advance` 确认
真正未发布（`REJECTED_EMPTY` / `DEGRADED_NO_PUBLISH`）**不推进**。

✅ **本修复已部署到生产**：release `997326580c86-20261008T010432Z`（2026-10-08）。

生产实测三项证据：

1. 该 release 内 `set(PUBLISHED_STATUSES) == {STATUS_EXIT 中 exit 0 且非 DRY_RUN}`
   完全一致（`['DEGRADED_DYNAMIC_PARTIAL','DEGRADED_FIXED_ONLY','OK']`）；
2. `/healthz` 返回 200 且 `status=ok`、`is_stale=false`；
3. 大G REVIEW-01 独立 spot-check 复核：`li-iptv.service` active+enabled、
   `NRestarts=0`、`NeedDaemonReload=no`，三端点全 200。

⚠️ 部署过程本身踩了一个坑（已记入运维文档）：`deploy upgrade` 默认**不启用
service manager**，会改 unit 文件但**不 stop/start**。判据是 `systemctl cat`
出现 `Warning: ... changed on disk`，必须手动 `daemon-reload && restart`。

---

## §45 明确不做 —— 逐条确认未做

Dashboard / Web GUI / Grafana / Prometheus / ELK / 用户系统 / 推荐系统 /
ML ranking / LLM selector / 视频代理 / 转码 / DVR / 录制 / CDN /
公网开放 / DNS / TLS / 多云部署 / 多地区 probe farm / 家庭常驻 agent /
VPN 自动切换 / schema V2 / 大规模频道扩容 / 第二 EPG 人工映射 /
为国际频道凑数量 —— **全部未做**。

**EV-Lab zero harm**：`ev-lab-task0006.service` 全程未触碰。

---

## §46 TASK-013 边界

**未启动 TASK-013，未碰 Apple TV。** 本任务状态置 `REVIEW` 后停止。

---

## 交付物清单（§35）

| 文件 | 状态 |
|---|---|
| `TASKS/TASK-012.md` | ✅ 任务书 |
| `REPORTS/TASK-012-REPORT.md` | ✅ 本文件 |
| `REPORTS/TASK-012-PROBE-AUDIT.md` | ✅ |
| `REPORTS/TASK-012-FAILOVER.md` | ✅ |
| `REPORTS/TASK-012-SOAK.md` | ✅ |
| `REPORTS/TASK-012-DYNAMIC-PREFLIGHT.md` | ✅（实现了 preflight 故产出） |
| `SOURCES/PLAYBACK-QUALITY-RECON-TASK012.md` | ✅ |
| `OPERATIONS/RUNTIME-CADENCE.md` | ✅ |

**新增源码**（全部只读派生层，不改 schema V1、不改 selector 打分）：

| 文件 | 作用 |
|---|---|
| `liptv/errors.py` | §9 14 类统一错误术语 |
| `liptv/stability.py` | §10-11 四态派生 |
| `liptv/preflight.py` | §17 动态预检 |
| `liptv/reliability.py` | §19 每日摘要 |
| `liptv/cadence.py` | §20 cadence 解析 |
| `liptv/retention.py` | §23 增长估算（**无 DELETE**） |

**新增 CLI**（3 个只读命令）：`reliability-status` / `stability-status` /
`dynamic-preflight`。

---

# 附录 A · REVIEW-01 返工（R1 日报闭环 + R2 文档口径）

Reviewer 结论：**CHANGES REQUESTED / 暂不 ACCEPT**。主体已通过，只做精准返工，
**不重做** stability 42/44、8/8 failover、preflight、KORICE advisory、
retention、no-change publish、healthz stale 修复本身。

## A1 · R1：日报从未真正进入生产无人值守闭环

### 缺口是什么

Reviewer 真机 spot-check 发现
`/var/lib/li-iptv-aggregator/reliability-summary.json` **不存在**。
根因不是配置写错，是**根本没有自动生成路径**：

- `reliability-status` 默认只打印，要人工加 `--write` 才落盘；
- scheduler / runtime / deploy **都没有**调用它；
- 结论就是「手工能生成，生产不会自动持续生成」。

这与「Daily Reliability / 长期无人值守」的目标直接矛盾 —— 报告里写着
可观测，实际上无人值守时那个文件永远不存在。

### 为什么坏在这里（不只是少个调用）

我原本把摘要落盘路径写死成 `/var/lib/li-iptv-aggregator/reliability-summary.json`
（`reliability.DEFAULT_SUMMARY_PATH`）。就算补上自动调用，这个写法在
**非默认前缀部署（`deploy install --root /opt/x`）** 时会把文件写到
服务账号无权访问的位置，然后**静默失败** —— 症状和现在一模一样，
只是从「没人调用」变成「调用了但写不出去」。

所以修法不止是「加一次调用」，而是先修路径推导，再挂自动。

### 实现：方案 A（挂现有 scheduler 每轮结束）

Reviewer 明确「优先 A，不要造新系统」。采纳。

| 步骤 | 实现 | 为什么这样 |
|---|---|---|
| 1 | `Scheduler(after_round=...)` | 轮次**状态落盘之后**才调用 —— 摘要里的 runtime 段必须含当轮，反了就永远慢一拍 |
| 2 | `_notify_after_round` 全吞异常 | 观测产物写不出来绝不能影响 `live.m3u`（F8 语义）；异常不外泄 ⇒ 本轮 `exit_code` 不变 |
| 3 | 钩子放在 `exit_code` 计算**之后** | 结构上就不存在「钩子改掉退出码」的可能 |
| 4 | `reliability.refresh_after_round()` 收敛全部失败 | `connect` / `inputs` / `build` / `write` 四种失败各自降级成返回字典里的 `stage` + `error` |
| 5 | `default_summary_path(status_path)` | 路径 = **运行数据目录** + `reliability-summary.json`。生产 status_path 在 `/var/lib/li-iptv-aggregator/` ⇒ 摘要正好落在 Reviewer 点名的位置，换前缀也不会跑偏 |
| 6 | 四种轮次一视同仁 | 成功 / 降级 / 拒绝发布 / 轮次异常**都**刷新。只在成功时刷新 = 故障时瞎掉 |
| 7 | `--no-reliability-summary` 逃生阀 | 默认开，但留一个不用改代码就能摘掉的开关 |
| 8 | `after_round_calls` / `after_round_failures` 进 CLI payload | 无人值守时能对账「到底有没有在每轮刷新、失败几次」 |

**没有新建 systemd timer / unit**（有测试钉死：`deploy/systemd/*.timer` 必须为空集）。

### Reviewer 九条验收条件逐条对照

| # | 条件 | 落实方式 | 测试 |
|---|---|---|---|
| 1 | 生产自动生成 | 挂 scheduler 每轮末尾 | `test_r1_hook_writes_summary_after_round` |
| 2 | service user 可读写 | 落在运行数据目录（同 `live.m3u`/`runtime-status.json`），不碰 `/etc` | `test_r1_summary_path_follows_runtime_data_dir`、`test_r2_summary_is_world_readable` |
| 3 | 原子写 | 既有 `.tmp` + `os.replace`（F8 实现原样复用） | `test_r1_write_is_atomic_no_partial_file` |
| 4 | 写失败不影响 publish | 异常全吞 + 放在 exit_code 之后 | `test_r1_write_failure_does_not_change_exit` |
| 5 | 只覆盖一个有界文件 | 固定单文件名，无 history 目录 | `test_r1_single_file_no_history_dir` |
| 6 | 无 URL/签名/Cookie/Auth/VPN | 沿用 `redaction` 五项，测试钉死 | `test_r1_summary_is_redacted` |
| 7 | 重启后继续更新 | 路径从 status_path 推导 ⇒ 跨重启恒定 | `test_r1_restart_continues_overwriting` |
| 8 | `generated_at` 随轮次推进 | 用轮次自己的 `finished_at` 做时间戳 | `test_r1_generated_at_advances_per_round` |
| 9 | 不新增 timer/dashboard | 无新 unit，AST/文件集合测试 | `test_r1_no_new_timer` |

### 一条容易被忽略的设计决定

`generated_at` 用**轮次自己的 `finished_at`**，不是「写盘瞬间的墙钟」。
理由：日报时间戳与 `runtime-status` 的轮次时间轴对齐，事后按时间对齐排查时
不会出现「文件比轮次新 3 秒」这种需要解释的偏差；`skipped`（锁丢失）
轮没有真实时间戳，回落到墙钟。

## A2 · R2：文档口径统一

Reviewer 指出正文说 healthz「尚未部署」、摘要说「已上线」，两者不能并存。
已按最终生产事实统一：

- §「`/healthz` 永久 stale」小节改为 **✅ 已部署** release
  `997326580c86-20261008T010432Z`，并列出三项生产/独立复核证据；
- soak 全文最终口径统一为 **55 轮**；原 21 轮保留但显式标注为
  **阶段快照（非最终数字）**，并说明后续运行到 55 轮指标一致；
- 开篇「一句话」段直接用 55 轮。

文档口径现在由测试钉死（`test_r2_no_stale_not_deployed_claim` /
`test_r2_soak_final_number_is_55` / `test_r2_summary_section_is_55_not_21`），
下次再写「尚未部署」这类旧快照会**测试直接失败**。

## A3 · 本轮新增测试

`tests/test_task012d.py`：**40 项**（R1 闭环 27 项 + R2 口径 6 项 +
文件卫生 3 项 + 参数装配 4 项）。

负向验证：把 `_notify_after_round` 改回「直接 `hook(entry)` 不吞异常」，
`test_r1_write_failure_does_not_change_exit` 必须 failed；
把钩子调用挪到 `record_round` 之前，`test_r1_hook_runs_after_status_recorded`
必须 failed。
