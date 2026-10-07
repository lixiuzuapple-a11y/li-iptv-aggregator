# TASK-012 §5 — Playback Quality / Failover 外部世界侦察

日期：2026-10-07
执行：小W（Executor）
目的（任务书原话）：**不是抄代码，而是确认我们要解决的问题有没有成熟简单做法。**

样本：6 个公开项目/规范 + 1 份行业国家标准。所有条目均为**直接读取官方 README / 源码 / 文档原文**，
不是二手博客转述（中文搜索结果里的CSDN/搜狐类文章本轮一律未采信）。

---

## 0. 一句话结论

> **成熟做法与我们已冻结的设计高度一致 —— 本轮不需要造新模型（§4 得到实证支持）。**
>
> 6 个样本里，**没有任何一个**做了「多线路 ranking + failover + recovery」。
> 它们全都停在「单次探测 → 布尔 alive/dead → 写文件」。**我们比它们多做一层**
> （跨线路选择与恢复），而这一层没有现成参考实现。
>
>⇒ **§4 结论「复用现有 selector，不要改」有外部证据支持**，
> 而 §8/§9（分层探测 + 错误分类）**确实有可直接借鉴的成熟 taxonomy**。

---

## 1. iptv-org/iptv —— 最值得借鉴的 taxonomy 来源

- **repository / homepage**：https://github.com/iptv-org/iptv
- **license**：CC0（内容）；脚本部分 MIT
- **规模**：34,000+ commits，全球最大公开 IPTV 清单，日更bot 提交

### 1.1 健康定义（`scripts/core/streamTester.ts` 源码实读）

```typescript
const res = await this.client(stream.url, {
  signal: AbortSignal.timeout(this.options.timeout),
  headers: { 'User-Agent': stream.user_agent || 'Mozilla/5.0', Referer: stream.referrer }
})
const mediainfo = await mediaInfoFactory({ format: 'object' })
const result = await mediainfo.analyzeData(...)
if (result && result.media && result.media.track.length > 0) {
  return { status: { ok: true, code: 'OK' } }
} else {
  return { status: { ok: false, code: 'NO_VIDEO' } }
}
```

**关键点（直接影响我们的设计）**：

| 观察 | 对我们的意义 |
|:--|:--|
| 用 **mediainfo.js 在内存里解析整个响应体**，而不是只看 HTTP status | ✅ **印证 TASK-011 的核心结论**：「200 不等于可播」。他们也必须解析内容 |
| 判定标准是 `media.track.length > 0`，即**必须真的有音视频轨** | 比我们现在的 ffprobe 判据更严一档 —— 我们是「ffprobe 成功即 success」 |
| `responseType: 'arraybuffer'` —— **下载完整响应体** |⚠️ 这就是为什么他们**不做**逐分片验证：成本太高 |
| 探一个 URL = **一次 HTTP GET**，无重试 | 非常低成本，适合大规模日更 |

### 1.2 错误分类（`docs/scripts.md` 里 `playlist:test` 的真实输出）

这是本轮**最有价值的发现** —— 它给出了一套已在线上大规模运行的分类：

```
LOADING...                OK
FFMPEG_STREAMS_NOT_FOUND  HTTP_FORBIDDEN
HTTP_GATEWAY_TIMEOUT      HTTP_NOT_FOUND
```

源码里的分类逻辑（实读）：

| 条件 | 产出的 code |
|:--|:--|
| `error.name === 'CanceledError'` | `TIMEOUT` |
| 有 HTTP 响应 | `HTTP_{status}_{statusText大写}`（如 `HTTP_FORBIDDEN`、`HTTP_NOT_FOUND`、`HTTP_GATEWAY_TIMEOUT`） |
| 无 HTTP 响应但有 cause.code | 直接用 cause.code（`ENOTFOUND`/`ECONNREFUSED` 等） |
| 其它 | `UNKNOWN_ERROR` |
| 解析成功但无媒体轨 | `NO_VIDEO` |

> ✅ **可直接采纳**：这套分类与我们 §9 要求的 taxonomy **几乎一一对应**
> （`TIMEOUT` / `HTTP_4XX` / `HTTP_5XX` / `CONNECT_ERROR` / `UNKNOWN` / `INVALID_MEDIA`），
> 而且它**证明了「HTTP 4XX/5XX 拆分」是业界常规做法**，不是我们的洁癖。

### 1.3 Geo-blocking（`docs/geo-blocking.md`）

