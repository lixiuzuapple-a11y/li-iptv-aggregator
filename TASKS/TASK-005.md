# TASK-005 — Real Stream Probing with ffprobe & Runtime Integration

状态：REVIEW（Review 01 REJECT → QA-005A/B 定向返工完成；见 REVIEWS/TASK-005-REVIEW-01.md 与 REPORTS/TASK-005-REPORT.md §14）
Owner：老李
Architect / Reviewer：大G
Executor：小W
基线：TASK-001 / 002 / 003 / 004 均已独立 ACCEPT；开工前拉取最新 main。
参考：DATA_MODEL_V1.md、V1_RUNTIME_FLOW.md、REVIEWS/TASK-004-REVIEW-03.md。
本轮完成后只允许进入 REVIEW；禁止自行启动 TASK-006。

## 目标

把现有“人工 / mock 写入 probe_result”的占位阶段升级为真实固定频道流测活：

1. 本机调用 ffprobe 对固定频道 stream URL 做短时、受控、只读探测；
2. 将真实结果写入已有 probe / probe_result；
3. 让 TASK-001 已验收的 selector 用这些历史结果选线；
4. 接入 TASK-004 scheduler，使每轮顺序固定为：

```
fetch fixed sources
→ stream-sync
→ probe eligible fixed streams
→ publish
→ runtime status
```

本轮只做单机真实探针。不做多地区节点、云端分布式探针、视频代理、持续拉流、转码或录制。

## 1. 只测固定库存

- 仅探测 stream.enabled = 1 的长期固定库存。
- stream 必须存在至少一个仍 active 的 source_channel provenance；stale / orphan stream 默认不测。
- dynamic_event_m3u 临时赛事 URL 绝不进入 stream / probe_result。
- 不自动创建 canonical / binding。
- 若 repo 已有权威库存查询，必须复用，不造第二套身份逻辑。

## 2. 不伪造测量值

保持 schema V1 不变。

- success：真实 ffprobe 是否识别到媒体流。
- checked_at：真实本轮时间。
- startup_ms：从启动 ffprobe 到得到满足成功条件的媒体信息的墙钟耗时。
- resolution_width / height：仅真实存在时写。
- bitrate_kbps：仅 ffprobe/format/stream 元数据可靠提供时写。
- protocol：根据实际 URL scheme / ffprobe 可验证信息写。
- ipv_family：只有实际连接路径能可靠判定时写，否则 NULL。
- http_status：ffprobe 无法可靠提供时 NULL。
- connect_ms：无法可靠拆出 DNS/TCP/TLS 阶段时 NULL。

禁止用总耗时冒充 connect_ms，禁止把 https URL 推断成 HTTP 200，禁止为了字段完整填猜测值。

## 3. 配置与 CLI

新增安全默认配置：

```toml
[probe]
enabled = false
name = "windows-local"
location = "Windows"
ffprobe_path = "ffprobe"
timeout_seconds = 12.0
analyze_seconds = 4.0
max_concurrency = 4
per_round_limit = 0
```

建议 CLI：

```
python -m liptv probe-check
python -m liptv probe-run
python -m liptv probe-run --stream-id 123
python -m liptv probe-run --dry-run
```

要求：

- enabled=false 为默认值，升级后 scheduler 不得突然请求全部播放流。
- probe-check 只检查 ffprobe 可执行文件与版本，不请求 stream。
- 找不到 ffprobe / 启动失败属于环境错误，不能给所有 stream 批量写“播放失败”。
- 不负责下载安装 ffmpeg/ffprobe，不提交二进制。

## 4. ffprobe 调用安全

- 必须 argv 数组 + subprocess，禁止 shell=True。
- 单条 URL 作为一个 argv 参数传入，不能 shell 拼接。
- 单条流有总 timeout，不能无限挂住。
- stdout/stderr 要限制大小或裁剪。
- 不把媒体 payload 写磁盘。
- 不持久保存完整 URL、query token、stderr 全文。
- timeout 后必须 terminate/kill + wait/reap，不能留孤儿 ffprobe。
- HLS 为识别媒体允许请求少量 playlist/segment，但不得持续播放、录制或持久化 segment。

## 5. 成功条件

一次 probe 仅在以下条件同时满足时 success=true：

- ffprobe exit code = 0；
- JSON 可解析；
- 至少存在一个 audio 或 video stream；
- 结构不是空媒体/明显无效数据。

video 有宽高则记录；纯音频没有宽高不能自动判失败。
不能仅凭 HTTP 能连接就判“可播”。

## 6. 稳定错误分类

error_type 至少支持：

- FFPROBE_NOT_FOUND
- FFPROBE_START_FAILED
- TIMEOUT
- DNS_ERROR
- CONNECT_ERROR
- TLS_ERROR
- HTTP_ERROR
- INVALID_MEDIA
- PROCESS_ERROR
- OUTPUT_INVALID
- UNKNOWN

分类必须可解释。stderr 只用于内存分类与脱敏诊断，不写数据库全文。无法精确区分时用更宽类别，不得假装精确。

## 7. 并发与资源限制

- 标准库优先，例如 ThreadPoolExecutor。
- max_concurrency 默认 4。
- per_round_limit=0 表示不额外限条数，但仍受并发限制；支持 --limit N。
- 一个 stream 异常不能中断其它 stream。
- Ctrl+C / scheduler stop 后不再启动新任务，并尽力终止当前子进程。
- worker 不得共享同一个 sqlite connection 并发写库；建议 worker 只返回结构化 ProbeObservation，由主线程统一 repo.add_probe_result。

