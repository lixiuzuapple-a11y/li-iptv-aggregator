# TASK-004 Execution Report

状态：REVIEW
Owner：老李
Executor：小W
Reviewer：大G
基线：TASK-003 ACCEPTED（见 [REVIEWS/TASK-003-REVIEW-02.md](../REVIEWS/TASK-003-REVIEW-02.md)）
基线 HEAD：`c1f8ce350ed963d3661b50dbcebdbd4023973e19`
实现提交：`5ab2e9d7b5ba6823ca180a711988a2a254d3f66a`（见 §10；由后续一次「记录提交」写入本报告，未使用 amend）
Review 01：`58e61742cc931764980708ab1704ee76e851966c` → **REJECT**（[REVIEWS/TASK-004-REVIEW-01.md](../REVIEWS/TASK-004-REVIEW-01.md)），
两处阻断 QA-004A（心跳未接线）/ QA-004B（release 会删不可解析的锁）。**返工范围、修复与新增回归见 §12。**

本轮**只**做「本地可长期运行的 scheduler + 单实例锁 + 只读 HTTP 订阅」，不部署腾讯云、
不代理视频流、不做 Dashboard、不引入 APScheduler/Celery 等重型依赖（纯标准库）。

---

## 1. 基线与变更

### 1.1 变更文件

| 文件 | 变更 | 说明 |
|---|---|---|
| `liptv/runtime.py` | **新增**（1014 行） | 单实例锁、脱敏状态文件、一轮运行链、可注入的 `Scheduler`、新鲜度 `compute_health` |
| `liptv/server.py` | **新增**（370 行） | 只读 `ThreadingHTTPServer`：`GET/HEAD /live.m3u`、`GET/HEAD /healthz`、其余 404 |
| `liptv/cli.py` | 修改（+324） | 新增 `run`（`--once` / 循环 / `--serve`）与 `serve` 两个命令及其全部参数 |
| `liptv/config.py` | 修改（+40） | `DEFAULT_CONFIG` 新增 `[runtime]`（9 项）与 `[server]`（5 项）；新增 `runtime_settings()` / `server_settings()` |
| `liptv/m3u.py` | 修改（+44 / −3） | 原子替换的**瞬时占用有界重试**（`_replace_with_retry`）；`write_m3u` 第 4 步改调它，其余回滚路径逐字不变 |
| `tests/test_runtime.py` | **新增**（953 行，36 项） | 锁 / 循环 / 状态 / 新鲜度 / `run --once` 离线 E2E / 真实进程停止 |
| `tests/test_server.py` | **新增**（516 行，23 项） | 路由 / 缺文件口径 / 信息边界 / 并发读一致性 / 只读性 |
| `tools/demo_runtime.py` | **新增**（560 行） | 纯离线端到端演示（28/28 通过） |
| `config/config.example.toml` | 修改（+52） | 新增 `[runtime]` / `[server]` 段与逐项说明 |
| `README.md` | 修改（+120 / −3） | 新增「本地定时运行 + 只读 HTTP 订阅」整节；状态表更新 |
| `TASKS/TASK-004.md` | 修改（1 行） | 状态 `READY_FOR_EXECUTOR` → `REVIEW` |

未改动（**继续冻结**）：`schema/schema_v1.sql`（TASK-004 **零 schema 改动**，`SCHEMA_VERSION` 仍为 1）、
`liptv/db.py`、`liptv/repo.py`、`liptv/ingest.py`、`liptv/fetch.py`、`liptv/select.py`、`liptv/util.py`、
`liptv/publish.py`（publish 语义**未动**，只是被新模块调用）。

### 1.2 复用的既有资产（不重造）

| 需要的能力 | 直接复用 |
|---|---|
| 抓取单个 fixed 来源 | `liptv.ingest.ingest_fixed_source` |
| 归集 stream | `liptv.repo.sync_streams` |
| 选线与组合发布 | `liptv.publish.publish`（含 `--require-dynamic` / fail-closed） |
| 流动快照护栏 | `liptv.publish.guard_runtime_output_path` |
| 脱敏 | `liptv.publish.redact_text` |
| 原子/事务写 M3U | `liptv.m3u.write_m3u` |

**没有任何**通过 shell 拼接调用自身 CLI 的地方：`run` 直接调用库函数，`tools/demo_runtime.py` 与
测试才走 CLI 入口（作为端到端验证）。

---

## 2. Scheduler

### 2.1 入口与模式

```
python -m liptv run --once        # 一轮后退出
python -m liptv run               # 长期循环
python -m liptv run --serve       # 循环 + 只读 HTTP
python -m liptv serve             # 只提供只读 HTTP（不抓取、不发布、不占锁）
```

`run` 的关键顺序：**先取锁，再做任何其它事**。取不到锁就立刻以 `3` 退出，
不会读数据库、不会抓取、不会发布、不会创建输出目录。

### 2.2 每轮顺序（`runtime.execute_round`，严格对应任务书 §1）

1. 抓取所有 enabled `fixed_m3u`（`repo.list_sources_by_kind(KIND_FIXED, enabled_only=True)`）。
   **每个来源单独 try/except**：某源抛异常只记该源 `ok=false` + `error_category`，
   其它来源照常抓（失败时先 `conn.rollback()`，不让坏连接污染后续）。
2. `repo.sync_streams` —— 只做归集；**绝不**调用 `add_canonical_channel` / `bind_source_channel`。
3. 按配置显式决定是否拉动态赛事源（`settings.include_dynamic`）。
4. 调用 TASK-003 的 `publish_mod.publish`（动态失败沿用 fail-closed 语义，一行未改）。
5. 写脱敏状态（`round_id` / 起止时间 / fetch 摘要 / publish 状态 / 耗时 / 异常分类 / `next_run_at`）。

`execute_round` 的 `finished_at` 统一取本轮 `stamp`（CLI 的 `--once --now` 可注入），
真实耗时另由 `duration_ms` 记录 —— 这样状态文件可复现，而不会用「时钟噪声」冒充耗时。

### 2.3 异常隔离

- **单源失败**：只影响该源（见 §2.2-1）。
- **单轮失败**：`Scheduler.run_once()` 把任何异常收敛进状态（`error.type/message/category`），
  **不向上抛**，因此 `run()` 的循环永远不会被一轮打死。
- **状态文件写失败**：只记日志，同样不杀 loop。
- `--now` 只对 `--once` 生效（loop 每轮用真实时间，否则所有时间戳会相同）。

### 2.4 可控停止

- `SIGINT` / `SIGTERM` / `SIGBREAK` 都注册成「请求停止」（`_install_stop_handlers`，退出时还原原处理器）。
- 长时间待机按 `SLEEP_POLL_SECONDS = 0.25` **切片休眠**，因此 Ctrl+C 能在 ~0.25s 内生效，
  而不是等完整个 3 小时。
