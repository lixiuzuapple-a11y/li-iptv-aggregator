# TASK-012 — Playback Quality, Auto-Curation & Daily Reliability

状态：REVIEW  
Owner：老李  
Architect / Reviewer：大G  
Executor：小W

基线：TASK-001 ～ TASK-011 全部 ACCEPTED。
生产主机：ev-lab-shanghai。
当前 fixed inventory：43 canonical。
TASK-011 Reviewer 最近独立实测：43 fixed with tvg-id；XMLTV 42 channels；M3U↔XMLTV 精确命中 41；France 24 已纳入；JSNZKPG + KORICE 保留；/live.m3u、/healthz、/epg.xml 均 200；8080 仍只监听 127.0.0.1。

本轮继续采用长任务模式。除非触碰授权边界、需要 Owner 私密凭据、存在不可逆生产风险，或真实世界证据证明目标不可达，小W应连续执行到 REVIEW。

> 执行链：侦察 → probe 数据审计 → 设计 → 编码 → 测试 → 生产部署 → 多轮真实 probe → 自动切线/恢复验证 → 动态赛事预检 → 连续运行 soak → 报告 → REVIEW。

普通 bug、单流失败、单源失效、测试失败、短时网络错误、文档修正，不得中途停下等待 Owner。

完成后只能进入 REVIEW。禁止自行启动 TASK-013。

---

## 1. 本轮核心目标

TASK-009～011 已经把系统从工程样机推进到 40+ fixed、双动态赛事源、metadata、logo、XMLTV EPG、生产服务闭环。

TASK-012 不再以“新增功能数量”为核心，只解决一个问题：

> 这个系统能不能长期自己跑，并且自动把坏线路赶出去、恢复线路接回来，让用户看到的列表尽量保持可播。

目标链：真实 probe history → 近期稳定性 → 可解释 selector → 坏流自然退出 → 恢复流自然回来 → 动态赛事轻量预检 → publish → 每日状态摘要 → 长期无人值守。

---

## 2. Fixed 稳定性硬目标

- 至少执行 6 个真实 production probe round。
- round 之间必须有真实时间间隔，不允许几秒内刷 6 次冒充长期样本。
- 优先跨至少两个自然 scheduler 周期；如执行时间受限，可用真实自然轮次 + 明确标注的压缩验证补足。
- 所有当前 fixed stream 进入统计。
- 目标：published fixed canonical 中至少 90% 在多轮窗口内保持健康。
- 如果真实低于 90%，不得降低门槛，直接报告真实比例和问题频道。

“健康”至少考虑：最近成功、最近失败、连续失败、success rate、样本数、是否存在更稳定备用 stream。

UNKNOWN 不得算 STABLE。

---

## 3. 自动切线与恢复

至少选择 5 个 multi-stream canonical 做可审计验证。

必须证明：
1. 当前优选 A。
2. A 连续失败达到现有退出门槛。
3. selector 自动选择 B。
4. canonical 名、tvg-id、logo、group、EPG mapping 不变。
5. A 恢复后重新进入候选集。
6. 不要求 A 立即抢回；是否切回应由可解释规则决定。
7. 全部 stream 失败时 canonical 自然 skip。
8. 任一 stream 后续恢复时 canonical 能自动重新发布。

目标是证明：不用人工编辑 M3U，也能完成 failover 和 recovery。

产出：REPORTS/TASK-012-FAILOVER.md。

---

## 4. 冻结原则：不造复杂新模型

禁止重新造复杂评分系统。

优先复用现有 selector：success rate、consecutive failures、last success、startup time、resolution、source diversity。

只有真实数据证明存在问题时，才允许最小增强，例如：
- 最小样本门槛；
- recent window；
- stale success 处理；
- 稳定性 tie-break；
- recovery hysteresis。

任何增强必须：简单、可解释、有真实缺陷证据、有 regression test、不依赖 ML/LLM、不引入黑盒权重。

如果现 selector 已足够完成本轮目标：不要改 selector。

---

## 5. 外部世界侦察

至少检查 6 个 IPTV health-check / stream checker / failover 相关公开项目或成熟实践，重点看：
- HLS segment validation；
- ffprobe/ffmpeg health probe；
- uptime tracking；
- recent-success ranking；
- failover/hysteresis；
- stale handling；
- recovery handling。

