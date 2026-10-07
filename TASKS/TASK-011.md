# TASK-011 — EPG, Logo, Metadata Normalization & Player Experience Upgrade

状态：ACCEPTED
Owner：老李

> **执行摘要（2026-10-07，小W）**
>
> - **§26 验收48/50**。技术指标全部达标：tvg-id 100%、logo 100%、
>   EPG 95.5%（央视 100% / 卫视 95.8%），生产 M3U 从 0 条 tvg-id 升级到 42 条。
> - **本轮最重要的发现**：初版 `tvg-id`（iptv-org 英文 id）与 XMLTV
>   channel id（中文 id）**交集为 0**，节目单会100% 失效 ——
>   而「M3U 里有 tvg-id」这个检查会通过。已修正并加两道 fail-closed。
> - **两条未达成，如实记录，未降低门槛凑数**：
>   ① §12「至少 2 个国际频道」→ 只1 个（France 24）可播，
>   15 个候选分片级实测后 NHK World/DW/Al Jazeera/CGTN 全部不可用；
>   ② §13 坏流 → 根因已定位但**不做排除**（性质从「超时」变成「假 200」，
>   不是同一条证据的延续）。
> - **方法论教训**：「playlist 返回 200」≠「可播」。任何可播结论必须到
>   **分片 + 媒体首包字节（MPEG-TS 0x47）**这一层。
> - 测试 62 passed，全量 681 passed，负向验证 6/6。
>
> 报告：`REPORTS/TASK-011-REPORT.md`、`TASK-011-EPG-COVERAGE.md`、
> `TASK-011-PLAYER-COMPAT.md`、`SOURCES/EPG-SOURCE-RECON-TASK011.md`
>
> **需 Reviewer 裁决三点**：① 是否接受国际频道只 1 个；② 坏流是否现在加排除；
> ③ epg.pw 是否值得做 42 条 id 映射（唯一可用的第二 EPG 源）。
>
> 小W 已停止，**未启动 TASK-012**。

Architect / Reviewer：大G  
Executor：小W

基线：TASK-001 ～ TASK-010 全部 ACCEPTED。  
生产主机：`ev-lab-shanghai`。  
当前 fixed inventory：43 canonical。  
Reviewer 最近 spot-check：约 41 fixed + 213 dynamic 正常发布。  
动态源：JSNZKPG + KORICE，failure_policy = isolate。

本轮继续采用**长任务模式**。除非触碰授权边界或发生不可逆风险，小W应连续推进到 REVIEW，不要把普通 bug、测试失败、单源失效、个别 logo/EPG 缺失拆成中途确认。

本轮目标不是继续堆底层基础设施，而是明显改善实际播放器体验。

---

## 1. 本轮最终目标

把当前“能播”的订阅升级成“更像一个长期可用 IPTV 订阅”：

```text
稳定频道名
+ 稳定 tvg-id
+ Logo
+ EPG
+ 清晰 group-title
+ 固定/动态共存
+ 播放环境差异不误判
+ 国际频道适量补充
= 日常可用订阅
```

### 本轮必须直接改善

1. Apple TV / 常见 IPTV App 里频道识别更稳定；
2. 大部分 fixed 频道能显示 Logo；
3. 主要 fixed 频道能显示当前/未来节目单；
4. 动态体育赛事不被错误套进固定频道 EPG；
5. fixed 输出的 tvg-id 稳定，不随 source 名字变化；
6. 至少补充 2 个真正可用的国际新闻/公共频道（若真实世界证据支持）。

---

## 2. 任务范围

### A. EPG
- XMLTV 优先；
- 允许多个公开 EPG source；
- 不自造节目单；
- 不抓登录态/私有接口；
- 不要求全世界频道全覆盖。

### B. Logo
- 优先公开 logo 目录；
- 允许 iptv-org logos / EPG source 自带 logo / 官方公开 logo；
- 不把图片二进制塞 Git；
- 输出使用稳定 URL；
- logo 缺失不阻断频道发布。

### C. Metadata
必须统一：
- tvg-id
- tvg-name
- group-title
- canonical display name
- logo
- EPG mapping

### D. 国际频道
优先：
- France 24
- NHK World
- 其它本轮侦察能稳定验证的国际新闻/公共频道

