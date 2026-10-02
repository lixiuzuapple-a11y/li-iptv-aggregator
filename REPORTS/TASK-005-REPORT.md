# TASK-005 Execution Report

状态：REVIEW
Owner：老李
Executor：小W
Reviewer：大G
基线：TASK-004 ACCEPTED（[REVIEWS/TASK-004-REVIEW-03.md](../REVIEWS/TASK-004-REVIEW-03.md)）
基线 HEAD：`4df758a8b4805ab3c33e91ed44d9777d48b1e948`
实现提交：`7000834d0f4526fe37c54a89c0308f07f0860392`（12 文件，`+4077 / −32`；SHA 由「记录提交」`0128e50` 写入，**未使用 amend**；远端独立核验见 §13.5）
Review 01：**REJECT → 定向返工**（[REVIEWS/TASK-005-REVIEW-01.md](../REVIEWS/TASK-005-REVIEW-01.md)，受审 HEAD `0128e50`）：QA-005A 混合环境故障被写成单条流失败、QA-005B `heartbeat` 的 read-check-write TOCTOU
返工提交：`3a8ee8f82f3bec16a16cf115c78acbc346a2eccd`（8 文件，`+758 / −24`；见 §14.7，由后续一次「记录提交」写入，**未使用 amend**）

本轮**只**把「人工 / mock 写入 `probe_result`」升级为**真实固定频道测活**：
用本机 ffprobe 对固定库存 stream URL 做短时、受控、只读探测，把真实结果写进
**已有**的 `probe` / `probe_result`，并接进 TASK-001 的 selector 与 TASK-004 的 scheduler。

不部署云、不做多探针、不代理视频、不转码、**零 schema 改动**（`SCHEMA_VERSION` 仍为 `1`）、
**未修改 selector 算法**、**离线可全自动验收**。

---

## 1. 基线与变更

### 1.1 变更文件

| 文件 | 变更 | 说明 |
|---|---|---|
| `liptv/probe.py` | **新增**（1092 行） | 测活内核：argv 构造、子进程调用与超时终止、输出上限、能力检查、错误分类、JSON 解析、`ProbeObservation`、并发收集器、`run_round` |
| `liptv/repo.py` | 修改（+56） | 新增**唯一口径**的可测活库存查询 `list_probe_candidates()`（CLI 与 scheduler 共用） |
| `liptv/config.py` | 修改（+24） | `DEFAULT_CONFIG` 新增 `[probe]`（8 项）；新增 `probe_settings()` |
| `liptv/runtime.py` | 修改（+71 / −9） | `execute_round` 插入 ③ 测活阶段（sync 之后、publish 之前）；`_round_outcome` / `round_exit_code` 纳入环境级测活故障 |
| `liptv/cli.py` | 修改（+166） | 新增 `probe-check` / `probe-run` 两个命令及其参数；`run` 接线测活并输出 probe 行 |
| `config/config.example.toml` | 修改（+32） | 新增 `[probe]` 段，与代码默认值**逐字一致**（有断言） |
| `tests/test_probe.py` | **新增**（1049 行，**31 项**） | 全部离线，走真实子进程路径 |
| `tools/fake_ffprobe.py` | **新增**（227 行） | 离线假 ffprobe（16 种模式），**绝不访问网络** |
| `tools/demo_probe_pipeline.py` | **新增**（783 行） | §14 的十项离线端到端演示（**41/41 通过**） |
| `TASKS/TASK-005.md` | 修改（1 行） | 状态 `READY_FOR_EXECUTOR` → `REVIEW` |
| `README.md` | 修改（+86 / −2） | 新增「固定频道测活：`probe-check` / `probe-run`（TASK-005）」整节；状态表与结论段更新（原「真实多探针 ffprobe 测活仍未实现」修正为「本机单机已实现，多探针仍未实现」） |

合计：**6 改 + 4 新**；受版本控制文件的净改动 `+340 / −9`（`liptv/` 与 `config/` 代码部分）
外加 `README.md` 的文档改动（不含 4 个新文件）。

> 上表是**首版**的规模。Review 01 REJECT 之后的**定向返工**（QA-005A / QA-005B）另见 §14，
> 那一轮的改动规模、逐文件 blob 与测试证据都记在 §14 里，不与本表混算。

### 1.2 复用的既有资产（不重造）

| 需要的能力 | 直接复用 |
|---|---|
| 可测活库存口径 | `repo.list_probe_candidates`（新增，但由 `repo.sync_streams` / `stream_source` 既有权威结构派生） |
| probe 节点登记 | `repo.ensure_probe`（**既有**，不新增业务表） |
| 测活结果写入 | `repo.add_probe_result`（**既有** 13 字段；本 TASK **零** schema 改动） |
| 选线 | `select.score_streams` / `select.select_best_stream`（**未改一行**） |
| 脱敏 | `publish.redact_text` |
| 轮次编排 | `runtime.execute_round` / `Scheduler`（在既有顺序里插一段） |

**没有**任何通过 shell 拼接调用自身 CLI 的地方：`runtime` 直接调用库函数；
`tools/demo_probe_pipeline.py` 与测试才走 CLI 入口（作为端到端验证）。

### 1.3 未改动的冻结模块（blob 对照，逐字节）

| 文件 | blob SHA | 结论 |
|---|---|---|
| **`schema/schema_v1.sql`** | `64c8d0ea4f0dde8d05f49cec5bac10942fcf8e07` | **未动**（零 schema 改动，`SCHEMA_VERSION=1`） |
| **`liptv/select.py`** | `ff1650249e3c160943d69cd4a6ba6afa75945229` | **未动**（选线算法一行未改） |
| **`liptv/publish.py`** | `639522d759f693ab80dfb7b642a8806243d571b6` | **未动** |
| **`liptv/server.py`** | `28526e378dd8e22159d115202da044340aa17305` | **未动** |
| **`liptv/m3u.py`** | `652d4676778f0fad6f75be9a49e2eb04469739c7` | **未动** |
| `liptv/ingest.py` | `fec6cd3666eeb93a68dfa919d3e5558db01fa8a8` | 未动（`git status --porcelain` 未列出） |
| `liptv/fetch.py` | `1ed75e6d7a1a35ae2ad29c5cbd165728362f5df8` | 未动 |
| `liptv/db.py` | `76a168b78e5f6c24720c67beb375c0de37a8cd28` | 未动 |
| `liptv/util.py` | `b6b5d6d177ffffcaa54bfac91c87ae884bf7ee17` | 未动 |

前 5 行的 blob 已用 `git hash-object <工作区文件>` 与 `git ls-tree HEAD` **双向核对相等**；
后 4 行取 HEAD blob 且 `git status --porcelain` 只列出 6 个文件（`README.md`、
`config/config.example.toml`、`liptv/cli.py`、`liptv/config.py`、`liptv/repo.py`、`liptv/runtime.py`），
说明它们确实没被碰。

本机工作区 blob（供大G 独立核对）：

```
liptv/probe.py                e0308fd4a28df8ff92c87b6c67a2b8ff7701746c
tools/fake_ffprobe.py         40a755c3792e9f757ee847918e18ca37a33e1928
tools/demo_probe_pipeline.py  3bc8ca653c06304197c067e53de3c62600f17e6c
tests/test_probe.py           0ab98ea5393c647ad11d9f46107829db8a886de5
liptv/repo.py                 1959281fb733484639e31ff8d5c513b324b30baa
liptv/config.py               da3a5239543f2e9ecd48a747f7f46912008e4845
liptv/runtime.py              a10fd06aa29577863ded8cf67970a52bad77afb1
liptv/cli.py                  79096c3df88ffee621b4d1987dc82c6739a9c414
config/config.example.toml    360a5da0a0dcd78449b2b31de428245a08118703
README.md                     7674955d46eaa8642dfb4f8e0a5f29d6eb9f617b
```

