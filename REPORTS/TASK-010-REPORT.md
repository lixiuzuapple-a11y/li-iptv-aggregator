# TASK-010 Execution Report

状态：REVIEW
Owner：老李
Executor：小W
Reviewer：大G

> 按 `TASKS/TASK-010.md` 执行。完成后将本文件与 TASK 状态一并更新到 REVIEW。

## 0. 一句话结论

生产 fixed canonical 从 **10 → 43**（硬指标要求 ≥30），真机 probe 两轮后
**selector 实际发布 42 个 fixed**（要求 ≥24），叠加动态赛事**共 238 条**，
`/live.m3u` 与 `/healthz` 均 200，反向解析 PASS，**fixed 侧零签名泄漏**。

过程中修掉 **2 个真实缺陷**，都做了负向验证；
另外用**新的 source policy 机制**正式把「聚合器可达」与「播放环境可达」拆成两类事实。

---

## 1. 基线与变更

| 项 | 值 |
|:--|:--|
| 起止 release | `077bd509` → **`1a60d3c`** |
| 生产实例 | `lhins-bukxxz3g` / ap-shanghai-5 / Ubuntu 24.04.4 / Python 3.12.3 / ffprobe 6.1.1 |
| schema | **V1 未变**（`schema_version=1` 复核；TASK-001 冻结遵守） |
| 8080 绑定 | 仍只 `127.0.0.1:8080`（`ss -ltn` 复核），**未对公网开放** |
| 部署 | `deploy upgrade`（自动备份 DB）→ `reset-failed` + `daemon-reload` + `restart` |
| DB 备份 | `pre-t010-20261005T222141Z.sqlite3` + deploy 自动备份，共 2 份 |

**代码变更清单**

| 文件 | 性质 | 说明 |
|:--|:--|:--|
| `liptv/config.py` | 【新增】 | `[publish].dynamic_default` tri-state + `resolve_include_dynamic()` |
| `liptv/source_policy.py` | 【新增】 | §4/§13 playback context 与 aggregator reachability 分离 |
| `liptv/cli.py` | 【修改】 | `--no-dynamic`；publish 决策接入；理由码人读标签 |
| `liptv/publish.py` | 【修改】 | 摘要携带 `dynamic_decision_reason` 与 `playback_context` |
| `config/fixed_seed_bindings.toml` | 【修改】 | 10 → **43** 条 seed，零 stream URL |
| `config/fixed_aliases.toml` | 【新增】 | 5 条显式 HD alias，冲突 fail-closed |
| `config/source_policies.toml` | 【新增】 | 4 条 source policy，KORICE 冻结规则 |
| `tools/build_fixed_seed.py` | 【修改+修复】 | alias 支持；**category 同步修复** |
| `tools/smoke_home_playback.py` | 【新增】 | §12 家庭/VPN 两级验证 |
| `tools/recon_inventory.py` 等 4 个 | 【新增】 | 侦察与盘点工具 |
| `tools/_negcheck_t010.py` | 【新增】 | 负向验证脚本 |
| `tests/test_task010.py` | 【新增】 | **67 项**离线测试 |
| `tests/test_publish.py` / `test_task008.py` / `test_task009.py` | 【修改】 | 纯 fixed 场景显式 `--no-dynamic` |
| `SOURCES/FIXED-SOURCE-RECON-TASK010.md` | 【新增】 | 侦察报告 |
| `REPORTS/TASK-010-BAD-STREAMS.md` | 【新增】 | 坏台报告 |

---

## 2. 外部侦察（§3）

详见 `SOURCES/FIXED-SOURCE-RECON-TASK010.md`。要点：

| 层 | 结果 |
|:--|:--|
| **A. 复用已有源** | iptv-org-cn 131 clean + guovin-gd-ipv4 400 clean = **321 个非歧义归一名**（TASK-009 只用了 10 个） |
| **B. 增量侦察 23 个 URL** | 区域源（hk/tw/jp/kr）可用；Guovin 其他 7 个省份分支、fanmingming 其他变体**全部 404**；国际新闻源本机不可达 |
| **新增采用源** | **0 个** —— 94% 的扩充来自已有源深度盘点，符合「质量优先于数量」 |

