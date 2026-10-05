# TASK-008 — Production Dynamic Sports Multi-Source Aggregation

状态：REVIEW（QA-008A 已修：per-source published/cross_source_duplicate 记账 + 7 项永久回归 + 2 次负向验证；全量 522 passed；见 REVIEWS/TASK-008-REVIEW-01.md 与 REPORTS/TASK-008-REPORT.md §13.1）
Owner：老李
Architect / Reviewer：大G
Executor：小W

基线：TASK-001 ～ TASK-007 全部独立 ACCEPT。
生产主机：`ev-lab-shanghai`（IPTV 已部署；EV-Lab 为受保护邻居）。
已登记动态源：

- `jsnzkpg-sports` — `dynamic_event_m3u`
- `korice-ppv` — `dynamic_event_m3u`

本轮完成后只允许进入 REVIEW；禁止自行启动 TASK-009。

---

## 1. 目标

把当前“能抓一个动态源”的能力推进为**真实生产可用的多动态赛事源聚合**：

```text
JSNZKPG ─┐
         ├─ fetch current M3U
KORICE ──┘
        → per-source validation/filter
        → per-source failure isolation
        → display collision disambiguation
        → compose current-only dynamic playlist
        → atomic publish live.m3u
        → scheduler refresh
        → /healthz + /live.m3u
```

本轮最重要的业务结果：

> 当两个动态源中一个临时失败时，另一个成功源的**本轮实时赛事仍然可以发布**；
> 但任何失败源都绝不能复用上一次的动态 URL。

当前 TASK-003 的“任一动态源失败 ⇒ 所有动态一起舍弃”继续作为兼容模式保留，
但不再作为生产多源的唯一策略。

---

## 2. 冻结原则

### 2.1 动态源仍然不是 fixed

两个源都继续：

- 不进入 `source_channel → canonical → stream` 固定库存；
- 不写 `probe_result`；
- 不参与 fixed selector；
- 不做长期 canonicalization；
- 不缓存旧赛事 URL；
- 不把签名 query/token 写入 Git、报告或日志。

**禁止**因为 TASK-007 fixed smoke 顺延，就把动态源冒充 fixed。

### 2.2 不做代理

播放器从 M3U 里拿到上游 URL 后仍直接访问上游。

本项目：

- 不代理视频；
- 不转码；
- 不 DVR；
- 不延长 token；
- 不绕过 DRM / 登录 / Cookie / Authorization；
- 不反向工程私有接口。

### 2.3 个人自用

公开可访问 ≠ 拥有再分发权。
本项目仍以个人自用、公开/明确可访问 M3U 为边界。

---

## 3. 新增动态失败策略

在 `[publish.dynamic]` 增加：

```toml
failure_policy = "all_or_nothing"
```

允许值：

### `all_or_nothing`

保持 TASK-003 既有语义，**必须零回归**：

- 任一动态源失败；
- 本轮所有动态条目全部舍弃；
- 有 fixed ⇒ `DEGRADED_FIXED_ONLY`；
- fixed 也为空 ⇒ `DEGRADED_NO_PUBLISH`；
- 不复用旧动态 URL。

这是默认值，保证旧配置完全兼容。

### `isolate`

TASK-008 新增的生产多源模式：

- 每个动态源独立 fetch / parse / validate / filter；
- 某一源失败 ⇒ 该源本轮贡献 0 条；
- 其它成功源的**本轮成功条目保留**；
- 绝不从失败源的旧 playlist / snapshot / previous M3U 回填；
- 至少 1 个动态源成功且最终有条目 ⇒ 可以发布；
- fixed 可以为 0；
- 全部动态源失败且 fixed=0 ⇒ 不发布，保留 last-known-good；
- 所有动态源成功但最终过滤后 0 条且 fixed=0 ⇒ 不发布空列表；
- fixed>0 时，动态全部失败仍可只发 fixed。

新增明确状态：

```text
DEGRADED_DYNAMIC_PARTIAL
```

含义：