---

## 2. ffprobe 能力与调用安全性

### 2.1 能力检查（`probe-check`）

```
python -m liptv probe-check --config config/config.toml [--json] [--ffprobe-path PATH]
```

* 只执行 `ffprobe -hide_banner -version`，**不请求任何 stream、不碰数据库**；
* 用**独立**预算 `CAPABILITY_TIMEOUT_SECONDS = 8.0`，刻意不用 `min(settings.timeout_seconds, …)`
  —— 否则把单条流的 `timeout_seconds` 配小会误判成「环境故障」；
* 成功 → 打印版本行，退出码 `0`；失败 → `error_type=FFPROBE_NOT_FOUND`，退出码 `1`
  （`EXIT_ENVIRONMENT`），并明确打印「ffprobe 缺失属环境级故障，此时写 0 条 `probe_result`，
  本工具不负责下载安装 ffmpeg」；
* 本 TASK **不**下载 / 安装 / 提交任何二进制。

### 2.2 调用形态：argv 数组 + `shell=False`

```python
argv = [*settings.command, "-hide_banner", "-nostdin", "-v", "error",
        "-print_format", "json", "-show_format", "-show_streams",
        "-analyzeduration", str(int(analyze_seconds * 1_000_000)),
        "-probesize", str(_probe_size_bytes(analyze_seconds)), url]
```

* `url` **恒为最后一个独立 argv**，不做任何拼接；
* `subprocess.Popen(..., stdin=DEVNULL, stdout=PIPE, stderr=PIPE, shell=False, close_fds=True)`；
* 有**静态护栏测试**：断言 `liptv/probe.py` 源码里不出现 `shell=True`；
* 有**行为测试**：URL 含 `token=SECRET&x=1` / `| whoami ; $(id)` 时，用 `argv` 模式原样回读 argv，
  证明 shell 元字符没被任何 shell 解释、URL 恰好是**一个**参数；
* `_probe_size_bytes(a) = max(32_768, min(int(a * 500_000), 5_000_000))`。

**`ffprobe_path` 支持数组**（可执行文件 + 前置参数），仍然 argv 直传。这不是取巧配置项，
而是一个**实测结论**：Windows 上没有可编译的假 ffprobe，而 `.cmd` 包装器实测**会篡改 argv**
（`token=SECRET&x=1` 被 cmd.exe 拆成两段、`| whoami ; $(id)` 触发 `'x' 不是内部或外部命令`），
所以离线测试必须用「真解释器 + 脚本」这种 argv 接缝，不能用 `.cmd`。

### 2.3 超时、终止、输出上限（全部实测）

| 项目 | 常量 / 做法 | 实测 |
|---|---|---|
| 单条流总超时 | `settings.timeout_seconds`（默认 12.0）→ `proc.wait(timeout)` | 用 `timeout_seconds=1.5` 打 `sleep` 流：`timed_out=True rc=1 elapsed_ms=1529` |
| 超时后清理 | `terminate → 等 2s → kill → wait`（`TERMINATE_GRACE_SECONDS`） | `_pid_alive(pid) is False` —— **无孤儿** |
| 停止请求收尾 | `ProcessRegistry.terminate_all()` | `rc=1 elapsed_ms=810 alive=False` |
| stdout 上限 | `STDOUT_CAP_BYTES = 262_144` | `len(stdout)=262144 truncated=True`（排水线程继续读掉剩余，**管道没堵死**） |
| stderr 上限 | `STDERR_CAP_BYTES = 8_192` | `len(stderr)=8192 truncated=True rc=1` |
| 媒体 payload | **不落盘**、不保存 | 只留内存里的 stdout 字节，解析完即丢 |

---

## 3. `ProbeObservation` 与字段映射（禁止猜测）

| schema 字段 | 写值的条件 | 何时写 `NULL` |
|---|---|---|
| `success` | exit 0 + JSON 可解析 + 至少一个 audio/video 流 + 结构不是空媒体 | 否则 |
| `error_type` | 失败时给 11 类之一（见 §4） | 成功时为 `NULL` |
| `startup_ms` | 成功时 = 从**启动子进程**到**拿到满足成功条件的媒体信息**的墙钟耗时（ms） | 失败时 `NULL`（实测样例 `887`） |
| `resolution_width` / `resolution_height` | 仅当存在 video 流且**同时**有 width 与 height | 纯音频 / 有 video 但缺宽高 / 失败 → `NULL` |
| `bitrate_kbps` | 依次取 `format.bit_rate` → 各 audio/video 流的 `bit_rate`，第一个可用的 `max(1, round(v/1000))` | 哪里都没有 → `NULL`（实测 `6128` / `96` / `None`） |
| `protocol` | 按 URL 的实际 scheme（`http` / `https` / …） | URL 不可解析 → `NULL` |
| `ipv_family` | **恒 `NULL`** | V1 无法可靠获得真实 IP 族，**不猜**、不为它扩 schema |
| `http_status` | **恒 `NULL`** | ffprobe JSON 不提供可靠状态码（HTTP 失败只体现为 stderr 文本） |
| `connect_ms` | **恒 `NULL`** | 无法把「连接建立」从总耗时里可靠剥离 |

写库前有断言：失败行**所有**测量字段必须为 `NULL`（测试 `test_failure_rows_never_invent_measurements`
与演示 §4 都验证了「失败行一个测量值都不许伪造」）。

> **纯音频无宽高不判失败**：`audio-only` 模式 → `success=1`、`resolution=NULL`、`bitrate=96`。
> **不能仅凭 HTTP 可连接就判可播**：必须有可解析的媒体信息且含 audio/video 流。

---

## 4. 错误分类

判定顺序（先 `timed_out`，再按 stderr 特征，最后兜底）：

| `error_type` | 判定依据 |
|---|---|
| `TIMEOUT` | `timed_out=True`（不看 stderr） |
| `HTTP_ERROR` | stderr 命中 `Server returned 4xx/5xx`、`HTTP error` 等 |
| `DNS_ERROR` | stderr 命中 `Failed to resolve hostname`、`Name or service not known` 等 |
| `TLS_ERROR` | stderr 命中 `TLS error`、`certificate verify failed`、`SSL` 等 |
| `CONNECT_ERROR` | stderr 命中 `Connection refused`、`Network is unreachable` 等 |
| `INVALID_MEDIA` | stderr 命中 `Invalid data found when processing input` 等 |
| `OUTPUT_INVALID` | exit 0 但 stdout 不是可解析 JSON / 缺 `streams` 数组 |
| `PROCESS_ERROR` | 其它非 0 退出码 |
| `FFPROBE_NOT_FOUND` | `Popen` 抛 `FileNotFoundError`（**环境级**） |
| `FFPROBE_START_FAILED` | `Popen` 抛其它 `OSError`（**环境级**） |
| `UNKNOWN` | 兜底（含探测函数自身抛异常的收敛） |

* **无法精确区分时给更宽的类别**，绝不猜一个更具体的类别；
* 环境级只有两类：`ENVIRONMENT_ERROR_TYPES = (FFPROBE_NOT_FOUND, FFPROBE_START_FAILED)`；
* **stderr 全文不落库**：`probe_result` 里只有 `error_type`；
  诊断摘要（≤400 字符）只出现在 CLI / `--json` 输出里，且先做「原 URL → `<url>`」替换、
  再过 `publish.redact_text`（有测试：故意让假 ffprobe 回显完整 URL，输出里只剩脱敏形态）；
* **环境错误绝不批量写成 stream failure**：见 §7.3。

---

