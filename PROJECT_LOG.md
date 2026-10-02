# Li IPTV Aggregator — Project Log

> 本文件是项目讨论阶段的活记录。当前尚未初始化 Git，也未冻结代码目录结构。
> 原则：先把目标、约束、外部侦察结果、架构判断和决策记录清楚，再进入正式开发。

## 0. 协作模式

- 老李 = Owner：定义“要什么”、最终决策、必要账号/凭据/支付/授权/物理操作。
- 大G = Architect / PM / Independent Reviewer：外部侦察、架构、TASK、验收标准、独立 QA、ACCEPT / REJECT。
- 小W = Executor：coding、测试、抓取、数据整理、部署、运维、报告。
- GitHub / repo = canonical state；聊天主要用于沟通。
- WebCodex = 大G操作本机项目、Git、GitHub、云服和真实运行状态的工具。

## 1. 项目目标

建设一个长期自动运行的 IPTV 系统，核心不是“收藏一份 M3U”，而是：

- 多来源 IPTV 源聚合
- 频道/线路去重与归一化
- 多探针测活
- 分类
- 历史质量评价
- 自动更新
- EPG / Logo 等元数据整合
- 生成供播放器订阅的小型 M3U

关注内容优先级：

1. 体育
2. 新闻
3. 影视
4. 纪录片
5. 港澳台 / 国际 / 音乐 / 动漫 / 儿童 / 地方台等后续扩展

## 2. 已知边界

允许纳入：

- 官方公开源
- GitHub / 社区公开分享
- M3U / TXT
- 老李本人有权使用的 IPTV
- 普通 URL 可直接播放的公开线路

不做：

- 破解 DRM
- 偷账号 / Token
- 绕认证
- 破解付费平台
- 破解加密视频

必须记录来源。

### 2.1 个人使用阶段来源处理补充（2026-09-30）

Owner 指定：对 JSNZKPG 等第三方公开发布的动态 M3U，优先简单直接使用，不为个人自用 MVP 建逐条版权审核流程；技术红线为不破解、不逆向、不绕过 DRM 或认证，不获取他人私密凭据，不延长或伪造签名令牌。本阶段播放器直接订阅第三方源，不做视频代理、公开转发或对外再分发。公开可访问本身不证明拥有转播或再分发授权；如后续拟公开发布聚合服务，或收到明确限制/权利投诉，另行核查。

## 3. 关键设计约束

### 3.1 Channel 与 Stream 分离

目标数据模型应类似：

Channel
- Stream A
- Stream B
- Stream C

线路级逐步记录：

- 来源
- 首次发现
- 最后成功
- 可用率
- 连续失败次数
- 启动延迟
- 分辨率
- 码率
- IPv4 / IPv6
- 地域
- 探针

初始排序偏好：

稳定性 > 启动速度 > 清晰度 > 码率

### 3.2 多探针

不能使用“上海访问失败 = 源无效”的逻辑。

应采用 stream × probe：

- 上海腾讯云
- Windows 本机
- 后续可增加香港 / 新加坡 VPS

一条线路可能：
- 上海 FAIL
- Windows PASS
- Singapore PASS

仍然属于有效线路。

### 3.3 腾讯云不做视频中转

现有云服资源：
- Ubuntu 24.04
- 2 CPU
- 2 GB RAM
- 50 GB SSD
- 4 Mbps 公网出口

定位：
- 拉取源列表
- 解析
- 去重
- 测活
- 数据库
- 分类
- EPG
- Logo
- 排序
- 生成 M3U
- 提供小型订阅文件

播放器应尽量：
Apple TV → 实际视频源

而不是：
Apple TV → 上海云服 → 视频源

同时不得影响云服上现有正式足球研究任务。

## 4. 外部侦察候选

已知第一批候选：

- Guovin/iptv-api
- iptv-org/iptv
- iptv-org/epg
- fanmingming/live
- Dispatcharr
- Threadfin
- StreamMaster

后续第一轮外部侦察应扩展到约 20–30 个项目，覆盖：

- IPTV 源仓库
- 聚合器
- 测活 / 测速
- EPG
- Logo
- M3U 管理
- failover
- dashboard