- `failure_policy=isolate`
- 至少一个选中的动态源失败；
- 至少一个其它动态源成功并有实际发布条目；
- 本次仍成功写出完整 M3U；
- exit code = 0；
- runtime / summary 必须明确列出 failed / succeeded source counts。

不得把它伪装成 `OK`。

---

## 4. `require_dynamic` 语义

避免继续扩 CLI 参数。

冻结：

### all_or_nothing

保持旧语义：

- 任一动态源失败 ⇒ 整次拒绝。

### isolate

`require_dynamic=true` 解释为：

> 最终必须至少有 1 条**本轮新鲜动态赛事**进入 playlist。

因此：

- 一个源失败、另一个成功并有条目 ⇒ 满足 require_dynamic；
- 所有源 fetch 成功但过滤后 0 条 ⇒ 不满足；
- 所有源失败 ⇒ 不满足；
- 不允许用 fixed 条目满足 `require_dynamic`。

---

## 5. 多源显示冲突

当前代码只做单来源内部的字节级重复过滤，跨源不去重。

TASK-008 **不做赛事实体识别**，也不设计 event canonical model。

冻结规则：

1. 不因为两个来源名字“看起来像同一场”就合并；
2. URL 不同的线路保留；
3. `[解说]` / `[原声]` 继续视为不同显示项；
4. 如果多个来源最终生成**完全相同的 normalized display name + group**，播放器里会无法区分，则只对冲突项追加稳定来源标签：
   - `[JSNZKPG]`
   - `[KORICE]`
   或使用 source.name 的稳定短标签；
5. 非冲突项不得无意义追加来源名；
6. 同 URL + 同显示名 + 同 group 的真正重复项允许跨源精确去重，但必须记录：
   - kept_source
   - duplicate_source
   - reason=`exact_cross_source_duplicate`
7. **禁止 fuzzy matching**、球队名 NLP、时间窗猜测或相似度合并。

目标是“可用、可解释、不会误合并”，不是做赛事知识图谱。

---

## 6. 每源统计与脱敏

publish summary / runtime status 增加动态源摘要，至少：

```json
{
  "dynamic": {
    "failure_policy": "isolate",
    "selected_sources": 2,
    "successful_sources": 1,
    "failed_sources": 1,
    "sources": [
      {
        "name": "jsnzkpg-sports",
        "ok": true,
        "fetched": 58,
        "included": 9,
        "published": 9,
        "discarded": 0,
        "duration_ms": 123
      }
    ]
  }
}
```

要求：

- 不含完整 stream URL；
- 不含 query / fragment；
- 不含 raw M3U；
- error text 必须脱敏、限长；
- source URL 最多输出 `scheme://host`；
- runtime-status 条数继续 bounded。

---

## 7. scheduler 生产模式

TASK-008 不重构 scheduler 架构。

当前生产没有 fixed inventory，probe 也未启用，因此本轮生产配置可以采用较短统一周期。

冻结生产建议：

```toml
[runtime]
interval_seconds = 900
```

即 **15 分钟**刷新一次。

理由：

- 两个来源都是实时赛事清单；
- 动态 URL/赛事变化比 fixed 频道快；
- 当前 fixed sources=0、probe.enabled=false，不会造成每 15 分钟全量 ffprobe。

如果未来引入 fixed inventory + probe，再单独做不同 stage cadence；**TASK-008 不提前重构多频率 scheduler**。

要求：

- immediate first round；
- 每 15 分钟重新抓本轮动态源；
- 不使用上一轮动态条目；
- 任一轮失败不删除 last-known-good；
- scheduler 失锁仍按 TASK-004/005 立即停止。

---

## 8. 生产配置

在 `ev-lab-shanghai` 的**真实生产配置**中：

- 启用 `jsnzkpg-sports`；
- 启用 `korice-ppv`；
- 两者 kind 均为 `dynamic_event_m3u`；
- `failure_policy = "isolate"`；
- `include_replay = false`；
- `server.host = 127.0.0.1`；
- `probe.enabled = false`（没有 fixed inventory 时继续关闭）；
- scheduler interval = 900 秒。