## 5. 目标 stream 选择（唯一口径）

```sql
SELECT s.*, cc.name AS canonical_name FROM stream s
JOIN canonical_channel cc ON cc.id = s.canonical_channel_id
WHERE s.enabled = 1
  AND s.status <> 'stale'
  AND EXISTS (
    SELECT 1 FROM stream_source ss
    JOIN source_channel sc ON sc.id = ss.source_channel_id
    JOIN source src ON src.id = sc.source_id
    WHERE ss.stream_id = s.id AND sc.active = 1 AND src.kind <> ?
  )
ORDER BY s.id
```

四条排除条件一次到位：`enabled=0`、`status='stale'`、**无任何 `active=1` 的来源 provenance**
（orphan / 来源已失活）、来源 `kind='dynamic_event_m3u'`。

> `EXISTS(sc.active = 1)` 这一条**必须**保留：`repo.sync_streams` 只在「跨频道错链被清掉」时
> 才把 stream 标 `stale`；单纯把来源条目置 `active=0` 会保留历史链接、stream 仍是 `observed`。
> 演示 §2 与测试都单独构造了这一类（`status` 仍 `observed`）来证明它被挡住。

选择粒度：`--stream-id N`（单条）/ `--limit N`（本轮最多 N 条）/ `per_round_limit`（配置）。
演示 §2 实测：库里 2 条固定流 → 候选恰好 2 条；同一进程里还启用了一个动态赛事源，
但 `stream` 与 `stream_source` 里**都没有**任何动态来源链接、也没有任何带 `txSecret` 的 URL。

---

## 6. 并发与 SQLite

* 标准库 `concurrent.futures.ThreadPoolExecutor(max_workers=settings.max_concurrency)`，
  默认 `max_concurrency = 4`；`per_round_limit = 0` 表示不限条数但**仍受并发上限约束**；
* **worker 不碰数据库**：每个 worker 只返回一个结构化 `ProbeObservation`；
  主线程统一补齐 `canonical_name`、统一 `repo.add_probe_result`、**单次 `conn.commit()`**；
  因此没有跨线程共享 sqlite connection，也没有「每个 worker 各 commit」的重复写；
* 一条流异常不中断其它（`_safe_result` 把异常收敛成 `UNKNOWN` 观察值）；
* 有界等待：主循环用
  `wait(pending, timeout=STOP_POLL_SECONDS=0.25, return_when=FIRST_COMPLETED)`
  —— 修掉了一个真实缺陷：不给 `timeout` 时只在「某条探测自己结束」时才检查停止请求，
  4 条 30s 超时的流会让轮次卡满 30s（实测 `elapsed=30.878s`）；改后停止请求最迟 0.25s 被感知；
* **被我们 terminate 掉的探测结果不可信**：收尾阶段把 pending future 一律转成 `cancelled=True`
  （`message="已因停止请求终止，结果不参与落库"`），**绝不**把「因停止被杀」写成 `PROCESS_ERROR`
  污染健康历史；
* 实测（`timeout_seconds` 大于停止窗口的 4 条流）：`elapsed < 20s`、新探测数 `< 8`、**0 条写入**、
  所有子进程 `alive=False`。

**每 stream × 本 probe 节点每轮最多 1 条**；历史只追加；`dry_run` 不写库；
`probe.last_seen_at` 只在**真实落库成功后**由 `repo.add_probe_result` 推进（演示 §8 验证了
disabled 轮次不推进它，§9 验证了环境故障轮次也不推进）。

---

## 7. Scheduler 集成

### 7.1 冻结顺序（`execute_round`）

```
① fixed fetch → ② stream-sync → ③ probe fixed streams（仅 enabled=true）→ ④ publish → ⑤ runtime status
```

③ 插在 ② 的 `commit()` 之后、④ 之前，**结果先 commit 再 publish** ⇒ 本轮测活必然影响同轮 selector。

* `probe.enabled = false`（或未传 `probe_settings`）⇒ ③ **完全不执行**：0 次 ffprobe（连
  capability check 都不跑），行为与 TASK-004 完全一致；
* 演示 §7 用 spy 包住四个模块级函数，实测事件序列 `["fetch", "sync", "probe", "publish"]`；
  §8 实测 disabled 时 `version_calls=0`、`stream_calls=0`，且**发布产物与「不传 probe_settings」
  逐字节相同**（`live-off.m3u == live-none.m3u`，151 bytes，`outcome` 均为 `ok`）。

### 7.2 单 stream 失败 ≠ 整轮失败

`stage = ok`（本轮尝试的流全成功）/ `degraded`（有流失败，但那是**正常健康历史**）/
`failed`（环境级故障）/ `disabled`。单条流失败**不**改变轮次结论。

### 7.3 环境级故障（ffprobe 缺失 / 起不来）

* `run_round` 在能力检查处**早退**：`stage=failed`、`environment_error=True`、**0 条写入**；
  语义上 `requested=0`（**根本没走到选目标**），`failed=0`、`error_counts={}`
  —— 刻意不报告「2 条流都失败了」（这是本节的核心红线）；
* 即使本轮所有探测都因环境错误失败，也只走 `stage=failed` + 0 写入这条路径；
* `runtime` 侧：`_round_outcome` 认为 `probe_stage=failed` 时，**即使 publish 成功也只能是
  `degraded`**，绝不报「完全 OK」；`round_exit_code` 直接给 `1`；
* 但**可以继续用旧历史 publish**（ffprobe 挂了不影响订阅可用性）——演示 §9 实测：
  `publish=OK published=True`、`outcome=degraded`、`exit=1`、`probe_result` 行数 `10→10`、
  `last_seen_at` 未推进；
* `runtime-status.json` 增加 `probe` 子摘要（`enabled/stage/probe_name/probe_id/dry_run/
  requested/succeeded/failed/written/cancelled/skipped/error_type/error_counts/failed_stream_ids`）
  —— **不含任何 URL、不含 stderr**；
* **未新增数据库业务表**。

---

## 8. Selector 一致性

`liptv/select.py` blob `ff1650249e3c160943d69cd4a6ba6afa75945229` —— **一行未改**；
TASK-001 全部既有测试（`tests/test_select.py` 11 项）原样通过。

本轮**只**做「把真实 `ProbeObservation` 写进 `probe_result`，再由既有规则打分」：

| 演示步骤 | 实测 |
|---|---|
| 同一 canonical 两条 stream，A 成功 / B DNS 失败 | `select_best_stream` → **A**；B `eligible=False, reason="窗口内成功次数 0 < 阈值 1"` |
| 下一轮**同一批 stream**（URL 未变）健康反转 | `select_best_stream` → **B**（按既有 7 天窗口 + 成功次数阈值规则自然翻转） |
| A 再连输两轮（合计连续 3 次失败） | A `consecutive_failures=3` → `eligible=False, reason="连续失败 3 次，达到上限 3"`（既有硬阈值生效） |
| 历史只追加 | 4 轮 × 2 条 = 8 条，A 4 条 / B 4 条 |

> 为让「同一 URL 的健康反转」可离线复现，给 `tools/fake_ffprobe.py` 加了一个**按 URL 覆盖模式**
> 的小能力（环境变量 `FAKE_FFPROBE_STATE` 指向 JSON）；真实 ffprobe 本来就能对同一地址在不同
> 时刻给出不同结果，因此这是对真实行为的模拟，而不是对 selector 的改动。该能力有独立单测。
>
> 若后续发现选线算法本身有问题（例如评分权重、窗口语义），**本轮不偷改**，按 TASK-005 §10
> 记录为后续 TASK 处理。

---

## 9. 离线 E2E

命令与退出码：

```
python tools/demo_probe_pipeline.py     # 退出码 0；41/41 通过
```

