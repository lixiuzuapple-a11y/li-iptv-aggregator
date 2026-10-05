# TASK-010 — Useful Channel Expansion, Quality Curation & Playback-Context Validation

状态：REVIEW  
Owner：老李  
Architect / Reviewer：大G  
Executor：小W

基线：TASK-001 ～ TASK-009 全部 ACCEPTED。  
生产主机：`ev-lab-shanghai`。  
当前生产 fixed：10 个 canonical。  
当前动态源：JSNZKPG + KORICE，生产 failure_policy = isolate。

本轮允许做成一个**较长的连续任务**。目标是提速，不再把每一个小改动拆成独立 TASK。

> 执行原则：只要没有触碰“必须停下报告”的授权边界，小W应连续推进：
> **侦察 → 选源 → 扩频道 → 绑定 → probe → 质量筛选 → publish → 生产 smoke → 测试 → 报告 → REVIEW**。
> 不要因为遇到普通代码缺陷、测试失败、单个公开源失效、某个频道不可播就停下来等待 Owner。
> 发现问题应自行修复、回归、记录，直到满足验收或出现明确阻断。

完成后只允许进入 **REVIEW**；禁止自行启动 TASK-011。

---

## 1. 本轮目标

TASK-009 已证明 fixed pipeline 在真实生产上成立：

```text
public fixed sources
    ↓
explicit canonical/binding
    ↓
stream inventory
    ↓
real ffprobe history
    ↓
selector
    ↓
fixed + dynamic
    ↓
live.m3u
```

TASK-010 不再证明“这条链能不能跑”，而是开始真正提高产品价值：

### 本轮必须直接改善至少三件事

1. **能看什么**
   - fixed 从 10 个 seed 扩到一个真正可日常使用的频道集合。

2. **好不好用**
   - 名称、分组、重复项、手工 publish 行为要更直观；
   - 用户不应该因为忘写一个 CLI flag，就误以为“今天没有动态赛事”。

3. **少不少坏台**
   - 不是“源里有就加”；
   - 必须基于真实 probe、重复线路、占位流、HTML 假流、签名/身份参数、稳定性做筛选。

### 本轮最终目标区间

生产 fixed canonical：

- **目标区间：30～60 个**
- **最低验收：30 个 canonical**
- **至少 24 个 canonical 经过真实 probe 后达到 selector 发布门槛**
- 不要求为了凑 60 个而保留差频道；
- 若真实优质公开源只能稳定得到 35 个，35 个比 60 个垃圾频道更好。

本轮核心原则：

> **质量优先于数量，实际可播优先于目录好看。**

---

## 2. 产品内容范围

本轮优先构建“日常有意义”的频道集合，不追求百科全书式 IPTV。

### A. 中国大陆公共频道

优先：

- CCTV-1 综合
- CCTV-2 财经
- CCTV-4 中文国际
- CCTV-5 体育
- CCTV-5+ 体育赛事
- CCTV-6 电影
- CCTV-7 国防军事
- CCTV-8 电视剧
- CCTV-9 纪录
- CCTV-10 科教
- CCTV-11 戏曲
- CCTV-12 社会与法
- CCTV-13 新闻
- CCTV-14 少儿
- CCTV-15 音乐
- CCTV-16 奥林匹克
- CCTV-17 农业农村

但必须遵守：

- 名称相近不等于同频道；
- CCTV-5 与 CCTV-5+ 不得合并；
- 高清/超清只是同频道不同线路时才可作为 stream variant；
- 若来源实际内容不匹配频道名，宁可不收。

### B. 省级卫视

优先覆盖常用主流卫视，例如：

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
- 厦门等

无需强行全部收齐。

原则：

- 有真实可播线路才进 production；
- 一个频道如果长期只有明显占位流/HTML 页面/失效流，不为“目录完整”保留。

### C. 新闻 / 国际新闻

这是本轮高优先级。

优先侦察公开、无需登录的：

- CGTN / CGTN Documentary 等
- Bloomberg
- DW
- France 24
- Al Jazeera
- NHK World
- CNA
- Arirang
- TRT World
- Euronews
- Sky News 等

是否进入 production 取决于：

- 公开入口是否明确；
- 生产上海是否可抓；
- stream 是否实际可播放；
- 是否存在明显 geo-block；
- 是否有短时 token；
- 是否适合长期 fixed。