生产 config 不入 Git。

真实 source URL 可以存在于服务器 config，但：

- 报告不粘底层播放 URL；
- Git 不提交 production config；
- journal 不打印完整动态 stream URL。

---

## 9. 生产真实 smoke

经 Owner 已授权大G做常规技术决策，本轮允许小W在现有 IPTV 隔离范围内更新
`li-iptv.service` 所用 IPTV release/config 并 restart IPTV 服务。

**不需要再次向老李申请**以下操作：

- 更新 IPTV 自身 release；
- 修改 IPTV 自身 config；
- restart `li-iptv.service`；
- 读取 IPTV journal；
- 访问两个已登记公网 M3U。

仍禁止：

- 修改 / restart EV-Lab；
- 修改安全组 / 防火墙 / DNS；
- 开公网 8080；
- 删除生产 DB；
- restore DB；
- 系统级大版本升级。

### smoke A — 两源都成功

在两个公网源当前都可访问时：

- scheduler/publish 拉取两源；
- 至少一个源有实际赛事时生成非空 `live.m3u`；
- `/healthz status=ok`；
- `/live.m3u HTTP 200`；
- playlist 可解析；
- 动态 source counts 与实际条目一致。

若某个源当前自然为空，不把“0 场赛事”伪造成失败；记录实际情况。

### smoke B — JSNZKPG 人工失败

测试环境/临时配置中把 JSNZKPG 指向本机明确失败 endpoint 或注入 opener，
**不要修改公网 DNS**。

要求：

- KORICE 成功条目仍发布；
- status=`DEGRADED_DYNAMIC_PARTIAL`；
- 失败源 published=0；
- 没有旧 JSNZKPG URL 回流。

### smoke C — KORICE 人工失败

对称验证。

### smoke D — 两源都失败

fixed=0 时：

- 不发布新文件；
- 当前 last-known-good 字节不变；
- status 明确 no-publish；
- `last_success_publish_at` 不推进；
- 不把上一次动态 URL 当作本轮结果。

---

## 10. last-known-good 与动态 URL

这里必须区分两个概念：

### 允许

如果新一轮完全无法产生可发布 playlist，磁盘上的旧 `live.m3u` 可以继续存在，
因为 TASK-003/004 已冻结“失败不覆盖 last-known-good”。

### 禁止

新一轮组合逻辑**不得读取旧 live.m3u 并把其中动态条目重新当作本轮成功条目**。

因此：

- old file 存在 ≠ old dynamic 被本轮复用；
- summary 必须能看出本轮成功来源和新鲜条目数；
- `last_success_publish_at` 只在真正写出新 playlist 时推进。

---

## 11. 动态条目 freshness

TASK-008 不尝试猜每条 URL 的 token expiry。

只冻结：

- 每次 publish 的动态条目必须来自**本轮 fetch**；
- summary 记录 `fetched_at`；
- runtime status 记录本轮动态抓取完成时间；
- playlist 本身不添加虚构 expiry；
- 不因为旧 playlist 文件还存在就声称其中 URL 仍有效。

---

## 12. KORICE 兼容性

对 `https://www.korice.eu.org/ppv_m3u.php` 做真实结构兼容测试。

当前已知特征：

- 标准 `#EXTM3U / #EXTINF`；
- `group-title` 可为 `American Football` 等赛事类别；
- 显示名可包含比赛双方与时间文本；
- 内容随赛事变化。

要求：

- 不硬编码 NFL；
- 不硬编码当前比赛名；
- 不把“非 JSNZKPG 联赛格式”当异常；
- 继续按通用动态过滤器处理；
- replay/promo 若该源未来出现，也继续走配置化排除规则。

---

## 13. JSNZKPG 兼容性继续冻结

不得回归早期错误：

- 不把 `正在直播` 当唯一 `group-title`；
- 联赛名 group 继续允许；
- M3U comment section 的“赛事回放”继续可识别；
- `include_replay=false` 时回放排除；
- 保留 `[解说]` / `[原声]`。

---

## 14. 离线自动测试