要点摘录（真实输出）：

```text
初始化 + 抓取：registered=2 enabled=2 / fetch requested=1 created=2
§1 capability：exit=0 version=ffprobe version 6.1.1-fake-offline …；version_calls=1 stream_calls=0
§2 候选：stream_ids=[1, 2]；dynamic_links=0 signed_streams=0
§3 第一轮：exit=0 stage=degraded requested=2 succeeded=1 failed=1 written=2
          error_counts={'DNS_ERROR': 1}；stream_calls=2
§4 写库：startup_ms=887 res=1920x1080 bitrate=6128 protocol=http
         失败行：error_type=DNS_ERROR，startup_ms/res/bitrate/… 全 None
§5 selector：best=1（A）；B eligible=False reason=窗口内成功次数 0 < 阈值 1
§6 反转：best=2（B）；A consecutive=3 reason=连续失败 3 次，达到上限 3；total=8 A=4 B=4
§7 顺序：events=['fetch','sync','probe','publish']
         probe={"stage":"degraded","requested":2,"succeeded":1,"failed":1,"written":2,"probe_id":3,…}
         fixed_count=1 status=OK；dynamic_count=3 但 probe requested 仍是 2
§8 disabled：version_calls=0 stream_calls=0
         live-off.m3u == live-none.m3u（逐字节） outcome=ok；rows 10→10
§9 ffprobe 缺失：exit=1 stage=failed error_type=FFPROBE_NOT_FOUND
         requested=0 failed=0 written=0 error_counts={}（候选仍有 2 条，但一条都没被记成失败）
         rows 10→10；整轮 outcome=degraded exit=1；publish 仍 OK（用旧历史）
§10 隐私：--json 不含 token/path/query；url 只显示 http://127.0.0.1:26617/...（两处）
         runtime-status.json 不含 token、不含线路标识、不含任何 http://
         publish-summary.json 同样脱敏
```

产物全部落在 `out/demo-task005/`（已被 `.gitignore` 忽略）；演示**不访问任何公网地址**，
探测目标是本机 mock HTTP 服务上的假地址，真正被执行的程序是本地 fake ffprobe。

---

## 10. 测试

### 10.1 全量结果（受控命令）

```
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
"C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe" \
  -m pytest -o addopts="" -p no:cacheprovider -q \
  --basetemp="C:/Users/Administrator/AppData/Local/Temp/qa5final" --durations=8
```

**300 passed in 400.67s（0:06:40），退出码 0。**
（这次是在**即将提交的那一份字节**上跑的：见 §13.2 的行尾复原说明。）

单条最慢项：`tests/test_server.py::test_concurrent_healthz_reads_never_fail_status_writes` 47.08s
（TASK-004 既有并发大用例）、`tests/test_runtime.py::test_heartbeat_continues_during_long_sleep_without_rounds` 25.32s、
本轮新增最慢项 `tests/test_probe.py::test_concurrency_is_bounded_and_one_row_per_stream_per_round` 12.93s。

| 测试文件 | 项数 |
|---|---|
| `tests/test_runtime.py` | 55 |
| `tests/test_publish.py` | 36 |
| **`tests/test_probe.py`（本轮新增）** | **31** |
| `tests/test_server.py` | 25 |
| `tests/test_remote_ingest.py` | 22 |
| `tests/test_m3u.py` | 20 |
| `tests/test_fetch.py` | 19 |
| `tests/test_dynamic.py` | 17 |
| `tests/test_schema.py` | 16 |
| `tests/test_review_qa002.py` | 13 |
| `tests/test_select.py` | 11 |
| `tests/test_review_qa002c.py` | 10 |
| `tests/test_repo.py` | 10 |
| `tests/test_identity.py` | 9 |
| `tests/test_cli.py` | 6 |
| **合计** | **300**（TASK-004 基线 269 + 本轮新增 31） |

单文件命令：`… -m pytest -o addopts="" -p no:cacheprovider -q tests/test_probe.py`
→ **31 passed**。`tests/test_probe.py` 单条最慢项 `test_concurrency_is_bounded_and_one_row_per_stream_per_round` ≈ 13s。

### 10.2 已知抖动（**TASK-004 遗留，不是本轮引入**）——诚实记录

本轮四次全量运行里有**一次**出现 1 项失败：

| 运行 | 结果 |
|---|---|
| 第 1 次（299 项时） | `299 passed in 528.36s` |
| 第 2 次（300 项时） | `1 failed, 299 passed in 458.42s` —— `tests/test_runtime.py::test_cli_run_loop_stops_when_lock_is_stolen_and_keeps_foreign_lock` |
| 第 3 次 | `300 passed in 405.08s` |
| 第 4 次（**提交前的最终字节**） | **`300 passed in 400.67s`** ← 以这次为准 |

**单测重复实验（同一台机、单跑该用例、各 6 次）**：

| 版本 | 失败次数 |
|---|---|
| 本轮工作区（含全部改动） | **2 / 6** |
| **基线 `4df758a`（`git stash -u` 后跑，我的改动全部移除）** | **1 / 6** |

⇒ 同一量级，**本轮改动没有引入它**。

**成因（代码级证据，非推测）**：`SingleInstanceLock.heartbeat()` 在 `token` 仍是自己的时候会
`self._write(self.path, self._info)` **重写锁文件**以刷新 `heartbeat_at`。该用例的
`round_then_steal` 在第一轮结束后把锁文件的 `token` 改成 `intruder-token`；若后台
`LockHeartbeat` 线程此刻恰好处于「已读到锁（还是自己的）→ 尚未写回」的窗口内，
写回操作会把 `intruder-token` **覆盖回自己的 token**，偷锁被静默抹掉，
于是 `lock_lost` 始终为 `False`、退出码为 `0`。
心跳周期 = `clamp(min(3600/4, 1/4, 300), 0.25, 300) = 0.25s`，掩盖窗口 ≈ 一次带重试的原子写耗时，
所以失败率在 1/6 ~ 1/3 之间 —— 与实测吻合。

**首版的范围结论（已被 Review 01 推翻，保留原文以示记录）**：当时把修它判为「设计变更、
TASK-005 不含」，因此只登记未改。大G 在 Review 01 的 **QA-005B** 指出这不属于测试抖动，
而是**单实例安全语义失效**（旧 owner 可能覆盖接管者的 token，两个 scheduler 会同时写库存），
必须在本轮修 —— 这个判断是对的，我原来的处理方式是错的。

**修复与证据**：见 **§14.5**。修法不是「回读校验」这种仍留窗口的做法，而是把写回改成
**原子 CAS**（`os.replace` 搬走 → 复核归属 → `os.link` 仅当路径不存在时发布），
并给 acquire / takeover / heartbeat / release 加同一把跨进程 gate。
修复后该用例本机**连续 10/10 通过**（§14.5.3），且新增两条**确定性**竞态注入回归
（不依赖概率重复）。

---

## 11. 真实 ffprobe smoke

按 TASK-005 §12：真实 ffprobe smoke **只是可选**，公网 URL **不得**作为自动验收条件。

**本机实测：PATH 内没有 ffprobe**，因此：

1. **未执行**任何真实公网流的探测（也**不**作为本轮验收条件）；
2. 但顺带拿到了**真实环境**下环境级故障路径的实测证据（用的是默认配置 `ffprobe_path = "ffprobe"`）：

```text
$ python -m liptv probe-check --config <probe: ffprobe_path="ffprobe"> --json
{
  "status": "ENVIRONMENT_ERROR",
  "ok": false,
  "ffprobe_path": "ffprobe",
  "version": null,
  "error_type": "FFPROBE_NOT_FOUND",
  "message": "[WinError 2] 系统找不到指定的文件。",
  "elapsed_ms": 6,
  "exit_code": 1
}
EXIT=1
```

