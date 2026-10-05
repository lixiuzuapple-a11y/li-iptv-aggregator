# TASK-010 Fixed Source Reconnaissance — 2026-10-06

Executor：小W
Reviewer：大G
任务书：`TASKS/TASK-010.md` §3
侦察时间：**2026-10-06 00:30 ~ 00:52 (Asia/Shanghai)**
探测发起地：Windows 本机（`E:\WebCodex-Workspace\li-iptv-aggregator`）
本轮结论会由生产主机 `ev-lab-shanghai` 复验（见 `REPORTS/TASK-010-REPORT.md`）

---

## 0. 与 TASK-009 的关系：增量，不重做

任务书 §3.1 明确「不要求把同样的工作机械重做一遍」。本轮侦察分两层：

| 层 | 做法 | 结果 |
|:--|:--|:--|
| **A. 复用** | 直接拉 TASK-009 已采用的 2 个源（`iptv-org-cn` / `guovin-gd-ipv4`）**全量条目**做盘点 | 发现 **321 个非歧义归一名**，而 TASK-009 只用了 10 个 ⇒ **本轮最大机会在已有源，不在新增源** |
| **B. 增量** | 新侦察 **23 个** 候选 URL（10 + 13 两轮） | 区域源可用；Guovin 其他省份分支、fanmingming 其他变体、国际新闻官方入口**全部不可用或不可达** |

> 结论先行：**TASK-010 的 fixed 扩充 94% 来自已有源的深度盘点**，不是靠堆新源。
> 这与任务书 §1「实际可播优先于目录好看」一致。

---

## 1. A 层：已有源全量盘点（本轮核心发现）

### 1.1 盘点方法

`tools/recon_inventory.py`，对每个源做 `normalize_name` 归一后统计。

**⚠️ 盘点脚本自身踩过的坑（记录下来防止后人重犯）**：

第一版把「同源内同名多条不同 host」判成 ambiguous，结果 **12 个跨源 CCTV 全部被错杀**，
跨源数显示为 0，与 TASK-009 生产报告矛盾。

读 `build_fixed_seed.py` 第 193-205 行后确认 TASK-009 原义是：
> **同一 source 内该归一名对应 _多个不同原始名_ ⇒ 才算歧义。**

同源内同名多条不同 host **不是歧义，那正是 multi-stream（多线路）**。
修正判据后跨源数恢复为 12 → 应用 alias 后为 13。

| 指标 | 值 |
|:--|--:|
| 归一名总数（含歧义） | **321** |
| 非歧义归一名 | **321**（歧义 0） |
| 跨独立源归一名 | **12** |
| 探测时间 | 2026-10-06 00:36 |

### 1.2 两个源的条目消耗（为什么只用了 10 个）

| source | 原始条目 | 因签名/身份 query 被剔除 | 保留 | 剔除率 |
|:--|--:|--:|--:|--:|
| `iptv-org-cn` | 145 | 14 | 131 | 9.7% |
| `guovin-gd-ipv4` | 473 | 73 | 400 | 15.4% |
| **合计** | **618** | **87** | **531** | **14.1%** |

剔除明细（`dropped_by_query`）：
- `iptv-org-cn`：`auth` / `key` 类身份参数
- `guovin-gd-ipv4`：`msisdn`（移动用户身份）/ `migutoken`（移动认证令牌）类**用户身份参数**

> 87 条被剔除的**不是坏流，是带用户身份参数的流**。把它们写进 fixed 库存等于
> 把某个匿名用户的 IMSI 级标识固化进长期存储 —— **隐私红线，无条件排除**。
> 详见 `config/fixed_seed_bindings.toml` 的 banned query 键清单。

### 1.3 抽样盘点样本

| 归一名 | 出现过的原始名 | 上游 group | 源 | stream 数 | 各源 stream | 不同 host | ambiguous |
|:--|:--|:--|:--|--:|:--|--:|:--:|
| `CCTV-2` | `CCTV-2`、`CCTV-2 (720p)` | Business / 📺央视频道 | guovin + iptv-org-cn | 6 | guovin 5 / cn 1 | 6 | 否 |
| `CCTV-1` | `CCTV-1` | 📺央视频道 | guovin + iptv-org-cn | 5 | guovin 4 / cn 1 | 5 | 否 |

归一规则只剥末尾的分辨率括号（`re.compile(r"\s*\(\d{3,4}\s*[pi]\)\s*$")`），
**其余逐字节保留** —— 这是 TASK-009 冻结语义，本轮未改。

---

## 2. B 层：增量侦察（23 个 URL 实测）

### 2.1 第一轮（10 个候选）