不为凑数量加入坏台。

### E. 播放器体验
- 输出 M3U 应尽量兼容常见 Apple TV IPTV 播放器；
- 不加入播放器专有 hack，除非是行业常见扩展；
- 重点测试 M3U + XMLTV 组合是否能被正确关联。

---

## 3. EPG 外部世界侦察

必须先侦察，不直接写代码。

至少检查 8 个候选 EPG/元数据来源，优先：

- iptv-org EPG
- epg.pw
- fanmingming EPG
- 其它公开 XMLTV
- 官方电视台节目单
- 国内公开电视节目单聚合
- GitHub 上活跃 XMLTV 项目
- 现有 IPTV 社区常用 EPG feed

产出：

`SOURCES/EPG-SOURCE-RECON-TASK011.md`

至少记录：
- source
- homepage/repo
- feed URL
- XMLTV validity
- channel count
- timezone
- update frequency
- logo availability
- channel id style
- mainland coverage
- international coverage
- auth/token requirement
- source reliability
- license/usage note
- choose/reject reason
- verified_at

禁止：
- 私密订阅
- Cookie
- Authorization
- 逆向 token
- 抓取付费 EPG
- 把不明个人凭据写入 Git

---

## 4. Canonical Metadata 设计

本轮允许新增**元数据映射层**，但不做 fuzzy 自动识别。

建议文件：

`config/channel_metadata.toml`

每个 canonical 可包含：

```toml
[[channel]]
canonical = "CCTV-13 新闻"
tvg_id = "CCTV13"
tvg_name = "CCTV-13"
logo = "https://..."
epg_source = "..."
epg_channel_id = "..."
group = "央视"
```

字段名可优化。

要求：

- canonical 是唯一主键；
- tvg-id 稳定；
- 不随 selected stream 改变；
- 不把 source_channel 名直接当永久 tvg-id；
- alias 映射与 metadata 映射分开；
- 同一个 canonical 不允许多个 conflicting tvg-id；
- 冲突 fail-closed；
- metadata 缺失不影响 stream selector。

---

## 5. tvg-id 规则

### 必须稳定
同一 canonical 无论今天选 guovin 还是 iptv-org，tvg-id 必须一样。

### 优先级
1. 已验证 XMLTV channel id
2. 官方/主流 EPG id
3. 明确静态人工 mapping
4. 最后才允许内部 stable id

### 禁止
- 用完整 stream URL hash 当 tvg-id
- 用 source_channel_id
- 用临时 source 名
- fuzzy 自动猜
- CCTV-5 与 CCTV-5+ 共用 tvg-id

---

## 6. EPG Mapping

至少覆盖以下优先集：

### 央视
尽量覆盖现有生产央视组：
- CCTV-1
- CCTV-2
- CCTV-4
- CCTV-5
- CCTV-5+
- CCTV-6
- CCTV-7
- CCTV-8
- CCTV-9
- CCTV-10
- CCTV-11
- CCTV-12
- CCTV-13
- CCTV-14
- CCTV-15
- CCTV-16
- CCTV-17

### 主流卫视
至少覆盖：
- 北京
- 东方
- 广东
- 深圳
- 浙江
- 江苏
- 湖南
- 山东
- 安徽
- 湖北
- 河南
- 辽宁
- 四川
- 重庆
- 天津
- 江西
- 黑龙江
- 吉林
- 河北
- 广西
- 云南
- 贵州
- 陕西
- 东南

### 国际
若 production 加入：
- France 24
- NHK World

则必须尝试 EPG；没有可靠 EPG 时允许频道发布但标记 no_epg。

---

## 7. EPG Coverage 目标

硬指标：

- production fixed published 中，至少 **70% 有稳定 tvg-id**
- 至少 **60% 有 EPG mapping**
- 央视组至少 **80% 有 EPG**
- 主流卫视至少 **60% 有 EPG**

理想目标：
- fixed overall EPG coverage ≥ 75%

不要为了 coverage 建错误 mapping。

错误 EPG 比没有 EPG 更差。

---

## 8. Logo 目标

硬指标：

- fixed published 至少 **80% 有 logo**
- 央视组 logo coverage ≥ 90%
- 主流卫视 logo coverage ≥ 80%

