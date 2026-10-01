# TASK-004 — Local Scheduler & Read-only HTTP Subscription Service

状态：REJECTED（Review 01：QA-004A/B 定向返工；见 REVIEWS/TASK-004-REVIEW-01.md）
Owner：老李
Architect / Reviewer：大G
Executor：小W
基线：TASK-001 / 002 / 003 均已独立 ACCEPT；开工前拉取最新 `main`。
参考：`V1_RUNTIME_FLOW.md`、`TASKS/TASK-003.md`、`REVIEWS/TASK-003-REVIEW-02.md`。
本轮完成后只允许进入 REVIEW；禁止自行启动 TASK-005。

## 目标

把 TASK-003 的“手工执行一次 publish”推进成一个**可长期运行的本地自动化进程**，并提供播放器可订阅的固定 HTTP 地址：

```
http://<host>:<port>/live.m3u
```

本轮仍然是个人自用、本地/局域网运行；不部署腾讯云、不做公网暴露、不做视频代理、不新增 Dashboard。

## 1. 自动运行链路

实现一个清晰入口，例如：

```
python -m liptv run
```

每轮按顺序执行：

1. 抓取所有 enabled `fixed_m3u`；
2. 对现有 binding / stream inventory 执行必要的 `stream-sync`；
3. 不自动创造 canonical/binding，不对未知频道做模糊强绑；
4. 按配置决定是否显式拉取已登记的动态赛事源；
5. 调用 TASK-003 已验收的统一 `publish`；
6. 记录脱敏的本轮状态、耗时、退出结果和下一次计划时间。

要求：

- 复用既有函数，不通过 shell 拼接调用自身 CLI。
- 单个 fixed 来源失败不能阻塞其他 fixed 来源抓取；发布仍遵循既有库存与 TASK-003 的安全语义。
- 动态赛事失败沿用 TASK-003 的 fail-closed：默认只发布固定频道，绝不从旧文件回拼动态签名 URL。
- scheduler 不能修改 canonical/binding 的人工决策。
- 未实现真实 probe 的情况下，**不得伪造测活结果**；固定频道仍只能使用数据库已有的合格 probe 历史。

## 2. 调度

支持两种运行模式：

### once
```
python -m liptv run --once
```

只执行一轮后退出，用于测试、系统计划任务和手工维护。

### loop
```
python -m liptv run
```

长期循环。

配置至少支持：

- `interval_seconds`：完整运行周期；示例默认 3 小时；
- 首轮启动是否立即执行：默认是；
- 每轮异常捕获：单轮异常不得杀死 loop；
- 可控停止：Ctrl+C / SIGTERM 后不再启动新一轮，正常释放锁和 HTTP 服务；
- 测试可注入 clock/sleep，自动化测试不得真的等待数小时。

**不要**在本轮自建复杂队列、cron 管理器、APScheduler/Celery 等重型依赖；标准库优先。

## 3. 单实例锁

同一套 data/output 目录禁止两个 scheduler 同时运行。

要求：

- 跨 Windows/Linux 的最小可行单实例锁；优先使用原子创建 lock file + PID/启动时间元数据。
- 第二实例检测到有效锁时明确退出，不执行 fetch/publish。
- stale lock 要有可解释策略：不能只因为文件存在就永远锁死；也不能在无法判断时随意抢锁。
- 正常退出、Ctrl+C、单轮异常后都不留下错误锁。
- 单独的 `publish` CLI 保持原行为；锁主要保护 `run` 的完整运行链。

## 4. 固定 HTTP 订阅服务

实现只读 HTTP 服务，例如：

```
python -m liptv serve
```

以及 `run --serve` 组合模式。

最低路由：

- `GET /live.m3u`：返回当前已发布文件；
- `HEAD /live.m3u`；
- `GET /healthz`：仅返回服务/文件存在与发布摘要的新鲜度状态，不泄漏 stream URL；
- 其他路径 404。

约束：

- 只服务 M3U 文件和最小健康状态；**绝不代理视频流**。
- 绑定地址默认 `127.0.0.1`，用户显式配置后才可绑定 `0.0.0.0` / 局域网。
- 默认不启用目录浏览，不暴露数据库、日志、previous 文件、summary 原文或任意磁盘文件。
- 防路径穿越；路由必须固定映射，不能把 URL 路径直接拼成本地路径。
- `live.m3u` 不存在时返回 503 或 404（选一种并冻结），不得生成空列表冒充成功。
- Content-Type 合理（如 `application/vnd.apple.mpegurl` 或兼容值）；支持 `Content-Length`、`Last-Modified` / ETag 至少一项。
- HTTP 服务读取发布文件时不得阻塞 scheduler 的原子 replace；播放器只能看到旧完整文件或新完整文件，不能看到半文件。