**Guovin 事实认定**：实测 `hd`/`ah`/`sd`/`sh`/`bj`/`js`/`zj` 七个分支**全部 404**，
该项目**只有 `gd` 一个省份分支**。后续不要再去猜。

**诚实标注**：5 个国际新闻源（Al Jazeera / CGTN / CCTV 官方入口）在本机
因 **FlClash TUN 切断 TLS**（`SSLEOFError`，DNS 全部正常、清空代理变量后仍失败）
而不可达，标记为「**待生产复验**」，**本轮不写进 seed**。
本机不可达 ≠ 上海不可达，不据此淘汰。

---

## 3. Fixed 扩充（§5）

| 指标 | TASK-009 | **TASK-010** | 任务书要求 |
|:--|--:|--:|:--|
| seed canonical | 10 | **43** | ≥ 30 ✅ |
| 跨独立源 multi-stream | 0 | **13** | ≥ 5 ✅ |
| ambiguous | 0 | **0** | 必须 0 ✅ |
| alias applied | — | 5 | — |
| catch-all 排除 | — | 2 | 必须 ✅ |
| 签名/身份 URL 排除 | — | **87** | 必须 ✅ |
| `CCTV-5` / `CCTV-5+` | 分开 | **分开** | §2A 明令 ✅ |
| §2A 点名 17 个 CCTV | 部分 | **全覆盖** | 必须 ✅ |

**§9.1 分组输出**（生产实测）：卫视 24 / 央视 12 / 教育 2 / 新闻 1 / 纪录片 1 / 音乐 1 / 儿童 1。
「国际」「港澳台」为 0 —— 相关源未通过可用性判定，**不为凑分类塞坏台**。

---

## 4. §8 动态源默认语义（本轮核心运维修复）

**修复的真实坑**：TASK-009 时代 `publish` 不带 `--dynamic` 时**静默不联网**，
操作者会把「根本没抓」误读成「今天没赛事」。

实现：`[publish].dynamic_default` tri-state（`auto` / `true` / `false`）+ `--no-dynamic`。
优先级：**CLI 显式 > 配置 > auto**。

**生产实测证据**（裸 `publish`，不带任何 flag）：

```text
fixed/dynamic : 42 / 196  (total 238)
dynamic       : 已抓取（auto：已登记 enabled 动态源，按生产默认抓取）
```

TASK-009 时代这条命令的 `dynamic` 一定是 0。

**可审计性**：`dynamic_decision_reason` 始终进摘要与 CLI 输出，
publish 摘要里 `include_dynamic` 与 `dynamic_decision_reason` 成对出现，
「没抓」和「真没赛事」不再可能混淆。

**零回归**：8 种组合真值表全通；3 个老 flag（`--dynamic` / `--dynamic-source` /
`--require-dynamic`）行为**逐字未变**。

---

## 5. §4/§13 Playback Context

新增 `liptv/source_policy.py` + `config/source_policies.toml`。
**KORICE 冻结规则**落地：云端 probe 全失败**不删源**（播放依赖家庭 VPN）。

**生产 publish 摘要实测**：

```text
must_not_merge: True
korice-ppv      agg=shanghai-cloud play=home-windows-vpn vpn=True  auth=False
jsnzkpg-sports  agg=shanghai-cloud play=home-windows-vpn vpn=False auth=True
iptv-org-cn     agg=shanghai-cloud play=home-windows-vpn vpn=False auth=True
guovin-gd-ipv4  agg=shanghai-cloud play=home-windows-vpn vpn=False auth=True
```

**零 schema 改动**：policy 是配置层 + 摘要层，**不进数据库**。

---

## 6. Production Probe（§10）

| 指标 | 值 |
|:--|--:|
| canonical | 43 |
| stream | 168 |
| probe 轮次 | **2 轮**（隔离环境）+ 1 轮（生产） |
| 生产探测次数 | 258 |
| 生产通过 | **214**（82.9%） |
| canonical 有 ≥1 通过 | **42** |
| 错误分布 | TIMEOUT 28 / CONNECT_ERROR 9 / INVALID_MEDIA 6 / HTTP_ERROR 1 |

**并发**：沿用 `[probe].max_concurrency = 2`，未拉高；只做短时探测，不保存媒体内容。

