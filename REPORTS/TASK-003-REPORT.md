# TASK-003 Execution Report

状态：REVIEW
Owner：老李
Executor：小W
Reviewer：大G
基线：TASK-002 ACCEPTED（见 [REVIEWS/TASK-002-REVIEW-03.md](../REVIEWS/TASK-002-REVIEW-03.md)）
基线 HEAD：`dc567ec2663c30da58f59a3b0041496656611551`
实现提交：`f890846b8958e07aeabb8a7d059c50d3ac66b9e1`（见第 8 节；本报告 SHA 由后续一次「记录提交」写入，未使用 amend）

---

## 0. Review 01 定向返工（QA-003A / QA-003B）

受审 HEAD `0a8267e` 被 [REVIEWS/TASK-003-REVIEW-01.md](../REVIEWS/TASK-003-REVIEW-01.md) 判为
**REJECT**，两处阻断。本轮**只修这两处**，不重构 TASK-001/002 已验收的数据身份与网络安全边界。

返工改动文件：

| 文件 | 变更 |
|---|---|
| `liptv/m3u.py` | 新增注释分区识别：`M3UEntry.section`、`ParseResult.sections`（纯新增字段） |
| `liptv/publish.py` | 默认纳入策略改排除法、回放双通道理由、fail-closed 真正收窄内容、新增 `dynamic_fail_closed`/`dynamic_discarded` |
| `liptv/config.py`、`config/config.example.toml` | 同步新默认值与说明 |
| `tools/mock_source_server.py` | 新增 `/dynamic-real-structure.m3u` 真实结构固定样本（全假 URL） |
| `tests/test_publish.py`、`tests/test_m3u.py` | 新增 6 项永久回归；2 项按新语义等价改写 |
| `tools/demo_publish_pipeline.py` | 新增 §9/§10 两段最小离线反例 |
| `README.md` | 修正纳入策略与 fail-closed 的描述 |

### 0.1 QA-003A：真实上游的赛事分组被默认过滤规则全部排除

**根因**：旧默认把 `include_groups` 当**白名单**，且写死为 `正在直播`/`即将开始`/`赛事回放`。
而真实上游（JSNZKPG）条目的 `group-title` 是**联赛名**，「正在直播 / 赛事回放」只是 M3U 内部的
**注释分区标记**（`# ===== 正在直播 =====`），从不作为条目的 `group-title` 出现 →
白名单一条都匹配不上 → 真实源被全量排除。

**真实结构依据**（2026-10-01，双方各自**只请求上游 M3U 文本**、未请求任何播放 URL）：

```
#EXTM3U
# 全部 - 更新: 2026/10/01 09:14:34
# ===== 正在直播 =====      ← 分区标记；区内条目 group-title = 联赛名
# ===== 赛事回放 =====      ← 分区标记；区内条目 group-title = 赛事回放
```

大G 当日抓到的分组统计：`✈️TG频道@stymei 1 / VTB 3 / 国际友谊 4 / 欧俱杯 4 / 欧协杯 8 /
欧女杯 4 / 欧青U21外 10 / 西亚锦 2 / 非洲杯 2 / 赛事回放 50`（88 条，其中 37 条为真赛事），
旧规则纳入 **0/88**。

**修法**（简单、配置化、可解释，**不硬编码任何联赛名单**）：

1. `liptv/m3u.py`：识别 `# ===== X =====` 形态的注释分区，把「当前分区」挂到
   `M3UEntry.section`；分区标记不再计为 `ignored_directive`（**纯新增字段，不改既有解析行为**）。
2. `liptv/publish.py`：默认策略由「白名单法」改为**排除法** ——
   先剔宣传/推广与回放，其余分组（= 各联赛名）一律保留。
   `include_groups` 默认**留空 = 不启用白名单**；显式填写时才进入严格白名单模式（退路保留）。
3. 回放识别**双通道**：分组名命中 `replay_groups` **或**落在 `replay_sections` 分区内，
   `include_replay=false` 时整类排除；两种理由分开计数（`replay_disabled` / `replay_section_disabled`），
   便于排查上游到底用哪种写法标注。
4. `liptv/config.py`、`config/config.example.toml`、`README.md` 同步默认值与理由说明。