## 5. 新鲜度 / 健康状态

TASK-003 已明确静态文件会过期，因此本轮必须把“多久没成功更新”暴露出来。

配置：

- `stale_after_seconds`，默认建议为调度周期的 2–3 倍；
- 动态赛事启用时可允许更短阈值，但不要假装能准确知道上游签名 TTL。

`/healthz` 至少输出：

- `status`: ok / stale / missing；
- 当前文件是否存在；
- 最后一次成功 publish 时间；
- 距上次成功秒数；
- 最近 scheduler run 状态；
- 当前服务版本/启动时间（可选）。

**不得**包含完整 stream URL、签名、数据库内容或来源完整列表。

健康状态 stale 只用于提示，不自动删除 current `live.m3u`；播放器仍可读取 last-known-good。不要把 stale 当成“文件必然不能播”。

## 6. 运行日志与状态文件

允许新增被 `.gitignore` 覆盖的运行时 JSON 状态，例如 `out/runtime-status.json`。

要求：

- 原子写入；
- 不含完整动态 URL / token；
- 记录 round_id、开始/结束时间、fetch 摘要、publish 状态、耗时、异常分类、下次运行时间；
- 历史不做无限增长；本轮只需“当前状态 + 最多少量最近轮次”或单文件覆盖。
- 写入路径必须复用 TASK-002/003 的 Git 运行产物护栏思想。

## 7. CLI / 配置

建议新增：

```
python -m liptv run --once
python -m liptv run
python -m liptv run --serve
python -m liptv serve
```

配置示例至少包含：

```toml
[runtime]
interval_seconds = 10800
lock_path = "out/liptv.lock"
status_path = "out/runtime-status.json"
stale_after_seconds = 21600

[server]
enabled = false
host = "127.0.0.1"
port = 8080
playlist_path = "/live.m3u"
health_path = "/healthz"
```

动态源是否自动加入每轮发布必须**显式配置**，默认仍不主动访问公网动态源；可配置 `jsnzkpg-sports` 作为示例但默认关闭。

## 8. 最低验收场景

1. 原有 **189 项**测试全部零回归。
2. `run --once` 离线 E2E：mock fixed fetch → stream-sync → 使用既有 mock probe 历史 → publish；动态显式启用时合并成功。
3. loop 用 fake clock/sleep 连续运行至少 3 轮，不真实等待；某轮 fetch 或 publish 抛异常后下一轮仍继续。
4. 单实例：第一实例持锁时第二实例明确拒绝；正常退出释放；stale lock 场景有永久测试。
5. HTTP：GET/HEAD `/live.m3u`、`/healthz`、404；缺文件状态；Content-Length/类型；并发 publish replace 时只读到完整旧版或新版。
6. 安全：`/../`、任意文件路径、数据库/previous/summary 请求均不可读取；服务不接触 M3U 内 stream URL。
7. 新鲜度：ok → stale → 新成功 publish 后恢复 ok；失败轮次不能伪造“最后成功 publish 时间”。
8. 动态源 fail-closed：上一轮含动态，本轮动态失败时新文件只含 fixed；scheduler 状态与 publish summary 一致。
9. Ctrl+C / shutdown 测试：停止后不启动下一轮，锁释放，HTTP thread/server 正常关闭。
10. `git diff --check`、完整执行报告、实际退出码、离线 demo、Git SHA、本地/远程一致。

## 9. 演示

新增一个纯离线脚本，例如：

```
python tools/demo_runtime.py
```

至少展示：

- 启动 mock source；
- 三轮 fake scheduler；
- 第一轮统一发布；
- 第二轮模拟动态失败但 loop 不死；
- 第三轮恢复；
- 本地 HTTP GET `/live.m3u` 与 `/healthz`；
- 第二实例锁拒绝；
- 关闭后锁释放。

演示不得访问真实 JSNZKPG 或真实视频 URL。

## 不包含

- 腾讯云/公网部署；
- TLS / 域名 / 鉴权；
- systemd / Windows Task Scheduler 的真实安装；
- Docker；
- ffprobe/ffmpeg 真测活；
- 多地区探针；
- 自动 canonicalization；
- EPG / Logo；
- Dashboard；
- 视频代理/转码；
- 对外公开分发。

这些后续另立 TASK。

## Stop / Gate

若要实现 scheduler 必须绕过 TASK-003 发布保护、伪造 probe 结果、把动态签名长期缓存、或 HTTP 服务必须代理视频，立即停止并记录。

完成后填写 `REPORTS/TASK-004-REPORT.md`，状态改为 REVIEW，commit + push `main` 后停止。只有大G独立 QA 可 ACCEPT / REJECT；禁止提前启动 TASK-005。
