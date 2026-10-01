# Li IPTV Aggregator

个人自用 IPTV 源聚合与自动维护项目。

## V1 目标

长期提供一个固定的 M3U 订阅 URL：

```
/live.m3u
```

后台自动完成：
- 拉取多个公开/授权 IPTV 来源
- 频道归一化与去重
- 保存同频道的多条 stream
- 多探针测活
- 基于历史稳定性选择最优线路
- 生成并原子发布 M3U
- 失败时保留 last-known-good

播放器直接访问实际 stream，本项目不做视频中转或转码。

## V1 原则

- 个人自用
- 最小可用
- SQLite
- CLI / 定时任务优先
- 不做 Dashboard
- 不做用户系统
- 不做转码/DVR/VOD
- 能复用成熟组件就不重造

## 快速上手

要求 Python 3.11+（仅用标准库）。

```bash
# 初始化数据库
python -m liptv init-db

# 导入本地 M3U
python -m liptv import-m3u examples/source_a.m3u --source "src-a"

# 建立归一化频道与绑定
python -m liptv canonical-add --name "CCTV-1 综合" --category 新闻
python -m liptv binding-add --source-channel-id 1 --canonical-id 1
python -m liptv stream-sync

# 选线并生成订阅
python -m liptv select --all
python -m liptv generate-m3u
```

运行测试：`python -m pytest -q`

> 本机（WorkBuddy 沙箱）跑全量测试时，需要把 pytest 的 `--basetemp` 指到**操作系统临时目录**，
> 否则临时目录清理会撞上沙箱的批量删除保护、把 setup 阶段打断：
> `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -o addopts="" -p no:cacheprovider -q --basetemp="$TEMP/liptv-pytest"`。
> 也不要把它指到本仓库工作树内（会改变「路径不在任何 Git 工作树内」那类用例的前提）。

## 远程来源（两类，严格分离）

| kind | 用途 | 是否进固定频道库存 |
|---|---|---|
| `fixed_m3u` | 相对稳定的公开频道列表 | 是（source_channel → canonical → stream） |
| `dynamic_event_m3u` | 动态赛事列表（如 JSNZKPG） | **否**，只做临时获取与预览 |

### 最短上手（全程不碰公网）

```bash
# 1) 起本地 mock 服务（另开一个窗口）
python tools/mock_source_server.py --port 8800

# 2) 注册来源（默认全部禁用，见 config/config.example.toml）
cp config/config.example.toml config/config.toml
python -m liptv source-register --from-config

# 3) 启用一个 fixed 来源（示例里 demo-fixed 指向 mock 的 /ok.m3u）
python -m liptv source-add --name demo-fixed --kind fixed_m3u \
    --url http://127.0.0.1:8800/ok.m3u --enable

# 4) 抓取并查看生命周期
python -m liptv fetch --all          # 只抓 enabled 的 fixed_m3u 来源
python -m liptv source-status        # 每个来源的状态与条目 active/inactive 统计

# 5) 动态赛事源：只预览，不落库
python -m liptv dynamic-fetch --source jsnzkpg-sports
```

要点：

- `fetch --all` **不会**请求禁用的来源，也**不会**请求 `dynamic_event_m3u` 来源（会明确列在 `skipped_*` 里）。
- 抓取失败（HTTP 非 2xx / 超时 / 解码失败 / 非法或空列表）**不会改动已有库存**，只更新该来源的 fetch 状态。
- 本次未出现的条目只置 `active=0`（不硬删，`first_seen_at` 与绑定保留），重新出现时自动恢复并复用原身份。
- 动态源摘要里的播放地址一律脱敏（去掉 query），不会把短时签名参数写进日志或报告。
- `dynamic-fetch --out <路径>` 是**三重强制**的：① 目标必须位于 `fetch.dynamic_tmp_dir`（默认 `out/tmp`）之内；
  ② 目标在 Git 层面必须安全 —— 位于某个 Git 工作树内时必须被 `.gitignore` 忽略，不在任何工作树内则允许；
  ③ 目标**不得已被该工作树跟踪**（存在于 Git 索引中）—— 因为 `.gitignore` 只对未跟踪文件生效，
  曾经 `git add -f` 过的文件必须另行拦截。
  任一不满足即拒绝写入且**不创建文件**、旧文件一个字节都不动；把 `dynamic_tmp_dir` 改成仓库内未被忽略的目录（如 `SOURCES/`）会被直接拒绝。
  判定用 `git ls-files --error-unmatch`（输出全部丢弃），取不到 git 时回退到直接解析 `.git/index`（v2/v3/v4）；
  两者都不可用则**保守拒绝**。可用环境变量 `LIPTV_GIT_EXECUTABLE` 指定 git 路径。