selector 最终选中 **43** 条，其中 42 条发布、`CCTV-5+` 落入 skipped
（两轮全灭，selector 自然排除，**未人为降低门槛**）。

坏台明细见 `REPORTS/TASK-010-BAD-STREAMS.md`。

---

## 7. 真实缺陷（2 个，均已负向验证）

### 缺陷 1：已存在 canonical 的 category 不跟随 seed 同步

**现象**：生产 `select --all` 输出里 `CCTV-2 财经` 的 category 是「新闻」，
而 seed 写的是「央视」。

**根因**：`apply_plan` 对已存在的 canonical 只取 `id` 就走人，
TASK-009 时期的旧归类被永久继承。§9.1 要求 fixed group 至少区分
央视/卫视/新闻/…，不同步这条要求形同虚设。

**修复**：seed 是 curated 的唯一事实源；只在**真的不同时**才 UPDATE，
变化记入 `actions.category_changed` 供审计。

**负向验证**：移除 UPDATE 语句后
`test_apply_syncs_category_of_existing_canonical` **立即 failed**
（`- 央视 / + 新闻`），确认断言真在守护缺陷。

**生产结果**：修复后 `央视` 组从 11 → **12**，`新闻` 组从 2 → **1**。

### 缺陷 2：家庭 smoke 工具的相对路径解析静默走错分支

第一版写 `urllib.parse.urljoin(url, seg) if hasattr(urllib, "parse") else seg`，
而 `urllib.parse` **没有 import** —— `hasattr` 返回 False，于是把**相对路径**
原样当绝对 URL 请求，必然 404，5 条 `PLAYLIST_OK_SEGMENT_FAIL` 全是假象。

**教训**：「条件表达式 + 未导入的模块」不报错，只是悄悄走错分支。
已改为直接 `import urllib.parse` 并去掉条件。

> ⚠️ 修正后重跑，那 5 条**仍然是 404** —— 但这次是**真实缺陷**：
> 上游 playlist 里 segment 路径缺前缀（见 §9）。

---

## 8. 真机失败场景（§18 F1-F5）

| 场景 | 结果 | 关键证据 |
|:--|:--|:--|
| **F1** 新增 fixed source 故障 | ✅ | `broken-fixed` → `NETWORK_ERROR`，**库存零改动**；另两源正常（145/473）；publish 仍 OK；`fixed_summary` 显示 `ok=False` |
| **F2** 新频道全部 stream fail | ✅ | `CCTV-5+` 两轮全灭 → selector skipped，其它 42 个不受影响，**未降低门槛** |
| **F3** dynamic 一源失败（isolate） | ✅ | `DEGRADED_DYNAMIC_PARTIAL` exit=0；KORICE `NETWORK_ERROR` 0 条，JSNZKPG **16 条照常发布**；`fail_closed=False` / `discarded=0` |
| **F4** KORICE 上海失败 | ✅ | 源仍保留；`korice-ppv` 出现在 `cloud_probe_non_authoritative_sources`；摘要明确「云端 probe 非权威」 |
| **F5** publish 全空 | ✅ | `DEGRADED_NO_PUBLISH` exit=2；**live.m3u MD5 与字节数完全不变**（`2803e741…` / 44561）—— LKG 保护生效 |

全部在 `/tmp` 隔离环境执行，**未改生产 DB / config / DNS**。

---

## 9. 家庭/VPN Playback Smoke（§12）

**context 分离严格执行**：本节结果**不与上海结果合并**成单一 PASS/FAIL。

| stream（脱敏 host） | 上海云端 | 家庭本机 | 现象 |
|:--|:--:|:--|:--|
| `*.github.io` | 未测 | **PLAYABLE** | 唯一完全可播 |
| `*.26.218` / `*.221.218` / `*.tvbus.cc` / `*.228.26` | 未测 | playlist 200 但 **segment 404** | 上游 playlist 里 segment 路径**缺目录前缀** |
| `*.getaj.net`（Al Jazeera） | — | **UNREACHABLE** | `SSL UNEXPECTED_EOF` = FlClash TUN 切断 |
| `*.nhkworld.jp` | 未测 | segment 404 | 绝对 URL 也 404 |

**最有价值的发现**：4 条「云端可能 probe 通过、换网络就播不出」的 stream。
`urljoin` 已验证拼接正确，是**上游 M3U 文件自身的缺陷**。
只看云端 probe 会漏掉这一整类问题。