## 8. 数据库写入语义

- 每个 stream × 当前 probe 节点每轮最多写 1 条 probe_result。
- 历史结果只追加，不覆盖。
- probe.last_seen_at 只在真实落库成功后推进。
- dry-run 不写数据库。
- 环境级错误导致整轮根本没开始 probing 时：0 条 probe_result，明确失败，不污染 selector 历史。

## 9. Scheduler 集成

在 TASK-004 execute_round 中冻结顺序：

```
1. fixed fetch
2. stream-sync
3. probe fixed streams（仅 probe.enabled=true）
4. publish
5. runtime status
```

probe.enabled=false：

- 完全保持 TASK-004 当前行为；
- 0 次 ffprobe；
- 已有历史 probe_result 继续供 publish/selector 使用。

probe.enabled=true：

- sync 后 probe，结果 commit 后再 publish；
- 本轮 probe 结果必须能影响同轮 selector；
- 单个 stream failure 是正常健康历史，不等于整轮环境失败；
- ffprobe missing 等环境级故障不写伪结果，可继续使用旧历史 publish，但 runtime 必须明确 probe_stage=failed/degraded，不能把整轮报告成完全 OK。

不需要新增数据库业务表；可在 runtime round summary 增加 probe 子摘要。

## 10. Selector 一致性

本轮不得重新设计 TASK-001 选线算法，也不得修改评分权重来让 demo 好看。

离线 E2E 至少构造同一 canonical 两条 stream：

- A：probe 成功且历史可用；
- B：连续失败或达到 max_consecutive_failures；
- 写库后现有 select_best_stream 选择 A；
- 下一轮结果反转后，selector 按现有 7 天窗口 / 连续失败规则产生预期变化。

如果真实数据暴露 selector 设计问题，记录为后续 TASK，不在本轮偷偷改算法。

## 11. 输出与隐私

probe-run 输出 requested / succeeded / failed / skipped，以及 stream_id、canonical 名、error_type、startup_ms、分辨率等。

- URL 默认只显示脱敏后的 scheme://host/...，不显示 query/fragment。
- --json 同样脱敏。
- report / runtime-status 不得出现完整 token、Authorization、Cookie。
- 本轮不引入任何账号凭据。

## 12. 自动测试必须离线

自动测试不得依赖公网，也不得要求 CI 已安装真实 ffprobe。

使用临时 fake ffprobe/helper 模拟：

- 成功 JSON：H.264 + AAC、1920×1080、bitrate；
- audio-only 成功；
- exit != 0；
- JSON 损坏；
- sleep 超时；
- stderr 含 DNS/TLS/HTTP/invalid data 特征；
- stdout 超大；
- 收到终止并退出。

真实 ffprobe smoke 只能是可选：检测本机存在才执行，优先本地 fixture；公网 URL 不得作为自动验收条件。

## 13. 最低验收

1. TASK-004 当前 269 项测试零回归。
2. probe-check：fake ffprobe 可用 PASS；missing executable 为环境错误且 0 probe_result。
3. 单 stream 成功：真实结构字段写库，selector 可立即使用。
4. timeout / invalid media / process error：各写一条 failure，error_type 稳定；stderr 全文不落库。
5. ffprobe missing / start failed：不能给所有 stream 批量写 failure。
6. 并发 20+ stream：受 max_concurrency 限制；每 stream 每轮最多 1 条；SQLite 无线程错误/重复写。
7. Ctrl+C / stop：不再启动新 probe，子进程清理，无 orphan。
8. URL 含 query token / shell 元字符：shell=False；作为单独 argv；日志/JSON/report 不泄漏 token。
9. stale/orphan/disabled stream 不测；active fixed inventory 正常测；dynamic_event_m3u 不进入 probe。
10. scheduler：probe disabled 与 TASK-004 兼容；probe enabled 时顺序可机验证；本轮成功结果影响同轮 publish；环境级 probe 故障不污染历史且 runtime 明确告警。
11. selector 算法不修改，现有 score/select 测试原样通过。
12. git diff --check、执行报告、真实命令/退出码、离线 E2E、Git SHA、本地/远端一致。

## 14. 离线演示

新增：

```
python tools/demo_probe_pipeline.py
```

至少演示：

1. fake ffprobe capability check；
2. 两条固定 stream；
3. 一条成功、一条失败；
4. probe_result 写库；
5. selector 选中健康线路；
6. 下一轮结果反转，连续失败规则开始生效；
7. scheduler 的 fetch → sync → probe → publish；
8. probe disabled 时 0 子进程；
9. ffprobe missing 时 0 污染；
10. token 脱敏。

## 不包含

- 多地区 / 多机器 probe agent；
- 云端探针协调；
- 腾讯云部署；
- ffmpeg 转码 / 代理 / DVR / 录制；
- 长时间 QoE、packet loss / jitter 深度分析；
- DRM / 登录 / Cookie / Authorization 绕过；
- 自动 canonicalization；
- EPG / Logo；
- Dashboard；
- 修改 selector 算法；
- schema V2。

如果 V1 字段无法表达某个真实测量值，宁可留 NULL，不要为本轮扩 schema。

## Stop / Gate

若发现必须代理视频、持久化媒体内容、绕过认证、修改 selector 核心算法或升级 schema 才能完成，立即停下记录证据，不扩大 TASK。

完成后填写 REPORTS/TASK-005-REPORT.md，状态改为 REVIEW，commit + push main 后停止。只有大G独立 QA 可以 ACCEPT / REJECT；禁止提前启动 TASK-006。