产出：SOURCES/PLAYBACK-QUALITY-RECON-TASK012.md。

至少记录：project/tool、repository/homepage、health definition、probe depth、failure classification、ranking/failover strategy、stale handling、recovery handling、可采纳点、不适用点、license、是否采纳。

目的不是抄代码，而是确认我们要解决的问题有没有成熟简单做法。

---

## 6. Probe 数据质量审计

在改 selector 前，先审 production probe history。

产出：REPORTS/TASK-012-PROBE-AUDIT.md。

至少统计：
- total stream；
- 0 probe；
- 1 probe；
- >=2；
- >=4；
- >=6；
- success rate distribution；
- consecutive failure distribution；
- last success age；
- error category distribution；
- single-stream canonical；
- multi-stream canonical；
- cross-source multi-stream canonical；
- all-stream-failing canonical；
- never-success stream；
- recovered stream；
- host concentration。

先回答：我们当前的数据够不够支撑“稳定性”判断？

不够就先补数据，不允许先写模型再假设数据够。

---

## 7. Probe round 设计

- 至少 6 round。
- 建议间隔不低于 15 分钟；更优是跨多个小时。
- 可利用现有 scheduler。
- 报告必须区分 natural round 与 compressed validation round。
- 继续使用 timeout / concurrency / analyze_seconds / per-round controls。
- 不允许为提速无限拉高并发。

上海 probe 仍只代表 shanghai-cloud，不得写成 globally playable。

家庭/VPN playback context 继续单独记录。

---

## 8. 分层探测

TASK-011 已证明 playlist 200 ≠ playable，但不能因此把所有频道每轮都做深度 segment 下载。

建议分层：
- Level 1：ffprobe / media open。
- Level 2：只有新流、结果矛盾、准备切线、长期异常 host、dynamic 当前赛事时才深入。
- Level 3：HLS segment + 首包字节。

禁止长期高频全量 Level 3。

---

## 9. 错误分类统一

至少统一以下术语：
- DNS_ERROR
- CONNECT_ERROR
- TIMEOUT
- HTTP_4XX
- HTTP_5XX
- HTML_FAKE
- INVALID_PLAYLIST
- INVALID_MEDIA
- SEGMENT_UNREACHABLE
- SIGNED_DOWNSTREAM_UNREACHABLE
- EMPTY_MEDIA
- GEO_BLOCK_SUSPECTED
- ENVIRONMENT_ERROR
- UNKNOWN

probe/report/status 尽量使用同一口径。

环境级 ffprobe failure 不得写成每条 stream 的假失败；HTTP 200 不得自动等于 success。

如 schema V1 不适合完整 category，优先用现有字段/派生报告，不为报表升级 schema V2。

---

## 10. Stable / Degraded / Failed / Unknown

允许新增运行时派生状态，不要求写 schema。

建议：
- STABLE：样本足够、success rate 高、最近有成功、无高连续失败。
- DEGRADED：成功/失败混合、近期退化、但还有可用线。
- FAILED：达到现有 selector 退出门槛或明确媒体层失败。
- UNKNOWN：样本不足。

这些只是派生状态，不能替代原始 probe history。

---

## 11. Recent window 与恢复

必须检查：
- 过旧 success 是否仍影响当前决策；
- 最近连续失败的线路不能被半年前的成功“救活”；
- 最近恢复成功的 stream 应能逐步重新进入竞争。

优先复用现有 window_days 或非常少的新参数，不要同时堆多套窗口。

---

## 12. Hysteresis / 防抖

只有真实生产数据证明 A/B 频繁抖动时才允许加防抖。

可接受的最小规则：
- 当前 stream healthy 时，challenger 需明显更优才切；
- 当前 stream degraded/failed 时允许立即切；
- 恢复流连续成功若干次后重新成为 preferred 候选。

如果没有抖动证据，不加 hysteresis。

---

## 13. Multi-stream重点频道

优先从以下实际 multi-stream 项中选至少 5 个追踪：CCTV-1、CCTV-5、CCTV-5+、CCTV-13、北京卫视、东方卫视、湖南卫视、France 24。

报告至少写：canonical、candidate stream count、source diversity、probe count、selected stream、why selected、second choice、failover evidence。

---

## 14. 单线路 canonical

列出所有 single-stream canonical、当前 health、是否能从现有 source inventory 补明确备用。