- 收到停止后：不再启动新一轮 → 关掉 HTTP → 释放锁（都在同一个 `finally` 里）。
- `KeyboardInterrupt`（例如信号处理器装不上时的兜底路径）同样收敛为正常退出（退出码 0），
  `interrupted=true`。

### 2.5 测试可注入

`Scheduler(round_fn=..., clock=..., sleep=..., now_fn=...)` 全部可注入。
`tests/test_runtime.py` 用 `SleepRecorder` 记录而非真实等待；`tools/demo_runtime.py` 用
`FakeClock`，3 轮 × 10800s 周期在**真实毫秒级**跑完（`fake_slept=21600s`）。

### 2.6 退出码（与 `publish` 不冲突）

| 退出码 | 含义 |
|---|---|
| `0` | `EXIT_OK`：正常（含 `DEGRADED_FIXED_ONLY` 降级发布；Ctrl+C 正常停止也是 0） |
| `1` | `EXIT_ROUND_FAILED`：本轮失败（直接沿用 `publish` 的 `REJECTED_*` 退出码） |
| `2` | 未发布但输入没问题（`DEGRADED_NO_PUBLISH`，沿用 publish） |
| `3` | `EXIT_LOCKED`：被另一实例持锁，**本轮没有执行任何 fetch/publish** |

---

## 3. 单实例锁

### 3.1 实现（`liptv.runtime.SingleInstanceLock`）

- **获取**：`os.open(path, O_CREAT|O_EXCL|O_WRONLY)` 原子创建（跨 Windows/Linux 都原子），
  成功后再写元数据（`pid` / `hostname` / `token` / `created_at` / `heartbeat_at` /
  `interval_seconds` / `version`），写入本身走「临时文件 + `os.replace`」。
- **释放**：只删 token 还是自己的那把锁（读回元数据比对）；不是自己的直接返回 `False`，不删。
- **心跳**：`heartbeat()` 原子刷新；若发现 token 已被别人接管，**立刻放弃所有权**（返回 `False`），
  避免两个写入者同时改库存。
- **接管**：`<lock>.steal` 独占创建占位 → `os.replace` 原子替换 → 删占位。
  两个实例同时抢时，后到者会撞上 `.steal` 已存在，抛 `held_concurrent_steal` 后退出，不会双赢。
- 锁路径先过 `guard_runtime_output_path`：必须落在被 `.gitignore` 忽略、且未被 Git 跟踪的位置。

### 3.2 stale 判定（可解释，不猜）

| 持有者 | 进程存活 | 心跳年龄 | 判定 |
|---|---|---|---|
| 本机 | 存活 | 新鲜 | `held_by_live_process` → **拒绝** |
| 本机 | 存活 | 过期 | `held_by_live_process_with_stale_heartbeat` → **拒绝**，并打印 PID 与清理办法 |
| 本机 | 已退出 | —— | `stale_dead_pid` → 接管 |
| 他机 | 无法判定 | 新鲜 | `held_by_remote_host` → 拒绝 |
| 他机 | 无法判定 | 过期 | `stale_heartbeat` → 接管 |
| 元数据损坏 | 无法判定 | 新鲜 | `held_metadata_unreadable` → 拒绝 |
| 元数据损坏 | 无法判定 | 过期 | 按 mtime 接管 |

设计取向：**「本机活进程 + 心跳过期」不抢锁**。文件锁的常见做法是「超时就抢」，
但那会造成两个写入者同时改同一个 SQLite 库存；这里宁可让运维显式删锁并给出 PID，
也不赌一把。反过来，「本机进程已退出」是可以确定的死锁，必须能自动接管，否则一次
崩溃就会把 scheduler 永久锁死。

**PID 存活判定**：Windows 走 `OpenProcess` + `WaitForSingleObject(handle, 0)`
（只查询，不影响目标进程）；POSIX 走 `os.kill(pid, 0)`。
**绝不**在 Windows 上用 `os.kill(pid, 0)` —— 那会走 `TerminateProcess` 把目标进程真的杀掉。

### 3.3 永久测试（`tests/test_runtime.py`）

`test_lock_second_instance_is_rejected`、`test_lock_released_on_normal_exit`、
`test_lock_context_manager_releases_even_on_exception`、
`test_lock_release_does_not_delete_someone_elses_lock`、`test_heartbeat_stops_when_token_changes`、
`test_stale_lock_dead_pid_is_taken_over`、`test_stale_lock_remote_host_old_heartbeat_is_taken_over`、
`test_fresh_lock_from_remote_host_is_not_stolen`、`test_live_pid_with_stale_heartbeat_is_not_stolen`、
`test_unreadable_lock_metadata_fresh_is_refused_and_old_is_taken_over`、
`test_lock_path_inside_untracked_git_worktree_is_refused`、`test_pid_alive_reports_self_and_dead_process`
—— **12 项**，覆盖 §3.2 表格的**每一行**。

`test_lock_path_inside_untracked_git_worktree_is_refused` 会**真的建一个临时 Git 仓库**，
确认护栏在真实 Git 语义下生效（不是靠 mock）。

---

## 4. HTTP 订阅服务（`liptv/server.py`）

### 4.1 路由与冻结口径

| 请求 | 行为 |
|---|---|
| `GET /live.m3u` | `200`，`Content-Type: application/vnd.apple.mpegurl`，正文 = 文件原字节；带 `Content-Length` / `Last-Modified` / `ETag` / `Cache-Control: no-store` |
| `HEAD /live.m3u` | `200`，**不读正文**，但 `Content-Length` 与 GET 完全一致 |
| `GET /healthz` | `200`，`application/json; charset=utf-8`，见 §5 |
| `HEAD /healthz` | 同上，无正文，`Content-Length` 与 GET 一致 |
| 其它任何路径 | `404` |
| `/live.m3u` 不存在 **或为空** | **`503`**（`runtime.HTTP_PLAYLIST_MISSING`） |

`503` 口径已**冻结**（任务书 §4 要求二选一）：服务在、内容不可用。**绝不**生成一个
`#EXTM3U` 空列表冒充成功 —— 那会让播放器把「上游挂了」误读成「所有频道都消失了」。

### 4.2 路径安全：路由是**固定映射**，永不拼磁盘路径

`_route()` 只做一件事：`urlsplit(self.path).path` 与配置字符串**完全相等**比较。
从来没有「把 URL 拼到根目录后面」这一步，因此不存在路径穿越面。实测 404 清单：