**实测对照**（2026-10-01 09:2x，对本机实时抓取的上游文本**只做分类、不落盘**）：

| 策略 | 61 条中的纳入数 |
|---|---|
| 旧默认（白名单法） | **0 / 61** |
| 新默认（排除法） | **12 / 61** —— WNBA 4、玻利杯 2、美乙 2、美职业 2、哥伦甲 1、国际友谊 1 |

排除项：回放 48 条（`replay_disabled`）+ `✈️TG频道@stymei` 推广 1 条（`excluded_keyword`）。
（上游条数随当天赛事变化，大G 抓到 88、本轮抓到 61，属正常波动。）

### 0.2 QA-003B：多动态源有一个失败时，状态与文件内容互相矛盾

**根因**：`build_composition()` 逐个来源累积成功条目，`publish()` 后来发现
`dynamic_failed=True` 就把状态写成 `DEGRADED_FIXED_ONLY`，**却没有把其他成功来源的动态内容摘掉** →
出现「声称仅固定、`live.m3u` 里却有动态线路」。大G 复现：`FIXED 1 DYNAMIC 1 DYNAMIC_FILE True`。

**修法**：把 fail-closed 收窄放到 `build_composition()` 内部（**一处收口，状态与内容不可能再分叉**）：

- 只要本轮有任一动态来源失败 → 本轮动态条目**全部舍弃**（含其他已成功来源的），
  `channels` 只含固定频道、`dynamic_count = 0`；
- 每个来源的报告里 `included` 归零、并被舍弃数记入新增字段 `discarded`（计数如实，不吞不瞒）；
- 顶层新增 `dynamic_fail_closed` / `dynamic_discarded` 两个字段，摘要同步写入；
- 告警文案明确写出「已 fail-closed 只发布固定频道，本轮其余 N 条动态条目一并舍弃」；
- `--require-dynamic` 维持整次拒绝、当前与上一版文件**一字节不改**；
- **不**新增「部分成功」模式（大G 明确本轮不要求扩展，也禁止把它冒充 `DEGRADED_FIXED_ONLY`）。

### 0.3 新增永久回归（离线，全部打本机 mock）

| 用例 | 锁住的行为 |
|---|---|
| `test_publish_real_upstream_structure_by_default` | 真实结构固定样本（11 条，全假 URL）：默认纳入 5 条联赛分组、排除宣传/TG 推广/回放（分组名与分区两种写法）、[解说]/[原声] 各自保留、同源字节重复只留 1 条 |
| `test_publish_unknown_group_is_included_by_default` | 默认不再有白名单：未被排除规则拦下的分组必须纳入（QA-003A 的直接反断言） |
| `test_publish_allowlist_mode_excludes_groups_not_listed` | 显式配置 `include_groups` 后严格白名单模式仍可用 |
| `test_classify_dynamic_entry_default_allowlist_and_section_modes` | 纯函数层：默认/白名单/分区/`include_replay` 四种模式 |
| `test_publish_partial_dynamic_failure_publishes_fixed_only` | **QA-003B 核心**：两源一成一败 → `DEGRADED_FIXED_ONLY`、`dynamic_count=0`、`discarded=3`、**实际文件里没有任何动态线路**、摘要一致 |
| `test_publish_all_dynamic_sources_ok_keeps_dynamic` | 反向约束：全部成功时**不得**误触发 fail-closed |
| `test_parse_tracks_comment_sections` / `test_parse_section_variants_and_absence` | 解析层分区识别与「普通注释不算分区」 |

**两处测试按新语义等价改写**（测试名与断言随规格变更，非功能回归）：

- `test_publish_unknown_group_is_not_included_and_reason_recorded`
  → `test_publish_allowlist_mode_excludes_groups_not_listed`：原用例断言「不在白名单就不纳入」，
  正是被判为错误的行为；现改为**显式开启白名单**后验证同一条规则。
- `test_dynamic_preview_length_variants_are_classified`
  → `test_classify_dynamic_entry_default_allowlist_and_section_modes`：扩展为覆盖默认/白名单/分区三种模式。

### 0.4 未触碰的范围

`schema/schema_v1.sql`、`SCHEMA_VERSION=1`、TASK-002 的 `dynamic-fetch --out` 三重强制与
「截断 M3U ⇒ `INVALID_M3U`」判据、`generate-m3u` / `select` 的既有行为 —— **逐字未动**，
对应回归（`tests/test_review_qa002.py`、`tests/test_review_qa002c.py`）全绿。