- 一次响应若「解析出了条目、但仍有 `#EXTINF` 没有配套播放地址」（被上游截断），会被判为 `INVALID_M3U`
  并整体拒绝 —— 不会因为一次截断的响应就把已有频道静默下线。结构完整的真正删台照常生效。

## 统一发布：`publish`（TASK-003）

把「已归一化、且有合格测活历史」的固定频道，与本轮即时抓取的动态赛事，
在**单次显式命令**里合并、校验并安全发布到一个**本地** M3U 文件。

```bash
# 只发固定频道（默认完全不联网：连一次 HTTP 都不会发）
python -m liptv publish

# 显式合入某个已登记的动态赛事源（可重复传 --dynamic-source）
python -m liptv publish --dynamic-source jsnzkpg-sports

# 动态失败就整次拒绝（默认策略是「降级为只发固定频道」）
python -m liptv publish --dynamic-source jsnzkpg-sports --require-dynamic

# 只组合与校验，不写任何文件（含不写摘要）
python -m liptv publish --dynamic --dry-run
```

| 状态 | 退出码 | 含义 |
|---|---|---|
| `OK` | 0 | 固定 + 动态都成功，已发布 |
| `DEGRADED_FIXED_ONLY` | 0 | 任一动态源失败 → fail-closed 只发固定频道（**本轮动态条目全部舍弃**，**绝不复用上一次的动态签名线路**） |
| `DRY_RUN` | 0 | 只校验，未写文件 |
| `DEGRADED_NO_PUBLISH` | 2 | 动态失败且固定也为空 → 不发布，**也不覆盖**已有文件 |
| `REJECTED_DYNAMIC_REQUIRED` | 1 | 指定了 `--require-dynamic` 但动态失败/缺失 → 整次拒绝 |
| `REJECTED_VALIDATION` | 1 | 组合/校验未通过（含全空结果、反向解析失败） |
| `REJECTED_IO` | 1 | 写盘失败 → 当前与上一版文件**字节不变** |

要点：

- **固定频道**复用 `select_playlist`：按历史探针记录选线、每个 canonical 最多一条、**不绕过最低成功阈值**（从未探测过的 stream 不会被发布）。
- **动态赛事**复用 TASK-002 的 `preview_dynamic_source`（HTTP 限额 + M3U 结构校验）；只取**本轮**实际成功获取的条目，**不落库**、也**不从旧 `live.m3u` 或历史快照回拼**动态线路；统一归入独立分组（默认 `体育赛事（实时）`），保留比赛名与 `[解说]` / `[原声]` 区别。
- 纳入规则**简单、配置化、可解释**（见 `[publish.dynamic]`）：默认走**排除法**——先剔掉宣传/推广与回放，其余分组（= 各联赛名）保留。原因是真实上游（JSNZKPG）的 `group-title` 是**联赛名**（`WNBA` / `欧俱杯` …），而「正在直播 / 赛事回放」只是 M3U 内部的**注释分区标记**；早期把「正在直播」当白名单会导致真实源 0/61 全被排除（TASK-003 QA-003A）。回放按**分组名或注释分区**两种写法识别（`# ===== 赛事回放 =====` 也能认），`include_replay=false` 时整类关闭；显式填写 `include_groups` 才会切换成严格白名单模式。过滤理由与计数都会写进摘要。
- 仅在同一次动态来源内做**字节级**去重（URL + 显示名 + 原始分组）；不跨来源去重，也不把 `[解说]`/`[原声]` 合并。
- **fail-closed 是「真」只发固定**：只要有一个动态来源失败，本轮**所有**动态条目（含其他成功来源的）一律舍弃，状态/计数/摘要/实际文件内容四者一致（QA-003B）。`--require-dynamic` 则整次拒绝、文件字节不变。
- 发布前完成全部校验；写盘走**同目录临时文件 → 原子替换 → `live.previous.m3u` 备份**，替换失败会把备份回滚，当前与上一版都不会半更新。全空结果默认**不覆盖**已有正常列表。
- 发布摘要（默认 `out/publish-summary.json`，已被 `.gitignore` 忽略）只含计数、过滤理由、来源抓取结果、checksum、退出码；**不含**完整签名 URL / playpath / 动态整表，URL 只保留 `scheme://host`。
- ⚠️ **本轮产物是单次静态文件**：在下一轮 `publish` 执行前**不会自动过期**，也**不是**「24 小时可用的稳定订阅」。这个限制留给后续调度/服务任务解决。