```
/ /index.html /live.m3u/ /live.m3u/extra /LIVE.M3U /live.m3u.bak
/live.m3u.previous.m3u /health /healthz/ /hello.txt
/.. /../ /../outside.txt /../%2e%2e%2foutside.txt /C:/Windows/win.ini
/%2Fetc%2Fpasswd /live.m3u%00.txt
```

同目录下的 `liptv.sqlite3` / `live.previous.m3u` / `publish-summary.json` /
`runtime-status.json` / `config.toml` 一律 404，且响应正文里不含这些文件的内容。
自定义路由（`playlist_path` / `health_path` 改名）生效后，旧路径立即失效。

### 4.3 只读性

- 服务只读**那一个** M3U 文件；条目里的播放地址**永远**不会被请求。
  `test_server_never_opens_outbound_connection` 把 `socket.socket.connect` 换成守卫版：
  任何非 loopback 目标都会立刻断言失败 —— 全程 0 次对外连接。
- `test_server_writes_nothing`：跑完 GET/HEAD/404 后目录内容**逐一相同**（只剩 `live.m3u`）。
- 默认 `host = 127.0.0.1`；绑非 loopback 需要显式配置，`validate_server_binding` 会返回安全提示，
  CLI 启动时打印。

### 4.4 与原子替换并发：Windows `WinError 5` 陷阱（本轮实测关键）

**现象（对照实验，2026-10-01，Windows / Python 3.13）**：

```
reader=none        -> ['OK', 'OK', 'OK', 'OK', 'OK']
reader=plain-open  -> ['FAIL(5)', 'FAIL(5)', 'FAIL(5)', 'FAIL(5)', 'FAIL(5)']
```

只要目标文件被**任何**句柄打开着（同进程也不例外），`os.replace` 就抛
`PermissionError [WinError 5]`。也就是说，**如果 HTTP 服务用普通 `open()` 读文件，
播放器的一次请求就能把 scheduler 的发布顶失败**。

**应对（两侧同时收窄）**：

1. **读侧**（`server._open_shared_read`）：Windows 走 `CreateFileW`，
   `dwShareMode = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`，
   再 `msvcrt.open_osfhandle` 转成 fd；POSIX 走 `os.open(O_RDONLY)`。
   正文与 `stat` 取自**同一个句柄**（`os.fstat` + 分块 `os.read`），因此
   `ETag` / `Last-Modified` 与读到的字节**必然自洽**；已打开的句柄始终指向它打开时的那个完整版本。
2. **写侧**（`m3u._replace_with_retry`）：**只**对「目标被占用」这类瞬时冲突做
   **有界**重试（12 × 0.1s ≈ 1.2s 上限）。判定收窄到 `winerror in (5, 32)`
   （POSIX 仅 `EBUSY`）；**其它 `OSError`（权限、磁盘满、路径不存在，以及测试注入的普通
   `OSError`）一律原样抛出**，既不改变既有失败语义，也不改变回滚路径（`test_publish.py`
   里注入 `OSError` 的两项用例语义未变）。

**实测（`GET /live.m3u` 与 30 次原子替换并发，含 3ms 间隔）**：

```
writes_ok=30/30  writes_failed=0
reads=1232       torn=0        max_read_ms=59.70   mean_read_ms=2.43
final_is_last_version=True
```

即：**0 次半文件、0 次写失败**。同一场景固化为永久回归
`tests/test_server.py::test_concurrent_get_never_returns_half_written_file`
（20 个版本、正文长度各不相同；读到的每个字节都必须**恰好等于某一个完整版本**，
且写入必须**全部成功**）。

### 4.5 其它永久测试（`tests/test_server.py`，23 项）

正常路径 5 项（GET / HEAD / 多尺寸 Content-Length / ETag 变更 / 自定义路由）、
不可用 3 项（缺文件 GET+HEAD 503 / 空文件 503 / 删除后立刻回 503）、
`/healthz` 4 项、路由与信息边界 6 项（404 清单 / 兄弟文件不可读 / 查询串 / 自定义路由 /
非法路由配置 / 绝不外连）、只读性 1 项、并发与句柄自洽 3 项、生命周期 3 项。

---

## 5. 新鲜度与运行状态

### 5.1 状态文件 `out/runtime-status.json`

原子写（同目录临时文件 + `os.replace`）、**轮次条数封顶**（`status_history`，默认 5）、
路径过 `guard_runtime_output_path` 护栏。字段：`schema` / `version` / `updated_at` /
`started_at` / `pid` / `current_round` / `rounds[]` / `last_run_at` / `last_run_outcome` /
`last_run_error_category` / `last_success_publish_at` / `last_success_publish_status` /
`next_run_at`。

**只有真的写了文件**（`published=true` 且 `publish_status ∈ {OK, DEGRADED_FIXED_ONLY}`）
才推进 `last_success_publish_at`。失败轮次**绝不**推进它 —— 否则 `/healthz` 会撒谎。
永久回归：`test_failed_round_does_not_fake_last_success`。

**脱敏**：`_summarize_fetch_result()` 逐字段白名单化，异常文本过 `redact_text`。
实测状态文件里不出现 `http://`、`https://`、`txSecret`、任何签名常量。
（注：`live.m3u` **本身**是交付物，其中动态条目的签名 URL 是正常的；
「不许出现签名」只约束状态文件 / 摘要 / 日志 / `/healthz`。）

### 5.2 `compute_health()`

输出 `status`（`ok` / `stale` / `missing`）、`service`（版本 / 启动时间 / uptime / `read_only`）、
`playlist`（`exists` / `bytes` / `last_modified`）、`freshness`（`last_success_publish_at` /
`seconds_since_last_success` / `stale_after_seconds` / `is_stale` / `source`）、`last_run`、`note`。

- `source` 区分「来自运行期状态」与「文件缺失时退回 mtime」两种情况，便于排查。
- 时间戳坏掉时按「最老」处理（进 `stale`），不会因为一条坏数据就把状态判成 ok。
- 判定顺序：文件不存在 ⇒ `missing`；存在但超过阈值 / 无法判断 ⇒ `stale`；否则 `ok`。
- **不含**任何 stream URL、签名、数据库内容或来源清单。
- `stale` **不**触发删除 current `live.m3u`：播放器仍可读 last-known-good。
  文案里明确写出「stale 只表示距上次成功发布较久，不代表文件不可播」。

`stale_after_seconds` 默认 21600 = 3 小时周期的 2 倍（任务书建议 2–3 倍）。

---

## 6. 离线 E2E

```
python tools/demo_runtime.py
```

真实输出（节选，完整 28 项）：