### D. 香港 / 台湾 / 日本 / 韩国公开频道

允许侦察并适量加入，但不作为硬数量指标。

优先：

- 明确公开；
- 无私密订阅；
- 无登录/Cookie/Authorization；
- 不需要逆向 token；
- 可稳定播放。

### E. 体育

固定体育频道只在确实存在稳定公开 fixed stream 时加入。

允许：

- CCTV-5 / CCTV-5+
- 公开体育资讯频道
- 公开体育网络频道

禁止：

- 把当前赛事临时 PPV 链路伪装成 fixed；
- 把明显轮换赛事地址存进 fixed inventory；
- 为了凑体育数量引入短时 token。

动态赛事继续由：

- JSNZKPG
- KORICE

负责。

---

## 3. 外部世界侦察：增量而不是从零重做

TASK-009 已完成 16 个 fixed 候选侦察。

TASK-010 不要求把同样的工作机械重做一遍。

要求：

### 3.1 复用 TASK-009 资产

必须先读：

- `SOURCES/FIXED-SOURCE-RECON-20261005.md`
- `config/fixed_seed_bindings.toml`
- `REPORTS/TASK-009-REPORT.md`
- `REVIEWS/TASK-009-REVIEW-01.md`

### 3.2 新增/复验来源

至少再新增或重新深挖 **8 个有价值候选**。

优先范围：

- iptv-org country/category 子集
- iptv-org news
- Guovin 其它明确安全输出
- fanmingming 当前可用的新入口（若恢复）
- 官方公开直播源
- 国家/地区公共电视台公开 HLS
- GitHub 上活跃的公开 M3U 项目
- 其它无需鉴权、明确公开的固定频道来源

必须产出：

`SOURCES/FIXED-SOURCE-RECON-TASK010.md`

字段至少包括：

- source name
- homepage/repository
- entry URL
- maintainer/project
- source kind
- fetched_at
- HTTP status
- parseability
- rough entry count
- category/country
- query/token profile
- auth requirement
- duplicate relationship with existing sources
- Shanghai reachability
- sampled media reachability
- fixed suitability
- final decision
- reject reason

### 3.3 不能只按“源”判断

对最终准备纳入的频道，至少抽样到**底层 stream**：

- HTTP/HLS 是否真的返回媒体；
- 是否 HTML 假页面；
- 是否重定向到短时签名；
- 是否同一个万能流被多个频道复用；
- 是否 geo-block；
- 是否在上海失败但家庭/VPN环境可用。

---

## 4. Playback Context：正式区分“聚合器可达”和“播放环境可达”

TASK-010 必须把 TASK-010 planning note 的认识正式纳入设计。

### 4.1 两个概念必须分开

#### Aggregator Reachability

表示：

> 上海生产机能否访问 source M3U / source endpoint。

用于判断：

- 能不能抓列表；
- source refresh 是否成功；
- 是否能更新 dynamic/fixed inventory。

#### Playback Reachability

表示：

> 实际观看网络能否访问底层 stream。

实际观看环境包括：

- 家庭网络
- 家庭 VPN
- Apple TV 所在网络
- Windows 本机代理/VPN 环境

二者绝不允许合成一个“全局 PASS/FAIL”。

### 4.2 KORICE 冻结规则

KORICE：

- 继续保留为 `dynamic_event_m3u`；
- 上海能抓 M3U 即满足 aggregator 层；
- 不得因为上海 ffprobe 某些 KORICE 底层流失败而自动删除整个 KORICE source；
- 不得用“上海 FAIL = 家庭播放 FAIL”；
- 不上传 VPN 配置、账号、密钥、节点地址、Cookie。

### 4.3 本轮最低实现

不强制做完整多节点 probe 系统。

但至少要完成一种**可审计的 playback context 表达**。

推荐优先级：

#### 优先方案 A：配置/状态层显式 metadata

例如 source policy 中可表达：

- `playback_context = "home-vpn"`
- `aggregator_probe = "shanghai-cloud"`
- `playback_requires_vpn = true`
- `cloud_stream_probe_authoritative = false`

字段名可调整。

要求：

- 不含 VPN 凭据；
- 不进入 selector 算法也可以；
- 但状态/报告必须能说明“上海抓列表成功，不代表上海能播放”。