并分类：
- 直接采用
- Fork / 改造
- 只作为数据源
- 借鉴设计
- 暂不需要
- 淘汰

## 5. 当前阶段

当前不是 coding 阶段。

先完成：
1. 外部生态侦察
2. 明确哪些能力直接采用成熟项目
3. 确定 V1 的产品边界与架构
4. 决定是否新建正式 GitHub repo
5. 冻结第一批 TASK

## 6. 当前待讨论的核心问题

- V1 是“自建薄层聚合器 + 复用成熟组件”，还是直接基于一个成熟项目二次开发？
- 数据库需要保存到什么粒度，SQLite 是否足够？
- 测活策略如何避免对源产生不必要压力？
- 频道归一化/去重采用哪些键与模糊规则？
- EPG / Logo 是一开始就纳入，还是 V1.1 再做？
- Apple TV 最终消费方式：直接 M3U、兼容某类播放器、还是提供额外 API？
- 云端与 Windows 探针如何协同上报结果？
- 是否值得增加海外轻量探针作为 V1 的一部分？

## 6.1 第一轮外部侦察初步判断（2026-09-30）

已验证：

- Guovin/iptv-api 已具备多源聚合、可用性校验、测速筛选、M3U/TXT/API 输出、频道别名归一化、IPv4/IPv6、Docker 等成熟能力。
- iptv-org 已形成拆分清晰的公共生态：iptv（流）、database（频道数据）、epg（节目单）、api（查询接口）。
- 独立 IPTV checker 工具已经可以基于 ffmpeg 检测存活、DRM、音频流、分辨率、FPS、码率和重复项。

因此初步架构方向调整为：

**不从零重写通用 IPTV 聚合器。**

V1 更值得自建的是一层很薄的“资产与决策控制层”，重点负责：

1. source registry：维护来源与抓取记录；
2. canonical channel：频道统一身份；
3. stream inventory：同频道多线路；
4. probe history：stream × probe 历史测活记录；
5. quality scoring：根据历史稳定性、延迟、清晰度等排序；
6. policy/output：按老李个人偏好输出最终 M3U。

成熟项目优先作为：
- 数据源；
- 采集/测速执行器；
- EPG/Logo 提供方；
- 设计参考。

是否直接 fork Guovin/iptv-api 尚未决定。需要继续比较其数据持久化模型、多探针能力、历史质量模型以及 AGPL-3.0 对未来部署方式的影响。

## 6.2 Owner 明确的最终使用方式（2026-09-30）

老李明确最终产品形态：

- 最终只需要一个稳定的 M3U 订阅链接；
- 后台系统按固定频率自动刷新/重建该 M3U；
- 播放器侧像普通 IPTV 订阅一样使用；
- 每次打开播放器时，由播放器手动或自动刷新该链接；
- 用户不需要登录后台、不需要复杂 Dashboard、不需要视频经过本系统中转。

这意味着 V1 的交付目标可以进一步收缩为：

```
后台定时采集/测活/排序
        ↓
生成最新 M3U 文件
        ↓
通过固定 URL 发布
        ↓
播放器刷新 URL 获取最新频道与线路
```

由此派生的架构原则：

1. **固定订阅 URL，内容可变。** URL 尽量长期不变，例如 /live.m3u。
2. **生成式而非实时代理式。** 播放器读取的是系统生成的 M3U 文件，不经过实时业务逻辑。
3. **视频直连原始 stream。** M3U 内 URL 指向实际视频源，云服不转发视频流量。
4. **发布应原子化。** 新 M3U 生成成功后再替换旧文件，避免播放器读到半成品。
5. **失败时保留 last-known-good。** 某轮采集/生成失败，不应覆盖上一版可用 M3U。
6. **V1 不要求 Dashboard。** 观测和维护可先通过日志/数据库/CLI 完成。
7. **定时刷新频率应与测活频率解耦。** 可以更频繁测活，但只按较低频率发布新 M3U，避免无意义抖动。

## 6.3 V1 产品原则：个人自用 + 最小可用（2026-09-30）

Owner 明确：
- 本项目为个人自用；
- 遵循最小可用原则；
- 不做平台化；
- 不为未确认的未来需求预先增加复杂度。