---

## 1. 提交和变更

### 变更文件

| 文件 | 状态 | 变更 |
|---|---|---|
| `liptv/publish.py` | 新增（716 行） | 组合 + 校验 + 安全发布主模块 |
| `tests/test_publish.py` | 新增（805 行） | 32 项离线测试（mock server，不访问公网） |
| `tools/demo_publish_pipeline.py` | 新增（354 行） | 离线端到端演示（一条命令 22 项断言） |
| `liptv/m3u.py` | 修改（+91/-16） | `write_m3u` 事务化（临时文件 → 原子替换 → 备份回滚）；抽出公开的 `normalize_attr_value` / `normalize_channel_name` |
| `liptv/cli.py` | 修改（+127） | 新命令 `publish` + `_resolve_dynamic_sources` |
| `liptv/config.py` | 修改（+29） | 默认 `[publish]` 段 + `publish_settings()` |
| `liptv/ingest.py` | 修改（+9） | 导出 `find_git_worktree_root()`（复用既有私有实现，不复制规则） |
| `tools/mock_source_server.py` | 修改（+35） | 新增 `/dynamic-publish.m3u`、`/dynamic-alt.m3u` 两个离线样本端点 |
| `config/config.example.toml` | 修改（+32） | `[publish]` / `[publish.dynamic]` 安全默认值 |
| `README.md` | 修改（+59/-16） | `publish` 命令、退出码表、演示命令、状态更新 |

### 有意**未**改动的部分

- `schema/schema_v1.sql` 与 `SCHEMA_VERSION=1`：**零改动**。`publish` 只读 SQLite，**不新增业务表**。
- TASK-002 冻结的不变量：`dynamic-fetch --out` 的**三重强制**（在 `dynamic_tmp_dir` 内 + 被 `.gitignore` 忽略 + 未被 Git 索引跟踪）与「截断 M3U ⇒ `INVALID_M3U`」逐字保留（`tests/test_review_qa002.py`、`tests/test_review_qa002c.py` 共 23 项全绿）。
- `generate-m3u` / `select` 行为：未变成联网命令，输出与退出码不变。

---

## 2. 本轮功能

`python -m liptv publish` 是本轮唯一新增入口，把两类来源合成**一个**本地 M3U：

1. **固定频道** —— 直接复用 TASK-001 的 `select.select_playlist`：按历史 `probe_result` 选线、每个 canonical 最多一条、按 `category_order` 排序、**不绕过最低成功阈值**（从未探测过的 stream 不会出现在输出里）。
2. **动态赛事** —— 复用 TASK-002 的 `ingest.preview_dynamic_source`（超时/字节上限/重定向上限 + M3U 结构校验）。只取**本轮实际获取成功**的条目；统一归入独立分组（默认 `体育赛事（实时）`）；保留比赛名与 `[解说]` / `[原声]` 区别，**不合并**。

纳入规则（`[publish.dynamic]`，简单 / 配置化 / 可解释，不做复杂赛事识别）。
默认走**排除法**（Review 01 QA-003A 修正，理由见 §0.1）：

| 顺序 | 规则 | 命中结果 |
|---|---|---|
| 1 | `exclude_groups`（精确匹配 `宣传`/`公告`/`推广`/`广告`） | 排除，理由 `excluded_group` |
| 2 | `exclude_group_keywords`（`✈️`、`TG频道`、`TG 频道`、`下载`、`app`，大小写不敏感） | 排除，理由 `excluded_keyword` |
| 3 | 分组名命中 `replay_groups`，且 `include_replay=false` | 排除，理由 `replay_disabled` |
| 4 | 条目落在 `replay_sections` 注释分区内，且 `include_replay=false` | 排除，理由 `replay_section_disabled` |
| 5 | `include_groups` **非空**时，分组不在其中 | 排除，理由 `group_not_in_include_list`（**留空则不启用白名单**） |
| 6 | 同源内 `(URL, 显示名, 原始分组)` 字节完全相同 | 排除，理由 `duplicate_byte_identical`（记录 `duplicate_of`） |