#### 方案 B：利用现有 probe.name / probe.location

如果现有 V1 probe 表已经能表达 location，则优先复用，不升级 schema。

可记录：

- `shanghai-cloud`
- `home-windows-vpn`

但本轮不要求家庭 probe 自动化到 scheduler。

### 4.4 家庭 Windows / VPN smoke

如果 WebCodex 本机当前 VPN/代理可用：

- 可以对少量 stream 做短时 HTTP/HLS media smoke；
- 不要求安装复杂新服务；
- 如果本机已有 ffprobe，可以用；
- 如果没有 ffprobe，不为此强制安装大型依赖；
- HTTP GET + HLS playlist/segment 结构验证可以作为“playback-context smoke”。

必须明确：

> 这是本机/VPN context 的事实，不写进上海 probe history 冒充上海结果。

---

## 5. Fixed Inventory 扩充策略

### 5.1 不允许“全量灌库然后自动猜”

即使上游有 1000 个频道，也不要自动变成 1000 个 canonical。

生产 canonical 仍然必须是**明确选中的 curated set**。

### 5.2 建议目标

从现有 10 个 canonical 扩到：

- 第一阶段：30
- 若质量足够：40
- 若仍有明确优质频道：50～60

超过 60 不属于本轮目标。

### 5.3 Binding

继续遵守 TASK-009：

允许：

- exact normalized
- 人工明确 alias
- 唯一无歧义 normalization
- 明确 seed mapping

禁止：

- Levenshtein
- fuzzy
- NLP 自动猜
- “名字差不多”
- 地区版/HD/备用版未经证据自动合并

### 5.4 Alias 可适度扩展

本轮允许增加一个**显式 alias 表**，例如：

```text
CCTV13
CCTV-13
CCTV13新闻
```

只有在人工/规则能明确证明是同一频道时使用。

Alias 必须：

- 可审计；
- 进入 Git 的只允许名称映射，不含 stream URL；
- 有冲突时 fail-closed；
- alias 不得跨语义频道。

---

## 6. 频道质量筛选

本轮必须增加“进入 production 的最低质量门槛”。

不能只看：

> source 里有 + ffprobe 偶尔成功一次。

### 6.1 最低要求

每个准备进入 production 的 canonical：

- 至少有 1 条真实 stream；
- 至少有真实 probe history；
- 至少达到 selector 当前门槛；
- 不得是明显万能流；
- 不得返回 HTML；
- 不得是已知短时 signed URL；
- 不得含用户身份 query；
- 不得因为 source 名字好看就保留。

### 6.2 优先保留多 stream

同一 canonical 如果有来自独立 source 的多条可播线路，优先级更高。

目标：

- 至少 **8 个 canonical** 拥有 2+ stream；
- 至少 **5 个 canonical** 的 stream 来自 2 个独立 source；
- 如果客观做不到，要在报告解释真实限制，不得造假凑数。

### 6.3 质量维度

可复用现有 selector：

- success rate
- consecutive failure
- latest success
- startup time
- resolution

本轮**不要重新设计评分模型**，除非真实数据证明现有 selector 有明确错误。

### 6.4 不追求 4K

对于个人日常 IPTV：

优先：

1. 稳定
2. 真频道
3. 延迟/启动速度
4. 720p/1080p
5. 更高分辨率

1080p 不应自动碾压一个明显更稳定的 720p。

---

## 7. “坏台”治理

本轮至少识别并统计以下坏台类型：

- DNS failure
- connect failure
- timeout
- HTTP error
- HTML/text fake stream
- invalid media
- HLS playlist 无有效 segment
- segment 不可达
- repeated catch-all stream
- signed/identity URL
- geo-block suspected
- channel-name/content mismatch（人工 smoke 可发现）

产出：

`REPORTS/TASK-010-BAD-STREAMS.md`

要求：

- 不写完整敏感 stream URL；
- 可写 source、canonical、stream id、host、错误类型；
- 明确“淘汰 / 保留备用 / 观察”；
- 不把一次临时 timeout 永久判死刑；
- 但连续失败达到现有 selector 门槛时应自然退出发布。

---

## 8. 手工 publish 易用性修复

TASK-009 已确认一个真实运维坑：

`publish` 不带 `--dynamic` 时，**根本不抓动态源**。

