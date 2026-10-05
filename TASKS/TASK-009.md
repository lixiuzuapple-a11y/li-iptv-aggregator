# TASK-009 — Production Fixed Inventory Bootstrap & Real Probe/Selection

状态：REVIEW
Owner：老李
Architect / Reviewer：大G
Executor：小W

基线：TASK-001 ～ TASK-008 全部 ACCEPTED。
生产主机：`ev-lab-shanghai`。

本轮完成后只允许进入 REVIEW；禁止自行启动 TASK-010。

---

## 1. 目标

把当前生产从 `fixed_count=0` 推进到一条真实、可长期运行、可审计的 fixed pipeline：

```text
公开/明确可访问 fixed M3U
        ↓
source registry / fetch lifecycle
        ↓
source_channel
        ↓
explicit canonical + binding
        ↓
stream inventory
        ↓
real ffprobe history
        ↓
historical selector
        ↓
fixed + TASK-008 dynamic
        ↓
atomic live.m3u publish
```

本轮核心不是扩频道数量，而是**第一次把 fixed 的真实生产闭环跑通**。

---

## 2. 为什么现在做

TASK-005 已完成真实 ffprobe 能力，TASK-006/007 已完成 Linux/systemd/生产部署，
但 TASK-007 因当时没有合适的 fixed M3U，把“真实 fixed probe/selector smoke”正式延期。

TASK-008 已把动态赛事双源生产化，但当前生产依然：

```text
fixed_count = 0
```

因此下一个最有价值的工作不是继续增加动态源，而是补齐 fixed inventory。

---

## 3. 外部世界侦察先行

在写代码或改生产前，先做 fixed source reconnaissance。

至少检查以下类别：

- 官方公开直播页/公开 M3U；
- iptv-org 等公开社区源；
- fanmingming/live 等公开聚合源；
- 其它明确公开、无需登录/Cookie/Authorization 的 M3U/TXT；
- 项目日志里已记录的候选。

至少记录 **10 个候选来源**，字段：

- name
- homepage / repository
- source entry URL（只允许公开入口 URL）
- kind
- 当前 HTTP 状态
- M3U 可解析性
- 大致频道数
- 内容类别
- 是否含 query/token
- 是否需要登录/认证
- 是否适合 fixed
- 选择 / 淘汰理由
- 最后验证时间

产物：

`SOURCES/FIXED-SOURCE-RECON-20261005.md`

侦察原则：

- 不因为 GitHub star 高就直接采用；
- 不依赖单一来源；
- 不收集私密订阅；
- 不绕过登录、Cookie、Authorization、DRM；
- 不逆向 token 生成；
- 有短时签名/赛事轮换特征的仍归 dynamic，不得冒充 fixed；
- 公开可访问不等于有再分发权，本项目仍只做个人自用。

---

## 4. 本轮 fixed seed 范围

不要一上来导入几千个频道做全自动归一。

本轮只做**小而真实的 seed inventory**。

目标：

- 最终至少 **2 个独立 fixed source** 进入候选；
- 生产至少形成 **8 个 canonical channel**；
- 至少 **5 个 canonical channel** 经过真实 probe 后达到 selector 发布门槛；
- 至少 **2 个 canonical channel** 拥有来自不同 source 的多条 stream，能验证历史选线而非“只有一条所以必选”。

内容优先：

1. 新闻 / 国际新闻
2. 体育资讯或稳定体育频道（若公开固定源确实存在）
3. 纪录片 / 综合公共频道

不要为了凑体育数量引入明显短时签名赛事线路。

---

## 5. Canonical / Binding 原则

TASK-009 **不做 fuzzy auto-binding**。

允许：

- 明确的人工 mapping；
- 完全确定的 exact alias；
- 大小写、空格、常见全半角/标点归一后仍唯一且无歧义的映射；
- 大G/小W明确审过的 seed mapping。

禁止：

- Levenshtein 阈值自动绑；
- NLP 猜频道；
- 名字“看起来差不多”就绑；
- 频道编号相近就绑；
- 未经确认把地区版/高清版/备用版合并。

如果不能确定，就保持 unbound，不要猜。

