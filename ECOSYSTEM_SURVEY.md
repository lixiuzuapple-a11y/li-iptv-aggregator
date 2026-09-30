# IPTV 外部生态侦察 v0.1

日期：2026-09-30

目标：不是评“谁最好”，而是判断哪些成熟轮子可直接采用、哪些只应借鉴、哪些能力必须由 Li IPTV Aggregator 自己掌握。

## 一、初步结论

外部生态已经成熟覆盖：
- 公共 IPTV 流数据
- 频道主数据
- EPG
- M3U 聚合/过滤/编辑
- Plex/Jellyfin/Emby 兼容
- FFmpeg 探测
- GUI/Dashboard
- Xtream API
- Docker 部署

因此 V1 不应重造完整 IPTV 平台。

建议自建核心：
1. Source Registry
2. Canonical Channel
3. Stream Inventory
4. Probe Registry
5. Probe History
6. Historical Quality Score
7. Selection Policy
8. Personalized M3U Output

## 二、重点候选

| 项目 | 主要角色 | 初步处理 | 说明 |
|---|---|---|---|
| Guovin/iptv-api | 聚合、校验、测速、输出 | 借鉴/执行器候选 | 中国语境成熟；功能覆盖广；AGPL-3.0；暂不建议直接 fork |
| iptv-org/iptv | 全球公共流数据 | 直接作为数据源候选 | 活跃、规模大、结构清楚 |
| iptv-org/database | 频道主数据 | 直接参考/同步候选 | canonical channel 的重要外部基准 |
| iptv-org/epg | EPG 抓取与公共 guide | 直接采用候选 | 不建议自造 EPG 抓取生态 |
| iptv-org/api | iptv-org 数据 API | 直接采用候选 | 简化数据获取 |
| iptv-org/awesome-iptv | 外部生态索引 | 侦察入口 | 长期发现新工具/源 |
| Dispatcharr/Dispatcharr | IPTV/EPG/VOD 管理、HDHomeRun、插件 | 借鉴设计，V1 暂不直接采用 | 功能很全但偏重；AGPL；包含我们不需要的代理/转码能力 |
| Threadfin/Threadfin | M3U/XMLTV proxy，Plex/Jellyfin/Emby | 兼容层参考 | 对媒体服务器兼容有价值；不是我们的核心 |
| xteve-project/xTeVe | M3U/XMLTV proxy | 历史参考/兼容层 | Threadfin 上游思想来源；成熟但较老 |
| carlreid/StreamMaster | M3U proxy/stream management | 借鉴 | 当前 fork 自已删除的原项目，需谨慎依赖 |
| m3ue/m3u-editor | M3U/EPG/Xtream 管理 | 借鉴 UI/管理功能 | 很完整，但超出 V1 需要 |
| kristofferR/IPTVChecker | FFmpeg 流检测 | 探针参考 | 可检测 alive/dead/geoblocked/DRM/audio-only/codec/resolution/FPS/bitrate/duplicate |
| cbulock/iptv-proxy | 多源合并、canonical channel、preferred stream、fuzzy mapping | 高价值架构参考 | 与我们的 Channel→Streams 模型接近，值得进一步读代码 |
| fanmingming/live | 国内 Logo/相关 IPTV 资源 | 数据源候选 | 对中文频道 logo 很有价值 |
| fyildirim-debug/M3uEditor | 自托管 M3U/Xtream/EPG 管理 | 功能参考 | MIT；包含健康扫描、去重、分类等思路 |
| bizzono/iptv-manager | SQLite M3U/EPG 管理 | 轻量架构参考 | 与我们的轻量路线接近，但项目较小 |
| ardoviniandrea/ViniPlay | 自托管播放器/UI | 暂不需要 | 属于消费/UI 层，不是 V1 核心 |
| jvdillon/netv | Web/Apple TV 播放与 transcoding/gateway | 后续观察 | Apple TV 和 gateway 思路有价值，但过重，尤其转码/GPU 与我们目标不同 |
| streamlink/streamlink | 从各类网站抽取视频流 | 特殊源扩展参考 | 非 M3U 聚合核心；插件机制值得以后考虑 |
| iptv-org/sdk | iptv-org API JS SDK | 可选依赖 | 若 V1 使用 JS/TS 有价值 |

## 三、二级候选 / 后续核验

来自 IPTV 社区索引，后续仅在需要对应能力时深入：

- WebGrab+Plus — EPG 抓取
- IPTV M3U Filter — playlist filtering
- iptv-checker-module — Node.js programmatic checker
- deepepg — 中国 EPG 候选
- epgshare01/share01 — 多国 EPG 数据
- IPTVnator — 播放器参考
- nodecast-tv — 自托管 Xtream/M3U Web 播放器
- IPTVBoss — IPTV 管理工具
- TVHeadend — 更重型的直播电视/DVR 后端；大概率超出本项目范围

## 四、Build / Borrow / Ignore 初版

### Build：我们自己做
- source registry
- channel canonicalization policy
- stream inventory
- probe identity / capability
- stream × probe 历史
- historical availability model
- source provenance
- selection/ranking policy
- personalized output policy
- retention/aggregation policy

这些决定了“哪些线路长期值得信任”，是本项目真正的资产。