这条不是 fake：本机确实没有 ffprobe，属真实环境判定 —— 环境级故障**被正确识别**为
`FFPROBE_NOT_FOUND` 并明确失败（而不是把流批量写成失败）。

---

## 12. 风险与范围外

**明确不在本轮**（TASK-005 §"不包含"）：多地区 / 多机器 probe agent、云端探针协调、
腾讯云部署、ffmpeg 转码 / 代理 / DVR / 录制、长时 QoE / packet loss / jitter、
DRM / 登录 / Cookie / Authorization 绕过、自动 canonicalization、EPG / Logo、Dashboard、
修改 selector 算法、schema V2。

**本轮已知边界（如实记录）**：

1. 只在 Windows 单机验证（本机）；`probe` 行已有 `name` / `location` 字段，但多探针协调未做。
2. `ipv_family` / `http_status` / `connect_ms` **恒为 `NULL`** —— V1 schema 能表达但无法可靠获得，
   按「宁可留 NULL，不为本轮扩 schema」处理；若大G 要求填，请另立任务并给出可靠来源。
3. 分辨率 / 码率取自 ffprobe 的静态声明，**不代表持续播放质量**；不做 QoE。
4. 停止请求的感知粒度是 `STOP_POLL_SECONDS = 0.25s`（有界，不是立即）。
5. 环境级故障时 `probe-run` 报 `requested=0`（在能力检查处早退，未走到选目标）。这是刻意的：
   环境坏掉时不该假装「选了 N 条」。若大G 认为应报「候选数」，那是一次语义变更，请指示。
6. `probe_result` 里不保存 URL —— 本轮**没有**为诊断而持久化任何 URL / token / stderr 全文。
7. §10.2 的 TASK-004 遗留抖动（非本轮引入，未修改）。

---

## 13. Git & Gate

### 13.1 `git diff --check`

干净（退出码 `0`），只有 Git 的 `LF will be replaced by CRLF` 提示（与本仓库既有约定一致，
不是空白错误）。

### 13.2 字符卫生自检（12 个交付文件）

用码点区间逐字节检查（`NUL` / `CRLF` / C0 控制字符 / 零宽字符 `U+200B..U+2060,U+FEFF` /
西里尔 / 希腊），**全部为 0，TOTAL_BAD = 0**：

| 文件 | NUL | CRLF | C0 | 零宽 | 西里尔/希腊 |
|---|---|---|---|---|---|
| `liptv/probe.py` | 0 | 0 | 0 | 0 | 0 |
| `tools/fake_ffprobe.py` | 0 | 0 | 0 | 0 | 0 |
| `tools/demo_probe_pipeline.py` | 0 | 0 | 0 | 0 | 0 |
| `tests/test_probe.py` | 0 | 0 | 0 | 0 | 0 |
| `liptv/repo.py` | 0 | 0 | 0 | 0 | 0 |
| `liptv/config.py` | 0 | 0 | 0 | 0 | 0 |
| `liptv/runtime.py` | 0 | 0 | 0 | 0 | 0 |
| `liptv/cli.py` | 0 | 0 | 0 | 0 | 0 |
| `config/config.example.toml` | 0 | 0 | 0 | 0 | 0 |
| `README.md` | 0 | 0 | 0 | 0 | 0 |
| `REPORTS/TASK-005-REPORT.md` | 0 | 0 | 0 | 0 | 0 |
| `TASKS/TASK-005.md` | 0 | 0 | 0 | 0 | 0 |

行尾统一为 **LF**，与仓库既有文件一致（仓库**无** `.gitattributes`，`core.autocrlf=true`）。

> 自查过程中的一条**如实记录**：中途用 `git stash -u` 做「基线抖动对照实验」（§10.2）时，
> `stash pop` 按 `core.autocrlf` 把 9 个文件在工作区写成了 CRLF（每个文件字节数正好 +行数）。
> 已用逐字节 `\r\n → \n` 复原，复原后 10 个 blob SHA 与复原前**完全一致**、文件字节数也一致，
> 上表即在**即将提交的这份字节**上复检的结果。

### 13.3 §13 最低验收逐条对照

| # | 要求 | 证据 |
|---|---|---|
| 1 | TASK-004 的 269 项零回归 | 全量 **300 passed**（269 + 31），退出码 0 |
| 2 | `probe-check`：可用 PASS；missing 为环境错误且 0 `probe_result` | 演示 §1 `exit=0`；§9 `exit=1 stage=failed FFPROBE_NOT_FOUND` + `rows 10→10`；真实环境 `probe-check` 同口径（§11） |
| 3 | 单 stream 成功：真实结构字段写库，selector 立即可用 | 演示 §3/§4/§5：`startup_ms=887 / 1920x1080 / 6128 / http` → `select_best_stream` → A |
| 4 | timeout / invalid media / process error 各一条 failure，`error_type` 稳定；stderr 全文不落库 | `tests/test_probe.py` 的 `FAILURE_CASES`（7 类，含 DNS/TLS/HTTP/invalid/exit1）+ timeout 与 process error 分离用例；`probe_result` 无 stderr 列 |
| 5 | ffprobe missing / start failed 不得批量写 failure | 演示 §9：`failed=0 written=0 error_counts={}` 而候选仍有 2 条；测试 `test_partial_environment_failure_is_discarded_not_written` |
| 6 | 并发 20+ stream：受 `max_concurrency` 限制；每 stream 每轮 ≤1 条；SQLite 无线程错误 | `test_concurrency_is_bounded_and_one_row_per_stream_per_round`（20 条，`max_concurrent` 实测在 2~4 之间；第二轮追加到 40 条） |
| 7 | Ctrl+C / stop：不启动新 probe、子进程清理、无 orphan | 停止请求用例（`elapsed < 20s`、新探测数 < 8、`_pid_alive` 假、0 写入）+ 超时无孤儿实测 |
| 8 | URL 含 token / shell 元字符：`shell=False`、单独 argv、日志/JSON 不泄漏 | argv 隔离用例（`| whoami ; $(id)` 原样回单参）+ 源码静态护栏（无 `shell=True`）+ 演示 §10 脱敏 |
| 9 | stale/orphan/disabled 不测；active fixed 正常测；dynamic 不进 probe | 目标筛选用例（五类全排除，只留 `normal_id`）+ 演示 §2（`dynamic_links=0 signed_streams=0`） |
| 10 | scheduler：disabled 兼容、enabled 顺序可机验证、同轮生效、环境级故障不污染且明确告警 | 演示 §7（`['fetch','sync','probe','publish']`、`fixed_count=1` 用本轮选出的 B）/ §8（0 子进程 + 逐字节一致）/ §9（`outcome=degraded`、`exit=1`、0 污染）；测试 `test_execute_round_order_is_fetch_sync_probe_publish` |
| 11 | selector 算法不修改，现有 score/select 测试原样通过 | `liptv/select.py` blob `ff165024…` 未变；`tests/test_select.py` 11 项全绿 |
| 12 | `git diff --check`、执行报告、真实命令/退出码、离线 E2E、Git SHA、本地/远端一致 | 本节 + §9 + §10.1 + §13.5 |

### 13.4 冻结语义自查