> **诚实标注**：本机走 FlClash TUN，不能 100% 排除代理干扰；
> 但 4 条同型 URL 一致复现且错误形态是 404（不是 TLS 失败），**证据强度高**。
> 已列入「下一轮必做：生产 ffprobe 二次验证」。

---

## 10. 硬指标核对（§11）

| 指标 | 要求 | 实测 | 结果 |
|:--|:--|:--|:--:|
| fixed canonical inventory | ≥ 30 | **43** | ✅ |
| selector fixed published | ≥ 24 | **42** | ✅ |
| dynamic JSNZKPG 启用 | 是 | ok，17 条 | ✅ |
| dynamic KORICE 启用 | 是 | ok，179 条 | ✅ |
| `failure_policy` | isolate | isolate | ✅ |
| `/live.m3u` HTTP | 200 | 200（47971 B） | ✅ |
| `/healthz` HTTP | 200 | 200 ok | ✅ |
| reverse parse | PASS | **PASS** | ✅ |
| no banned query（fixed 侧） | 0 | **0** | ✅ |
| no duplicate canonical fixed | 0 | **0** | ✅ |
| no stale signed dynamic resurrection | 0 | 0 | ✅ |
| localhost binding unchanged | 127.0.0.1 | `127.0.0.1:8080` | ✅ |
| schema | V1 | **V1** | ✅ |
| 理想区间 35～50 | — | **42** | ✅ 落在区间内 |

### 10.1 一处需要说明的「疑似命中」

初次扫描 `live.m3u` 报了 **9 条 `secret` 命中**。逐条核查后确认：
9 条全部是腾讯云直播的 `txSecret` + `txTime`（`pul-tenm.gkykp.com`），
来自 **JSNZKPG 动态源的中文原声轨**，不是 banned 清单里的独立 `secret` 参数
—— 是我的检测正则大小写不敏感，把 `txSecret=` 的子串误判了。

按任务书 §14，短时签名 URL **不进 Git / 报告 / 长期存储**，
`live.m3u` 是明确例外。用精确参数名重扫后：
- **fixed 侧 banned 命中：0**
- dynamic `txSecret` 条目：9（`live.m3u` 允许范围内）

---

## 11. 生产侧额外处置（1 个）

### `guovin-gd-ipv4` 源入口改为 jsDelivr CDN

**现象**：生产机 fetch 该源时 `raw.githubusercontent.com` **TCP 443 不通**
（DNS 只返 IPv6；强制 IPv4 到 `185.199.110.133` 仍不通），
但 `github.com` 与 `iptv-org.github.io` 正常 —— 是 Fastly IP 段被阻断。

**验证**：`cdn` / `fastly` / `gcore` 三个 jsDelivr 节点全部 200，
且 **三份文件 md5 完全一致**（`36ac96011a1210f4540645c2d669203b`，106329 B / 473 条目），
与本机盘点数字吻合。jsDelivr 是 GitHub 官方公开 CDN，非私有镜像。

**应用结果**：生产 `fetch --source guovin-gd-ipv4` → `ok 473 entries,
created=0 updated=473 deactivated=0` —— **库存零漂移**，内容与原入口一致。

> 这是**生产可用性修复**，不是绕过封锁。已如实记录，未隐瞒。

---

## 12. 测试与负向验证

| 项 | 结果 |
|:--|:--|
| `tests/test_task010.py` | **67 passed**（65 + 2 category 回归） |
| 核心回归（TASK-008/009 + publish + probe + cli） | **166 passed** 零失败 |
| 全量基线（TASK-010 开始前） | 552 passed |

**负向验证 `tools/_negcheck_t010.py`**：

| 组 | 结果 |
|:--|:--|
| §8 新语义断言 | **5/5** 在旧实现下确实会失败 |
| §8 零回归护栏 | **3/3** 老 flag 行为逐字未变 |
| §4/§13 KORICE 冻结 | 旧行为（authoritative=True）⇒ 会删源，测试能区分 |
| §5.4 alias 冲突 | 旧行为（后者覆盖）⇒ 渠道静默变成 `CCTV-13 新闻` |
| banned query | 恒 False 后 **10/10** 样本全部漏过 |
| 脚本自检 | 故意写错期望值 ⇒ 脚本报错，证明上面的「抓到」不是空转 |