即：**没被 1–5 拦下的分组（= 各联赛名）默认纳入**。回放识别为「分组名 / 注释分区」双通道，
两种写法都能认。

去重作用域严格限制在**同一个动态来源内**：不跨来源去重、不跨固定频道去重、不合并 `[解说]`/`[原声]`。

输出为单个合法 `#EXTM3U`，每个 `#EXTINF` 与 URL 成对；生成物本身会被**反向解析二次校验**（头、条目数、URL、显示名、分组逐条比对）。

---

## 3. 发布可靠性

### 写盘事务（`m3u.write_m3u` 最小兼容修复）

`write_m3u` 现在保证：

1. 先写同目录临时文件（**不碰 `live.m3u`**）；
2. 若要保留上一版：先把**当前** `live.m3u` 原子挪到 `prev_tmp` 再 `os.replace` 成 `live.previous.m3u`；
3. 再 `os.replace(tmp, target)` 落到目标；
4. 若第 3 步失败 → `_restore_previous()` 把备份**回滚**回去，不留半更新；
5. `finally` 清理临时残留。

旧 API（位置参数、返回值键）保持不变。

### 发布状态机与退出码

| 状态 | 退出码 | 触发条件 |
|---|---|---|
| `OK` | 0 | 固定 + 动态都成功 |
| `DEGRADED_FIXED_ONLY` | 0 | 动态源失败，但固定非空 → fail-closed 只发固定 |
| `DRY_RUN` | 0 | `--dry-run`，只校验不写盘 |
| `DEGRADED_NO_PUBLISH` | 2 | 动态失败**且**固定为空 → 不发布、不覆盖 |
| `REJECTED_DYNAMIC_REQUIRED` | 1 | `--require-dynamic` 且动态失败/缺失 |
| `REJECTED_VALIDATION` | 1 | 组合/反向校验未通过（含全空结果） |
| `REJECTED_IO` | 1 | 写盘异常 → 当前与上一版字节不变 |

所有拒绝/降级路径都在**写盘之前**判定；`--dry-run` 与拒绝路径的 `expected_checksum` 均已计算但未落盘。

### 发布摘要

默认 `out/publish-summary.json`（`.gitignore` 覆盖，实测 `git check-ignore` 命中 `out/`）。字段：`published_at` / `status` / `fixed_count` / `dynamic_count` / `channel_count` / `dynamic_sources`（含抓取状态、耗时、纳入/排除计数与样本）/ `dynamic_excluded_by_reason` / `warnings` / `output_path` / `checksum` / `bytes` / `exit_code` / `note`。

**写入摘要本身也走运行期产物护栏**：若目标在 Git 工作树内却未被忽略、或已被跟踪 → 直接拒绝写入（`tests/test_publish.py::test_runtime_output_guard_rejects_git_tracked_location`）。摘要写失败只记 `summary_error`，**不影响已经发布的 `live.m3u`**。

---

## 4. 动态节目失效语义

| 场景 | 本轮行为 | 是否复用旧动态线路 |
|---|---|---|
| 动态源成功、有合格赛事 | `OK`，写入本轮动态线路 | —— |
| 动态源**成功但零合格条目**（全是推广/回放/白名单外） | `OK`，只写固定频道 + `warnings` 记「按动态空集处理」并附**主要排除原因与条数** | **否** |
| **任一**动态源失败，固定非空（含「其余来源成功」的部分失败态） | `DEGRADED_FIXED_ONLY`（exit 0），**本轮动态条目全部舍弃**、只写固定频道 | **否**（明确不复用上一次签名 URL） |
| 动态源失败 + 固定为空 | `DEGRADED_NO_PUBLISH`（exit 2）+ `risk` 告警 | **否** |
| `--require-dynamic` 且动态失败 | `REJECTED_DYNAMIC_REQUIRED`（exit 1），**文件一字节不改** | **否** |

> Review 01 QA-003B 之后，上表第三行是**真的**「只写固定频道」：舍弃发生在组合阶段
> （`channels` 与 `dynamic_count` 同时收窄），而不是只改状态字符串。摘要里的
> `dynamic_fail_closed` / `dynamic_discarded` 与 `dynamic_sources[*].included/discarded`
> 让「状态 / 计数 / 实际文件」三者可以互相核对。