| 冻结项 | 是否触碰 | 证据 |
|---|---|---|
| selector 评分 / 选线算法 | **否** | `liptv/select.py` blob 未变；TASK-001 测试全绿 |
| schema / `SCHEMA_VERSION` | **否** | `schema/schema_v1.sql` blob `64c8d0ea…` 未变；`probe_result` 字段一个没加 |
| `probe` / `probe_result` 既有语义（历史只追加、`last_seen_at` 真实成功后推进） | 否（**遵守**） | `repo.add_probe_result` 未改；演示 §8/§9 验证不推进 |
| 发布语义 / fail-closed / `live.m3u` 原子写 | **否** | `liptv/publish.py`、`liptv/m3u.py` blob 未变 |
| HTTP 路由 / 503 口径 / 信息边界 | **否** | `liptv/server.py` blob 未变 |
| 单实例锁 / 心跳 / fail-closed 释放（QA-004A/B/C） | 首版**否**；Review 01 返工**是**（仅 heartbeat 的归属原子性） | 首版 `runtime.py` 的改动只在 probe 相关行。返工后按 QA-005B 要求修了 `SingleInstanceLock.heartbeat` 的 TOCTOU：新增跨进程 gate + 原子 CAS 提交；`release` 的 fail-closed 口径、stale 判定表、`LockHeartbeat` 回调语义**均未改**；相关 QA-004A/B/C 用例全绿（§14.5） |
| 退出码 `0/2/3` 既有口径 | 否（**扩展**一处） | `round_exit_code` 新增「`probe_stage=failed` ⇒ 1」，其余分支原样；TASK-004 退出码用例全绿 |
| 动态源绝不进固定库存 | 否（**遵守**） | 演示 §2/§7：动态条目照常临时发布，但 `stream`/`stream_source`/候选里都没有它 |

### 13.5 提交与远端独立核验（云端连接器，非本地 git 自述）

```text
4df758a  task: define TASK-005 real ffprobe stream probing            （大G，本轮基线）
7000834  feat(probe): TASK-005 real fixed-stream probing via ffprobe  （实现提交：12 文件 +4077/−32）
0128e50  report: record TASK-005 implementation commit SHA 7000834    （记录提交）
8006a9a  review: reject TASK-005 on mixed probe environment failure and lock CAS race （大G，Review 01）
3a8ee8f  fix(probe): TASK-005 QA-005A/B rework -- probe fail-closed + lock heartbeat CAS （返工实现：8 文件 +758/−24）
```

> 本节（§13.5）记录的是**首版**那一次提交与云端核验；Review 01 之后的**返工提交**
> `3a8ee8f` 及其云端核验见 **§14.7**。

用 GitHub 连接器（**不是**本地 `git`）直接读远端仓库核验：

| 核验项 | 结果 |
|---|---|
| 远端提交存在 | `GET /repos/lixiuzuapple-a11y/li-iptv-aggregator/commits/7000834d0f4526fe37c54a89c0308f07f0860392` → 命中，消息与本地一致 |
| 作者 / 提交者 | 均为 `lixiuzu <lixiuzuapple@gmail.com>`（`2026-10-01T15:57:43Z`） |
| 改动规模 | **12 文件，+4077 / −32**（与本机 `git show --stat` 一致） |
| 逐文件数字 | `README.md +62/−3`、`REPORTS/TASK-005-REPORT.md +523/−19`、`TASKS/TASK-005.md +1/−1`、`config/config.example.toml +32`、`liptv/cli.py +165/−1`、`liptv/config.py +24`、`liptv/probe.py +1092`、`liptv/repo.py +56`、`liptv/runtime.py +63/−8`、`tests/test_probe.py +1049`、`tools/demo_probe_pipeline.py +783`、`tools/fake_ffprobe.py +227` |
| 10 个已提交 blob | 与本地 `git hash-object` **全等**（`e0308fd4…` / `40a755c3…` / `3bc8ca65…` / `0ab98ea5…` / `1959281f…` / `da3a5239…` / `a10fd06a…` / `79096c3d…` / `360a5da0…` / `7674955d…`） |

> **更强的独立证据**：连接器返回的改动文件清单里**根本没有** `liptv/select.py`、
> `liptv/publish.py`、`liptv/server.py`、`liptv/m3u.py`、`liptv/ingest.py`、`liptv/fetch.py`、
> `liptv/db.py`、`liptv/util.py`、`schema/schema_v1.sql` —— 这些文件本轮**一个字节都没动**，
> 不是靠我自己声明。

**推送通道的如实记录**（供大G/后续参考）：本轮 `github.com:443` 在本机**三条通路全部不通**
（直连 20s 超时、沙箱代理 `CONNECT tunnel failed, response 502`、FlClash 7890 端口从会话侧连不上），
原地重试 3 次均失败；而 `api.github.com` 全程 **200**。
因此按既有预案改用 **REST 兜底脚本**（`li_iptv_push_api.py`，不调用 `git credential fill`、
token 只在内存）复刻本提交：blob → tree → commit 三级 **SHA 全等**（`match_local_head=True`）后
才 `PATCH /repos/.../git/refs/heads/main`，`{"force": false}`。
结果：`RESULT: PUSHED via API, remote == local == 7000834d0f4526fe37c54a89c0308f07f0860392`。

> ⚠️ 按既有教训：API 推送**不会**更新本地远端跟踪引用，已手动
> `git update-ref refs/remotes/origin/main 7000834d0f4526fe37c54a89c0308f07f0860392`；
> 之后 `git status -sb` 为 `## main...origin/main`（无 ahead/behind），本地与远端一致。

字符卫生自检（12 个交付文件）：`NUL=0`、`CRLF=0`（统一 LF）、无 C0 控制字符、
无零宽字符、无西里尔/希腊字符 —— 详见 §13.2。

> 本报告会随「记录提交」再改一次以写入上面的实现提交 SHA 与云端核验结果，
> 因此它在最终提交里的 blob 会再变一次 —— 与 TASK-002/003/004 的做法一致，**未使用 amend**。

---

## 14. Review 01 定向返工（QA-005A / QA-005B）

Review 01（[REVIEWS/TASK-005-REVIEW-01.md](../REVIEWS/TASK-005-REVIEW-01.md)，受审 HEAD `0128e50`）
判 **REJECT**，要求只修两个语义错误。本节是返工的自证：**只改这两处**，不扩大范围。

### 14.0 一页摘要

| 项 | 结论 |
|---|---|
| 修了什么 | ① QA-005A：`run_round` 的环境级错误落库语义；② QA-005B：`SingleInstanceLock.heartbeat` 的 read-check-write TOCTOU |
| 没动什么 | `select.py`、`schema_v1.sql`、`publish.py`、`m3u.py`、`server.py`、`ingest.py`、`fetch.py`、`db.py`、`util.py` 逐字节未改；selector 算法与 schema V1 冻结不变 |
| 新增回归 | **5 条永久用例**：QA-005A 2 条（DB 级 + runtime 级）、QA-005B 3 条（其中 2 条是**确定性竞态注入**） |
| 全量测试 | **305 passed in 714.87s**（269 基线 + 31 首版 + 5 返工），退出码 0（§14.5.2） |
| 原抖动用例 | 修复后本机**连续 10/10 通过**（§14.5.3） |
| 顺带修掉 | 1 条与本 TASK **无关**的既有「时间炸弹」用例（基线同样失败，有基线证据）；见 §14.6 |

### 14.1 QA-005A：环境级错误必须 fail-closed

**问题（大G 的反例）**：`run_round()` 只在「本轮 attempted **全部**都是环境错误」时才整轮丢弃。
只要同一轮里有一条正常成功、另一条 `FFPROBE_START_FAILED`，环境错误那条就会掉进普通写库循环，
被写成 `success=0 / error_type=FFPROBE_START_FAILED`，把「本机 ffprobe 中途失效」
持久化成**真实的频道健康历史**，之后 selector 会把它当成「这条线不能播」。

**改法（`liptv/probe.py`，推荐的第 2 条 fail-closed 语义）**：