由此冻结 V1 设计原则：

1. 能用静态 M3U 文件解决，就不做实时播放 API。
2. 能用 SQLite 解决，就不上 PostgreSQL。
3. 能用系统定时任务解决，就不先引入复杂任务队列。
4. 能用 CLI / 日志维护，就不先做 Dashboard。
5. 能直接引用原始视频流，就不做视频代理/转码。
6. 能复用外部成熟数据/工具，就不重写。
7. 只有已经出现的真实问题，才进入下一轮复杂化。

V1 成功标准不是“功能完整”，而是：

> 老李在播放器里配置一个固定 M3U URL，系统能自动维护这份列表，使其长期保持可用、干净、相对稳定。

## 6.4 V1 默认 M3U 形态（2026-09-30）

在 Owner 暂不指定细节的前提下，由大G先冻结一个最小可用默认方案，后续按实际使用再调。

### 输出目标

固定订阅地址：
```
/live.m3u
```

播放器只需要订阅一次，后台自动更新内容。

### 默认频道组织

V1 先按以下 group-title 分类：

1. 体育
2. 新闻
3. 影视
4. 纪录片
5. 港澳台
6. 国际
7. 音乐
8. 动漫
9. 儿童
10. 地方台
11. 其他

分类顺序可后续按实际使用调整。

### 同一频道的输出策略

默认：
- 每个 canonical channel 在主 M3U 中只输出 **1 条当前最优线路**；
- 备用线路留在数据库，不全部暴露给播放器；
- 若最佳线路失效，则后台下一轮发布时自动切换到次优线路。

原因：
- 保持列表干净；
- 避免同频道出现多条重复项；
- 播放器侧无需理解 failover；
- 后台可控地完成线路切换。

### 排序策略

频道级排序优先：
1. 用户关注类别；
2. 频道重要性/常用度；
3. 稳定性；
4. 名称排序。

线路级排序初版：
1. 最近 7 天成功率；
2. 最近连续失败次数；
3. 最近成功时间；
4. 启动延迟；
5. 分辨率；
6. 码率。

第一版不做复杂机器学习或动态权重优化。

### 稳定性原则

- 不因单次失败立即下架；
- 连续失败达到阈值后降级；
- 新发现线路先进入观察期；
- 长期稳定线路优先；
- 某轮生成失败时继续提供上一版 last-known-good M3U。

### M3U 字段

V1 至少输出：
- tvg-id
- tvg-name
- tvg-logo
- group-title
- display name
- stream URL

可选后续：
- catchup
- user-agent
- referrer/header
- EPG custom attrs

### EPG

V1 数据结构预留 tvg-id 与 EPG 映射；
EPG 功能可稍后接入，不阻塞首版 M3U。

### V1 不做

- 同频道在主列表输出多个备用线路；
- 播放器端 failover；
- 实时代理；
- 动态转码；
- 花哨排序 UI；
- 多用户个性化配置。

## 7. 决策记录