结果：

- fixed 正常；
- dynamic_summary.sources = []
- 操作者可能误以为“今天没有赛事”，实际是“根本没抓”。

TASK-010 必须解决。

### 8.1 目标语义

如果 config 中已经明确：

```toml
[publish.dynamic]
sources = [...]
```

或等价配置，

则普通：

```bash
python -m liptv publish
```

应采用生产默认配置。

推荐：

- config 有 enabled dynamic sources ⇒ 默认抓；
- `--no-dynamic` 才显式关闭；
- `--dynamic` 可保留作兼容 alias；
- scheduler 与手工 publish 语义一致。

如果认为改变默认语义风险太高，可采用其它方案，但必须满足：

> **用户不能在无明显提示的情况下，把“没有执行动态 fetch”误读成“动态赛事自然为 0”。**

### 8.2 必须零回归

- TASK-003 all_or_nothing 默认仍成立；
- TASK-008 isolate 仍成立；
- require_dynamic 仍成立；
- LKG 仍成立；
- failed dynamic source 不复用旧签名 URL。

---

## 9. 频道命名与分组

本轮要把输出做得更像“能长期用的订阅”。

### 9.1 fixed group 建议

至少区分：

- 央视
- 卫视
- 新闻
- 纪录片
- 教育
- 音乐
- 国际
- 港澳台
- 其它

可以根据实际频道调整。

### 9.2 dynamic group

继续保留：

- 体育赛事（实时）

不得因为 TASK-010 扩 fixed 而污染动态赛事组。

### 9.3 名称

输出 canonical 名称应：

- 简洁
- 稳定
- 不带 source 名
- 不带无意义“高清”“线路1”前缀
- 多 stream 仍只发布 selector 选中的 1 条

动态 source collision 的 [JSNZKPG] / [KORICE] 标签继续只在必要时出现。

---

## 10. Production Probe 执行

扩充后必须在上海真机执行真实 probe。

### 10.1 至少两轮

要求：

- 新增 stream 全部至少 2 round；
- 原 TASK-009 旧 stream 不要求人为清空历史；
- 新旧 stream 可一起 probe；
- 环境级 ffprobe failure 继续 0 history writes。

### 10.2 规模控制

若 stream 数扩到较多：

- 使用现有 concurrency；
- 不把并发无脑拉高；
- 不造成 CPU/网络长时间打满；
- 不保存媒体内容；
- 只做短时探测。

### 10.3 结果统计

至少报告：

- canonical count
- stream count
- source count
- probe requested
- probe pass
- probe fail
- error distribution
- selected fixed count
- skipped fixed count
- multi-stream canonical count
- cross-source multi-stream count

---

## 11. Production Publish 目标

最终生产 publish：

### 硬指标

- fixed canonical inventory >= 30
- selector 最终 fixed published >= 24
- dynamic JSNZKPG 仍启用
- dynamic KORICE 仍启用
- failure_policy = isolate
- `/live.m3u` HTTP 200
- `/healthz` HTTP 200
- reverse parse PASS
- no banned query
- no duplicate canonical fixed output
- no stale signed dynamic resurrection
- localhost binding unchanged

### 理想指标

如果质量允许：

- fixed published 35～50

但不得为理想数字降低 selector 门槛。

---

## 12. 家庭/VPN 可用性 smoke

这一项是**产品验证**，不是云端 selector 输入。

至少抽样：

- KORICE 3～5 条当前看起来有意义的 event/channel；
- fixed 3～5 条；
- 如有国际频道，再抽 2～3 条。

环境：

- WebCodex Windows 本机当前网络；
- 若 Owner 当前 VPN 已启用，可直接利用该网络；
- 不读取/导出 VPN 凭据。

记录：

- context
- timestamp
- channel/event
- host（可脱敏）
- HLS playlist 是否可达
- media segment 是否可达
- HTTP/content type
- result

结论必须标记：

- `home-vpn PASS`
- `shanghai-cloud FAIL`

可以同时成立。

---

## 13. Source Policy / Context Metadata

如果实现成本低，本轮应把来源特征结构化。

建议至少支持：

```text
source:
  aggregator_context
  playback_context
  playback_requires_vpn
  cloud_probe_authoritative
  notes
```

不要求 schema V2。

优先放 config / source-doc / runtime summary。