优先使用已有资产，不为“2 条线路 KPI”去收垃圾新源。

---

## 15. 120.76.248.139 延续裁决

TASK-011 REVIEW-02 已冻结：暂不做 host 级永久封禁。

本轮至少跨 3 个新 round 继续观察：fake 200 / segment fail / signed downstream / 是否存在成功例外。

只有跨轮稳定同型失败且无有效例外，才允许考虑精确排除。

优先 stream-level 或 source/path-level rule，最后才是 host-level rule。

任何排除必须有 regression test。

---

## 16. CCTV-5+ 专项

- 至少 6 round。
- 记录全部候选 stream。
- 如存在长期 usable route，识别最佳和备用。
- 当前最佳失败时 selector 能切备用。
- 若全部长期失效，允许 CCTV-5+ 自然退出，不绕过门槛。

---

## 17. Dynamic Event Preflight

允许新增一个克制的 current-round dynamic 预检，目的是少把明显挂掉的赛事 URL 推给播放器。

冻结：
- 不写 probe_result 历史；
- 不进入 fixed selector；
- 不保存旧 signed URL；
- 不做长时间深 probe；
- 不转码；
- 不代理。

建议轻量检查：URL scheme → DNS/connect → HTTP status → content-type/playlist header → 如为 HLS 最多检查一个 media playlist 和一个 segment。

结果至少：PRECHECK_PASS / PRECHECK_FAIL / PRECHECK_UNKNOWN。

总预算必须可控。

---

## 18. JSNZKPG 与 KORICE policy

JSNZKPG 是否允许上海 preflight 作为 authoritative，必须先用真实抽样证明上海结果对用户播放有代表性。

KORICE 继续冻结：
- aggregator list fetch 可在上海完成；
- playback 可能依赖家庭 VPN；
- playback_requires_vpn=true；
- cloud_probe_authoritative=false；
- 上海 FAIL 只能 advisory，不能据此全局删除。

如果实现统一 preflight，必须能表达 authoritative / advisory 或等价 policy。

非权威结果只能记录和统计，不能删除条目。

---

## 19. Daily Reliability Summary

必须新增一份真正有用的 reliability summary，不做 Dashboard。

建议 production 路径：/var/lib/li-iptv-aggregator/reliability-summary.json。

可新增 reliability-status CLI，或合理增强现有状态命令。

至少包含：

Fixed：canonical_total、published、stable、degraded、failed、unknown、stream_total、stream_healthy、stream_failed、failover_count、recovered_count。

Dynamic：JSNZKPG fetched/published/precheck_fail；KORICE fetched/published/advisory_fail。

EPG：last_success、age、channels、programmes、coverage。

Runtime：last publish、service uptime、scheduler last round、last error、stale status。

禁止放完整 stream URL、signed query、Cookie、Authorization、VPN 信息。

还要提供一个短的人类可读输出，不要打印成几百行日志。

---

## 20. Scheduler cadence

明确生产 cadence，产出 OPERATIONS/RUNTIME-CADENCE.md。

参考范围：
- dynamic：约 15 min；
- fixed source refresh：1～3 h；
- fixed probe：30～60 min 或与固定刷新解耦；
- publish：跟随 dynamic/fixed 状态变化；
- EPG：6～12 h。

必须避免：EPG 每 15 分钟抓、全量 ffprobe 每 15 分钟、每轮无意义写盘、dynamic URL 过期太久。

---

## 21. Publish no-change / churn

检查 M3U 完全无变化时是否存在无意义重写。

允许优化：
- fixed no-change 时不重写；
- 或保持原子流程但明确记录 no-change。

目标是减少 mtime/previous churn，但不得破坏 LKG 和 dynamic freshness。

必须区分 fixed no-change 与 dynamic current-round changed。

---

## 22. Continuous Reliability Soak

至少做一次连续运行 soak。

最低：6 小时真实生产运行，或至少覆盖 3 个真实 scheduler 周期 + 明确标注的压缩离线 soak。

推荐目标 12～24 小时，但禁止承诺“后台跑完以后再给结果”。必须在当前任务可取得的证据范围内完成并诚实标注。

观察：service restart、scheduler lock、stale lock、temp、previous、publish summary、runtime status、epg status、SQLite size、journal error、memory、CPU、disk、open fd、ffprobe child/zombie、repeated exception、dynamic temp cleanup。

不能只看“服务还活着”。