```python
# 修改前
env_failures = [item for item in attempted if item.environment_error]
if attempted and len(env_failures) == len(attempted):        # ← 只覆盖「全环境错误」
    ...  stage=FAILED, written=0 ...

# 修改后
env_failures = [item for item in attempted if item.environment_error]
if env_failures:                                             # ← 任一环境级错误 ⇒ fail-closed
    ...  stage=FAILED, written=0, discarded_observations=len(attempted) ...
```

**为什么选「整轮丢弃」而不是「只丢环境错误条目、保留其它样本」**：后者会让同一轮出现
「一部分 stream 有记录、另一部分没有」的**部分样本偏斜**，selector 在同一轮拿到的健康样本
不是同一批，反而制造选线偏差；而环境故障（ffprobe 缺失 / 起不来）是**本机问题**，
不属于任何一条 stream 的可播性。整轮丢弃最保守、也最好解释。仍沿用旧历史 publish。

**同口径的两处配套改动**（都是为了「环境故障 ≠ 流失败」这条语义一致）：

* `summarize_for_status()`：`ENVIRONMENT_ERROR_TYPES` 不再计入 `failed_stream_ids`
  —— 否则 `runtime-status.json` / `/healthz` 会把环境事故误报成「一批流不可播」；
* `cli probe-run`：payload 增加 `environment_error` / `environment_failed_streams` /
  `discarded_observations`，并多打一行环境级告警（`run` 的告警本来就按 `stage=failed` 判定，
  混合场景自动覆盖，无需再改）。

**未改**的既有语义：`stage` 取值、`ENVIRONMENT_ERROR_TYPES` 的成员、`probe_result` 字段、
`probe.last_seen_at` 只在真实落库后推进、dry-run 不写库 —— 一个都没动。

### 14.2 QA-005A 的回归证据

新增两条永久用例（`tests/test_probe.py`）：

| 用例 | 断言要点 |
|---|---|
| `test_mixed_environment_failure_discards_the_whole_round` | 能力检查**先通过**；一条 success + 一条 `FFPROBE_START_FAILED` ⇒ `stage=failed`、`written=0`、`succeeded=0`、`failed=0`、`discarded_observations=2`；DB **0 新行**（成功那条也不写）；`failed_stream_ids == []`；`probe.last_seen_at` 不推进 |
| `test_mixed_environment_failure_keeps_runtime_degraded_and_uses_old_history` | runtime 侧：`probe_stage=failed`、`written=0`、**旧历史照常 publish**（`published=True`）、`outcome=degraded`、`round_exit_code=1` |

单独运行：

```
> -k "mixed_environment_failure"  →  2 passed
```

### 14.3 QA-005B：`heartbeat` 的 TOCTOU（这是安全语义，不是测试抖动）

**问题**：旧 `heartbeat()` 是裸 read-check-write：

```python
current, _ = self._read_metadata()
if current.get("token") != self._token:   # ① 确认归属
    ...
self._info = dataclasses.replace(self._info, heartbeat_at=self._stamp())
self._write(self.path, self._info)        # ② 写回 —— ① 与 ② 之间有窗口
```

在 ① 与 ② 之间，若另一个实例完成接管（写入它自己的 token），旧 owner 仍会把自己的
`LockInfo` 覆盖回去 ⇒ **别人的锁被静默抹掉**，而旧 scheduler 认为自己仍持锁、继续跑
⇒ 两个写入者同时改库存。TASK-005 把 probe 接进 scheduler 后，长轮次/并发子进程让
scheduler 更依赖可靠锁，因此必须在本轮修。

**改法：两道防线（`liptv/runtime.py`）**

1. **跨进程 gate**：acquire / takeover / heartbeat / release 全部走 `_gate()`。
   进程内按锁文件绝对路径共享一把可重入 RLock（同进程多实例也串行），跨进程对
   `<lock>.gate` 取 OS 建议锁（Windows `msvcrt.locking(LK_LOCK)` / POSIX `flock(LOCK_EX)`）。
   gate 只承载**互斥**，不承载归属信息（归属仍只在锁文件里）。
2. **原子 CAS 提交** `_commit_heartbeat()` —— 这条才是关键，它把「写回」从
   *覆盖式写入* 换成 *仅当目标不存在时才创建*：

```text
① 候选内容写进唯一临时文件
② os.replace(锁文件 → 私有 claimed 名)       # 原子搬走「当前」那份
③ 复核 claimed 仍是自己的 token
     不是 ⇒ 原样放回、放弃所有权（绝不吞掉别人的锁）
④ _publish_exclusive(tmp → 锁文件)           # 本质是 os.link：路径已存在 ⇒ FileExistsError
     被抢先 ⇒ 返回 False、放弃所有权
```

**为什么不采用「写后回读校验」**：那只会在**已经被覆盖之后**才发现，别人的 token 已经丢了，
属于事后告警而不是原子性。CAS 的价值是**结构上不可能覆盖**：本实现里对锁路径的**唯一**
写操作就是第 ④ 步的 `os.link`，它在目标存在时必然失败 —— 不存在「窗口够小所以碰不到」的运气成分。
（跨文件系统硬链接不支持时退化为「存在性检查 + 原子替换」，仍有极小窗口，但**绝不主动覆盖
已知存在的锁**，并已在代码注释里写明。）

**保留不变的语义**：stale 判定表（本机死 PID / 本机活 PID + 心跳过期 / 他机 / 元数据损坏）、
`<lock>.steal` 的接管协议、`release()` 的 fail-closed 口径、`LockHeartbeat` 的回调语义、
`EXIT_LOCKED` 与 `LOCK_REASON_LOST` 的区分 —— 全部原样。

### 14.4 复现大G 的确定性注入（前后对比）

注入方式：在 heartbeat「已确认 token 是自己的」之后、真正写回之前，把锁文件换成
`intruder-token`，随后让原 heartbeat 继续（确定性注入，与 Review 01 的做法同口径）。

| 观测量 | 修复前（Review 01） | 修复后（实测） |
|---|---|---|
| `HEARTBEAT_RETURN` | `True` | **`False`** |
| `FINAL_TOKEN` | 原 owner token | **`intruder-token`** |
| `INTRUDER_SURVIVED` | `False` | **`True`** |
| `LOCK_THINKS_LOST` | `False` | **`True`** |

修复后 `lock.last_error` = `提交前复核失败：锁已被其它实例接管（未覆盖对方 token）`，
`ownership_lost=True`、`lost_reason=lock_lost`。

### 14.5 测试

#### 14.5.1 新增回归清单

| 用例 | 位置 | 性质 |
|---|---|---|
| `test_mixed_environment_failure_discards_the_whole_round` | `tests/test_probe.py` | QA-005A，DB 级 |
| `test_mixed_environment_failure_keeps_runtime_degraded_and_uses_old_history` | `tests/test_probe.py` | QA-005A，runtime 级 |
| `test_heartbeat_cas_never_clobbers_foreign_token_injected_before_commit` | `tests/test_runtime.py` | QA-005B，**确定性竞态注入** |
| `test_heartbeat_cas_publish_refuses_when_lock_path_is_taken` | `tests/test_runtime.py` | QA-005B，**确定性竞态注入**（验证 CAS 的 swap 步骤） |
| `test_live_heartbeat_never_clobbers_a_foreign_lock` | `tests/test_runtime.py` | QA-005B，后台心跳线程持续运行下也不会抹掉别人的锁 |

#### 14.5.2 全量结果

```
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
"C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe" \
  -m pytest -o addopts="" -p no:cacheprovider -q \
  --basetemp="C:/Users/Administrator/AppData/Local/Temp/qa5rework" --durations=10
```

**305 passed in 714.87s（0:11:54），退出码 0。**