```
liptv TASK-004 本地运行期演示（纯离线）
mock server : http://127.0.0.1:56933
工作目录    : .../out/demo-task004

=== 0. 初始化 / 注册来源 / 抓取 / 归集 / 既有测活历史 ===
  [PASS] init-db: exit=0
  [PASS] source-register（动态源默认关闭）: registered=3 enabled=1
  [PASS] fetch --all（只抓 enabled 的 fixed 源）: exit=0 requested=1 created=3
  [PASS] 固定侧准备完成（3 canonical / 3 stream / 3 条模拟测活）: canonical=3 stream=3

=== 1. 三轮 fake scheduler（走真实 Scheduler.run，只把 sleep 换成假实现） ===
  [PASS] 三轮全部跑完，且**没有**真实等待任何 interval: rounds=3 failed=0 fake_slept=21600s（真实耗时毫秒级）
  [PASS] 第 1 轮：固定 + 动态统一发布: status=OK fixed=3 dynamic=3
  [PASS] 第 2 轮：动态部分失败 → fail-closed 只发固定（状态与计数一致）: status=DEGRADED_FIXED_ONLY discarded=3
  [PASS] 第 3 轮：动态恢复 → 重新统一发布（loop 从未被打断）: status=OK dynamic=3
  [PASS] 第 2 轮实际写出的文件：没有任何动态分组、也没有任何动态条目名: degraded_bytes=301 动态分组出现=否
  [PASS] 第 3 轮文件覆盖了降级版本（动态分组回来了）: 动态分组出现=是 与降级版不同=是
  [PASS] 发布摘要与状态文件口径一致: summary_status=OK

=== 2. 单轮抛异常：异常被收敛进状态，下一轮照常发布 ===
  [PASS] 第 1 轮异常被记为 failed 轮次（不向上抛、不杀死 loop）: rounds=2 failed=1
  [PASS] 第 2 轮仍然正常发布（loop 结构完好）: outcome=ok status=OK

=== 3. 只读 HTTP：GET/HEAD /live.m3u、/healthz、503、404 ===
  [PASS] GET /live.m3u：200 + 原样返回 + 正确的 Content-Type / Content-Length: status=200 type=application/vnd.apple.mpegurl len=978
  [PASS] HEAD /live.m3u：与 GET 同一 Content-Length，且不发正文: status=200 len=978 body=0B
  [PASS] ETag / Last-Modified 都给出（供播放器做条件请求）: etag="1790823533416211800-978"
  [PASS] GET /healthz：200 + ok + 文件存在 + 新鲜度字段齐全: status=200 state=ok age=0s
  [PASS] 未知路径 / 越界路径 / 其它磁盘文件一律 404: 6 条路径全部 404，且没有泄漏任何文件内容
  [PASS] 缺文件：503（不生成空列表冒充成功）: status=503 body=b'playlist not available\n'

=== 4. 单实例锁：第二个 scheduler 被明确拒绝 ===
  [PASS] 第二个实例退出码 = 3（EXIT_LOCKED），并给出可解释原因: exit=3 status=LOCKED reason=held_by_live_process
  [PASS] 被锁定的一轮没有执行任何 fetch/publish（live.m3u 字节未变）: bytes_before=978 bytes_after=978
  [PASS] 持有者的锁没有被误删（token 不同就不动别人的锁）: lock_exists=True

=== 5. 关闭后：锁释放、HTTP 地址不可再连接 ===
  [PASS] 锁已释放: lock_exists=False
  [PASS] 只读服务已关闭（端口不再接受连接）: port=56945 reachable=False

=== 6. 信息边界：状态文件与 /healthz 不含任何 URL / 签名 ===
  [PASS] runtime-status.json：脱敏、条数封顶、关键字段齐全: rounds=5 bytes=6176 last_success=...
  [PASS] 失败轮次没有伪造「最后成功发布发布时间」（它必须等于最后一次成功发布）: last_success=...
  [PASS] /healthz 同口径：无 URL / 无签名 / 无来源清单: bytes=627
  [PASS] 发布摘要同样脱敏（签名/路径/查询串都不落盘，最多保留 origin）: bytes=3069 含签名=否

===== 汇总：28/28 通过 =====
```

**退出码 0**；产物全部在 `out/demo-task004/`（已被 `.gitignore` 忽略）。
脚本访问的全部地址都是 `127.0.0.1` 上的本机 mock 服务，
M3U 样本里的播放地址一律是 `*.invalid.example` 假域名；
**没有**访问真实 JSNZKPG，**没有**请求任何真实视频 URL。

---

## 7. 逐条验收（对应 `TASKS/TASK-004.md` §8）

| # | 验收项 | 结果 | 证据 |
|---|---|---|---|
| 1 | 原 189 项零回归 | **PASS** | §8：`248 passed`（189 + 59 新增，0 failed / 0 error） |
| 2 | `run --once` 离线 E2E；动态显式启用时合并成功 | **PASS** | `test_run_once_offline_e2e`、`test_run_once_with_dynamic_enabled_merges_and_fails_closed`；demo §1 |
| 3 | loop 用 fake clock/sleep 连跑 ≥3 轮，不真实等待；某轮抛异常后下一轮继续 | **PASS** | `test_loop_runs_three_rounds_without_real_waiting`（断言 `len(sleep.calls) == 2*3600/0.25`）、`test_loop_survives_round_exception`；demo §1/§2 |
| 4 | 单实例：第二实例拒绝；正常退出释放；stale 有永久测试 | **PASS** | §3.3 的 12 项；`test_run_second_instance_exits_locked`（exit=3）；demo §4 |
| 5 | HTTP：GET/HEAD `/live.m3u`、`/healthz`、404；缺文件状态；Content-Length/类型；并发 replace 只读到完整版 | **PASS** | §4.5 的 23 项；并发实测 1232 读 / 0 半文件（§4.4） |
| 6 | 安全：`/../`、任意文件路径、db/previous/summary 不可读；服务不接触 M3U 内 stream URL | **PASS** | `test_unknown_and_hostile_paths_return_404`（17 条路径）、`test_sibling_files_are_not_served`、`test_server_never_opens_outbound_connection`（0 次外连） |
| 7 | 新鲜度 ok → stale → 新成功 publish 后恢复 ok；失败轮次不能伪造最后成功时间 | **PASS** | `test_health_ok_then_stale_then_ok_again`、`test_failed_round_does_not_fake_last_success` |
| 8 | 动态源 fail-closed：状态与 publish summary 一致 | **PASS** | `test_run_once_with_dynamic_enabled_merges_and_fails_closed`；demo 第 2 轮**逐字节核对那一轮写出的文件** |
| 9 | Ctrl+C / shutdown：停止后不启动下一轮、锁释放、HTTP thread/server 正常关闭 | **PASS** | `test_real_process_stops_cleanly_on_signal_and_releases_lock`（**真子进程** + `CTRL_BREAK_EVENT` / `SIGINT`，断言 rounds==1、锁不存在、日志含「只读订阅服务已关闭」、原端口不可再连）、`test_keyboard_interrupt_is_caught_and_releases_lock` |
| 10 | `git diff --check`、执行报告、实际退出码、离线 demo、Git SHA、本地/远程一致 | **PASS** | §10 |