产出：REPORTS/TASK-012-SOAK.md。

---

## 23. 资源与数据增长

采样 baseline / after：CPU、RSS、DB size、journal、temp、open process。

检查 probe_result 是否会无限增长，估算当前速率下 30 天 / 180 天 / 1 年规模。

如果很小：明确结论，不做 retention。

如果明显膨胀：只设计 retention 方案，默认不执行不可逆大清理；如需删 production history，必须先 backup/dry-run/报告预计删除行数，等待 Reviewer。

禁止用“定期重启”掩盖资源泄漏。

---

## 24. 日志与临时文件生命周期

systemd journal 已有轮转，不另造 ELK/Loki。

检查：out/*.json 是否覆盖式、dynamic temp 是否清理、diagnostic temp 是否残留、debug 输出是否进入 production。

状态文件和临时目录必须有界。

---

## 25. Self-healing 边界

允许自动恢复：source 临时失败、stream recovery、selector fallback、EPG LKG、dynamic isolate、stale lock takeover。

禁止自动做：重启整台服务器、改 firewall、改 DNS、切 VPN、改安全组、删 DB、restore DB、重装系统。

---

## 26. Healthz 保持最小

不要把所有 reliability 细节塞进 /healthz。

/healthz 继续以 service alive、playlist exists、freshness 为核心。

最多加少量 degraded count / epg stale warning；复杂状态由 reliability-status 承担。

---

## 27. 真实 failover/recovery 验证方式

允许：生产自然失败、隔离 DB/测试配置模拟 failure、临时 mock upstream。

必须证明 selector decision、selected stream id 变化、publish URL 变化，而 canonical metadata 完全不变。

Recovery 必须证明失败 stream 不需要人工 reset，恢复后能重新进入候选集。

---

## 28. Dynamic 输出冻结

动态赛事仍可随事件变化，但禁止：
- 复活上一轮 signed URL；
- preflight FAIL 后偷偷用 stale URL；
- KORICE advisory FAIL 被误当 authoritative。

---

## 29. Failure smoke

至少覆盖：
F1 fixed source fetch failure → 老 inventory 保留。
F2 selected stream 连续失败 → 自动切备用。
F3 all streams fail → canonical skip。
F4 failed stream recovery → 重新进入候选。
F5 JSNZKPG 明确坏 current URL → authoritative 时本轮排除。
F6 KORICE cloud fail → advisory only。
F7 EPG fail → LKG。
F8 reliability summary 写失败 → 不影响 live.m3u publish。
F9 environment ffprobe fail → 0 per-stream fake failure writes。
F10 publish empty → LKG 不覆盖。

---

## 30. TASK-001～011 全部冻结回归

不得破坏：identity、binding、lifecycle、fetch fail-closed、dynamic current-round only、LKG、scheduler、CAS lock、localhost server、ffprobe、deployment、JSNZKPG、KORICE、isolate、fixed inventory、dynamic default、playback context、metadata、tvg-id、logo、EPG、France 24。

---

## 31. 自动测试最低清单

至少覆盖 60 类场景：
1 stable；2 degraded；3 failed；4 unknown；5 minimum samples；6 stale success；7 consecutive failure；8 recent recovery；9 PASS/PASS；10 PASS/FAIL；11 all fail；12 A→B failover；13 recovery re-enters；14 no forced switch-back；15 metadata stable after failover；16 multi-source preference；17 single-stream；18 fake 200；19 HTML fake；20 segment fail；21 signed downstream fail；22 environment failure zero fake history；23 dynamic precheck PASS；24 FAIL；25 UNKNOWN；26 JSNZKPG policy；27 KORICE advisory；28 KORICE fail still publish；29 stale dynamic not reused；30 isolate；31 all_or_nothing；32 require_dynamic；33 reliability JSON redaction；34 human summary；35 EPG status integration；36 cadence parsing；37 no-change publish；38 runtime status；39 lock；40 LKG；41 metadata；42 tvg-id stable；43 logo stable；44 EPG mapping stable；45 France24；46 CCTV-5+；47 fixed source fail；48 output reverse parse；49 /live.m3u；50 /healthz；51 /epg.xml；52 deployment；53 no sensitive URL；54 no VPN credential；55 retention estimate helper；56 temp cleanup；57 status bounded；58 dynamic temp bounded；59 restart persistence；60 EV-Lab zero-touch guard where applicable。

真实新增 bug 必须增加 regression test。

---

## 32. 生产数据安全

默认不删除 probe history。

若研究认为需要 retention：先 backup、dry-run、输出预计删除行数，等待 Reviewer。TASK-012 不自行执行不可逆大清理。

---

## 33. EV-Lab Zero Harm

必须继续：不改 EV-Lab 文件/service/timer/database，不 restart，不占端口，不共享 lock/data path。

报告提供零改动证据。

---

## 34. 生产操作授权

小W可自行：修改 IPTV repo、修改 IPTV production config、deploy release、restart li-iptv.service、daemon-reload、fixed probe、dynamic preflight、publish、EPG refresh、读取 IPTV journal/DB、创建只读统计、/tmp 故障 smoke、IPTV backup。

必须停止并报告：删除 production DB、restore production DB、大规模删除 probe history、改 EV-Lab、改防火墙/安全组、DNS/TLS、公网暴露、VPN 配置/凭据、新增长期付费服务、DRM/auth bypass、私密 IPTV。

---

## 35. 必须交付

- TASKS/TASK-012.md
- REPORTS/TASK-012-REPORT.md
- REPORTS/TASK-012-PROBE-AUDIT.md
- REPORTS/TASK-012-FAILOVER.md
- REPORTS/TASK-012-SOAK.md
- SOURCES/PLAYBACK-QUALITY-RECON-TASK012.md
- OPERATIONS/RUNTIME-CADENCE.md

如实现 dynamic preflight，再加 REPORTS/TASK-012-DYNAMIC-PREFLIGHT.md。

---

## 36. 报告数字口径

必须明确区分：canonical inventory、currently published fixed、stream inventory、stream with sufficient samples、stable/degraded/failed/unknown canonical、dynamic fetched/published/advisory fail/authoritative fail、EPG channels、M3U total entries。

禁止再出现 TASK-011 那类 41/42/43 用同一个词的口径混淆。

---

## 37. 真实指标

Fixed：canonical_total、published_fixed、stable、degraded、failed、unknown、multi_stream、cross_source_multi_stream、failovers、recoveries。

Probe：requested、success、failure、environment_failure、median startup、p95 startup（样本足够才算）、error distribution。

Dynamic 每 source：fetched、filtered、precheck pass/fail/unknown、published。

System：service uptime、restarts、CPU sample、RSS sample、DB size、journal errors、temp count、open ffprobe process after round。

---

## 38. 不做虚假的 90%

90% stable 是目标，不是必须伪造的 KPI。

如果 43 个 canonical 真实只有 35 个稳定，就报告 35/43 = 81.4%。

禁止：删掉不稳定 canonical 改分母、降低 selector、减少 probe、把 UNKNOWN 算 STABLE。

---

## 39. Curated inventory 的真实语义

curated 只表示我们明确知道“这是哪个频道”，不表示这个频道无论如何必须发布。

canonical 可以存在；当前无健康 stream 时不发布；恢复后自动回来。

本轮必须把这个语义落实。

---

## 40. 配置设计

若新增 reliability 配置，集中在一个清晰 section；如果只需 1～2 个字段，则优先复用 selection，不造新 section。

不要把规则散到 selection/runtime/probe/publish 四处。

---

## 41. CLI

允许新增 reliability-status / dynamic-preflight，但避免 CLI 泛滥。

能合理增强 source-status / probe-status / publish summary 的，优先增强现有命令。

---

## 42. 文档与临时工具整理

允许保留可复用 recon/probe/diag/verify；可以删除已被正式工具替代的一次性 helper。

禁止删除历史 REPORTS / REVIEWS / SOURCES。

删除前确认没有正式文档引用。

---

## 43. 最低验收（60 条）

1. TASK-011 零回归
2. external recon >= 6
3. probe audit 完成
4. 至少 6 real fixed probe rounds
5. round 不是瞬间刷完
6. error taxonomy 一致
7. stability 派生状态可解释
8. UNKNOWN 不算 STABLE
9. stable target 如实报告
10. 不为 90% 降门槛
11. 至少 5 canonical failover 验证
12. A→B 自动切换
13. all-fail canonical skip
14. recovery re-enters
15. 不要求人工 reset
16. metadata failover 前后稳定
17. tvg-id failover 前后稳定
18. logo failover 前后稳定
19. EPG mapping failover 前后稳定
20. multi-stream stats
21. cross-source stats
22. single-stream list
23. 120.76.248.139 至少 3 新 round
24. 不无证据 host blacklist
25. CCTV-5+ 至少 6 round
26. fake 200 不当 success
27. segment fail 分类
28. signed downstream fail 分类
29. dynamic preflight 可审计
30. dynamic preflight 有超时预算
31. JSNZKPG policy 有实测依据
32. KORICE advisory 冻结不破坏
33. KORICE cloud FAIL 不全局删
34. stale dynamic URL 不复用
35. isolate 保留
36. daily reliability JSON
37. human readable status
38. JSON 无敏感 URL
39. cadence 文档
40. EPG cadence 不过频
41. probe cadence 不过频
42. dynamic cadence 合理
43. no-change 行为可解释
44. 至少一个连续 soak
45. service 无异常 restart
46. lock 正常
47. temp 不无限增长
48. ffprobe 无残留 zombie
49. DB growth 有估算
50. probe retention 有结论
51. 不擅自删历史
52. /live.m3u 200
53. /healthz 200
54. /epg.xml 200
55. localhost binding unchanged
56. EPG mapping 保持
57. France24 保持
58. EV-Lab zero harm
59. full regression PASS
60. commit + push main，状态只到 REVIEW，停止

---

## 44. 理想指标

若真实世界允许：fixed published >=40；stable canonical >=90%；degraded <=10%；failed <=2；至少 5 次自动 failover 证据；至少 2 次 recovery 证据；JSNZKPG 明显坏赛事能在 current round 被预检剔除；KORICE 不被上海 false-negative 误杀；service 连续运行无异常 restart。

这些是目标，不允许造数。

---

## 45. 明确不做

Dashboard、Web GUI、Grafana、Prometheus、ELK、用户系统、推荐系统、ML ranking、LLM selector、视频代理、转码、DVR、录制、CDN、公网开放、DNS/TLS、多云部署、多地区 probe farm、家庭常驻 agent、VPN 自动切换、schema V2、大规模频道扩容、第二 EPG 42-id 人工映射、为国际频道凑数量。

---

## 46. TASK-013 边界

TASK-012 不解决 Apple TV 最终交付通道。

不要开公网 8080，不新建 Tailscale/NAS，不换播放器，不修改家庭网络。

这些留给未来 TASK-013：Player Delivery & Real Apple TV Usage。

TASK-012 只保证：后端长期运行质量足够好，值得交付给播放器。

---

## 47. 执行节奏

普通 probe fail、source fail、test fail、bug、selector edge case、dynamic preflight false positive、report mismatch、soak 暴露临时文件，都按：定位 → 修复 → regression → 继续。

只有授权边界、不可逆风险、需私密凭据、需改 EV-Lab、需改防火墙/公网、目标被真实证据证明不可达时才停止报告。

---

## 48. 结束条件

完成后：
1. 所有必须报告完成；
2. TASK-012 状态改为 REVIEW；
3. git diff --check PASS；
4. 专项测试 PASS；
5. 受影响回归 PASS；
6. production smoke PASS；
7. reliability summary 真实可读；
8. commit；
9. push origin main；
10. git status clean；
11. 停止。

**禁止自行启动 TASK-013。**
---

## 49. 执行摘要（小W · 2026-10-08 提交，等大G 验收）

**状态：REVIEW。未启动 TASK-013。**

### 硬指标

| 指标 | 实测 | 目标 | 判定 |
|---|---|---|---|
| fixed stable | **42/44 = 95.5%** | ≥90% | ✅ 未降门槛 |
| failover/recovery | **8/8 频道** | ≥5 | ✅ |
| 生产 soak | **55 轮**（#1→#55） | ≥6 轮 / 间隔≥15min | ✅ |
| 全量回归 | **873 passed / 0 failed** | 零回归 | ✅ |
| TASK-012 新增测试 | **186 项**（48+62+76） | — | ✅ |

门槛三值全部按 `liptv/stability.py` 原值未动：
`min_samples=3`、`min_success_rate=0.8`、`max_last_success_age_hours=48`。

### 🚨 本轮最重要的发现：`/healthz` 永久 stale（功能性故障，已修复并上线）

生产实测矛盾：`status=stale`、`last_success` 停在 12:43，
但 `live.m3u` mtime 是 13:16、round #22 刚跑过。

根因：`runtime.PUBLISHED_STATUSES` 漏了 `DEGRADED_DYNAMIC_PARTIAL`
（TASK-008 引入的 isolate 降级态，`publish.py` 明确标 `EXIT_OK` + 文件已写出）。
isolate 下任一动态源失败 ⇒ 每轮该状态 ⇒ `last_success` 永不推进
⇒ 45 分钟后永久 stale，**而服务完全正常**。运维会误判服务已死。

修法：补该状态 + 加**结构性约束**
`test_r3_published_statuses_match_exit_zero` ——
`PUBLISHED_STATUSES` 必须与 `STATUS_EXIT` 中所有 exit 0 且非 DRY_RUN 的状态
完全一致。以后 `publish.py` 新增 exit 0 态若忘记同步，测试立刻失败。

**已上线**：release `997326580c86-20261008T010432Z`，
生产实测 `set(PUBLISHED_STATUSES) == exit0_set` ⇒ `match True`
`['DEGRADED_DYNAMIC_PARTIAL','DEGRADED_FIXED_ONLY','OK']`。

### 本轮修掉的 5 个真 bug

| # | Bug | 危害 |
|---|---|---|
| 1 | **`/healthz` 永久 stale** | 运维误判服务已死 |
| 2 | 代理伪造 502 | JSNZKPG 可能被整批删赛事 |
| 3 | 时间炸弹测试 | 7 日窗口一过集体假失败 |
| 4 | 增长口径污染 | 相反的 retention 结论 |
| 5 | no-change 改 LKG 语义 | `previous` 不再产生 |

防复发：`tests/conftest.py` autouse 时间守卫 —— 时间敏感函数收到
`now=None` 直接 fail。

### 口径勘误（两处）

1. **增长基准**：初版用「两天平均 6012 行/天」，其中 10-05 是**冷启动日**
   （服务 14:25 才起，半天 762 行）。改为「最近完整日 **11,262 行/天**」，
   外推一年 **228.0 MiB < 512 MiB 阈值 ⇒ 不需要 retention**。
   每行字节从 `count(pageno)×page_size`（58 B）改为 `dbstat sum(pgsize)`（131 B）。
2. **CCTV-5+**：早期记「198 次探测 0 成功，全 TIMEOUT」，
   2026-10-07 实测 **330 次探测 322 成功** —— 早期结论已过期。

### 🚨 四个数字口径必须分清

- **44** canonical 库存 / **43** 已发布 fixed / **174** stream 库存
- **42** = XMLTV `<channel>` 数 / **41** = 43 个 tvg-id 命中 EPG 的
- **`#EXTINF` 总数随赛事变动**（本轮部署时 246），不是固定值

### 已知不足（如实列出，不掩盖）

1. 生产无自然换线证据 —— 未出过故障，8/8 全靠生产库**在线备份副本**注入。
2. preflight FAIL 路径无生产实证 —— 最近两轮两源都 ok。
3. 只验证 top 8 频道，剩 35 个多线路频道未逐一验证。
4. 未验证跨源切换（需改生产 `stream.status`，属生产写）。
5. 固定 probe 22～24 分钟快于 §20 参考值（30～60 min）——
   原因是「轮间隔 ≈ 单轮执行时长」，非配置错误。
6. journal 有 10-04 的历史 `start-limit-hit` 未清理（清理不可逆，需授权）。

### 交付物

`REPORTS/TASK-012-REPORT.md`（436 行主报告）、
`TASK-012-PROBE-AUDIT.md`、`TASK-012-FAILOVER.md`、
`TASK-012-SOAK.md`、`TASK-012-DYNAMIC-PREFLIGHT.md`、
`SOURCES/PLAYBACK-QUALITY-RECON-TASK012.md`、`OPERATIONS/RUNTIME-CADENCE.md`。

新增只读派生层：`liptv/errors.py`（14 类统一错误术语）、
`stability.py`（四态派生）、`preflight.py`、`reliability.py`、
`cadence.py`、`retention.py`（**无任何 DELETE/VACUUM**，AST 测试固定）。
新增 3 个只读 CLI：`reliability-status` / `stability-status` / `dynamic-preflight`。

**schema 仍 V1，selector 打分一行未改，未开公网，未动安全组/DNS，
EV-Lab 全程零干扰。**