离线端到端演示（不访问任何公网地址）：

```bash
python tools/demo_publish_pipeline.py
```

`python -m liptv publish` 与旧命令兼容：`generate-m3u` / `select` 行为不变，**不会**被悄悄改成联网命令。

## 本地定时运行 + 只读 HTTP 订阅（TASK-004）

把上面的「手工跑一次 `publish`」推进成一个**可长期运行的本地进程**，并给播放器一个固定地址。

```bash
# 只跑一轮（测试 / 系统计划任务 / 手工维护）
python -m liptv run --once

# 长期循环（Ctrl+C / SIGTERM 干净停止：不启动新一轮、释放锁、关掉 HTTP）
python -m liptv run

# 循环 + 只读 HTTP 订阅
python -m liptv run --serve

# 只提供只读 HTTP 订阅（不抓取、不发布、不占单实例锁）
python -m liptv serve
```

每轮顺序：① 抓取所有 enabled `fixed_m3u`（单源失败不阻塞其它源）→ ② `stream-sync` 归集
→ ③ 按配置显式决定是否拉动态赛事源 → ④ 调用 TASK-003 的统一 `publish` → ⑤ 记录脱敏状态与下次计划时间。

| 退出码 | 含义 |
|---|---|
| `0` | 本轮/循环正常（含 `DEGRADED_FIXED_ONLY` 这类降级发布；Ctrl+C 停也算正常） |
| `1` | 本轮失败（沿用 `publish` 的 `REJECTED_*` 语义）；**也用于「运行中失去单实例锁」**——那是异常终止，不是正常收尾 |
| `2` | 未发布但输入本身没问题（`DEGRADED_NO_PUBLISH`） |
| `3` | `EXIT_LOCKED`：**一开始就**被另一个实例持锁，**本轮没有执行任何 fetch/publish** |

### 单实例锁

同一套 data/output 目录只允许一个 scheduler。锁是原子创建（`O_CREAT|O_EXCL`）的 JSON 文件，
含 PID / 主机名 / 创建与心跳时间 / token。stale 判定是**可解释**的，不会「文件在就永远锁死」，
也不会「判断不了就抢锁」：

| 持有者 | 进程存活 | 心跳年龄 | 判定 |
|---|---|---|---|
| 本机 | 存活 | 新鲜 | 拒绝（`held_by_live_process`） |
| 本机 | 存活 | 过期 | **拒绝**并打印 PID 与清理办法（`held_by_live_process_with_stale_heartbeat`，避免两个写入者同改库存） |
| 本机 | 已退出 | —— | 安全接管（`stale_dead_pid`） |
| 他机 | 无法判定 | 新鲜 | 拒绝（`held_by_remote_host`） |
| 他机 | 无法判定 | 过期 | 按心跳接管（`stale_heartbeat`） |
| 元数据损坏 | 无法判定 | —— | 新鲜⇒拒绝，过期⇒按 mtime 接管 |

接管走「`<lock>.steal` 独占占位 → 原子替换」，两个实例不会同时抢到锁。
`release()` 只删 token 还是自己的那把锁。Windows 上的存活判定走 `OpenProcess` +
`WaitForSingleObject`（**绝不**用 `os.kill(pid, 0)` 去猜，那会真的杀进程）。

### 锁心跳：由谁、多久刷一次

长跑不刷新心跳，锁就会被别人当成 stale 抢走 —— 于是共享目录上出现两个写入者。
因此心跳是**真的接进调度生命周期**的，不只是"实现了没调用"：

| 场景 | 谁来刷 |
|---|---|
| 每轮开始 / 每轮结束（含本轮抛异常） | `Scheduler` 自己 |
| 轮与轮之间的长休眠 | `Scheduler._sleep_between_rounds` 按周期刷 |
| **单轮本身很久**（例如慢抓取） | `LockHeartbeat` 后台线程兜底 |