### Borrow：优先复用
- 公共流：iptv-org/iptv、中文公开源
- 频道主数据：iptv-org/database
- EPG：iptv-org/epg 等
- Logo：fanmingming + 其他公共 logo 数据
- 低层流检测：ffprobe/ffmpeg 或成熟 checker 的方法
- M3U/XMLTV parsing：成熟库
- HTTP server：成熟框架
- scheduler：系统 cron / APScheduler 等成熟组件

### Ignore / Later
V1 不做：
- 视频中转
- 转码
- DVR
- VOD 管理
- 用户系统
- 多租户
- Plex/Emby/Jellyfin 虚拟 tuner
- 花哨 Dashboard
- 自研播放器
- 自研 EPG 网站 scraper 大生态

## 五、对 V1 架构的影响

### 推荐形态

```
Source adapters
   ↓
Raw observations
   ↓
Normalizer
   ↓
Canonical Channel + Stream Inventory
   ↓
Probe jobs (Shanghai / Windows / future SG-HK)
   ↓
Probe history
   ↓
Aggregation + quality score
   ↓
Selection policy
   ↓
M3U (+ later XMLTV)
```

### 核心原则

1. “源是否有效”不是全局布尔值，而是 stream × probe × time。
2. Channel 与 Stream 必须分离。
3. 原始来源保留 provenance，不覆盖。
4. 评分用历史，不只看最近一次。
5. 上海云服只控制/测活/生成列表，不做视频中转。
6. V1 输出以 Apple TV 可消费的干净 M3U 为主。
7. 所有外部组件都应可替换，核心数据库不依赖某一个聚合器内部格式。

## 六、当前架构判断

### 不建议 fork Guovin 作为主项目
理由：
- 它的核心目标已经是“采集→测速→输出”，与我们的历史资产模型不同。
- 我们若深改多探针、长期质量、source provenance，可能会长期承受上游合并成本。
- AGPL 对未来网络服务式部署需要谨慎处理。
- 更合理的关系可能是：借鉴其 alias/采集/测速逻辑，必要时作为独立执行器。

### Dispatcharr / m3u-editor 不作为 V1 基座
理由：
- 功能过多。
- UI、VOD、代理、媒体服务器兼容等不是当前核心。
- 部署和资源占用会增加腾讯云风险。

### 值得优先深读 cbulock/iptv-proxy
原因：
- 已明确采用 canonical channel workflow；
- 支持 preferred streams；
- 有 fuzzy mapping；
- 把多个 M3U/EPG 统一输出。
它与我们想要的“Channel → multiple Streams → preferred selection”结构最接近。

### ffprobe/ffmpeg 应成为探针底座
不建议让 GUI checker 成为生产依赖；更适合直接采用底层 ffprobe/ffmpeg，参考 IPTVChecker 的检测维度。

## 六.1 深读补充：cbulock/iptv-proxy 与 Guovin/iptv-api

### cbulock/iptv-proxy

代码检索确认它不仅在 README 里说“canonical channel”，内部确实有：
- source_channel_id
- canonical_channel_id
- channel binding
- preferred stream
- SQLite 持久化
- output profile
- fuzzy mapping 建议

这说明我们的核心模型方向不是臆想，已经有外部项目验证过类似结构。

但它近期 review 记录也暴露了一个非常有价值的反例：
- 如果把播放路由绑定在“频道名字”而不是稳定的 source-channel identity 上，频道重命名或 canonical mapping 后可能造成路由 404。
- 同一个 source 内重复 display name 也会导致歧义。

因此 Li IPTV Aggregator 从第一版 schema 就必须：
- 使用稳定内部 ID；
- display name 只是属性，不能作为实体主键；
- source channel identity 与 canonical channel identity 分离；
- stream URL、source relation、channel relation都应通过稳定 ID 关联。

这是一个应当冻结为架构原则的结论。

### Guovin/iptv-api

当前代码检索显示：
- 有成熟 alias 编辑机制；
- 支持正则 alias；
- 有任务历史和运行日志；
- 有 IPv6 speed test、频道结果质量检查等工程能力；
- 但未看到它以 SQLite/长期 probe history 作为核心资产层的明显证据。

因此 Guovin 仍更像“高成熟度采集/清洗/测速流水线”，不是我们想要的长期 stream × probe 历史数据库。

建议：
- 借它 alias 规则和采集/测速工程经验；
- 不把其内部模型当我们的 canonical state；
- 暂不 fork。

## 七、下一轮需要回答

1. 频道 canonicalization 的主键优先级：tvg-id / iptv-org id / normalized name / manual alias？
2. 同 URL 不同来源是否保留 source relation，而 stream 实体只去重一次？
3. 探针一次检测应读取多少数据才足够判断启动速度、分辨率、码率？
4. 是否把“地域封锁 / DNS / TCP / HTTP / media parse”错误拆分？
5. V1 测试频率如何做分层：新源高频、稳定源低频、失败源退避？
6. SQLite schema 如何设计以避免 probe_history 无限增长？
7. Windows probe 是中央 scheduler 远程调用，还是本地 agent pull job？
8. Apple TV 当前实际播放器的 M3U/EPG 兼容约束是什么？

