# TASK-012 §6 — 生产 probe history 只读审计

日期：2026-10-07
执行：小W（Executor）
对象：大G（Reviewer）
性质：**先审数据、再谈模型**。本报告只读，未改生产、未改 selector、未改 schema。
工具：`tools/audit_probe_history_t012.py`（DB 以 `mode=ro` 打开，零写、不拿写锁）

---

## 0. 一句话回答 §6 的核心问题

> **当前数据足够支撑「稳定性」判断 —— 但有一个例外区必须先说清楚。**

- 174 条 stream **全部有 ≥6 个样本**（最多的 97 个），不存在「0 样本」盲区。
- 但样本的**时间分布极不均匀**：10-06 全天 24 小时各约 504 条（scheduler 自然轮次），
  10-07 只到 06:43。**中间有约 8 小时空档**（服务在 12:31~13:15 经历配置修改与
  两次重启，属TASK-011 REVIEW-01 返工，非故障）。
- ⇒ 「样本数够」成立；**「时间连续性」有一个人为缺口**，报告里必须标注，
  不能把它当成「系统停了8 小时」。

---

## 1. 库存（口径见 §36）

| 指标 | 值 | 说明 |
|:--|--:|:--|
| `canonical_channel` 总数 | **44** | 含 France 24（TASK-011 新增） |
| `canonical_channel` enabled | **44** | 无禁用频道 |
| `stream` 总数 | **174** | |
| `stream` enabled | **174** | `status='stale'` = **0** |
| single-stream canonical | **1** | |
| multi-stream canonical | **43** | |
| cross-source multi-stream canonical | **9** | stream 层面：同一 stream 被多源提供 |

> ⚠️ **口径澄清**（§36 要求）：任务书 §1 写「当前 fixed inventory: 43 canonical」，
> 而 DB 里是 **44**。差异 = **France 24**（TASK-011 §12 新增，ccid=44）。
> 本报告一律用 DB 实测值 44，「43 fixed」指 **M3U 里带 tvg-id 的条目数**，
> 两者不是一回事。

---

## 2. Probe history 总量

| 指标 | 值 |
|:--|--:|
| `probe_result` 总行数 | **15,778** |
| 成功 | **12,749**（80.8%） |
| 失败 | **3,029**（19.2%） |
| 首次 probe | `2026-10-05T14:25:16+00:00` |
| 最近 probe | `2026-10-07T06:43:05+00:00` |
| 覆盖到的 stream 数 | **174 / 174**（100%） |

### 时间分布（决定 §7 多轮 probe 怎么排）

| 日期 | 行数 | 有 probe 的小时数 |
|:--|--:|--:|
| 2026-10-05 | 762 | 2 |
| 2026-10-06 | 11,262 | **24**（全天每小时约 504 条） |
| 2026-10-07 | 3,754 | 7（到 06:43） |

**读法**：每小时 504 ≈ 174 stream × 约 3 条/轮，说明 10-06 当时是**每小时一轮全量probe**。
这不是 TASK-012 设计的 cadence（§20 目标 fixed probe 30~60 min），而是既有 scheduler 的实际行为。

---

## 3. 样本量分桶（§6 必需项）

| 样本数 | stream 数 |
|:--|--:|
| 0 probe | **0** |
| 1 probe | **0** |
| ≥2 | **174（100%）** |
| ≥4 | **174（100%）** |
| ≥6 | **174（100%）** |
| 单条 stream 最大样本数 | **97** |

**结论**：无「样本不足」的 stream。这直接支撑 §10 的派生状态划分 ——
**`UNKNOWN`（样本不足）在本生产现状下应当极少**，若出现必有其他成因（如全部被门槛淘汰）。

---

## 4. 错误类型分布（现状 vs §9 目标 taxonomy）

**生产实际写入的 `error_type`：**

| error_type | 次数 | 在 §9 目标 taxonomy 里的对应 |
|:--|--:|:--|
| （空= 成功） | 12,749 | — |
| `TIMEOUT` | 1,907 | `TIMEOUT` ✅ 已对齐 |
| `CONNECT_ERROR` | 659 | `CONNECT_ERROR` ✅ 已对齐 |
| `INVALID_MEDIA` | 366 | `INVALID_MEDIA` ✅ 已对齐 |
| `HTTP_ERROR` | 94 | ⚠️ **未区分 4XX / 5XX** —— §9 要求拆开 |
| `PROCESS_ERROR` | 3 | ⚠️ **不在 §9 列表里** —— 需归入 `ENVIRONMENT_ERROR` |