特别是 KORICE：

- aggregator_context = shanghai-cloud
- playback_context = home-vpn
- playback_requires_vpn = true（若实际确认）
- cloud_probe_authoritative = false

JSNZKPG 根据实际结果记录。

不得存：

- VPN 用户名
- VPN 密码
- VPN server secret
- Cookie
- Authorization
- 私有 subscription

---

## 14. 安全与合规

继续冻结：

- 个人自用；
- 不公开再分发；
- 不做代理；
- 不转码；
- 不录制；
- 不破解 DRM；
- 不绕登录；
- 不偷 Cookie；
- 不逆向 token；
- 不购买/使用盗版私密订阅；
- 不把完整 signed URL 写 Git/report。

公开可访问 ≠ 拥有版权或再分发权。

---

## 15. 生产操作授权

沿用 TASK-007～009。

小W可自行：

- 更新 IPTV release；
- 修改 IPTV 自身 config；
- restart `li-iptv.service`；
- 读取 IPTV journal；
- 访问公开 source；
- 调整 fixed seed；
- 新增/删除明确 curated binding；
- 运行 ffprobe；
- 运行 publish；
- 运行 IPTV backup；
- 在 `/tmp` 或隔离 DB 做失败 smoke；
- 调整 IPTV 自身运维工具。

### 必须停止并报告

不得自行：

- 修改/restart EV-Lab；
- 删除 production DB；
- restore production DB；
- 改腾讯云安全组；
- 改防火墙；
- 改 DNS/TLS；
- 对公网开放 8080；
- 系统大版本升级；
- 新增长期明显付费服务；
- 使用私密/盗取 IPTV；
- DRM/auth bypass；
- 修改 Owner VPN 配置或凭据。

---

## 16. 执行节奏：不要碎片化停顿

本任务刻意允许更长执行。

小W执行时：

### 不需要停下的情况

- 某个公开源 404
- 某个源 timeout
- 某个频道 probe fail
- 单个测试 fail
- 普通代码 bug
- binding 规则需要修正
- source 需要替换
- 某频道数量不够
- 文档需要补
- 发现重复流
- 发现 HTML 假流
- 发现 selector 选错并能在本任务范围修

处理方式：

> 修 → 测 → 记录 → 继续。

### 需要停下的情况

仅限：

- 授权边界
- 生产数据可能不可逆
- 需要付费
- 需要 Owner 私密凭据
- 要动 EV-Lab
- 任务目标本身被真实世界证据证明不可达

否则不要中途问“下一步做什么”。

---

## 17. 自动测试

公网只用于 smoke。

自动测试必须离线。

至少覆盖：

1. 扩展 seed mapping；
2. alias 明确映射成功；
3. ambiguous alias fail closed；
4. signed query 不绑定；
5. identity query 不绑定；
6. catch-all stream 排除；
7. HTML fake stream 不判 PASS；
8. fixed source failure 不清库存；
9. missing → inactive；
10. reappear 恢复 identity；
11. multi-stream selector；
12. cross-source selector；
13. all-below threshold；
14. fixed group 输出；
15. fixed canonical 不重复；
16. dynamic default publish 行为；
17. explicit no-dynamic 行为（如实现）；
18. all_or_nothing 回归；
19. isolate 回归；
20. require_dynamic 回归；
21. dynamic exact dedup；
22. dynamic display collision；
23. LKG；
24. fixed_summary；
25. dynamic_summary；
26. sensitive URL redaction；
27. source context metadata 不泄密；
28. playback-context 不污染 Shanghai probe history；
29. KORICE cloud FAIL 不自动全局删除；
30. output reverse parse。

如实际修复新缺陷，应增加对应 regression test。

---

## 18. 真机失败场景

至少做：

### F1. 一个新增 fixed source 故障

验证：

- 其它 source 继续；
- 老库存保留；
- publish 不被清空；
- fixed_summary 可见失败。

### F2. 新频道全部 stream fail

该 canonical：

- selector skipped；
- 其它 canonical 不受影响；
- 不为了凑数量降低门槛。

### F3. dynamic 一源失败

isolate：

- 另一源成功条目照常发布；
- failed source 0 当前轮条目；
- 无旧 URL 回填。

### F4. KORICE 上海 stream fail

验证：