Logo 要求：
- HTTP(S) 可访问；
- content-type 是 image；
- 不允许 HTML 假图片；
- 不要求本地缓存；
- logo URL 失效不阻断频道播放；
- 不下载大图片入仓库。

动态赛事：
- JSNZKPG/KORICE 不要求 event logo；
- 可以保留源自带 tvg-logo，但不得强造。

---

## 9. EPG Fetch / Cache

允许实现轻量 EPG fetch/cache。

目标：
- 不每次 player 请求都去外网抓 XMLTV；
- scheduler 定期刷新；
- 抓取失败保留 last-known-good；
- 原子替换；
- 避免半文件；
- XML 无效不覆盖 LKG。

建议路径：
- `/var/lib/li-iptv/epg.xml`
或现有数据目录等价路径。

可新增：
- `epg-refresh`
- `epg-status`

CLI 名称可调整。

---

## 10. HTTP 服务

在现有 localhost server 上增加：

```text
/epg.xml
```

要求：
- 只读；
- 仍仅 localhost；
- 正确 Content-Type；
- 无 EPG 时明确 404/503，不返回半文件；
- 不影响 /live.m3u 和 /healthz；
- server restart 行为零回归。

如果播放器更适合 M3U 的 x-tvg-url，则 publish 可在 `#EXTM3U` header 输出：

```text
x-tvg-url="http://127.0.0.1:8080/epg.xml"
```

但必须确认真实播放器使用场景；若 Apple TV 端无法访问服务器 localhost，则不要错误写 localhost URL。

注意：
当前播放器最终在家庭网络，不应把上海服务器 localhost URL 当成 Apple TV 可访问地址。
如果订阅本身未来通过其它方式传入播放器，则需区分“服务器本机测试 URL”和“用户播放器 EPG URL”。

本轮优先把 **EPG 文件生成正确**，不要为了 URL 暴露去改公网、防火墙/DNS。

---

## 11. Apple TV / Player Reality Check

这一项必须做，不允许只按规范想象。

至少查明：
- 当前用户实际播放器如何导入 M3U；
- 是否支持单独 XMLTV URL；
- 是否读取 tvg-id / tvg-logo；
- 是否读取 group-title；
- 是否支持 M3U header x-tvg-url；
- 本地/云端 URL 对 Apple TV 的可达性限制。

若无法自动操控 Apple TV：
- 通过生成文件做结构验证；
- 给 Owner 最少一步人工验证；
- 不把“理论支持”写成“已验证”。

---

## 12. 国际频道增量

基于 TASK-010 reviewer 事实：

- France 24：上海 HLS 可达
- NHK World：上海 HLS 可达
- DW 旧入口：404
- Al Jazeera / CGTN / CCTV direct：当前上海解析/连接失败

本轮必须：

1. 优先复验 France 24；
2. 优先复验 NHK World；
3. 若稳定可播，则纳入 fixed；
4. 给它们独立 canonical / metadata / logo / EPG；
5. 不为了“国际”分类好看加入不可播源。

最低：
- 若真实可用，至少新增 **2 个国际频道**

若其中某个后来不可用：
- 必须记录；
- 不降低门槛；
- 可用其它真实候选替代。

---

## 13. 当前坏流继续治理

延续 TASK-010：

### 必须复验
- 家庭 context 发现 segment 404 的 4 条线路
- `120.76.248.139` 持续超时 host
- `CCTV-5+`

要求：
- 再增加真实 probe history；
- 如果长期稳定证明某 host 是坏线路，可加入显式排除；
- 排除必须 host/url pattern 精确，不得误伤其它流；
- 一次失败不永久封禁。

---

## 14. Playlist 输出升级

fixed EXTINF 应尽量输出：

```text
#EXTINF:-1 tvg-id="..." tvg-name="..." tvg-logo="..." group-title="...",频道名
URL
```

要求：
- 字段转义正确；
- 缺 logo 可省略；
- 缺 EPG 可省 tvg-id 或使用明确稳定 internal id；
- 不输出空字符串垃圾字段；
- 不泄漏 source 名；
- 不泄漏内部 DB id；
- 动态赛事保持 TASK-008 collision 逻辑。

---