本轮建议增加一个**显式 seed mapping 文件**，只保存 canonical 名称与 source-channel 可审计匹配规则，
不得保存完整 stream URL 或任何签名 query。

例如：

`config/fixed_seed_bindings.toml`

如果现有 CLI 足够完成明确映射，也可以不新增新格式；但执行报告必须能复现每个 binding 的来源与理由。

---

## 6. Fixed source 生命周期

必须继续遵守 TASK-002：

- fixed source 抓取失败时不破坏当前库存；
- source 本轮消失的条目只 `active=0`，不硬删；
- 再出现时恢复同一身份；
- dynamic_event_m3u 永不进入 fixed inventory；
- source URL 若带敏感 query/token，不写 Git / report / journal。

侦察中若候选源表现为：

- URL 每轮变化；
- 赛事列表按当前比赛轮换；
- 底层大量短时签名；

则淘汰为 fixed，必要时只记录为 future dynamic candidate。

---

## 7. Real ffprobe production smoke

至少对最终 seed inventory 做真实 ffprobe。

生产主机使用 TASK-005 已存在的 ffprobe pipeline，不另造 probe 工具。

要求：

- `ffprobe` 真实执行；
- probe 记录写入现有 V1 `probe/probe_result`；
- 环境级 ffprobe 故障仍按 TASK-005 fail-closed：整轮不污染历史；
- 单 stream 网络/媒体失败正常记为该 stream 失败；
- 不把 HTTP 200 当播放成功；
- 不下载/保存视频内容；
- probe 超时/并发/资源限制沿用现有配置与保护。

至少形成：

- 2 个 probe round；
- 两轮间隔允许人工触发，不要求等 scheduler 自然周期；
- 对同一 canonical 的多 stream 能看到不同历史结果；
- selector 的选择必须可由 probe history 解释。

---

## 8. Selector 验证

至少构造/找到以下真实场景：

### A. 多 stream 都 PASS

selector 根据现有评分/稳定性规则选出 1 条。

### B. 一条 PASS、一条 FAIL

必须选 PASS，不得随机或按来源顺序硬选。

### C. 全部未达到门槛

该 canonical 不发布。

### D. 历史变化

若第二轮让原优选线路失败、备用线路成功，确认历史评分变化能影响后续选择；
不要求一次失败就强制翻转，按现有 score 规则解释即可。

禁止为了让 smoke 好看去临时降低 selector 门槛，除非任务书明确记录并只在隔离测试环境使用。

---

## 9. Production publish：fixed + dynamic 共存

生产配置继续保留 TASK-008：

- `jsnzkpg-sports`
- `korice-ppv`
- `failure_policy=isolate`
- 15 分钟 scheduler

在此基础上加入 fixed inventory。

至少完成一次真实生产 publish，要求：

- `fixed_count >= 5`；
- dynamic 若当时有赛事则同时存在；
- dynamic 若当时自然为空，不算失败；
- `/live.m3u` HTTP 200；
- `/healthz` 正常；
- M3U 反向解析通过；
- fixed 条目来自 selector，dynamic 条目仍来自本轮 fetch；
- fixed 与 dynamic 的 group 可区分；
- 不出现旧签名 dynamic 回流。

---

## 10. Fixed failure 与 LKG

至少验证：

### F1. 一个 fixed source fetch 失败

- 其它 fixed source 正常继续；
- 失败 source 的已有 inventory 不被错误清空；
- 现有可用 stream 仍按既有库存/probe history 参与 selector（遵现有设计）；
- summary/status 明确 degraded/fetch failure。

### F2. 新 probe round 环境级失败

- 不写任何新的 probe_result；
- 旧历史仍保留；
- publish 不因一轮环境故障伪造新健康结论。

### F3. publish 结果变空

- 不覆盖 last-known-good。

不要求故意破坏真实公网源；可以使用隔离 DB / mock / 本机失败 endpoint 做受控验证。

---

## 11. 数据与隐私

Git / report / runtime status / journal 禁止出现：

- 私密订阅 URL；
- Cookie；
- Authorization；
- 完整带签名 query 的 stream URL；
- raw 大型 M3U；
- 用户账号凭据。

允许记录：