| # | name | 入口 | HTTP | 条目 | 结论 |
|--:|:--|:--|--:|--:|:--|
| 1 | `iptv-org-news` | `.../categories/news.m3u` | 200 | 1035（clean 1030） | **淘汰**：36 条带 `token`/`hdnts`；与 iptv-org-cn 同上游重叠，无独立 stream |
| 2 | `iptv-org-hk` | `.../countries/hk.m3u` | 200 | 18 | **备选**：量小，§D 非硬指标 |
| 3 | `iptv-org-tw` | `.../countries/tw.m3u` | 200 | 26 | **备选**：同上 |
| 4 | `guovin-hd-ipv4` | `Guovin/iptv-api@hd/...` | **404** | — | **淘汰**：分支不存在 |
| 5 | `guovin-hd-ipv6` | 同上 | **404** | — | **淘汰** |
| 6 | `guovin-ah-ipv4` | 同上 | **404** | — | **淘汰** |
| 7 | `guovin-sd-ipv4` | 同上 | **404** | — | **淘汰** |
| 8 | `fanmingming-ipv4` | `live.fanmingming.com/tv/m3u/ipv4.m3u` | **404** | — | **淘汰**：TASK-009 已实测 IPv6-only 且 0/12 可达 |
| 9 | `Zhejiang-IPTV-List` | — | **404** | — | **淘汰** |
| 10 | `fanmingming-live` | — | **404** | — | **淘汰** |

> **Guovin 事实认定**：实测 `hd` / `ah` / `sd` / `sh` / `bj` / `js` / `zj` **全部 404**，
> 该项目**只有 `gd` 一个省份分支存在**。后续任何人不要再去猜分支路径。

### 2.2 第二轮（13 个候选，含 §2C 国际新闻与 §2D 区域源）

| # | name | HTTP | 条目 | 结论 |
|--:|:--|--:|--:|:--|
| 11 | `iptv-org-hk` | 200 | 18 | 备选（复验一致） |
| 12 | `iptv-org-tw` | 200 | 26 | 备选（复验一致） |
| 13 | `iptv-org-jp` | 200 | 7 | 淘汰：仅 7 条 |
| 14 | `iptv-org-kr` | 200 | 81 | 淘汰：与 iptv-org 同项目，不算独立第二源 |
| 15 | `fanmingming-ipv6` | 200 | 82 | **淘汰**：TASK-009 已实测生产 0/12 可达（IPv6-only） |
| 16 | `aljazeera-en` | **URLError** | — | 待生产复验 |
| 17 | `dw-en` | **404** | — | 淘汰：入口 URL 已变更 |
| 18 | `france24-en` | 200 | **0** | **淘汰**：HTTP 200 但 M3U **零条目** —— 典型「假 200」，不收录 |
| 19 | `nhk-world-jp` | 200 | 8 | 待生产复验 |
| 20 | `cgtn-doc` | **URLError** | — | 待生产复验 |
| 21 | `aljazeera-ar` | **URLError** | — | 待生产复验 |
| 22 | `cctv1-direct` | **URLError** | — | 待生产复验 |
| 23 | `cctv5-direct` | **URLError** | — | 待生产复验 |

### 2.3 🚨 URLError 归属：不是源的问题，是本机代理的问题

用 `tools/diag_network.py` 分层诊断（`t010_net.log` / `t010_net_direct.log`）：

| 层级 | 结果 |
|:--|:--|
| DNS 解析 | **全部 OK**（Al Jazeera / CGTN / CCTV 官方域名都能解析） |
| 清空 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` 后重试 | **仍然失败** |
| 失败形态 | `SSLEOFError` —— TLS 握手被中途切断 |

**结论**：本机 FlClash **TUN 透明代理**（`127.0.0.1:23903`）在中间切断了 TLS。
这不是环境变量问题（清空后无效），是 TUN 层拦截。

> ⚠️ **重要方法论**：**本机不可达 ≠ 上海生产机不可达**。
> TASK-009 已有先例（fanmingming IPv6 本机 200、生产 0/12）。
> 所以这 5 个 URLError 源标记为「**待生产复验**」而非直接淘汰，
> 由 `ev-lab-shanghai` 实测后再定生死。**本轮不写进 seed。**

---

## 3. 固定性判定

沿用 TASK-009 §6 方法：间隔抓取两次，比对「去 query 后 stream URL 集合」指纹。

| 源 | 判定 | 依据 |
|:--|:--|:--|
| `iptv-org-cn` | **稳定 ⇒ fixed** | TASK-009 60 s 内指纹一致，本轮复核一致 |
| `guovin-gd-ipv4` | **稳定 ⇒ fixed** | 同上 |

---

## 4. 本轮实际采用的源

| source | kind | 入口 | 采用理由 |
|:--|:--|:--|:--|
| `iptv-org-cn` | `fixed_m3u` | `https://iptv-org.github.io/iptv/countries/cn.m3u` | TASK-009 已验证；本轮贡献 131 clean 条目 |
| `guovin-gd-ipv4` | `fixed_m3u` | `https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/ipv4/result.m3u` | TASK-009 已验证；本轮贡献 400 clean 条目 |