- source M3U 如果 fetch 成功，source 仍保留；
- 不把 cloud playback failure 当全局不可用；
- playback context 说明正确。

### F5. publish 全空

LKG 不覆盖。

---

## 19. 数据与报告

本轮至少产出：

### 必须

- `TASKS/TASK-010.md`
- `REPORTS/TASK-010-REPORT.md`
- `REPORTS/TASK-010-BAD-STREAMS.md`
- `SOURCES/FIXED-SOURCE-RECON-TASK010.md`

### 推荐

若需要：

- `config/fixed_seed_bindings.toml` 扩充
- `config/fixed_aliases.toml`
- source policy/context 文档

报告必须包括：

- final source list
- reject list
- canonical list
- group list
- binding statistics
- stream count
- multi-stream count
- cross-source count
- probe rounds
- pass/fail/error distribution
- selected/skipped
- production live.m3u count
- dynamic per-source count
- home/VPN smoke
- Shanghai smoke
- failure smoke
- tests
- Git SHA
- EV-Lab zero-harm

---

## 20. 最低验收（40 条）

1. TASK-009 基线零回归。
2. 复用 TASK-009 source reconnaissance。
3. 新增/复验至少 8 个候选。
4. 最终至少 2 个独立 fixed source。
5. production canonical >= 30。
6. production selected fixed >= 24。
7. 不为数量降低 selector 门槛。
8. 至少 8 个 multi-stream canonical，或报告客观不足。
9. 至少 5 个 cross-source multi-stream canonical，或报告客观不足。
10. 无 fuzzy binding。
11. alias 显式可审计。
12. ambiguous alias 不绑定。
13. banned query 不绑定。
14. identity query 不绑定。
15. catch-all stream 不绑定。
16. HTML fake stream 不判为健康。
17. 至少 2 round real ffprobe。
18. 环境级 ffprobe failure 0 history writes。
19. selector PASS/PASS 正常。
20. selector PASS/FAIL 正常。
21. all below threshold 不发布。
22. fixed groups 清晰。
23. fixed canonical output 无重复。
24. manual publish 不再静默漏抓 dynamic。
25. TASK-003 all_or_nothing 零回归。
26. TASK-008 isolate 零回归。
27. require_dynamic 零回归。
28. JSNZKPG production 保留。
29. KORICE production 保留。
30. KORICE cloud stream fail 不等于 global fail。
31. aggregator reachability / playback reachability 有明确表达。
32. 至少做一次 home/VPN context smoke（若 Owner 当前 VPN 网络可用；否则明确 SKIP 原因）。
33. /live.m3u = 200。
34. /healthz = 200。
35. reverse parse PASS。
36. no banned sensitive URL in output/report/Git。
37. localhost 8080 binding unchanged。
38. EV-Lab zero change。
39. 小W全量测试通过并报告真实数字。
40. commit + push main，TASK 状态只到 REVIEW，然后停止。

---

## 21. 明确不做

本轮仍不做：

- 自动 fuzzy canonicalization
- LLM/NLP 自动识别频道
- 几千频道全量导入
- 视频代理
- 转码
- DVR
- 录制
- CDN
- 对公网暴露
- DNS/TLS
- 用户系统
- Dashboard
- Web GUI
- Docker/Kubernetes
- schema V2
- 完整 EPG
- Logo 美化
- 推荐算法
- 自动家庭常驻 probe agent
- VPN 管理
- VPN 凭据读取
- 多地区云节点部署

EPG / Logo 优先留给 TASK-011。

---

## 22. 对已有规划备注的处理

`TASKS/TASK-010-PLANNING-NOTE.md` 不删除。

它作为 KORICE / playback-context 决策来源保留。

正式 TASK-010 以本文件为执行依据。

若 planning note 与本文件冲突：

> **以 TASK-010 正式任务书为准。**

---

## 23. 结束条件

满足验收后：

1. 更新 `REPORTS/TASK-010-REPORT.md`
2. 更新 `REPORTS/TASK-010-BAD-STREAMS.md`
3. 更新 `SOURCES/FIXED-SOURCE-RECON-TASK010.md`
4. 更新 `TASKS/TASK-010.md`：

```text
状态：REVIEW
```

5. commit
6. push origin main
7. `git status` clean
8. 停止

**禁止自行启动 TASK-011。**