## 15. EPG 合并

如果最终采用多个 XMLTV source：

需要：
- channel id namespace 处理；
- programme 根据 channel id 关联；
- duplicate programme 去重；
- 时间合法；
- timezone 保留；
- 同一 canonical 不允许两个互相冲突 EPG 无声合并。

允许：
- canonical → 单一 authoritative EPG source

优先简单稳定，不需要设计复杂 EPG ranking engine。

---

## 16. EPG 质量检查

至少检查：

- XML well-formed
- channel id unique
- programme start/stop 合法
- channel reference 存在
- timezone 可解析
- 当前时间附近有节目
- 未来 24h/48h 有节目
- 不出现明显过期几个月的 stale feed
- 不把空 XML 当成功

---

## 17. Refresh 策略

EPG 不需要 15 分钟刷新。

建议：
- 每 6～12 小时
或
- 每日 2～4 次

Logo 不需要周期抓图片。

必须避免：
- 高频轰炸公开源
- 每 15 分钟下载大 XML
- EPG fetch 卡住主 publish

EPG refresh 失败不应阻断 live.m3u publish。

---

## 18. 状态与可观测性

/healthz 不需要因为 EPG 临时失败就整体 DOWN。

建议 health/status 增加：
- epg_last_success
- epg_last_error
- epg_age
- epg_channel_count
- epg_programme_count
- metadata_coverage
- logo_coverage

但不得把 health 做得过度复杂。

EPG stale 可：
- warning
- degraded metadata

不应导致视频订阅不可用。

---

## 19. Production Smoke

最终至少验证：

### M3U
- /live.m3u 200
- fixed >= 24
- JSNZKPG 保留
- KORICE 保留
- group 正常
- tvg-id 格式正常
- logo URL 格式正常

### EPG
- epg.xml 能生成
- XML parse PASS
- channel/programme reference PASS
- LKG PASS
- 无半文件
- refresh failure 不覆盖好文件

### 关联
抽样至少：
- CCTV-1
- CCTV-5
- CCTV-13
- 北京卫视
- 东方卫视
- 湖南卫视
- 广东卫视
- France 24（若加入）
- NHK World（若加入）

核对：
```text
M3U tvg-id == XMLTV channel id
```

---

## 20. 失败场景

至少做：

### F1 EPG source HTTP failure
- LKG 保留
- live.m3u 仍正常

### F2 malformed XML
- reject
- 不覆盖 LKG

### F3 empty XML
- reject
- 不覆盖 LKG

### F4 logo 404
- channel 仍发布
- 状态可 warning

### F5 metadata mapping conflict
- fail closed
- 不静默覆盖

### F6 programme references unknown channel
- 统计/过滤
- 不生成坏 XML

### F7 France24/NHK stream fail
- selector 自然处理
- 不降低门槛

### F8 dynamic source fail
- TASK-008 isolate 仍成立

---

## 21. 自动测试

至少覆盖：

1. metadata load
2. duplicate canonical reject
3. duplicate tvg-id reject
4. explicit alias metadata
5. missing metadata fallback
6. tvg-id M3U rendering
7. tvg-logo rendering
8. XML escaping
9. group rendering
10. EPG XML parse
11. channel unique
12. programme references
13. timezone
14. malformed XML reject
15. empty XML reject
16. LKG preservation
17. EPG atomic write
18. refresh failure no overwrite
19. logo HEAD/GET fake HTML detection helper
20. logo missing non-blocking
21. EPG merge deterministic
22. programme duplicate handling
23. M3U ↔ XMLTV tvg-id match
24. CCTV-5 ≠ CCTV-5+
25. fixed metadata does not alter selector
26. dynamic events don't inherit fixed EPG
27. JSNZKPG regression
28. KORICE regression
29. all_or_nothing regression
30. isolate regression
31. dynamic default regression
32. playback_context regression
33. no credentials in metadata
34. France24 mapping
35. NHK mapping
36. EPG health warning
37. /epg.xml HTTP behavior
38. /live.m3u regression
39. /healthz regression
40. reverse parse

如发现真实 bug，必须增加对应 regression test。

---

## 22. 安全 / 隐私 / 合规

继续冻结：