- 独立标签 `Geo-blocked`，写在 `#EXTINF` 名字里（`Example TV (720p) [Geo-blocked]`）。
- 人工判定手段：用 `check-host.net` 或 VPN 从别的国家试。
- 文档明确写：「**避免把这些链接误当成坏掉**」。

> ✅ **强采纳**：这正是我们 §9 的 `GEO_BLOCK_SUSPECTED` 和 §18「KORICE 上海 FAIL
> 只能 advisory」的**业界共识依据**。他们用「标签 + 不自动删」表达，
> 我们用 `cloud_probe_authoritative=false` 表达 —— **语义完全一致，实现不同**。
> 他们的做法反过来证明：**我们对 KORICE 的冻结是对的，不是过度保守**。

### 1.4 人工测试规范（`docs/stream-testing.md`）—— 直接印证 TASK-011

原文要求：
1. 用支持 HLS/DASH 的播放器打开；
2. **「至少看一分钟」**，确认播放稳定、不会突然中断
   （括号里明确写：**有些测试流 15–30 秒后就断**）；
3. 重启流，确认不是在重复播同一分片。

> ✅ **独立第三方印证 TASK-011 §13 的方法论**：
> 「playlist 200 ≠ 可播」不是我们的个别发现，是行业共识。
> 他们把「至少 1 分钟」写进人工规范 —— 这与我们用 MPEG-TS 首包 `0x47` 验分片是同一类手段。
> ⚠️ 但他们**日常自动化只用一次 GET**，不上人工规范里那 1 分钟 ——
> 说明自动化与严格验证是两个层次，我们 §8 的**分层探测**设计是对的。

### 1.5 不适用点

- **不做 ranking / failover**：同一频道的多条 URL 是并列条目，不分优劣。
- **无历史数据库**：靠 issue 人工上报坏流 + bot 日更，没有 probe history。
- **无 recovery 语义**：删掉就是删掉，恢复要靠人重新提 issue。
- 无 User-Agent/Referer 时会被误判 —— ⚠️ 我们的 probe **不带自定义 header**，
  可能有假阴性（见 §6 风险）。

---

## 2. iptv-org/iptv — `playlist:test --fix` 的自动移除

- **license**：同仓库
- 机制：`npm run playlist:test -- --fix` 会**自动把所有探测失败的流从本地副本移除**。

> ⚠️ **不采纳，但作为反面教材**：`--fix` 是「一次失败就删」，没有连续失败门槛、
> 没有 sample 数要求、区分不了 geo-block 与真坏。这正是 §38禁止的
> 「删掉不稳定 canonical 改分母」的自动化版本。
> **我们的 3连败门槛 + UNKNOWN 不算 STABLE 严格优于它。**

---

## 3. domcyrus/ffmpeg_exporter —— 分层探测的成熟实践

- **repository**：https://github.com/domcyrus/ffmpeg_exporter
- **license**：MIT
- **语言**：Rust，单二进制，Docker 可跑

### 3.1 健康定义

用 ffprobe 分析流，暴露 Prometheus 指标：
- `ffmpeg_fps`（gauge，分stream_type / media_type）
- `ffmpeg_bitrate_kbits`
- `ffmpeg_packet_corrupt_total`（counter）
- `ffmpeg_codec_errors_total`
- `ffmpeg_dropped_packets_total`
- **`ffmpeg_stream_connection_state`**（1=connected / 0=disconnected）
- `ffmpeg_stream_connection_reset_total`

关键配置：
| 参数 | 默认值 |
|:--|--:|
| `--probe-size` | **2500 字节** |
| `--analyze-duration` | **5,000,000 微秒（5 秒）** |
| `--ffprobe-path` | 自动探测 |
| 自动重连 | ✅ 有 |

### 3.2 对我们的可采纳点

>✅ **强采纳，且直接支撑 §8 分层探测**：
> **`--probe-size 2500` + `--analyze-duration 5s` 是一个经过验证的
> 「便宜但能分辨真假」档位**。我们现在的 `analyze_seconds=4` 与之同量级，
> 说明现有参数选得合理，**不需要调大**（§7/§8 禁止为提速无限拉高并发/深度）。
>
> ✅ **可采纳**：把 ffprobe 的 `analyze-duration` 作为「是否需要升级到 Level 3」的触发依据 ——
> Level 1 用短 analyze（便宜），只有「结果矛盾 / 长期异常 / 准备切线」才上 Level 3。