补充自查（任务书未单列，但属本轮红线）：

| 项 | 结果 | 证据 |
|---|---|---|
| 不伪造 probe 结果 | **PASS** | `runtime.py` 全文无 `add_probe_result` 调用；固定频道只走既有 `select_playlist`；demo 里那 3 条测活历史由**演示脚本**显式写入并标注为「模拟」 |
| 不自动创造 canonical/binding | **PASS** | `test_run_once_does_not_create_canonical_or_binding`（跑完 `list_canonical_channels` / `list_bindings` 均为空） |
| 不代代理视频流 | **PASS** | `server.py` 只 `os.read` 那一个文件；`test_server_never_opens_outbound_connection` 0 次外连 |
| 不部署腾讯云 / 不做 TLS / 不做 Docker / 不装 systemd | **PASS** | 本轮无任何部署产物；`[server]` 默认 `enabled=false` 且只绑 `127.0.0.1` |
| 不引入重型调度依赖 | **PASS** | 仅标准库（`threading` / `http.server` / `socket` / `signal` / `json`） |

---

## 8. 测试

### 8.1 命令（受控三件套）

```
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -o addopts="" -p no:cacheprovider \
  -q --basetemp="$TEMP/liptv-t4-full" tests/
```

（`--basetemp` 必须落在操作系统临时目录：指到仓库工作树内会改变
`test_lock_path_inside_untracked_git_worktree_is_refused` 那类用例的前提。）

### 8.2 结果

```
........................................................................ [ 29%]
........................................................................ [ 58%]
........................................................................ [ 87%]
................................                                         [100%]
248 passed in 74.17s (0:01:14)
```

**退出码 0，无 failed / error / warning。**

### 8.3 组成

| 分组 | 项数 | 说明 |
|---|---|---|
| TASK-001/002/003 既有 | 189 | **全部通过，零回归**（本次未改这些模块的行为） |
| `tests/test_runtime.py` | 36 | 锁 12 / 循环 6 / 状态与新鲜度 6 / 配置一致 4 / `run --once` 离线 E2E 与停止 8 |
| `tests/test_server.py` | 23 | 见 §4.5 |
| **合计** | **248** | |

### 8.4 关于测试规模

`liptv/m3u.py` 的改动只影响 `write_m3u` 的第 4 步（`os.replace` → 带重试的同义调用），
`test_publish.py` 里注入普通 `OSError` 的两项用例仍按**旧语义**失败即抛（`winerror`/`errno`
均为 `None` ⇒ 判定为非瞬时 ⇒ 立刻抛出，不重试），因此那两项断言一字未改、全部通过。

`tests/test_runtime.py` 在首轮自测中发现 3 项**断言写错**（非实现缺陷），已按实现真实语义修正：

1. `sync_streams` 的返回键是 `streams_created` / `streams_updated`（不是笼统的 `created`）；
2. 空组合在 TASK-003 冻结语义下走 `validate_composition` 的 `composition_errors`，
   状态是 `REJECTED_VALIDATION`（`REJECTED_EMPTY` 只是声明未使用的常量），断言改为前者并
   额外断言原因含「组合结果为空」；
3. `--dynamic` 不带 `--dynamic-source` 时，`runtime.resolve_dynamic_sources` 原先**漏了**
   「留空 ⇒ 回落数据库 enabled 动态源」这一步 —— 这是**真缺陷**（与 TASK-003 `publish` 的
   `_resolve_dynamic_sources` 语义不一致），已修正为与 publish 完全同口径，见 §11-①。

---

## 9. 风险与范围外

### 9.1 本轮**不是**云端稳定公网服务

- `out/live.m3u` 仍是**单次静态文件**：scheduler 会按周期刷新它，
  但**只有进程在跑的时候**才会刷新；进程停了，文件就停在最后一版。
- 「静态文件 ≠ 24h 订阅」的限制由本轮**部分**缓解（有了周期刷新），
  但只要 `run` 没在跑就仍然成立。动态赛事的短时签名 URL 依旧会自然过期 —— 
  这也是 `stale_after_seconds` 存在的原因：把「多久没成功更新」暴露出来，
  而不是假装文件永不过期。
- 未做 TLS / 域名 / 鉴权；默认只绑 `127.0.0.1`。要跨机器使用需自行评估网络可信度。

### 9.2 未实现（另立 TASK）

ffprobe/ffmpeg **真实**测活、多地区探针、automatic canonicalization、EPG / Logo、
Dashboard、视频代理/转码、腾讯云/公网部署、Docker、systemd / Windows 计划任务的真实安装、
`status_history` 之外的历史持久化。

### 9.3 已知取舍

1. **锁的 stale 策略**：「本机活进程 + 心跳过期」选择**拒绝**而非抢夺（§3.2）。
   代价：一次真的卡死需要人工删锁；收益：不可能出现两个写入者同时改库存。
2. **重试上限**：`_replace_with_retry` 最坏阻塞 ≈1.2s。若某读者持有句柄超过 1.2s，
   本轮发布仍会失败（并按既有语义回滚、当前与上一版都不半更新）。
   实测读者持有时间是毫秒级（mean 2.43ms / max 59.7ms），余量充足。
3. **`--now` 只对 `--once` 生效**：loop 每轮必须用真实时间，否则状态文件里的时间戳全部相同。

---

## 10. Git & Gate

### 10.1 提交前检查

| 检查 | 结果 |
|---|---|
| `git diff --check` | **退出码 0**（无空白/冲突标记错误） |
| `git status --porcelain -uall` | 只有预期的 6 个修改 + 5 个新增，**无** `out/` / `data/` / `*.sqlite3` |
| `check-ignore` | `out/live.m3u`、`out/runtime-status.json`、`out/liptv.lock`、`out/liptv.lock.steal`、`out/tmp/x.m3u`、`out/demo-task004/out/live.m3u`、`data/liptv.sqlite3` **全部 IGNORED** |
| 提交署名 | `lixiuzu <lixiuzuapple@gmail.com>` |
| 实现提交 SHA | `5ab2e9d7b5ba6823ca180a711988a2a254d3f66a` |
| 基线 | `c1f8ce350ed963d3661b50dbcebdbd4023973e19`（父提交，与推送前的 `origin/main` 一致） |

### 10.1.1 远端独立核验（云端连接器，非本地 git 自述）

`git push` 首次失败（`Failed to connect to github.com:443`），第 2 次成功：

```
c1f8ce3..5ab2e9d  main -> main
```

推送后用**云端连接器**独立读取远端，逐项比对：