- 公开 source 入口 URL；
- canonical channel 名；
- source 名；
- 计数；
- 脱敏 host；
- probe 状态与统计。

生产 DB / config 不入 Git。

---

## 12. 自动测试

公网来源只用于 smoke，自动测试不得依赖公网在线。

至少新增离线测试覆盖：

1. 两个 fixed source 导入；
2. source A/B 含同一 canonical 的不同 stream；
3. 明确 mapping 成功；
4. 不确定 mapping 保持 unbound；
5. source fetch 失败不清库存；
6. source 条目消失 → inactive，不 hard delete；
7. 再出现恢复身份；
8. 两 stream probe PASS/PASS selector 唯一选 1；
9. PASS/FAIL 选 PASS；
10. 全部低于门槛不发布；
11. 环境级 ffprobe failure 0 history writes；
12. fixed + dynamic 同时 publish；
13. dynamic isolate 行为零回归；
14. last-known-good 保护；
15. report/runtime 不泄漏敏感 URL/query。

---

## 13. 生产安全边界

沿用 Owner 对 IPTV 日常技术操作的授权。

小W可自行：

- 更新 IPTV release；
- 修改 IPTV 自身 config；
- restart `li-iptv.service`；
- 读取 IPTV journal；
- 访问公开 fixed source；
- 在 IPTV 数据库内导入/绑定 seed inventory；
- 运行 ffprobe；
- 做 IPTV 自身 backup。

仍需停止并报告，不得自行执行：

- 修改/restart EV-Lab；
- 开安全组/防火墙/DNS/TLS；
- 对公网开放 8080；
- 删除生产 DB；
- restore 生产 DB；
- 系统级大版本升级；
- 付费购买 IPTV；
- 使用私密/盗取订阅；
- 绕登录/DRM/鉴权。

---

## 14. 最低验收

1. TASK-008 基线全量测试零回归。
2. 完成 fixed source 外部侦察，至少 10 个候选并记录取舍。
3. 至少选择 2 个独立 fixed source。
4. 至少形成 8 个明确 canonical channel。
5. 至少 5 个 canonical 真实 probe 后达到 selector 门槛并可发布。
6. 至少 2 个 canonical 有跨 source 多 stream。
7. mapping 全部可解释，无 fuzzy auto-binding。
8. fixed fetch failure 不破坏既有库存。
9. missing→inactive→reappear 生命周期成立。
10. 真实 ffprobe 至少 2 round。
11. 环境级 ffprobe failure 仍为 0 新 history writes。
12. selector 至少验证 PASS/PASS、PASS/FAIL、all-below-threshold。
13. selector 输出可由历史记录解释。
14. 生产 `fixed_count >= 5`。
15. fixed + TASK-008 dynamic 能共存发布。
16. `/live.m3u` 200、可解析。
17. `/healthz` 正常。
18. dynamic isolate / LKG 零回归。
19. 生产 config/DB/stream URL 不入 Git。
20. EV-Lab unit/data/timer 零改动。
21. `git diff --check` 通过。
22. 小W全量测试通过并报告命令/exit code。
23. TASK 状态只改到 REVIEW。
24. commit + push main 后停止。
25. 禁止启动 TASK-010。

---

## 15. 明确不做

- fuzzy canonicalization；
- 自动频道知识图谱；
- 大规模几千频道全量生产；
- 多地区 probe；
- Windows probe 上报；
- EPG；
- Logo；
- Dashboard；
- 视频代理；
- 转码；
- DVR；
- TLS / DNS / 公网暴露；
- schema V2；
- Docker/Kubernetes。

---

## 16. 交付

小W完成后必须更新：

- `REPORTS/TASK-009-REPORT.md`
- `SOURCES/FIXED-SOURCE-RECON-20261005.md`
- `TASKS/TASK-009.md` 状态 → REVIEW

报告必须列出：

- 候选来源清单与取舍；
- 最终采用 source；
- canonical/binding 表；
- stream inventory 统计；
- 两轮真实 probe 统计；
- selector 解释样例；
- production publish 统计；
- failure/LKG smoke；
- EV-Lab zero-harm；
- 测试与 Git SHA。

完成后 commit + push `main`，停止等待大G独立验收。