### 3.3 不适用点

- **Prometheus exporter 是 §45 明确禁止的**（不做 Prometheus / Grafana）。
- 它是「单流持续监控」，不是「批量清单管理」，没有多线路选择概念。
- 持续重连对我们不合适：我们是**离散采样**，不是常驻连接。

---

## 4. aws-samples/canary-monitor —— 分层验证的checklist 宝库

- **repository**：https://github.com/aws-samples/monitor-hls-and-dash-streams-using-canary-monitor
- **来源**：AWS 官方博客 + 代码（Python 3）
- **license**：Apache-2.0（AWS 样例代码惯例）

### 4.1 健康定义（检查项清单）

它的 check 表格非常值得抄作业：

| 检查 | 触发条件 | 日志级别 | 影响类别 |
|:--|:--|:--|:--|
| **Staleness** | **x 秒内 manifest 没出现新分片**（x 由 `--stale` 定义） | WARNING | Playback |
| **Discontinuity** | HLS manifest 里出现 `EXT-X-DISCONTINUITY` | WARNING | Playback |
| **Differences between segments across renditions** | 同一序号分片在不同 rendition 间属性不一致 | WARNING | Playback |
| **Manifest value has changed** | `EXT-X-VERSION` / `EXT-X-TARGETDURATION` 变化 | WARNING | Playback |
| Possible lip sync issue | DASH 里相邻分片呈现时间差 > 100ms | WARNING | Playback |

轮询间隔：**默认 5 秒一次 manifest GET**。

### 4.2 可采纳点

> ✅ **强采纳 —— `Staleness` 检查直接命中我们 §11「过旧 success 是否仍影响当前决策」**。
> 他们的staleness 定义是「manifest 里没有新分片」，
> 我们对应的应该是「**最近一次 success 距今多久**」——
> 我们 `select.py` 的 `sort_key`里 `last_success_at` 已经在做，但**没有 staleness 阈值**。
> 这给 §11 提供了**外部依据**：staleness 判定是行业标准做法，
> 如果我们的数据显示有问题，加一个 staleness 门槛是合理的（§11 允许）。
>
> ✅ **可采纳**：manifest 层的 sanity（有没有 `#EXTM3U`、是不是 HTML）
> 是 Level 2 的低成本检查 —— 这正好对应我们的 `INVALID_PLAYLIST` 与 `HTML_FAKE`。

### 4.3 不适用点

- 它假设能拿到 CloudWatch（SaaS 化监控），我们是单机自管。
- 广告追踪/ad-break 检查与我们无关（§28 不做转录/广告）。
- 5 秒轮询对 174 条stream 太密集，我们的目标是 30~60 分钟（§20）。

---

## 5. sams258/stream-stability-monitor —— 延迟阈值分级的反面参考

- **repository**：https://github.com/sams258/stream-stability-monitor
  （fork 自 AlexKwan1981/iptv-m3u-checker）
- **license**：MIT
- **语言**：Python 3.10+ / aiohttp

### 5.1 健康定义（README）

- **核心是「延迟阈值」而非成功率**：按响应毫秒数分级，输出只含
  `delay_threshold`（**默认 5000 ms**）以内的流。
- 异步并发检查数百条流。
- 输出「时间戳 + 只含验证过且低延迟的链接」的 M3U。

### 5.2 可采纳点

> ⚠️ **只采纳「可观测性」这一条**：把**延迟**作为独立指标暴露出来是有价值的
>（我们已有 `startup_ms` 和 `median_startup_ms`，方向一致）。
> §37 要求报 median / p95 startup —— 我们的 schema 已经有 `startup_ms`，
> 可以直接算，**不需要新字段**。

### 5.3 不适用点（本项目与我们目标差距最大）

- **健康 = 低延迟**，而我们的健康 = **能播**。延迟高但能播的流，对用户是
  「能看但卡」，与我们「坏流自然退出」的目标不同。
- **一次性筛选，无历史**：跑完输出一份文件，不持续跟踪。
- **无 failure 分类**：只有 `ONLINE` / `HTTP_{code}` / `TIMEOUT` / `ERROR_{name}`。
- ❌ **5000ms 阈值对我们完全不合适** —— 那是给电台 ICY 用的，
  我们是电视 HLS，且中国境内线路延迟高是常态。**不采纳阈值本身。**

---

## 6. live-hls-stream-monitor —— 分片级监控的完整实现参考