周期由 `stale_after_seconds` 与 `interval_seconds` 统一推导，恒 ≤ `stale_after_seconds × 0.25`
（即至少 4 倍余量），并夹在 `[0.25s, 300s]`：默认 6h/3h 配置下是 **300s**。
**不是配置项** —— 它就是不该被配置成比 stale 阈值还大。

失去锁怎么办：

- `heartbeat()` 一旦返回 `False`（token 被替换 / 锁文件被删或损坏 / 写盘失败），
  调度器立刻 `request_stop(lock_lost)`：**不再进入下一轮，不再执行任何 fetch/publish**。
- 进入轮次前的守卫会再核一次归属，失去锁的那一轮**连 `round_fn` 都不会被调用**，
  只在状态文件里留一条 `outcome=lock_lost` 的记录说明为什么停。
- 循环模式下「运行中丢锁」以退出码 `1` 结束（`--once` 同理由轮次自身决定），
  CLI 输出里的 `lock_lost` / `lock heartbeat` 行会写清楚。

保守语义（**无法证明就什么都不做**）：

- `release()` 只在「能解析 + `token` 是自己的」时才删。文件缺失、空文件、损坏 JSON、
  缺 `pid`、token 不同 —— 一律**不删**并返回 `False`（宁可留一把要人工清理的锁，
  也绝不误删别人正在用的锁）。
- `heartbeat()` 同理：读不出归属就放弃所有权并返回 `False`，**绝不覆盖未知锁**。

### 只读 HTTP 订阅

| 路由 | 行为 |
|---|---|
| `GET /live.m3u` | 返回当前已发布的 M3U 文件（`application/vnd.apple.mpegurl`，带 `Content-Length` / `Last-Modified` / `ETag`） |
| `HEAD /live.m3u` | 与 GET 报出**一致**的 `Content-Length`，不发正文 |
| `GET /healthz` / `HEAD /healthz` | 服务 / 文件存在与发布新鲜度（JSON） |
| 其它任何路径 | `404` |

冻结口径与安全边界：

- **缺文件**（或空文件）返回 **`503`** —— 不会生成一个空列表冒充成功。
- **绝不代理视频流**：服务只读那一个 M3U 文件，条目里的播放地址永远不会被请求。
- **路由是固定映射**：只做「请求路径 == 配置里的确切字符串」比较，从不把 URL 拼成磁盘路径，
  因此 `/../`、`%2e%2e%2f`、绝对路径等自然落进 404，不存在路径穿越面。
- 不暴露数据库、`*.previous.m3u`、发布摘要原文或任何其它磁盘文件。
- 默认只绑 `127.0.0.1`；绑非 loopback 需要显式配置，启动时会打印安全提示。
- **HTTP 读取不阻塞 scheduler 的原子替换**：Windows 上若有句柄打开着目标文件，
  `os.replace` 会抛 `PermissionError [WinError 5]`（同进程也一样，实测）。读取侧因此统一用
  `FILE_SHARE_READ|WRITE|DELETE` 的共享读，写侧再加一层**只针对瞬时占用**的有界重试
  （12 × 0.1s，其余 `OSError` 原样抛出、不改变既有回滚语义）。播放器只会看到完整旧版或完整新版。
  > 实测补充（2026-10-01）：`os.replace`（= `MoveFileEx(REPLACE_EXISTING)`）**不认**
  > `FILE_SHARE_DELETE`，目标被任何句柄打开都会失败——所以真正兜住这里的其实是**写侧重试**，
  > 共享读让「一方抖动 = 另一方失败」的概率大幅下降、并且保证玩家读到的永远是完整版本。
  > 状态文件因为读频率高得多，另有专门处理，见下节。

### 新鲜度 `/healthz`

```json
{
  "status": "ok | stale | missing",
  "service": {"version": "...", "started_at": "...", "uptime_seconds": 0, "read_only": true},
  "playlist": {"exists": true, "bytes": 1234, "last_modified": "..."},
  "freshness": {"last_success_publish_at": "...", "seconds_since_last_success": 12,
                "stale_after_seconds": 21600, "is_stale": false, "source": "runtime_status"},
  "last_run": {"round_id": "...", "outcome": "ok", "finished_at": "...",
               "publish_status": "OK", "error_category": null},
  "note": "..."
}
```