= TASK-004 基线 269 + 首版新增 31 + 本次返工新增 **5**（QA-005A 2 条、QA-005B 3 条）。
`git status` 干净、无 `failed`、无 `error`、无 `warning`。

受影响的两个文件单独跑：`tests/test_probe.py tests/test_runtime.py` → **91 passed in 360.32s**
（33 + 58，退出码 0）。

#### 14.5.3 原抖动用例的稳定性

`tests/test_runtime.py::test_cli_run_loop_stops_when_lock_is_stolen_and_keeps_foreign_lock`
（就是 §10.2 那条 2/6 失败的用例），修复后**单跑连续 10 次**：

```text
run 1..10  均 1 passed（3.27s ~ 3.95s）
```

10/10 通过。原因是结构性的（见 §14.3 的「不可能覆盖」），不是碰运气。

### 14.6 顺带修掉一条**与本 TASK 无关**的既有「时间炸弹」用例

按「原有测试必须零回归」的要求跑全量时，发现另一条既有用例在**基线提交上也失败**：

| 用例 | 现象 |
|---|---|
| `tests/test_runtime.py::test_publish_advances_last_success_while_healthz_is_hammered` | `assert 'stale' == 'ok'` |

**定位**：该用例把两次轮次时间写死为 `NOW = 2026-10-01T12:00Z` / `LATER = 2026-10-01T18:00Z`，
而 `/healthz` 用 **真实墙钟**（`server.utcnow_iso()`）去算 `seconds_since_last_success`。
真实时间一旦越过 `LATER + stale_after(3600s)` = **2026-10-01T19:00Z**，这条断言就永远 stale。

**基线证据（证明不是本轮引入）**：用 `git worktree` 在基线提交 `8006a9a` 上（**不含**本轮任何改动）
单跑同一条用例 —— **同样 `1 failed`**：

```text
> 基线 worktree：FAILED test_publish_advances_last_success_while_healthz_is_hammered
                  assert 'stale' == 'ok'      （1 failed, 299 deselected in 13.80s）
```

**改法（只改测试，不改生产代码；不削弱任何断言）**：按本仓库其它用例既有的「假时钟注入」惯例，
在该用例里对 `server_mod.utcnow_iso` 注入固定时钟（返回 `LATER`），使断言与运行日期解耦。
断言本身一句未改，`/healthz` 的行为也一行未动（`liptv/server.py` blob 未变）。

> 之所以不把它丢在这里不管：它会让「全量一遍全绿」在任何 2026-10-01T19:00Z 之后的机器上
> 都不可能达成，也会挡住大G 的独立复核。这是我**主动发现并如实报告**的仓库既有缺陷，
> 不属于 TASK-005 的功能范围，若大G 认为不该在本轮动，我可以回退成「仅登记」。

### 14.7 变更规模、冻结文件与提交

**本轮返工只改 8 个文件，0 新增文件**（`git diff --numstat`）：

| 文件 | 增 / 删 | 性质 |
|---|---|---|
| `liptv/probe.py` | `+25 / −7` | QA-005A：`if env_failures:` fail-closed；状态摘要排除环境错误；docstring |
| `liptv/runtime.py` | `+244 / −4` | QA-005B：gate（`_gate` + OS 建议锁 + 进程内 RLock）、CAS 提交（`_commit_heartbeat` / `_publish_exclusive` / `_link_exclusive` / `_restore_aside`）、`acquire`/`heartbeat`/`release` 收口 |
| `liptv/cli.py` | `+6 / −0` | `probe-run` 输出 `environment_error` / `environment_failed_streams` / `discarded_observations` + 一行告警 |
| `tests/test_probe.py` | `+97 / −0` | QA-005A 两条永久回归 |
| `tests/test_runtime.py` | `+115 / −1` | QA-005B 三条永久回归 + 时间炸弹用例的时钟注入（§14.6） |
| `README.md` | `+12 / −4` | 环境级故障 fail-closed 口径、锁 CAS 说明、TASK-004 段落加指路 |
| `REPORTS/TASK-005-REPORT.md` | 本节 | — |
| `TASKS/TASK-005.md` | `+1 / −1` | `REJECTED` → `REVIEW` |

**冻结文件逐字节未动**（工作区 `git hash-object` **等于** `git ls-tree HEAD`）：

| 文件 | blob（与 HEAD 全等） |
|---|---|
| `liptv/select.py`（selector 算法） | `ff1650249e3c160943d69cd4a6ba6afa75945229` |
| `schema/schema_v1.sql`（schema V1） | `64c8d0ea4f0dde8d05f49cec5bac10942fcf8e07` |
| `liptv/server.py`（HTTP） | `28526e378dd8e22159d115202da044340aa17305` |
| `liptv/publish.py` | `639522d759f693ab80dfb7b642a8806243d571b6` |
| `liptv/m3u.py` | `652d4676778f0fad6f75be9a49e2eb04469739c7` |
| `liptv/ingest.py` | `fec6cd3666eeb93a68dfa919d3e5558db01fa8a8` |
| `liptv/fetch.py` | `1ed75e6d7a1a35ae2ad29c5cbd165728362f5df8` |
| `liptv/db.py` | `76a168b78e5f6c24720c67beb375c0de37a8cd28` |
| `liptv/util.py` | `b6b5d6d177ffffcaa54bfac91c87ae884bf7ee17` |
| `liptv/repo.py` / `liptv/config.py` / `config/config.example.toml` | `1959281f…` / `da3a5239…` / `360a5da0…`（首版已提交，本轮未再动） |

> `liptv/server.py` 的 blob 与 HEAD **逐字节相同** —— 这是 §14.6 那条「时间炸弹」修复
> **只改测试、没动生产代码**的独立证据。

提交序列：

```text
8006a9a  review: reject TASK-005 on mixed probe environment failure and lock CAS race   （大G，返工基线）
3a8ee8f  fix(probe): TASK-005 QA-005A/B rework -- probe fail-closed + lock heartbeat CAS（实现提交：8 文件 +758/−24）
<记录提交> report: record TASK-005 rework commit SHA
```

**云端独立核验**（GitHub 连接器读远端仓库，**不是**本地 `git` 自述）：

| 核验项 | 结果 |
|---|---|
| 远端提交存在 | `GET /repos/lixiuzuapple-a11y/li-iptv-aggregator/commits/3a8ee8f82f3bec16a16cf115c78acbc346a2eccd` → 命中，消息与本地一致 |
| 作者 / 提交者 | 均为 `lixiuzu <lixiuzuapple@gmail.com>`（`2026-10-02T02:07:11Z`） |
| 改动规模 | **8 文件，+758 / −24**（与本机 `git show --stat` 一致） |
| 逐文件数字 | `README.md +12/−4`、`REPORTS/TASK-005-REPORT.md +258/−7`、`TASKS/TASK-005.md +1/−1`、`liptv/cli.py +6`、`liptv/probe.py +25/−7`、**`liptv/runtime.py +244/−4`**、`tests/test_probe.py +97`、`tests/test_runtime.py +115/−1` |
| **更强证据** | 连接器的改动清单里**没有** `liptv/select.py`、`liptv/publish.py`、`liptv/server.py`、`liptv/m3u.py`、`liptv/ingest.py`、`liptv/fetch.py`、`liptv/db.py`、`liptv/util.py`、`schema/schema_v1.sql` —— 这些文件本轮**一个字节都没动**，不是靠我自己声明 |

推送方式：本次 `git push origin main` **直接成功**（`8006a9a..3a8ee8f`，退出码 0）——
与首版那次的「`github.com` 三条通路全不通、只能走 REST 兜底」不同，这次网络可达，
因此**没有**动用 REST 脚本，也未手动 `update-ref`。