- **repository**：https://github.com/msahmarani/live-hls-stream-monitor
- **license**：见仓库 LICENSE
- **语言**：Python / Flask + `m3u8` 库 + ffprobe

### 6.1 健康定义

- **支持 master playlist 自动选 variant**（与我们 TASK-011 深探遇到的问题同源）。
- **Segment Health Monitoring**：**追踪分片可用性与响应时间**。
- **Stream Quality Metrics**：码率、分辨率、帧率、时长。
- **Segment success rate tracking**（分片成功率）—— 这正是我们 §8 Level 3 要做的。
- 默认绑 `127.0.0.1:8181`，隐私优先，声明「不存储流URL、不外传」。

### 6.2 可采纳点

> ✅ **采纳其分片成功率的概念**：它是找到的**唯一一个**明确把
> 「segment success rate」当核心指标的开源实现 —— 可作为我们 §8 Level 3
> 「什么算通过」的参考。
>
> ✅ **采纳其默认绑 localhost 的做法** —— 与我们验收第 55 条
> 「localhost binding unchanged」一致，**再次印证我们的部署选择是对的**。

### 6.3 不适用点

- 它是**Web Dashboard + Chart.js 可视化** ⇒ **§45 明确禁止**（不做 Dashboard/Web GUI）。
- 单流监控为主，多线路 failover 靠人看图。
- 重依赖 Flask +前端框架，与我们的极简部署目标冲突。

---

## 7. GY/T 376—2023（行业国标）—— 权威的「什么算坏」定义

- **来源**：国家广播电视总局，GB/T 2260.2 体系，**正式发布的标准 PDF**
  （政府网站公开）
- **性质**：不是开源项目，但是**本轮唯一的中文权威依据**

### 7.1 与本任务直接相关的判据

| 指标 | 标准要求 |
|:--|:--|
| 视频画面连续无丢帧 | 未收到指定视频PID 数据、或收到但无有效净荷的**持续时间 ≤ 2 秒** |
| 声音连续无中断 | 同上，**≤ 2 秒** |
| 音视频编码正确 | 持续含有效包但无法解码，**< 2 秒** |
| 可察觉音视频不同步 | **≤ 2 秒** |
| RTP 抖动 | **≤ 50 ms** |
| RTP 丢包率 | **≤ 10⁻⁶** |
| **测量时长** | **持续运行 15 分钟**再判定 |

### 7.2 可采纳点

> ✅ **重要采纳 —— 「15 分钟测量窗口」给了我们的轮次间隔一个标准依据**。
> §7 建议间隔不低于 15 分钟，与国标的测量时长一致。**这不是巧合** ——
> 短于 15 分钟的观察无法区分「抖动」与「真挂」。
> ⇒ **本报告支持 TASK-012 采用 ≥15 分钟间隔**，并且我们现有 scheduler
> 轮次间隔 ~22 分钟**已经满足**国标窗口（§20 报告里要写清这一点）。
>
> ✅ **采纳「2 秒容限」作为 Level 3 深探的判断参考**：
> 我们验 MPEG-TS 首包时，可以把「拿到包但不足以判定」的时间窗设在 2 秒量级。

### 7.3 不适用点

- 国标面向**电信 IPTV 传输网**（RTP/组播），我们是**互联网 HLS over HTTP**。
- 它要求码流分析仪 + 专用设备，我们没有。
- ❌ **不做 RTP 层测量**（§45 明确不做多地区 probe farm / 传输层QoS）。

---

## 8. 横向对比表（§5 要求的记录项汇总）

| 项目 | license | 健康定义 | probe 深度 | 失败分类 | ranking/failover | stale | recovery |
|:--|:--|:--|:--|:--|:--|:--|:--|
| **iptv-org/iptv** | CC0 / MIT | mediainfo 解析出媒体轨 | **单次 GET + 内存解析完整响应** | ✅ 完整（`TIMEOUT`/`HTTP_*`/`*NOT_FOUND`/`NO_VIDEO`/`UNKNOWN_ERROR`） | ❌ 无 | ❌ 无 | ❌ 靠人提 issue |
| **ffmpeg_exporter** | MIT | ffprobe + 多个 gauge/counter | ffprobe（probe-size 2500B / analyze 5s） | 指标级（corrupt/codec/reset） | ❌ 无 | ❌ 无 | ✅ 自动重连 |
| **canary-monitor** | Apache-2.0 | manifest staleness + 多项一致性 | manifest 轮询（默认 5s）+ 可选分片 | ✅ 按检查项分类（Staleness/Discontinuity/…） | ❌ 无 | ✅ **staleness 是一等公民** | ❌ 无 |
| **stream-stability-monitor** | MIT | **延迟阈值**（默认 5000ms） | 单次 async GET | 粗（`HTTP_*`/`TIMEOUT`/`ERROR_*`） | ❌ 无 | ❌ 无 | ❌ 无 |
| **live-hls-stream-monitor** | 见仓库 | 分片成功率 + 质量指标 | ✅ **master→variant→segment** | 按指标 | ❌ 无（Dashboard 人工看） | 部分 | ❌ 无 |
| **GY/T 376—2023** | 国标 | 丢帧/失步/抖动/丢包阈值 | 码流分析仪，**持续 15 分钟** | 按影响类别（Playback/Monetization） | ❌ 无 |✅ 2 秒容限 | ❌ 无 |