「不复用」有两条独立回归：`test_publish_dynamic_failure_degrades_to_fixed_only`（失败场景）与 `test_publish_does_not_reuse_previous_file_dynamic_entries`（本轮动态空集场景，先发一版带动态线路、再断言旧动态 URL 不出现在新文件里）。

> ⚠️ **静态文件局限（不掩盖）**：本轮产物是**单次静态文件**。在下一轮 `publish` 命令执行前它**不会自动过期、也不会自动刷新**，因此**不是**「24 小时可用的稳定订阅」。动态赛事本身带短时签名（`txSecret`/`txTime`），播放器读取的文件一旦过期只能等下次手动/调度重跑。这条限制已写进 `publish` 返回值的 `note` 与发布摘要，并留待后续调度/服务任务解决。

---

## 5. 离线 E2E 和逐条验收

全部离线：固定源与动态源都打本机 mock HTTP 服务（`tools/mock_source_server.py`），**不访问任何公网地址**，也不依赖 JSNZKPG 在线或任何真实流可播。

### 5.1 独立演示脚本（Review 01 后 28/28 通过）

```bash
python tools/demo_publish_pipeline.py
```

实际输出（真实记录，产物落在 `out/demo-task003/`，已被 `.gitignore` 忽略）：

```
mock server : http://127.0.0.1:22226
=== 0. 初始化与来源注册 ===
  [PASS] init-db: exit=0
  [PASS] source-register（默认禁用动态源）: registered=4 enabled=1 disabled=3
=== 1. 固定源：抓取 → 归一化 → 写入模拟测活结果 ===
  [PASS] fetch --all（只抓 enabled 的 fixed 源）: exit=0 requested=1 created=3
  [PASS] 固定侧准备完成: canonical=3 stream=3 probe_result=3
=== 2. 只发固定频道（publish 默认完全不联网） ===
  [PASS] publish（无动态）: exit=0 status=OK fixed=3 dynamic=0
  [PASS] 输出结构合法（头 + 3 对 EXTINF/URL + 分类顺序）: header=True entries=3 groups=['体育', '新闻', '纪录片']
  [PASS] 默认路径不写 previous（首次发布）: previous=None
=== 3. 固定 + 动态赛事统一发布（离线 mock 动态源） ===
  [PASS] 两类条目齐备、固定在前、动态独立分组: exit=0 fixed=3 dynamic=3
         groups=['体育','新闻','纪录片','体育赛事（实时）','体育赛事（实时）','体育赛事（实时）']
  [PASS] [解说] / [原声] 各自保留（不合并）:
         names=['演示体育台','演示新闻台','演示纪录台','[解说] 曼城 vs 阿森纳','[原声] 曼城 vs 阿森纳','[解说] 皇马 vs 巴萨']
  [PASS] 每 canonical 只输出一条线路: fixed_entries=3
  [PASS] 上一版被留到 live.previous.m3u: previous_exists=True
=== 4. 纳入/过滤规则的可解释计数 ===
  [PASS] 同源字节重复去重 1 条: fetched=7 included=3 duplicate=1
  [PASS] 回放默认关闭 + 宣传/TG 推广被排除: replay_disabled=1 excluded_group=1 excluded_keyword=1
=== 5. 动态源失败 → fail-closed 降级（不复用旧签名线路） ===
  [PASS] 降级为只发布固定频道（exit 0）: exit=0 status=DEGRADED_FIXED_ONLY published=True
  [PASS] 新文件里没有动态分组、也没有旧签名材料: 动态分组出现=否
=== 6. --require-dynamic → 整次拒绝，文件字节不变 ===
  [PASS] 整次拒绝（exit 1）: exit=1 status=REJECTED_DYNAMIC_REQUIRED
  [PASS] 当前与上一版文件一个字节都没改: live未变=True previous未变=True
=== 7. --dry-run 不写任何文件 ===
  [PASS] dry-run 只组合与校验: exit=0 status=DRY_RUN expected_checksum=7494c99f1ac1…
  [PASS] live.m3u 与摘要文件均未被改写: live未变=True 摘要未变=True
=== 8. 信息边界：签名 URL 只存在 live.m3u ===
  [PASS] stdout JSON 与发布摘要都不含签名参数 / playpath: 脱敏口径：只留 scheme://host，path 与 query 一律抹掉
  [PASS] 播放器要读的 live.m3u 里线路原样保留（否则没法播）: live.m3u 内保留完整线路
  [PASS] 摘要含监测字段（计数/过滤/来源/checksum/退出码）
=== 9. QA-003A 反例：真实上游结构（联赛名分组 + 直播/回放注释分区） ===
  [PASS] 默认策略纳入各联赛分组（旧版本这里会是 0/N）: fetched=11 included=5 dynamic=5
  [PASS] 宣传/TG 推广/回放（分组名与分区两种写法）都被排除、理由可解释:
         labels={'…重复…':1, '命中排除分组（宣传/公告/推广/广告）':1, '分组名命中排除关键词（推广入口）':1,
                 '回放分组默认关闭（include_replay=false）':2, '位于回放注释分区（include_replay=false）':1}
  [PASS] 分区被识别；[解说]/[原声] 保留；回放与推广不落盘: sections={'正在直播': 8, '赛事回放': 3} names=8
=== 10. QA-003B 反例：多动态源部分失败 → 状态与文件内容一致 ===
  [PASS] 状态=仅固定，且 dynamic_count=0 / fail-closed 被如实记录:
         exit=0 status=DEGRADED_FIXED_ONLY discarded=3
  [PASS] 实际文件里没有任何动态线路（旧版本这里会残留成功来源的动态条目）: entries=3 动态分组出现=否
  [PASS] 摘要与状态一致（dynamic_count=0 / fail_closed=true）: summary_status=DEGRADED_FIXED_ONLY
===== 汇总：28/28 通过 =====
EXIT=0
```