**不含**任何完整 stream URL、签名、数据库内容或来源清单。`stale` 只表示「距上次成功发布较久」，
**不代表文件不能播**——不做自动删除，播放器仍可读 last-known-good。

`stale_after_seconds` 建议取 `interval_seconds` 的 2–3 倍（默认 3 小时周期 / 6 小时阈值）。
失败轮次**不会**推进 `last_success_publish_at`，否则 `/healthz` 会撒谎。

### 运行期状态文件

`out/runtime-status.json`（已被 `.gitignore` 忽略，原子写、轮次条数封顶）：记录 `round_id`、
开始/结束时间、fetch 摘要、publish 状态、耗时、异常分类、下一次运行时间。同样脱敏。

#### 状态文件与 `/healthz` 的并发（QA-004C）

`/healthz` **每次请求**都会读这个文件，而 scheduler 每轮都会重写它 —— 两边必须能真正并存。
实测（Windows 10 / Python 3.13）：

| 写侧用的 API | 读者 `FILE_SHARE_READ` | 读者 `FILE_SHARE_READ\|WRITE\|DELETE` |
|---|---|---|
| `MoveFileEx(REPLACE_EXISTING)`（`os.replace`） | `WinError 5` | **`WinError 5`** |
| `ReplaceFileW` | `WinError 32` | **成功** |

即 `os.replace` 根本不认 `FILE_SHARE_DELETE`。所以状态文件的替换走 `ReplaceFileW`
（目标不存在时退化为 `os.replace`），读侧用共享读，两边再各配一层很短的瞬时重试：

- 写侧：`ReplaceFileW` + 40 × 0.01s 的**窄口径**有界重试（只认 `winerror` 5/32；
  权限、磁盘满、非法路径等其它 `OSError` 仍立即上抛）；
- 读侧：`FILE_SHARE_READ|WRITE|DELETE` 共享读，并对「替换那一瞬间文件短暂不存在」
  做 6 × 0.01s 重试。

效果（大G第二轮反例：500 次状态写有 291 次 `WinError 5`）：**500 次写 0 失败、
7 万+ 次 `/healthz` 读取 0 次读不到状态文件、最终值正确落地**。
`last_success_publish_at` 因此在并发下也能正常推进，不会长期停在旧值。

写 `lock_path` / `status_path` 时会复用 TASK-002/003 的 Git 运行产物护栏：路径必须位于
被 `.gitignore` 忽略、且未被 Git 跟踪的位置，否则直接拒绝写入。

### 边界（本轮**不含**）

腾讯云/公网部署、TLS/域名/鉴权、systemd / Windows 计划任务的真实安装、Docker、
ffprobe/ffmpeg 真测活、多地区探针、自动 canonicalization、EPG/Logo、Dashboard、
视频代理/转码、对外公开分发。

`run` **不伪造 probe 结果**：固定频道只用数据库里已有的真实测活历史，runtime 从不写 `probe_result`。
也**不自动创造 canonical/binding**，只做 `stream-sync` 归集，不改变任何人工决策。

离线端到端演示（注入假 clock/sleep，不访问任何公网地址，也不真实等待 3 小时）：

```bash
python tools/demo_runtime.py
```


详细设计与命令说明见：

- [数据模型 V1](DATA_MODEL_V1.md)
- [运行时流程](V1_RUNTIME_FLOW.md)
- [TASK-001 执行报告](REPORTS/TASK-001-REPORT.md)
- [TASK-002 执行报告](REPORTS/TASK-002-REPORT.md)
- [TASK-003 执行报告](REPORTS/TASK-003-REPORT.md)
- [TASK-004 执行报告](REPORTS/TASK-004-REPORT.md)
- [TASK-005 执行报告](REPORTS/TASK-005-REPORT.md)

## 固定频道测活：`probe-check` / `probe-run`（TASK-005）

对**固定库存** stream 做真实、短时、受控、只读的 ffprobe 探测，结果写入**既有**
`probe` / `probe_result`，直接由 TASK-001 的 selector 使用。
**零 schema 改动**（`SCHEMA_VERSION` 仍为 1）、**未修改 selector 算法**。