自动测试**不得依赖公网两个站点在线**。

需要扩展 fake source server / opener，至少模拟：

1. 两源成功且均有条目；
2. A 成功 / B HTTP 500；
3. A timeout / B 成功；
4. 两源都失败；
5. 一源成功但过滤后 0 条；
6. 两源出现完全相同条目；
7. 两源相同 display name/group、不同 URL；
8. `[解说]` / `[原声]` 不误合并；
9. malformed M3U；
10. source response 截断；
11. query/token 脱敏；
12. old live.m3u 中有旧动态 URL，新轮失败时不得被组合层读取。

---

## 15. 兼容性

必须保证：

- 默认 `failure_policy=all_or_nothing`；
- TASK-003 旧测试全部不改语义；
- 单动态源行为与旧版本一致；
- fixed-only publish 行为不变；
- selector 不改；
- schema V1 不改；
- probe 不改；
- HTTP 路由不改；
- systemd unit 结构不改；
- restore/upgrade/lock 安全语义不改。

---

## 16. 最低验收

1. 当前基线 **461 tests** 零回归。
2. 新增 `failure_policy` 配置解析与非法值 fail-fast。
3. 默认 `all_or_nothing` 完整保持 TASK-003 语义。
4. isolate：A fail + B success ⇒ B 本轮条目发布，状态 `DEGRADED_DYNAMIC_PARTIAL`。
5. isolate：B fail + A success ⇒ 对称成立。
6. isolate：A/B 全 fail + fixed=0 ⇒ 不发布，LKG 字节不变。
7. isolate：A/B 全 fail + fixed>0 ⇒ 可只发 fixed，状态明确 degraded。
8. isolate：一个源成功但过滤后 0，另一个源成功有条目 ⇒ 有条目的源正常发布。
9. require_dynamic 在 isolate 下以“至少 1 条本轮动态条目”为准。
10. exact cross-source duplicate 可精确去重并记录来源。
11. display collision 不 fuzzy merge，只追加稳定来源标签。
12. report/runtime 不泄漏 query/token/raw M3U。
13. JSNZKPG 真实结构 smoke 通过。
14. KORICE 真实结构 smoke 通过。
15. 生产 config 两源启用、failure_policy=isolate、15min refresh。
16. 真机至少完成一次两源实际 fetch/publish；若当时某源自然不可达，如实记录并用 isolate 语义继续。
17. 真机人工单源失败 smoke 至少一次，验证另一源继续发布。
18. 两源全失败离线/受控 smoke，验证 last-known-good 不被覆盖。
19. `/healthz` 与 `/live.m3u` 在有新鲜动态发布时达到 ok / 200。
20. EV-Lab unit checksum、数据目录、timer 状态保持不变。
21. Git 不含生产 config、完整 stream URL、token、Cookie、Authorization。
22. `git diff --check`、测试命令/退出码、Git SHA、本地/远端一致。
23. 状态只到 REVIEW；禁止启动 TASK-009。

---

## 17. 不包含

- fixed_m3u 搜索/采购；
- fixed canonical 自动识别；
- 赛事实体模型；
- fuzzy dedup；
- 多地区 probe；
- 视频代理/转码/DVR；
- EPG / Logo；
- Dashboard；
- 公网匿名暴露；
- TLS / DNS；
- 用户账号系统；
- schema V2；
- Kubernetes / Docker。

---

## 18. Stop / Gate

只有以下情况需要停止并报告，不要自行扩大范围：

- 需要修改 EV-Lab；
- 需要开放公网/安全组/DNS；
- 需要绕过登录/DRM/鉴权；
- 需要持久保存动态签名 URL 才能继续；
- 需要修改 selector 或 schema；
- 两个源实际响应已不再是公开 M3U，而变成需要私密认证；
- 真实生产操作会覆盖未知 config/DB。

其余 IPTV 自身常规技术决策由大G代理 Owner 决定，小W无需等待老李逐项授权。

完成后填写 `REPORTS/TASK-008-REPORT.md`，TASK 状态改为 REVIEW，commit + push main 后停止。