**差距（§9 待做）**：
1. `HTTP_ERROR` 未按 4XX/5XX 拆分，而 §15 要求的「fake 200 / segment fail /
   signed downstream」区分**依赖这个分类粒度**（有 `http_status` 字段可补）。
2. `PROCESS_ERROR` 语义不明。TASK-005 已定「环境级 ffprobe failure 不得批量写失败」，
   但这里仍有 3 条落库，**需要查清是真失败还是环境抖动**。
3. §9 要求的 `HTML_FAKE` / `INVALID_PLAYLIST` / `SEGMENT_UNREACHABLE` /
   `SIGNED_DOWNSTREAM_UNREACHABLE` / `EMPTY_MEDIA` / `GEO_BLOCK_SUSPECTED`
   目前**没有任何一条被写入** —— 因为现有 probe 只做到 ffprobe 层，
   没做 §8 的 Level 3（分片 + 首包字节）。这是 §8 分层探测要补的。

> 结论：**现有 taxonomy 覆盖 ffprobe 层的常见失败，但不足以支撑 TASK-012 的
> 坏流分类诉求**。需要扩展，但**不升 schema V2**（§9 允许派生）。

---

## 5. Single-stream / multi-stream 分布

### 5.1 多线路 canonical（候选池，前 15）

| canonical | ccid | streams | 跨源数 | 已 probe |
|:--|--:|--:|--:|--:|
| CCTV-2 财经 | 1 | 6 | 2 | 6 |
| CCTV-9 纪录 | 2 | 6 | 2 | 6 |
| **France 24** | 44 | 6 | 1 | 6 |
| CCTV-15 音乐 | 3 | 5 | 2 | 5 |
| CCTV-6 电影 | 16 | 5 | 1 | 5 |
| CCTV-7 国防军事 | 17 | 5 | 2 | 6 |
| CCTV-8 电视剧 | 18 | 5 | 2 | 6 |
| 安徽卫视 | 26 | 5 | 1 | 5 |
| 山西卫视 | 39 | 5 | 1 | 5 |
| **湖南卫视** | 5 | 5 | 1 | 5 |
| **CCTV-1 综合** | 11 | 4 | 2 | 5 |
| CCTV-10 科教 | 19 | 4 | 2 | 5 |
| **CCTV-13 新闻** | 22 | 4 | 2 | 5 |
| CCTV-14 少儿 | 23 | 4 | 1 | 4 |
| CCTV-17 农业农村 | 25 | 4 | 1 | 4 |

§13 点名的 8 个（CCTV-1/5/5+/13、北京/东方/湖南卫视、France 24）**都在多线路池里**，
failover 验证有足够素材。

### 5.2 Single-stream canonical（§14）

仅 **1 个**。需在 §14 单独列出并说明能否从现有 source inventory 补备用
（**不得为凑「2 条线路 KPI」去收垃圾新源**）。

### 5.3 All-stream-failing canonical

| canonical | ccid | streams |
|:--|--:|--:|
| **CCTV-5+体育赛事** | 15 | 2 |

**只有 1 个**。这与 TASK-011 §16 的记录一致（CCTV-5+ 是已知难点），
当前 selector 已把它自然 skip —— **符合 §39「curated ≠ 必须发布」语义**。

---

## 6. Never-success stream（27 条，§6 必需项）

**27 条 stream 从未成功过一次 probe**。这是「稳定性」判断里最需要解释的一块
—— 它们目前靠 `min_successes` 门槛被排除，但**不是被排除，而是根本没成功过**。

部分列举（按 canonical）：

| canonical | stream id |
|:--|--:|
| CCTV-15 音乐 | 13 |
| CETV-1 中国教育 | 18, 21 |
| 广东卫视 | 37 |
| 山东卫视 | 41 |
| **北京卫视** | 44, 45 |
| CCTV-3 综艺 | 53 |
| **CCTV-5+ 体育赛事** | 61, 62 |
| CCTV-6 电影 | 63, 64 |
| …共 27 条 | |

**北京卫视 2 条 stream 全部 never-success** 值得注意：它有 2 条线路但都不能用。