- 2026-09-30：创建本地项目目录 li-iptv-aggregator。
- 2026-09-30：暂不初始化 Git，暂不建立完整代码结构。
- 2026-09-30：先以 PROJECT_LOG.md 作为讨论阶段的单一活记录，待架构收敛后再冻结 repo 结构与 TASK。
- 2026-09-30：本地 Git 与 GitHub 远程仓库已闭环；WebCodex 可直接执行 commit/push/pull，GitHub CLI 已持久登录并通过端到端核验。后续 GitHub 为 canonical state，不再依赖人工中转 Git 操作。
- 2026-09-30：TASK-001 第二轮独立验收 ACCEPT（70/70）；已登记 JSNZKPG 动态体育赛事源。TASK-002 定义为远程 M3U 获取、固定来源生命周期与动态赛事临时拉取；不提前部署或实现统一发布，完成后必须停 Gate。
- 2026-09-30：TASK-002 执行完毕并推 REVIEW。落地两条互不干扰的来源链路：`fixed_m3u`（可进入 canonical/stream 库存，失败不污染、消失只置 inactive、恢复复用原身份）与 `dynamic_event_m3u`（只做临时获取与脱敏预览，绝不落库、绝不进 /live.m3u）。未新增数据库业务表，未改动 TASK-001 冻结的数据身份设计。
- 2026-09-30：TASK-002 经第三轮独立验收 ACCEPT（151 项完整分组回归通过）；关闭截断源误下线、动态文件忽略目录与 Git 已跟踪文件覆盖三项问题。定义 TASK-003 为**显式单次**本地统一列表合并及安全发布（动态赛事每轮重新抓取，失败禁止沿用旧签名）；不把静态文件伪称自动保鲜/公网在线。
- 2026-10-01：TASK-003 经第二轮独立验收 ACCEPT（189 项分组回归通过），真实 JSNZKPG 联赛分组与多动态源 fail-closed 反例均通过。定义 TASK-004 为本地 scheduler + 单实例锁 + 只读 HTTP `/live.m3u` 与 `/healthz`；仍不做腾讯云部署、真实 ffprobe、多探针、EPG/Logo。
- 2026-10-01：TASK-004 经第三轮独立验收 ACCEPT；关闭 scheduler 心跳、保守锁释放和 Windows 状态文件并发三类问题。定义 TASK-005 为单机真实 ffprobe 流测活并接入现有 probe_result / selector / scheduler；保持 schema V1 与 selector 算法不变，不测动态赛事临时源。
- 2026-10-02：TASK-005 经第二轮独立验收 ACCEPT；真实 ffprobe 固定流测活、环境级错误 fail-closed 与 heartbeat CAS 归属保护通过。定义 TASK-006 为单机 Linux 生产部署与运维固化：systemd、非 root 运行、持久化目录、doctor、SQLite 备份、升级/回滚和受控固定订阅入口；不做多主机协调、Kubernetes 或视频代理。

## 8. TASK-002 执行进度（2026-09-30）

### 8.1 本轮做到什么

- **远程拉取层**（`liptv/fetch.py`）：标准库 urllib；超时 / 响应体上限 / 重定向次数 / User-Agent 全部配置化；失败分类为 HTTP_STATUS、NETWORK_ERROR、TIMEOUT、TOO_MANY_REDIRECTS、RESPONSE_TOO_LARGE、DECODE_ERROR；**只请求被登记的 M3U 地址，绝不去请求条目里的播放地址**。
- **来源编排层**（`liptv/ingest.py`）：M3U 文本校验（INVALID_M3U / EMPTY_LIST）、fixed 单来源事务快照、动态源临时预览与 URL 脱敏。
- **fixed_m3u 生命周期**：本次出现的创建/更新 → 本次消失的置 `active=0`（**不硬删**，`first_seen_at`、绑定、stream 历史保留）→ 重新出现自动恢复 `active=1` 并复用原身份；重复抓取幂等；单一来源事务边界内「库存变更 + fetch 状态」同生共死；失败时库存零改动。
- **dynamic_event_m3u**：每次显式获取都重新请求原始 URL，只返回临时结果与摘要（赛事显示名、group-title、线路类型、当轮条目数）；摘要里的播放地址一律去 query 并截断 path；`--out` 仅允许写入 `fetch.dynamic_tmp_dir`（默认 `out/tmp`，已被 `.gitignore` 忽略）。
- **CLI**：`source-register`（--from-config 批量注册）、`source-status`、`fetch`（--source / --all）、`dynamic-fetch`（--source / --url / --out）；`source-add` 增加 `--enable/--disable`。
- **本地演示与测试**：`tools/mock_source_server.py`（成功/变更/空/损坏/非 M3U/超大/慢速/HTTP 错误/重定向/带签名动态源）、`tools/demo_local_pipeline.py`（15/15 场景 PASS）、`tools/smoke_jsnzkpg.py`（可选手工 smoke，公网不可达标 NETWORK_UNAVAILABLE 且不判失败）。
- **测试**：原 TASK-001 70 项全部通过，新增 58 项，合计 **128 passed**，退出码 0。

### 8.2 本轮刻意不做

统一 `/live.m3u` 合并发布、自动调度、多节点真实 ffprobe、腾讯云部署、EPG / Logo 抓取、自动赛事识别、历史动态赛事库、视频转码或代理、GUI。

### 8.3 安全与合规边界