- 不破解 DRM
- 不绕登录
- 不拿 Cookie
- 不拿 Authorization
- 不逆向 token
- 不上传 VPN 凭据
- 不代理视频
- 不录制
- 不转码
- 不公开再分发

EPG/Logo 公开可访问不等于可重新托管。

因此：
- 优先引用原始公开 logo URL
- 不批量镜像 logo
- 不公开对外提供第三方 EPG 服务

---

## 23. 生产操作授权

小W可自行：
- 修改 IPTV 代码
- 修改 IPTV config
- 新增 metadata config
- 新增 EPG fetch/cache
- restart li-iptv.service
- 读取 IPTV journal
- 部署 IPTV release
- IPTV backup
- 访问公开 EPG/Logo source
- probe 国际频道
- 调整 curated fixed seed
- 在 /tmp 做失败 smoke

必须停止：
- EV-Lab 修改/restart
- 删除/restore production DB
- 腾讯云安全组
- 防火墙
- DNS/TLS
- 公网暴露 8080
- 新增付费服务
- 私密 IPTV/EPG
- VPN 凭据
- DRM/auth bypass

---

## 24. 执行节奏

本轮继续长任务，不要碎片停。

普通问题：
- EPG 某源 404
- logo 404
- XML 格式 bug
- test fail
- 国际频道失效
- mapping conflict
- parser bug
- timezone bug

都属于：
```text
修 → 测 → 记录 → 继续
```

只有授权边界/不可逆风险/需要私密凭据/真实目标无法达到才停。

---

## 25. 交付物

必须：
- `TASKS/TASK-011.md`
- `REPORTS/TASK-011-REPORT.md`
- `SOURCES/EPG-SOURCE-RECON-TASK011.md`
- metadata mapping config
- EPG implementation
- tests

推荐：
- `REPORTS/TASK-011-EPG-COVERAGE.md`
- `REPORTS/TASK-011-PLAYER-COMPAT.md`

---

## 26. 最低验收（50 条）

1. TASK-010 零回归
2. EPG recon >= 8 sources
3. metadata mapping 可审计
4. stable tvg-id
5. duplicate tvg-id fail closed
6. no fuzzy mapping
7. fixed tvg-id coverage >=70%
8. EPG coverage >=60%
9. CCTV EPG >=80%
10. major satellite EPG >=60%
11. logo coverage >=80%
12. CCTV logo >=90%
13. satellite logo >=80%
14. logo failure non-blocking
15. EPG XML valid
16. XML channel unique
17. programme refs valid
18. timezone valid
19. EPG refresh
20. EPG LKG
21. malformed XML reject
22. empty XML reject
23. atomic write
24. refresh failure no overwrite
25. M3U tvg-id render
26. M3U tvg-logo render
27. M3U group stable
28. dynamic events no fixed EPG inheritance
29. CCTV-5/CCTV-5+ distinct
30. France24 production probe
31. NHK World production probe
32. 若可用，至少新增 2 国际频道
33. 不为国际分类降低门槛
34. segment-404 复验
35. bad host 复验
36. CCTV-5+ 复验
37. /live.m3u 200
38. /healthz 200
39. epg.xml 可生成
40. M3U/XMLTV mapping sample PASS
41. JSNZKPG 保留
42. KORICE 保留
43. isolate 保留
44. dynamic default 保留
45. playback context 保留
46. no sensitive credentials
47. localhost binding unchanged
48. EV-Lab zero change
49. full tests pass
50. commit + push main，状态只到 REVIEW，停止

---

## 27. 明确不做

- Dashboard
- Web GUI
- 用户系统
- 账号体系
- 收藏同步
- DVR
- 录制
- 转码
- CDN
- 视频代理
- 公网暴露
- DNS/TLS
- Kubernetes
- 多区域云 probe
- 自动家庭常驻 agent
- 推荐算法
- 全世界几千频道
- schema V2（除非不改 schema 根本无法正确实现；若真需要必须停下报告）

---

## 28. 结束条件

完成后：

1. TASK-011 状态 → REVIEW
2. 报告写全真实数字
3. git diff --check PASS
4. tests PASS
5. production smoke PASS
6. commit
7. push main
8. git status clean
9. 停止

**禁止自行启动 TASK-012。**