> §9/§10 是本轮 Review 01 返工新增的**最小离线反例**：§9 复现「真实上游结构被正确处理」，
> §10 复现「多源部分失败时状态与文件内容一致」——两者在返工前都会 FAIL。

生成的统一列表（`out/demo-task003/out/live.m3u`，固定在前、动态独立分组）：

```
#EXTM3U
#EXTINF:-1 tvg-name="演示体育台" group-title="体育",演示体育台
http://stream.invalid.example/sports/index.m3u8
#EXTINF:-1 tvg-name="演示新闻台" group-title="新闻",演示新闻台
http://stream.invalid.example/news/index.m3u8
#EXTINF:-1 tvg-name="演示纪录台" group-title="纪录片",演示纪录台
http://stream.invalid.example/doc/index.m3u8
#EXTINF:-1 tvg-name="[解说] 曼城 vs 阿森纳" group-title="体育赛事（实时）",[解说] 曼城 vs 阿森纳
http://jsnzkpg.invalid.example/live/mci-ars/pc.m3u8?txSecret=AAA111&txTime=6A1B2C3D
#EXTINF:-1 tvg-name="[原声] 曼城 vs 阿森纳" group-title="体育赛事（实时）",[原声] 曼城 vs 阿森纳
http://jsnzkpg.invalid.example/live/mci-ars/raw.flv?txSecret=BBB222&txTime=6A1B2C3E
#EXTINF:-1 tvg-name="[解说] 皇马 vs 巴萨" group-title="体育赛事（实时）",[解说] 皇马 vs 巴萨
http://jsnzkpg.invalid.example/live/rma-bar/pc.m3u8?txSecret=CCC333&txTime=6A1B2C3F
```

> 上面全部是 `*.invalid.example` 假地址与合成签名值（`AAA111`/`BBB222`/`CCC333`），非任何真实来源；真实签名 URL 从未进入 Git / 报告 / 长期存储。

### 5.2 对应 TASKS/TASK-003.md 最低验收 1–7