- 不破解、不逆向、不绕过 DRM / 登录 / 认证；不提取或延长令牌有效期；不代理或再分发视频。
- 不把短时签名 URL 写入 Git、报告或长期存储；动态快照只能落在被忽略的运行目录。
- 遵守 [SOURCES/JSNZKPG-SPORTS.md](SOURCES/JSNZKPG-SPORTS.md) 的个人自用来源处理原则：公开可访问不等于拥有转播或再分发许可。

## 9. TASK-002 Review 01 定向返工（2026-09-30）

Review 01（`REVIEWS/TASK-002-REVIEW-01.md`）驳回两个阻断点，本轮定向修复，未重构无关模块：

- **QA-002A（数据完整性）**：M3U 校验原先只看「有条目就接受」，忽略了 `#EXTINF` 未配 URL 的截断形态。
  一次「前段完整 + 末尾截断」的上游响应会被当作完整快照，把本次未出现的旧条目静默置 `active=0`。
  现改为：**有条目但仍有未配 URL 的 `#EXTINF` → 判 `INVALID_M3U` 并整体拒绝**，库存逐字段不动；
  同时明确「结构完整的真正缩减」仍照常置 inactive，零条目文本仍归 `EMPTY_LIST`。
- **QA-002B（快照落盘安全）**：动态快照原先只校验「位于配置的 `allowed_dir` 之内」，
  而该目录可被配置覆盖成仓库内**未被忽略**的路径（如 `SOURCES/`）。
  现在写盘前增加第二道强制校验：目标在某个 Git 工作树内时必须被 `.gitignore` 忽略，否则拒绝且不创建文件；
  不在任何工作树内则允许（明确策略）。`is_ignored_by_gitignore()` 同步升级为可用的忽略规则判定
  （支持通配/锚定/目录模式/否定/注释，遵循「父目录被忽略 ⇒ 内容也被忽略」）。

证据：`tools/qa002_repro.py` 反例重跑 6/6 PASS；新增 `tests/test_review_qa002.py` 13 项永久回归；
全量测试 **141 passed**（原 128 项零回归），单命令 exit 0。状态改回 `REVIEW`，停 Gate 等第二轮验收。

## 10. TASK-002 Review 02 定向返工（2026-09-30）

Review 02（`REVIEWS/TASK-002-REVIEW-02.md`）确认 QA-002A / QA-002B 通过，仅剩一个安全边界 QA-002C，
本轮定向关闭，未改动网络抓取、数据模型或原任务范围：

- **QA-002C（快照落盘安全）**：`.gitignore` **只对尚未被跟踪的文件生效**。一个快照文件如果历史上被
  `git add -f` 强加进索引（如 `out/tmp/signed.m3u`），之后往同名文件写入就会直接变成
  **待提交的已跟踪变更**，原有的「忽略规则」校验拦不住。
  现在在工作树内的目标上再增加一道**索引校验**：`git ls-files --error-unmatch -- <相对路径>`
  （输出全部丢弃，不依赖任何输出内容；argv 传参，带空格路径安全；`-C <root>` 天然支持附属工作树）；
  取不到 git 时回退到直接解析 `.git/index`（v2 / v3 / v4，含 `gitdir:` 指针形态），
  遇到读不懂的版本或 split index **保守拒绝**而非放行。可用 `LIPTV_GIT_EXECUTABLE` 指定 git 路径。
  最终形态：`--out` 为**三重强制** —— ① 在 `dynamic_tmp_dir` 内；② 在 Git 层面被忽略；
  ③ **未被该工作树跟踪**。任一不满足即拒绝，且不创建文件、旧文件一个字节不动。

证据：`tools/qa002_repro.py` 反例重跑 **10/10 PASS**（新增 QA-002C 全流程：`BEFORE_TRACKED False` →
`git add -f` → `AFTER_GIT_ADD_F True` → 写入被拒 → 旧字节不变 → 对照组仍可写）；
新增 `tests/test_review_qa002c.py` **10 项真实临时 Git 仓库回归**（含带空格路径与 nested worktree）；
全量测试 **151 passed**（既有 141 项零回归），单命令 exit 0。状态改回 `REVIEW`，停 Gate 等第三轮验收。