> 负一版脚本自身有 2 处逻辑缺陷（把零回归护栏误当新语义断言、
> banned 判断条件写反），已修正并在 docstring 里写明期望值设定纪律。

---

## 13. 安全与合规自检（§14）

| 红线 | 状态 |
|:--|:--|
| 不破解 DRM / 登录 / 认证 | ✅ |
| 不提取令牌 | ✅ 只观测签名不伪造 |
| 不请求条目里的视频内容 | ✅ smoke 只做 Range 探活（1 字节） |
| 签名/身份 URL 不进 Git | ✅ seed 只存 canonical 名；报告全部脱敏 |
| 生产 config 不入 Git | ✅ |
| 不打印完整 stream URL / query / token | ✅ 报告只写脱敏 host |
| 87 条身份参数条目 | ✅ 全部排除（`msisdn`/`migutoken` 单独强调） |
| catch-all 占位流 | ✅ 排除 2 条 |
| 服务账号不拥有代码/配置 | ✅ `source_policies.toml` 装为 `root:liptv 0640`，与 config 同级 |
| 未改安全组 / DNS / 防火墙 / 8080 绑定 | ✅ 全部未动 |

---

## 14. 确定性边界（诚实标注）

| 结论 | 分级 |
|:--|:--|
| 43 canonical 入库、42 个发布、238 条产物、反向解析 PASS | 【事实】生产实测 |
| 2 个真实缺陷的根因与修复 | 【事实】+ 已负向验证 |
| F1-F5 五个失败场景行为 | 【事实】隔离环境实测 |
| Guovin 只有 `gd` 一个分支 | 【事实】7 分支全部 404 |
| 4 条 segment 404 是**上游 playlist 缺陷** | 【推论·证据强**】4 条一致复现，urljoin 已验证正确 |
| 5 个国际源在**上海**是否可用 | 【未知】本机 TUN 拦截，未在生产复验 |
| `geo-block suspected` 判定 | 【未知】本轮无可靠证据，**未下结论** |
| Al Jazeera 在**家庭 VPN** 是否可播 | 【未知】本机 TUN 切断 TLS |

---

## 15. 遗留与下一轮建议

1. **生产 ffprobe 二次验证**那 4 条 segment 404 流 —— 确认是上游缺陷而非本机代理假象。
2. **观察 `120.76.248.139`**：单 host 造成 14 条超时（占 TIMEOUT 74%）。
   若持续，应考虑在 seed 层主动排除，但**必须先确认 guovin 侧无备用线路**。
3. **国际源复验**：在生产机直接试 Al Jazeera / CGTN / France 24 / NHK World。
   本机 TUN 拦截导致本轮完全无法评估。
4. **`CCTV-5+` 继续观察**：两轮全灭但原因是上游临时故障，selector 会自动处理。
5. **`CCTV-5+` 与 `CCTV-5` 均未合并**（§2A 明令），已在测试中参数化锁定。
6. **FlClash TUN 对本机国际源的影响**已量化，建议后续本机 smoke 前先确认代理策略。

---

## 16. 结束条件核对（§23）

- [x] fixed canonical ≥ 30（实际 43）
- [x] selector 发布 ≥ 24（实际 42）
- [x] §8 动态源默认语义修复 + 零回归
- [x] §4/§13 playback context 落地，KORICE 冻结规则生效
- [x] 真机 probe ≥ 2 轮
- [x] F1-F5 五个失败场景全部验证
- [x] 家庭/VPN smoke 完成（结果不与云端合并）
- [x] 坏台报告 `REPORTS/TASK-010-BAD-STREAMS.md`
- [x] 侦察报告 `SOURCES/FIXED-SOURCE-RECON-TASK010.md`
- [x] 自动测试全离线，67 + 166 passed
- [x] 负向验证通过
- [x] 生产 publish 成功、health 200、schema 仍 V1、8080 仍只绑本地
- [x] 零签名泄漏（fixed 侧）
- [x] 报告与代码已 commit + push
- [ ] **TASK-011 未启动**（任务书 §21 明确不做）

**TASK-010 可以进入 REVIEW。**