| # | 要求 | 结果 | 证据 |
|---|---|---|---|
| 1 | 原 151 项零回归 + 新增跨模块/失败保护测试 | **PASS** | 全量 `189 passed`（151 基线 + 38 TASK-003，含 Review 01 新增 6 项） |
| 2 | 离线 E2E：固定有 mock probe 历史 + 动态成功 → 两类条目齐备、[解说]/[原声] 各自保留；固定优先、同频道只一条；过滤宣传与默认排除回放 | **PASS** | `test_publish_composes_fixed_and_dynamic`、`test_publish_fixed_first_and_one_entry_per_canonical`、`test_publish_filters_promo_and_keyword_groups`、`test_publish_replay_is_off_by_default_and_switchable`；演示 §3/§4 |
| 3 | 失败与时间边界：不复用旧动态签名线路；可用固定则降级；`--require-dynamic` 拒绝且原文件与备份不变；动态空集与全空都有明确输出与保护 | **PASS** | `test_publish_dynamic_failure_degrades_to_fixed_only`、`test_publish_does_not_reuse_previous_file_dynamic_entries`、`test_publish_require_dynamic_rejects_and_keeps_both_files`、`test_publish_dynamic_empty_is_treated_as_empty_set`、`test_publish_dynamic_failure_with_empty_fixed_is_degraded_no_publish`、`test_publish_empty_result_refuses_to_overwrite_existing_list` |
| 4 | 文件完整性：临时文件/备份/replace 注入；当前与 previous 不半更新；重复发布可解释并记 checksum；`--dry-run` 不写 | **PASS** | `test_publish_replace_failure_keeps_current_and_previous`、`test_write_m3u_backup_is_restored_when_target_replace_fails`、`test_write_m3u_removes_stale_previous_when_none_existed`、`test_publish_previous_file_holds_last_published_content`、`test_publish_summary_write_failure_does_not_block_publication`、`test_publish_dry_run_writes_nothing` |
| 5 | 信息边界：签名 URL 只存在于内存/最终播放列表；不进 Git/明文日志/报告；保留 TASK-002 快照 Git 约束 | **PASS** | `test_publish_output_and_summary_never_leak_signed_urls`、`test_runtime_output_guard_rejects_git_tracked_location`；TASK-002 的 `test_review_qa002c.py`（10 项真实临时仓库）全绿 |
| 6 | 两种来源调用与出版顺序可追溯、异常有分类和退出码；测试/演示不依赖 JSNZKPG 在线 | **PASS** | 摘要含逐来源 `dynamic_sources` 报告；错误分类见 §3 状态表；全部测试与演示只打本机 mock |
| 7 | `git diff --check` 干净；报告提供真实命令、全部退出码、演示记录、边界说明、Git SHA | **PASS** | 见 §3 / §5.1 / §6 / §7 / §8 |

附加检查（超出最低要求）：

- `test_publish_without_dynamic_never_hits_network` —— monkeypatch `fetch_text` 使其一旦被调用即断言失败，证明**默认路径连一次 HTTP 都不发**。
- `test_publish_skips_channels_without_probe_history` / `..._with_failed_probes` —— 证明没有合格探针历史时**不发布**，不绕过最低成功阈值。
- `test_publish_cli_registered_and_generate_m3u_still_works` —— 旧命令兼容。
- `test_publish_dynamic_source_must_be_dynamic_kind` / `test_publish_unknown_dynamic_source_is_rejected` —— 把固定源或不存在来源当作 `--dynamic-source` 会被拒绝。

---

## 6. 测试

### 受控测试环境（本机 Windows + WorkBuddy 沙箱）

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -o addopts="" -p no:cacheprovider -q \
    --basetemp="$TEMP/liptv-pytest"