| 项 | 远端 | 结论 |
|---|---|---|
| `main` HEAD | `5ab2e9d7b5ba6823ca180a711988a2a254d3f66a` | 与本机 `HEAD` **相等** |
| 父提交 | `c1f8ce350ed963d3661b50dbcebdbd4023973e19` | 与推送前 `origin/main` **相等** |
| 提交者 | `lixiuzu <lixiuzuapple@gmail.com>`，2026-10-01T03:05:34Z | 一致 |

blob 级逐一比对（远端 SHA == 本机 `git rev-parse HEAD:<path>`，**17/17 全等**）：

| 路径 | blob SHA |
|---|---|
| `liptv/runtime.py` | `65d3a76efe6dcd7407bb100c592442e15f59a6b1` |
| `liptv/server.py` | `28526e378dd8e22159d115202da044340aa17305` |
| `liptv/cli.py` | `17bf283ce461abf046ab6331778dc3d6478018ce` |
| `liptv/config.py` | `13ce0c4d375b3447a400eb94c43ecad8d3691439` |
| `liptv/m3u.py` | `652d4676778f0fad6f75be9a49e2eb04469739c7` |
| `liptv/publish.py`（未改，核对未被动过） | `639522d759f693ab80dfb7b642a8806243d571b6` |
| `liptv/repo.py`（未改） | `19fe5434daaa712c73245f4389b489ddc7bb720f` |
| `tests/test_runtime.py` | `7bf2ca71b6b19bb6363170639ca1dad425cc0a55` |
| `tests/test_server.py` | `c56647fae1f3d7622ef6a42179c744675668be2d` |
| `tests/test_publish.py`（未改） | `6b31de2db8d71210fd243124cfd0174f941a9e52` |
| `tests/test_m3u.py`（未改） | `aa451072275cc8fd35d4e79e301e684f16e41865` |
| `tools/demo_runtime.py` | `3e4770938bac1b515b51970fa9ae6229428cb397` |
| `tools/mock_source_server.py`（未改） | `cadcdc31b5be6d5b3df16c7e3124f4a7eeb8c44a` |
| `TASKS/TASK-004.md` | `74db5fb316a116d07becd1ac60e473b06d9283c9` |
| `REPORTS/TASK-004-REPORT.md` | `3f7b9197ae4a8442a49fe6525f50999adb5dbf85` |
| `config/config.example.toml` | `f7d0cf6f079da43a1d03075c5b3ee919b99c6258` |
| `README.md` | `aa85c7074e05255ecbbf5286e6d5a141a841df31` |

（`REPORTS/TASK-004-REPORT.md` 的 blob SHA 对应**实现提交**里的版本；
本报告随后被一次「记录提交」更新以写入上面的实现 SHA，因此该值的最终 blob 会再次变化 —— 
这与 TASK-002/003 的做法一致，未使用 amend。）

### 10.2 变更规模

```
 README.md                  | 120 ++++++++++++++++-
 TASKS/TASK-004.md          |   2 +-
 config/config.example.toml |  52 ++++++++
 liptv/cli.py               | 324 +++++++++++++++++++++++++++++++++++++++++++++
 liptv/config.py            |  40 ++++++
 liptv/m3u.py               |  44 +++++-
 6 files changed, 578 insertions(+), 4 deletions(-)

新增：liptv/runtime.py(1014) liptv/server.py(370)
      tests/test_runtime.py(953) tests/test_server.py(516) tools/demo_runtime.py(560)
```

### 10.3 Gate

- 状态已置为 **REVIEW**；`ACCEPTED` / `REJECTED` 只有大G可改。
- **未**启动 TASK-005，**未**做任何部署动作。
- 等大G独立验收。

---

## 11. 自查发现并已修正的问题

| # | 问题 | 性质 | 处理 |
|---|---|---|---|
| ① | `runtime.resolve_dynamic_sources` 漏了「tokens 留空 ⇒ 回落数据库 enabled 动态源」，导致 `run --dynamic` 在 `[runtime] dynamic_sources` 留空时把动态源解析成空列表 | **实现缺陷**（与 TASK-003 `publish` 语义不一致） | 改为与 `cli._resolve_dynamic_sources` **完全同口径**：显式 token 优先（允许指向已登记但未启用的源），留空则取 `list_sources_by_kind(KIND_DYNAMIC, enabled_only=True)` |
| ② | 首轮自测里 3 项断言与实际语义不符（`created` 键名、`REJECTED_EMPTY` vs `REJECTED_VALIDATION`、动态源回落） | 测试断言错误 | 按实现真实语义改写，并在注释里写清依据（① 是其中唯一真正的实现缺陷） |
| ③ | `demo_runtime.py` 把 `config_mod.runtime_settings(path)` 当成「传配置字典」用了 | 脚本缺陷 | 改为先 `config_mod.load_config(cfg)` |
| ④ | `demo_runtime.py` 曾断言 `live.m3u` 里不出现 `txSecret` | 断言方向错误（`live.m3u` 是交付物，动态条目本就带签名 URL） | 改为断言「**降级那一轮实际写出的文件**里没有动态分组/条目名」；「不许有签名」的约束只留给状态文件 / 摘要 / `/healthz` |
| ⑤ | `test_server.py` 并发用例初版把 `(payload, raw)` 元组当字节写盘 | 测试缺陷 | 修正后实测：写入 20/20 全成功、读取全部命中完整版本 |
| ⑥ | 新增 `test_config_example_runtime_and_server_match_code_defaults`：锁死示例配置与代码默认值 | 预防性回归 | 防止「文档说的」与「程序做的」漂移 |

---

## 12. Review 01 定向返工（QA-004A / QA-004B）

受审 HEAD：`58e61742cc931764980708ab1704ee76e851966c`（REJECT）。
**只**动锁与调度生命周期，**未**重构 HTTP 服务、`publish` 逻辑、数据模型或 TASK-001/002/003 已验收模块。

### 12.1 QA-004B：`release` / `heartbeat` 改为 fail-closed

**问题（大G反例已复现）**：旧 `release()` 在「元数据无法解析 ⇒ `current is None`」时直接
`unlink`，即「无法证明锁是自己的」也照删，与模块自己声明的语义矛盾。

**修复**：`release()` 现在只有在**能解析**且 **`token` 等于自己的 token** 时才删；以下一律
**不删并返回 `False`**：文件不存在 / 空文件 / 损坏 JSON / 缺 `pid` / `token` 不同 /
文件存在但读不出来（I/O 故障）。宁可留下一次需要人工清理的锁，也绝不误删别人正在用的锁。

`heartbeat()` 按同一口径收紧（这是 Review 01 §"必须修复"最后一条明确要求的）：