> ⚠️ 需要区分两种情况（§6 要求「数据够不够」）：
> - **上游真的不可用**（该 URL 长期挂）→ 应自然淘汰；
> - **我们从来没真正测到它**（比如 selector 门槛导致从未进入 probe）→ 是数据缺陷。
>
> 本报告**暂不能区分**，需在 §8 分层探测时对 never-success stream 做一次
> 定向 Level 2/3 复验，才能定性。

---

## 7. Host 集中度（前 10）

| host | stream 数 | 备注 |
|:--|--:|:--|
| `63.141.230.178:82` | **34** | ⚠️ 单一 host 占 19.5% |
| `107.150.60.122` | 29 | |
| `74.91.26.218:82` | 16 | |
| **`120.76.248.139`** | **15** | TASK-011 §13 / TASK-012 §15 关注对象 |
| `198.204.228.26` | 14 | |
| `204.12.221.218:8181` | 12 | |
| `live.264788.xyz` | 7 | |
| `live.france24.com` | 5 | |
| `69.30.245.50` | 5 | |
| `207.56.13.146:81` | 5 | |

**风险**：`63.141.230.178` 承载 34 条 stream。**该 host 一次故障会同时打掉 34 个频道**，
这是「source diversity」维度上最大的单点。§13 要求报告 source diversity，
这一项必须写进去 —— 目前 selector 的 sort_key **不含 host 分散度**
（§4 冻结原则：不造新模型，但这属于「已有source diversity」是否真正被使用的核查点）。

---

## 8. 当前 selector 视角

用生产代码 `select.select_best_stream` 逐个 canonical 实跑：

| 项 | 值 |
|:--|--:|
| 当前可发布 canonical | **43** |
| 被 skip 的 canonical | **1**（CCTV-5+，ccid=15） |

> 43 published + 1 skipped = 44 total✅ 与 §1 库存一致。
> M3U 里「43 条带 tvg-id」这个数字来自 **metadata 覆盖**，与 selector 的
> 43 published **是两回事**（虽然这次巧合同为 43）—— 不要混用（§36）。

---

## 9. 结论：数据够不够支撑「稳定性」判断

| 判断维度 | 数据是否足够 | 依据 |
|:--|:--:|:--|
| 样本数 | ✅ **足够** | 174/174 有 ≥6 样本 |
| 成功/失败比 | ✅足够 | 12,749 / 3,029 |
| 连续失败分布 | ✅足够 | 可从 15,778 行现算 |
| last success age | ✅足够 | 覆盖 2 天 |
| 多线路 failover 素材 | ✅ **足够** | 43 个 multi-stream，9 个跨源 |
| all-fail canonical 样本 | ⚠️ **仅 1 个** | CCTV-5+；failover 验证可以覆盖，但「批量坏流」场景样本不足 |
| **never-success 定性** | ❌ **不足** | 27 条从未成功，**无法区分「上游死」与「我们没测到」** |
| **fake 200 / signed downstream 分类** | ❌ **不足** | 现有 probe 只到 ffprobe 层，没有 Level 3 分片验证 |
| **时间连续性** | ⚠️有缺口 | 10-07 06:43→12:31 约 8 小时空档（TASK-011 返工重启所致，非故障） |

### 据此得出的执行决定（§6「不够就先补数据，不允许先写模型」）

1. **不改 selector。** 现有 sort_key 已含 success_rate / consecutive_failures /
   last_success / startup / pixels / bitrate，§4 说「够用就别改」。
   本轮数据**没有证明**存在必须改模型的缺陷。
2. **§8 分层探测必须做** —— 这是补数据的正路：Level 2/3 才能补上
   fake200 / segment fail / never-success 定性这三块缺口。
3. **§9 taxonomy 扩展做「最小必要」**：只拆 `HTTP_ERROR → HTTP_4XX/HTTP_5XX`、
   给 `PROCESS_ERROR` 一个明确归属，**不重构**。
4. **§16CCTV-5+ 专项有真实价值**：它是唯一的 all-fail canonical，
   6 轮追踪能回答「是否自然退出」这个 §39 语义问题。

---

## 10. 本审计未做的事（明确声明）

- **未修改生产 DB**（`mode=ro`）。
- **未修改 selector / probe / publish 任何代码**。
- **未删除任何 probe history**（§32冻结）。
- **未启动 TASK-013**。
- **未动防火墙 / 安全组 / DNS / 8080 绑定**（§45 冻结）。