```

两个必须遵守的约束（本轮实测踩到并已定位）：

1. `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` + `-o addopts=""` + `-p no:cacheprovider`：插件自动加载会超时。
2. `--basetemp` 必须指向**操作系统临时目录**（`%TEMP%`）。若指向仓库工作树内或普通工作目录，pytest 清理 ~180 个临时目录时会触发沙箱的**批量删除保护**（阈值 50/次），在 fixture setup 阶段抛 `SystemExit(1)`，把 72 个项目报成 ERROR —— **测试逻辑其实全绿，是环境假阳性**。指向仓库工作树内还会改变 `test_qa002b_path_outside_any_worktree_is_allowed` 的前提（`tmp_path` 落进 Git 工作树 → `git_worktree` 不再为 `None`）。

### 结果

| 项目 | 数量 | 结果 |
|---|---|---|
| 全量（基线 + 新增） | **189** | `189 passed in 60.31s`，exit **0** |
| 其中 TASK-001/002 基线 | 151 | 全绿（**零回归**） |
| 其中 TASK-003（含 Review 01 新增） | 38 | 全绿 |
| 演示脚本断言 | 28 | 28/28 通过，exit 0 |

Review 01 新增 6 项（`test_publish.py` +4、`test_m3u.py` +2），另有 2 项按新语义等价改写
（见 §0.3）：`183 → 189`。

退出码实测覆盖：**0**（OK / DEGRADED_FIXED_ONLY / DRY_RUN）、**1**（REJECTED_DYNAMIC_REQUIRED / REJECTED_VALIDATION / REJECTED_IO）、**2**（DEGRADED_NO_PUBLISH）—— 三者都有断言的测试。

`--dynamic.m3u`（TASK-002 的 4 条样本）**未改动**，`tests/test_dynamic.py` 的断言不受影响。

---

## 7. 偏差与风险

### 与任务书的偏差

无。未新增 CLI 之外的能力，未扩大 TASK，未触碰 schema，未部署云端。

### 已知限制与风险（如实声明）

1. **静态文件不会自动刷新**：这是本轮最本质的限制。`live.m3u` 是单次快照，`txSecret`/`txTime` 到期后必须重跑 `publish`。**不宣称**已经解决无人值守的签名过期问题，**不宣称**这是「24 小时可用的稳定订阅」。
2. **未验证真实流可播**：全部证据来自 mock/合成数据，没有做过真实 ffprobe 或播放验证。「已归一化」「已选线」不等于「实际能播」。
3. **动态纳入规则仍是启发式**：Review 01 后默认改为「排除法」（只按分组名/关键词/注释分区排除宣传与回放），
   不再依赖联赛白名单。残留风险有两个方向：上游若把**推广**换成未被关键词覆盖的新分组名会被**误纳**；
   上游若把某类**真赛事**放进名含 `回放`/`录像` 的分区会被**漏纳**。两者都可在 `[publish.dynamic]`
   里增删词表即时调整，且每次发布的**逐条理由计数**都写进摘要，便于发现词表漂移。
   本轮**未**做基于比赛时间的语义识别（不在范围内）。
4. **TASK-002 未覆盖项**：`dynamic-fetch --out` 依旧禁止把快照写成仓库内未忽略文件；`publish` 不落任何动态快照到磁盘，因此不存在新的 Git 泄漏面 —— 这条由 TASK-002 的既有回归继续守着。
5. **并发安全**：`publish` 无进程间锁。若两个 `publish` 同时跑同一 `m3u_path`，靠 `os.replace` 的原子性保证「不会出现半截文件」，但最终内容取决于谁后写。本轮不做单实例锁（不在范围内）。
6. **未做版权/再分发审查**：与 `SOURCES/JSNZKPG-SPORTS.md` 声明的原则一致，公开可访问 ≠ 拥有再分发许可。

---

## 8. Git & Gate

- `git diff --check`：**干净**（exit 0；仅有 LF→CRLF 的常规提示）。
- `git check-ignore -v` 实测：`out/live.m3u`、`out/publish-summary.json`、`out/demo-task003/out/live.m3u`、`out/tmp/x.m3u` 全部命中 `.gitignore:11:out/`；`data/liptv.sqlite3` 命中 `.gitignore:10:data/`。**运行期产物未入库**。
- 追踪文件中不含任何真实签名 URL / 真实动态整表；演示/测试里的 `*.invalid.example` 与 `AAA111` 等均为合成占位。
- 本实现提交：`f890846b8958e07aeabb8a7d059c50d3ac66b9e1`（已 push 到 `origin/main`；SHA 由后续一次「记录提交」写入报告，未使用 amend）。
- 远端核验：push 后用 GitHub 连接器**独立读取远端** `main` 的 HEAD 与提交内容，与本机一致（12 个文件、+2482/−27，与 `git diff --stat` 相符）；远端 `TASKS/TASK-003.md` 状态确认为 `REVIEW`。
- 本机 `github.com:443` 当时完全不可达（`git push` 经代理连续 5 次失败：1× `Empty reply from server` + 4× `CONNECT tunnel failed, response 502`；绕开代理直连亦 `Failed to connect to github.com:443 after 21071 ms`），故按既有预案改走 GitHub REST API **精确复刻**同一 commit（blobs → trees → commits，SHA 全等 `f890846…` 后才 PATCH ref），并以本地跟踪引用对齐收尾。

**Gate**：本轮仅回到 `REVIEW`，**不启动 TASK-004**，等待大G独立 QA。