**新增源：0 个。** 这是有意的 —— 任务书 §1「质量优先于数量」+ §5.1「不允许全量灌库然后自动猜」。

---

## 5. Seed 扩充结果

`tools/build_fixed_seed.py` 实测（`t010_plan.json`）：

| 指标 | TASK-009 | **TASK-010** | 任务书要求 |
|:--|--:|--:|:--|
| seed canonical | 10 | **43** | ≥ 30 ✅ |
| 跨独立源 multi-stream | 0 | **13** | ≥ 5 ✅ |
| ambiguous | 0 | **0** | 必须 0 ✅ |
| unmatched | 0 | **0** | — |
| alias applied | 0 | **5** | — |
| catch-all 排除 | — | **2** | 必须排除 ✅ |

### 5.1 分组分布（§9.1）

| category | 数量 |
|:--|--:|
| 卫视 | 24 |
| 央视 | 13 |
| 教育 | 2 |
| 纪录片 | 1 |
| 新闻 | 1 |
| 儿童 | 1 |
| 音乐 | 1 |
| **合计** | **43** |

§9.1 建议的 9 类中，「国际」「港澳台」两类为 0 —— 因为 §2C/§2D 源全部未通过可用性判定，
**不为凑分类而塞坏台**。「其它」为 0（43 个全部有明确归类）。

### 5.2 §2A 点名的 17 个 CCTV：**全部有数据**

CCTV-1~17 + CETV-1，无一缺失。

### 5.3 `CCTV-5` 与 `CCTV-5+` 严格不合并（§2A 明令）

`normalize_name` **不折叠** `+` 号，两个频道各自独立成条、各自独立 probe。
这是任务书点名要求，也是最容易出错的地方 —— 已在 `tests/test_task010.py` 参数化锁定。

### 5.4 刻意不收的取舍（可审计）

| 不收的东西 | 理由 |
|:--|:--|
| HD 后缀不合并（`CCTV-1 HD`） | 归一规则只剥分辨率括号，HD 是**语义后缀不是分辨率**。5 条走**显式 alias 表**（§5.4），不进归一逻辑 |
| `CCTV-5 ↔ CCTV-5+` | §2A 明令禁止合并 |
| `CCTV-6 [Geo-blocked]` | 源自己标注地理封锁 |
| `CCTV-6 ↔ CCTV-4K HD` | 频道号不同，不是一回事 |
| `CCTV-8 ↔ CCTV-8K HD` | 同上 |
| 地方市县台 | 超出「日常有意义」范围 |
| CHC 付费院线 | 需付费订阅 |
| 三沙/大湾区/内蒙古/新疆卫视 | **41 个已达标，不凑数** |
| iptv-org 家族其他国家源 | 同项目，不算独立源 |
| 国际新闻源 | 本机不可达，待生产复验（§2.3） |
| `Anhui TV ↔ 安徽卫视` | 跨语言且无实测证据，**不建 alias** |

---

## 6. 安全与合规自检（§14）

| 红线 | 本轮状态 |
|:--|:--|
| 签名/身份参数不绑定 | ✅ 87 条带 `msisdn`/`migutoken`/`token`/`auth`/`key` 的条目全部排除 |
| 不设 Cookie / Authorization | ✅ 全程匿名 `GET` |
| 不逆向 token 生成 | ✅ 遇到签名只观测不伪造 |
| 不保存媒体内容 | ✅ 只做 M3U 文本解析 + 短时 ffprobe |
| stream URL 不入 Git | ✅ seed 只存 canonical 名，URL 由源实时抓取 |
| 公开可访问 ≠ 有再分发权 | ✅ 仅个人自用 |

**banned query 键清单（10 个）**：
`token`、`auth`、`key`、`secret`、`msisdn`、`migutoken`、`sign`、`hdnts`、`expire`、`mdspid`

> 其中 `msisdn`（移动用户号码）/ `migutoken`（移动认证令牌）是**用户身份参数**，
> 比一般签名参数更敏感，单独在测试中强调。

---

## 7. 本轮侦察的确定性边界（诚实标注）

| 结论 | 分级 |
|:--|:--|
| 两个已采用源在**本机**稳定可抓、条目数与指纹一致 | 【事实】实测 |
| 321 个非歧义归一名、87 条被剔除 | 【事实】实测 |
| Guovin 只有 `gd` 一个分支存在 | 【事实】7 个分支全部 404 实测 |
| france24-en 是「假 200」（零条目） | 【事实】实测 |
| 5 个国际源在**上海生产机**是否可用 | 【未知】本机被 TUN 拦截，待生产复验 |
| 43 个 seed 在生产 probe 后的实际可播数 | 【未知】见 `REPORTS/TASK-010-REPORT.md` |

> **不夸大**：本轮 seed 从 10 扩到 43 是「库存扩充」，
> **不等于** 43 个都能播。真实可播数必须由生产 probe 给出，本报告不做任何预判。