| 情况 | 旧行为 | 新行为 |
|---|---|---|
| `token` 不是自己的 | 放弃所有权，返回 `False` | 同左（不变） |
| 内容不可解析 / 缺 `pid` | **照常覆盖写回自己的元数据** | **放弃所有权、返回 `False`、绝不覆盖未知锁** |
| 文件不存在 | 照常重建锁文件 | 放弃所有权、返回 `False`、**不重建** |
| 读文件抛 `OSError`（权限/占用） | 异常直接冒泡 | 返回 `False`，**保留**所有权（不是"易主"，是暂时读不了），记录 `last_error` |
| 写盘抛 `OSError` | 异常直接冒泡 | 返回 `False`，**保留**所有权（文件里仍是自己的 token），记录 `last_error` |

新增只读属性 `ownership_lost` / `lost_reason` / `last_error`，供日志与状态判定使用。

### 12.2 QA-004A：心跳真正接入调度生命周期

Review 01 的核心事实：`heartbeat()` 实现了，但 `cmd_run` / `Scheduler` **一处业务调用都没有**，
长跑中 `heartbeat_at` 永远停在 `acquire` 时刻。

**周期推导**：新增 `resolve_heartbeat_interval(stale_after_seconds, interval_seconds)`，
取 `min(stale × 0.25, interval × 0.25, 300)` 并夹在 `[0.25s, 300s]`：

| 配置 | 心跳周期 | 相对 stale 阈值 |
|---|---|---|
| 默认 `stale=21600, interval=10800` | **300s** | 1/72 |
| `stale=10, interval=60`（测试尺度） | **2.5s** | 1/4 |
| `stale=3600, interval=1` | **0.25s** | 1/14400 |

即**恒 ≤ stale 阈值的 1/4**，且**不是配置项** —— 它本来就不该被配成比 stale 阈值还大。

**四个刷新点**（缺一就会出现"长轮次/长休眠仍被判 stale"）：

| # | 位置 | 覆盖的场景 |
|---|---|---|
| ① | `Scheduler.run_once` 进入轮次前 | 轮次起点；**同时是守卫**：核不通过就连 `round_fn` 都不调用 |
| ② | `Scheduler.run_once` 轮次结束（含本轮抛异常） | 把 `heartbeat_at` 对齐到轮次边界 |
| ③ | `Scheduler._sleep_between_rounds` 休眠切片累计到周期 | `interval ≫ stale` 时的长休眠 |
| ④ | `LockHeartbeat` 后台线程（`run` 与命令行 `--once` 期间常驻） | **单轮本身很久**（慢抓取）——①②③ 都盖不住 |

**失去锁 ⇒ 停**：任一次心跳返回 `False` 即 `request_stop(LOCK_REASON_LOST)`：

- 不进入下一轮；进入轮次前的守卫会让那一轮**不调用 `round_fn`**（即不 fetch、不 publish），
  只在状态文件里留一条 `outcome=lock_lost` / `skipped=true` / `next_run_at` 记录说明原因；
- 后台线程发现丢锁时通过 `on_lost` 回调同样置停（`_sleep_between_rounds` 因此会立刻返回，
  不会睡满整个周期）；
- 循环模式下「运行中丢锁」以退出码 **`1`** 结束（`--once` 由轮次自身决定）。这是**新增条件**，
  没有改动 `0/1/2/3` 任何既有语义，`EXIT_LOCKED=3` 仍然只表示"一开始就没拿到"。
- `release()` 此时返回 `False`（已放弃所有权）—— 不会删掉别人的锁。CLI 会打印
  「保守处理：未删除锁文件」。

### 12.3 返工过程中**新发现**的两个真问题（接线程才暴露）

这两条不是 Review 01 提出的，是"把心跳真正跑起来"之后实测出来的，属同一修复面的必然产物：

| # | 问题 | 实测现象 | 处理 |
|---|---|---|---|
| ⑦ | **进程内锁文件 I/O 竞争**：心跳线程与业务线程同时碰同一把锁文件，而读侧用的是默认 `open()`（不允许别人替换） | `test_heartbeat_thread_refreshes_during_a_long_round` 实测 `lock_lost=True`：主线程在 `round_end` 心跳时读到瞬时错误，把一把**健康的锁**误判成"已失去归属" | ① `SingleInstanceLock` 增加 `RLock`（`_io`）串行化锁文件 I/O；② 读侧改走 `CreateFileW(FILE_SHARE_READ\|WRITE\|DELETE)`（`_read_shared_bytes`，与 `liptv.server` 读播放列表同一手法，瞬时 5/32 有极短重试）；③ `_read_metadata` 区分「读不出内容」与「打不开文件」，后者交给调用方保守处理 |
| ⑧ | **临时文件同名**：`_write` / `_atomic_write_json` 用 `f"{name}.tmp{pid}"`，两个线程会算出同一个临时名 | 一个线程写一半、另一个 `os.replace` 掉它 → 对方 `FileNotFoundError` | 改为 `_unique_tmp_path()`：`f"{name}.tmp{pid}-{next(_TMP_SEQ)}"`（全局递增序号，跨线程/跨进程都不撞） |

> ⑧ 的严重性其实高于 QA-004A 本身：它会让**心跳线程自己把调度器打停**。已用
> `test_heartbeat_thread_and_round_heartbeats_do_not_self_conflict` 把它钉死（25 轮 + 0.01s 线程周期
> 高密度交错，断言 `lock_lost is False`、无残留 `*.tmp*`）。

另外为让"心跳可持续"名副其实，锁文件写入沿用了 TASK-004 已确立的**瞬时占用有界重试**口径
（`_replace_with_retry`，只对 winerror 5/32 生效）。不这样做的话，另一个实例 `acquire` 时的
一次读取抖动就会让长跑停摆 —— 那是把 QA-004A 换了个形式复发。

### 12.4 新增永久回归（18 项，全部收进 `tests/test_runtime.py`）

| 组 | 用例 |
|---|---|
| QA-004B 保守 release（6） | `test_release_refuses_corrupt_lock_file`、`test_release_refuses_empty_lock_file`、`test_release_refuses_metadata_without_pid`、`test_release_refuses_when_lock_file_already_gone`、`test_release_deletes_only_its_own_lock`、`test_heartbeat_refuses_and_does_not_overwrite_unreadable_lock`、`test_heartbeat_refuses_when_lock_file_missing` |
| 心跳周期（1） | `test_heartbeat_interval_is_far_below_stale_threshold`（含 5 组参数 + 上界不变式） |
| QA-004A 长跑（2） | `test_long_run_refreshes_heartbeat_and_remote_observer_cannot_take_over`、`test_heartbeat_continues_during_long_sleep_without_rounds` |
| QA-004A 丢锁即停（3） | `test_scheduler_stops_after_lock_is_stolen_and_never_touches_it`、`test_run_once_after_lock_lost_skips_without_calling_round_fn`、`test_heartbeat_thread_does_not_start_when_lock_already_lost` |
| 心跳线程（2） | `test_heartbeat_thread_refreshes_during_a_long_round`、`test_heartbeat_thread_and_round_heartbeats_do_not_self_conflict` |
| 接线回归（2） | `test_cli_run_wires_lock_heartbeat_into_scheduler`（spy 包住 `heartbeat`，**任何**真实调用都会被数到）、`test_cli_run_loop_stops_when_lock_is_stolen_and_keeps_foreign_lock` |
| 不回归（1） | `test_no_lock_means_scheduler_untouched_by_heartbeat`（不传 `lock` 时行为与首版一致） |