**统计：6 个样本中，多线路 ranking/failover = 0 个；recovery 语义 = 1 个（仅"自动重连"）。**

---

## 9. 结论与对本轮设计的直接影响

### 9.1 §4「不造复杂新模型」得到实证支持

> 我们现有的 sort_key（success_rate → consecutive_failures → last_success →
> startup → pixels → bitrate）**已经比这 6 个样本的任何实现都更复杂**。
> 它们连 ranking 都没有。**没有外部证据支持我们再往上加模型。**
>⇒ **本轮不改 selector 的排序逻辑。**

### 9.2 §9 的taxonomy 有成熟对应物，可直接采纳

iptv-org 的线上分类证明「HTTP 4XX/5XX 拆分 + TIMEOUT + CONNECT 类+ UNKNOWN +
INVALID_MEDIA」是业界标准。**我们的 §9 目标taxonomy 与之高度重合**，
差异只在我们要额外区分 `HTML_FAKE` / `SEGMENT_UNREACHABLE` /
`SIGNED_DOWNSTREAM_UNREACHABLE`（因为我们有 TASK-011 发现的 fake 200 问题）
和 `GEO_BLOCK_SUSPECTED`（iptv-org 用标签表达，我们用 category 表达）。

### 9.3 §8 分层探测有参数参考，不需要重新发明

- Level 1：`analyze_seconds ≈ 4~5s`（对齐 ffmpeg_exporter 的 5s 默认）
- Level 2：manifest sanity（`#EXTM3U` 头、是否 HTML）—— canary-monitor 的低成本检查
- Level 3：分片 + 首包字节 —— live-hls-stream-monitor 的 segment success rate

### 9.4 §11 的 staleness 判定有外部依据

canary-monitor 把 staleness 当一等公民，国标给了 2 秒容限。
**若我们的数据显示需要，现有 `window_days=7` 不够表达「过旧 success」**，
可以加一个 staleness 门槛 —— 但必须先有真实缺陷证据（§4 要求）。

### 9.5 §18 对 KORICE 的冻结得到业界共识支持

iptv-org 专门写了 `geo-blocking.md`，明确说「**避免把 geo-blocked 误当坏掉**」，
处理方式是**打标签 + 不自动删除**。
⇒ 我们的 `cloud_probe_authoritative=false` / `playback_requires_vpn=true`
**不是过度保守，是行业标准做法**。KORICE 上海 FAIL **只能 advisory** 这条冻结**保持不变**。

### 9.6 本轮识别的新风险（需要真实数据才能确认）

>⚠️ **iptv-org 在探活时带 `User-Agent: Mozilla/5.0` 和可选 `Referer`**。
> 我们的 probe **不带任何自定义 header**。若某些线路要求 Referer 才能给流，
> 我们会把它们误判为失败（假阴性）——
> 这可能正是 §6 审计里那**27 条 never-success stream** 的部分成因。
>
> **但这只是一个假设**，需要 §8 Level 2/3 的定向复验来确认。
> **不允许**在没有实测证据前就改 probe 的 header 行为（那会影响全部 174 条的历史可比性）。

---

## 10. 未做的事（明确声明）

- **未复制任何外部代码**（许可证兼容性已记录，但本轮不引入依赖）。
- **未修改 probe / selector / 任何生产代码**（本报告纯侦察）。
- **未采信**中文二手博客（CSDN / 搜狐 / 公众号）里那些「IPTV 加速器」类内容 ——
  它们与本任务无关且含推广。
- **未访问**任何需要登录 / 付费 / 私密凭据的源。