```bash
# 1) 只检查 ffprobe 可执行文件与版本：不请求任何 stream、不碰数据库
python -m liptv probe-check

# 2) 对固定库存做一轮真实测活（需要先开 [probe] enabled = true）
python -m liptv probe-run
python -m liptv probe-run --stream-id 12      # 只测一条
python -m liptv probe-run --limit 50          # 本轮最多 50 条
python -m liptv probe-run --dry-run           # 跑但**不写库**
```

`[probe]` 配置段（默认**关闭**；不装 ffprobe 也完全不影响 TASK-004 行为）：

```toml
[probe]
enabled = false                 # 默认 false：0 次 ffprobe，行为与 TASK-004 逐字节一致
name = "windows-local"
location = "Windows"
ffprobe_path = "ffprobe"        # 也可写数组：["python", "tools/fake_ffprobe.py"]（离线替身）
timeout_seconds = 12.0
analyze_seconds = 4.0
max_concurrency = 4
per_round_limit = 0             # 0 = 不限条数（仍受 max_concurrency 约束）
```

要点：

- **只测固定库存**：`enabled = 1` 且至少有一个 `active = 1` 的来源 provenance；
  `stale` / orphan / `disabled` 一律不测，`dynamic_event_m3u` **绝不**进 `stream` / `probe_result`；
- **不猜字段**：`startup_ms` 是「启动 ffprobe → 拿到满足成功条件的媒体信息」的实测墙钟耗时；
  分辨率 / 码率只在真实存在时写；`ipv_family` / `http_status` / `connect_ms` **恒为 NULL**
  （V1 无法可靠获得，宁可留空也不扩 schema）；
- **环境级故障不污染历史**：ffprobe 缺失 / 起不来 ⇒ `stage = failed`、**0 条 `probe_result`**、
  绝不把整批流写成「失败」；整轮结论也不会报「完全 OK」（但仍可继续用旧历史发布）。
  先跑 `probe-check` 排查；
- 调用安全：argv 数组 + `shell = False`，单条 URL 是一个独立参数，总超时后
  terminate / kill 并回收子进程（不留孤儿），stdout / stderr 有上限，
  **不落盘任何媒体内容**；
- **URL 一律脱敏**：命令输出、`--json`、`runtime-status.json` 只显示 `scheme://host/...`，
  不含 path / query / token；
- `run` 的每轮顺序固定为
  `fixed fetch → stream-sync → probe（仅 enabled = true）→ publish → runtime status`；
  测活结果在 publish **之前**落库，因此必然影响**同轮**选线。

本工具**不**下载 / 安装 / 提交 ffmpeg，不代理视频，不绕过任何认证。

离线端到端演示（不访问任何公网地址，探测目标是本机 mock 假地址）：

```bash
python tools/demo_probe_pipeline.py
```

## 当前状态

- [TASK-001](TASKS/TASK-001.md)：V1 Skeleton / Data Foundation —— **ACCEPTED**（见 [第二轮独立验收](REVIEWS/TASK-001-REVIEW-02.md)）
- [TASK-002](TASKS/TASK-002.md)：远程 M3U 抓取及动态体育赛事源临时获取 —— **ACCEPTED**（见 [最终独立验收](REVIEWS/TASK-002-REVIEW-03.md)）
- [TASK-003](TASKS/TASK-003.md)：固定频道 + 动态赛事本地统一 M3U 组合与安全发布 —— **ACCEPTED**（见 [最终独立验收](REVIEWS/TASK-003-REVIEW-02.md)）
- [TASK-004](TASKS/TASK-004.md)：本地定时运行 + 只读 HTTP 固定订阅服务 —— **ACCEPTED**（见 [最终独立验收](REVIEWS/TASK-004-REVIEW-03.md)）
- [TASK-005](TASKS/TASK-005.md)：真实固定频道 ffprobe 测活 + scheduler 集成 —— **REVIEW**（见 [执行报告](REPORTS/TASK-005-REPORT.md)）

动态体育赛事源已登记：[JSNZKPG 体育赛事 M3U](SOURCES/JSNZKPG-SPORTS.md)。可用 `publish --dynamic-source jsnzkpg-sports` 显式并入统一 `out/live.m3u`（默认仍为禁用/不联网）。

TASK-001/002/003/004 已验收。TASK-005 实现了**本机单机**的真实固定频道测活并接入 scheduler，
当前处于 REVIEW。仍未实现：**多地区 / 多机器探针协调**、EPG / Logo、腾讯云/公网部署。