**关于"判别力"**——为免出现"用例本身就不成立"的自证式回归，长跑用例里额外做了**反证**：

> 把 `heartbeat_at` 冻结在 `acquire` 时刻（= 旧实现的行为），同一组参数下另一个主机**立刻**能
> 以 `stale_heartbeat` 接管。也就是说：上面断言到的 `held_by_remote_host`，是"心跳真的在跑"
> 挣来的，不是场景不成立。

`test_long_run_refreshes_heartbeat_and_remote_observer_cannot_take_over` 的实测形态：
`stale=10s` / `interval=60s` / 5 轮 = 模拟 **240s**（stale 的 24 倍），
逐休眠切片采样心跳年龄，实测 `max_age = 2.5s`（= 推导出的周期），
结束时 `heartbeat_at` 由 `2026-10-01T05:19:13+00:00` → `2026-10-01T05:23:13+00:00`。

`test_heartbeat_thread_refreshes_during_a_long_round` 用**假墙钟 + 真实线程**区分职责：
锁的"现在"只有轮次自己能推进，而线程按真实 0.05s 跑；断言"轮次**进行中**读到的心跳已经
跟着假钟前进"——这一步只可能来自后台线程，轮次边界心跳还没轮到。

### 12.5 测试结果

| 项目 | 结果 |
|---|---|
| `tests/test_runtime.py` | **54 passed**（36 → 54，+18），耗时 ~33s |
| 全量 | **266 passed in 100.62s**（248 基线 + 18 新增，**零回归**，无 warning） |
| 稳定性 | 锁/心跳相关 32 项**连跑 8 次全绿**（`-k "heartbeat or lock or release"`），无抖动 |
| `tools/demo_runtime.py` | **36/36 通过**（新增 §4b 段，见 §12.6） |
| 命令 | `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -o addopts="" -p no:cacheprovider -q --basetemp="$TEMP/<名>"` |

> 一处**既有**用例断言随日志文案更新：`test_real_process_stops_cleanly_on_signal_and_releases_lock`
> 原断言 `"锁已释放：True" in out`，现日志改为打印具体锁路径（信息量更大），
> 断言改为 `f"[runtime] 锁已释放：{lock}" in out` 并加一条"不得走保守分支"的反向断言。
> 该用例的语义断言（锁文件确实被删、端口确实关闭）未变。

### 12.6 离线演示新增 §4b（可直接复现）

`python tools/demo_runtime.py` 新增一段 8 项检查，把两处返工都在离线环境里演一遍：

```
=== 4b. 锁心跳与保守 release（QA-004A / QA-004B 返工） ===
  [PASS] 长跑 240s（stale 阈值只有 10s）时心跳持续推进，年龄从未逼近阈值: rounds=5
         heartbeat_at 2026-10-01T05:19:13+00:00 -> 2026-10-01T05:23:13+00:00 max_age=3.00s（周期 2.5s）
  [PASS] 轮次结束把心跳对齐到当前时刻（不是只在 acquire 时刷一次）
  [PASS] 同一时刻由「他机」观察同一把锁：判 held_by_remote_host，不抢锁、锁未被改动
  [PASS] token 被替换后：调度器停止（lock_lost），一轮都没跑，也不碰别人的锁
  [PASS] release 遇到「损坏 JSON」：保守不删（无法证明归属）
  [PASS] release 遇到「空文件」：保守不删（无法证明归属）
  [PASS] release 遇到「能解析但缺 pid」：保守不删（无法证明归属）
  [PASS] release 遇到「自己的锁」：照常删除（保守语义不许误伤正常路径）
```

### 12.7 变更规模（返工 commit，相对 `58e6174`）

```
 README.md                  |  33 +-
 REPORTS/TASK-004-REPORT.md |   2 +
 TASKS/TASK-004.md          |   2 +-
 liptv/cli.py               |  24 +-
 liptv/runtime.py           | 568 ++++++++++++++++++++++++++++++++++++++++------
 tests/test_runtime.py      | 540 ++++++++++++++++++++++++++++++++++++++-
 tools/demo_runtime.py      | 162 +++++++++++-
 7 files changed, 1303 insertions(+), 65 deletions(-)
```

文件规模：`liptv/runtime.py` 1014→**1525**、`tests/test_runtime.py` 953→**1491**、
`tools/demo_runtime.py` 560→**721**、`liptv/cli.py` 1380→**1402**、`README.md` 285→**320**。

### 12.8 范围确认

- **未触碰**：`liptv/server.py`（HTTP 一行未改）、`liptv/publish.py`、
  `liptv/repo.py`、`liptv/ingest.py`、`liptv/fetch.py`、`liptv/select.py`、`liptv/db.py`、
  `schema/schema_v1.sql`（`SCHEMA_VERSION` 仍为 1，**零 schema 改动**）、`liptv/util.py`、
  `config/config.example.toml`（心跳周期是推导值，**不新增配置项**，示例与默认值一致性用例因此仍绿）。
- **未启动** TASK-005；**未**做任何部署动作；**未**降低任何既有安全/保守口径。

### 12.9 本次返工对既有冻结语义的影响（逐条自查）

| 冻结项 | 是否变化 |
|---|---|
| 退出码 `0/1/2/3` 含义 | **不变**（仅新增"运行中丢锁 ⇒ 1"这一新条件的映射） |
| stale 判定五分支表 | **不变**（新增的只有"读不出来 ⇒ 拒绝抢锁"这一保守分支） |
| HTTP 缺文件 503 口径 | **不变**（未触碰 `server.py`） |
| 「不生成空列表冒充成功」 | **不变** |
| 「不伪造 probe / 不代理视频流 / 不提前部署腾讯云」 | **不变** |
| 状态文件字段与脱敏口径 | **不变**（仅新增 `skipped`/`skip_reason`/`outcome=lock_lost` 这一新情形的取值） |
| `[runtime]` / `[server]` 配置项 | **不变**（心跳周期是推导值） |
