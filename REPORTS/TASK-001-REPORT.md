# TASK-001 Execution Report

状态：REVIEW

Executor：小W  
Reviewer：大G  
执行日期：2026-09-30  
基线提交：`3af8c6e685b7a0cd5870d28a0cd3a051377f11db`（开工时 `origin/main`）

> 本报告与实现代码同属一个提交，提交 SHA = push 后 `main` 的 tip（见 §10）。

## 1. 实现摘要

在 `3af8c6e` 基础上，从零建立 Li IPTV Aggregator 的 V1 最小工程骨架：

- **技术栈**：Python 3.11+ / 标准库 `sqlite3`（**无 ORM、无 PostgreSQL、无 Web UI**）/ `argparse` CLI / `pytest` 测试。
- **数据层**：8 张业务表 + `schema_version`，逐字段对应 `DATA_MODEL_V1.md`；所有实体用自增整型代理主键，频道名 / tvg-id / URL **一律不作主键**。
- **M3U**：最小 parser（`#EXTM3U` / `#EXTINF` / `#EXTGRP`）与 generator（原子写 + last-known-good 备份）。
- **选线**：7 日窗口成功率优先、连续失败强惩罚并设硬阈值、最近成功时间降级、启动速度次要、清晰度/码率仅在稳定性相近时参与。V1 不落 `stream_score` 表，每次从 `probe_result` 现算。
- **CLI**：16 个子命令覆盖全部验收行为。
- **测试**：`60 passed`（0 failed）。
- **端到端演示**：真实跑通「导入 → 绑定 → 归集 stream → 写 probe_result → 选线 → 生成 live.m3u」，见 §7.3。

## 2. 变更文件

新增（21 个文件，未改动任何既有文件的内容）：

| 文件 | 行数 | 说明 |
|---|---:|---|
| `schema/schema_v1.sql` | 147 | V1 DDL（8 表 + schema_version + 初始探针） |
| `liptv/__init__.py` | 12 | 包元信息 |
| `liptv/__main__.py` | 6 | `python -m liptv` 入口 |
| `liptv/util.py` | 36 | 时间/哈希工具 |
| `liptv/db.py` | 67 | 连接、schema 初始化、版本读写 |
| `liptv/config.py` | 65 | TOML 配置加载与默认值 |
| `liptv/m3u.py` | 254 | M3U parser / generator |
| `liptv/repo.py` | 375 | 数据访问（source/binding/stream/probe） |
| `liptv/select.py` | 233 | 流评分与选线 |
| `liptv/cli.py` | 549 | 命令行入口（16 子命令） |
| `config/config.example.toml` | 34 | 配置样例 |
| `pyproject.toml` | 24 | 打包与 pytest 配置 |
| `.gitignore` | 13 | 忽略 `__pycache__` / `*.db` / `out/` 等 |
| `examples/source_a.m3u` | 7 | 演示源 A |
| `examples/source_b.m3u` | 9 | 演示源 B |
| `tests/conftest.py` | 64 | 测试夹具 |
| `tests/test_schema.py` | 109 | schema 与主键约束 |
| `tests/test_m3u.py` | 172 | parser / generator |
| `tests/test_repo.py` | 164 | 导入、绑定、stream 归集 |
| `tests/test_select.py` | 193 | 评分为选线 |
| `tests/test_cli.py` | 181 | CLI 端到端 |

同时更新了 `README.md`（补充「快速上手」与最新状态，属 TASK-001 范围内）。

## 3. 数据模型实现

`PRAGMA foreign_keys = ON`；主键全部为 `INTEGER PRIMARY KEY AUTOINCREMENT`（不复用）。

| 表 | 主键 | 关键约束 / 索引 |
|---|---|---|
| `schema_version` | `version` | 记录已应用版本与时间 |
| `source` | `id` | `name` UNIQUE |
| `source_channel` | `id` | FK `source_id`→`source(id)` ON DELETE CASCADE；UNIQUE(`source_id`,`raw_stream_url`)；`raw_name` 只作数据字段 |
| `canonical_channel` | `id` | `name` **非**唯一（允许同名不同类）；索引 `category` |
| `channel_binding` | `id` | FK →`source_channel` / `canonical_channel`；UNIQUE(`source_channel_id`,`canonical_channel_id`)，实现「多 source_channel → 一 canonical」 |
| `stream` | `id` | FK `canonical_channel_id`；`url_hash` SHA-256 UNIQUE（去重辅助键，非业务主键）；`status` 默认 `observed` |
| `stream_source` | `id` | FK `stream_id` / `source_channel_id`；UNIQUE(`stream_id`,`source_channel_id`)，实现「一 stream 多来源」 |
| `probe` | `id` | `name` UNIQUE；初始两行 `shanghai-cloud` / `windows-local` |
| `probe_result` | `id` | FK `stream_id` / `probe_id`；**不设唯一约束**，保留 stream×probe 多时刻历史；索引 (`stream_id`,`checked_at`) |

四类「停止条件」关系均有原生表结构支持，**未使用任何临时字段绕过**。

## 4. CLI

运行：`python -m liptv <command> [options]`；公共参数 `--config` / `--db` / `--json`。

| 命令 | 作用 |
|---|---|
| `init-db` | 初始化 schema 并写入版本号 |
| `source-add` / `source-list` | 增/列来源 |
| `import-m3u <path> --source NAME` | 导入本地 M3U 为来源原始条目 |
| `source-channel-list` | 列出原始频道条目 |
| `canonical-add` / `canonical-list` | 增/列归一化频道 |
| `binding-add` / `binding-list` | 建立/列出 source_channel→canonical 绑定 |
| `stream-sync` / `stream-list` | 由绑定归集 stream / stream_source |
| `probe-list` | 列出测活节点 |
| `probe-result-add` / `probe-result-list` | 写入/列出测活结果 |
| `select [--canonical-id\|--all]` | 按 V1 规则选线 |
| `generate-m3u [--out]` | 生成 live.m3u（原子写） |
| `status` | 数据库概况 |

最小示例：

```bash
python -m liptv init-db
python -m liptv import-m3u examples/source_a.m3u --source "src-a"
python -m liptv canonical-add --name "CCTV-1 综合" --category 新闻
python -m liptv binding-add --source-channel-id 1 --canonical-id 1
python -m liptv stream-sync
python -m liptv select --all
python -m liptv generate-m3u
```

## 5. M3U Parser / Generator

**支持的最小字段**：`#EXTM3U` 头、`#EXTINF:<duration> [tvg-id= tvg-name= tvg-logo= group-title=],Display`（`key="value"` 与 `key=value` 两种写法）、`#EXTGRP:<group>`。

**导入行为**：显示名取「第一个引号外逗号」之后的内容（属性值内含逗号不会切错）；`#EXTGRP` 无论出现在 `#EXTINF` 之前或之后，都按「最近一次声明」在读到 URL 时回填 `group-title`（仅当 `group-title` 为空时）。统计未识别行：`ignored_directive` / `url_without_extinf` / `extinf_without_url`。

**生成行为**：固定输出 `#EXTM3U`；每条输出 `#EXTINF:-1 <attrs>,<name>` + URL；属性顺序 `tvg-id, tvg-name, tvg-logo, group-title`；生成前校验 URL 非空、`key` 不重复（拒绝生成），先校验后落盘；写出流程为「生成文本 → 备份现有为 `*.previous.m3u` → 写 `*.tmp` → `os.replace` 原子替换」。

**已知限制**：不解码 header 指令（`#EXTVLCOPT` / `#KODIPROP` 等，仅计数忽略）；不做 EPG 关联 / catchup / logo 抓取；不做模糊匹配。

## 6. Stream 选择规则

`score_streams()`：取窗口内（默认 7 天，`now` 可注入）`enabled=1` 的 stream 的 `probe_result`，按 `checked_at DESC` 计算：

- `success_rate` = 成功数 / 总数；
- `consecutive_failures` = 从最新往前连续失败数；
- `last_success_at`、`median_startup_ms`、分辨率、码率（取窗口内最近一次成功记录）。

**eligible 门槛**（不满足则不出线，并给出 `reason`）：

1. 窗口内无测活记录 → 不可用；
2. 成功次数 < `min_successes`（默认 1）→ 不可用；
3. 连续失败 ≥ `max_consecutive_failures`（默认 3）→ 不可用。

**排序键**（越小越优先，字典序）：`-success_rate` → `consecutive_failures` → `-last_success_ts` → `median_startup_ms` → `-pixels` → `-bitrate_kbps` → `stream_id`（稳定兜底）。

`select_best_stream()` 返回首个 eligible；无则返回 `None`（频道进 `skipped`）。`select_playlist()` 按「分类顺序（配置 `category_order`）→ priority 升序 → 名称」排列频道输出。

## 7. 测试

### 7.1 命令与结果

```bash
python -m pytest -q
# 60 passed in 7.91s
```

- 测试总数：**60**
- 通过：**60**，失败：**0**
- 环境：Python 3.13.14（`pytest 9.1.1`）

### 7.2 关键覆盖点

- **schema（`test_schema.py`）**：8 表齐备、`schema_version=1`、`AUTOINCREMENT` 主键、无「name/url 作主键」、外键级联、UNIQUE 约束生效、`probe_result` 允许同 stream×probe 多行。
- **M3U（`test_m3u.py`）**：属性引号/非引号、显示名含逗号、`#EXTGRP` 前置/后置回填、`tvg-*` 解析、`url_without_extinf` 等统计、生成属性顺序、空 URL 与重复 key 拒绝、原子写 + `*.previous.m3u` 备份。
- **repo（`test_repo.py`）**：来源幂等、`import-m3u` 写入 `source_channel`、`binding` 唯一、`stream-sync` 归集与 `url_hash` 去重、一 stream 多来源。
- **select（`test_select.py`）**：成功率优先、连续失败硬阈值、`min_successes`、窗口过滤、`last_success` 降级、分辨率/码率次要、`None` 分支、分类排序。
- **CLI（`test_cli.py`）**：`init-db`→`import-m3u`→`canonical-add`→`binding-add`→`stream-sync`→`probe-result-add`→`select`→`generate-m3u`→`status` 全链路，及 `--json` 输出。

### 7.3 端到端演示（真实 CLI，非单测）

两源（`examples/source_a.m3u` / `source_b.m3u`）跑完整流程，关键输出：

```
counts: {"source": 2, "source_channel": 6, "canonical_channel": 2, "channel_binding": 3,
         "stream": 3, "stream_source": 3, "probe": 2, "probe_result": 8}

select --all → selected: [(2, '五星体育', 3), (1, 'CCTV-1 综合', 1)]  skipped: []

--- out/live.m3u ---
#EXTM3U
#EXTINF:-1 tvg-name="五星体育" group-title="体育",五星体育
http://src-a.example/sports5/index.m3u8
#EXTINF:-1 tvg-id="cctv1.cn" tvg-name="CCTV-1 综合" tvg-logo="http://logo.example/cctv1.png" group-title="新闻",CCTV-1 综合
http://src-a.example/cctv1/index.m3u8

generate stats: channel_count=2, bytes=285, checksum=e057cffd…fcc628
previous 备份存在 = True
```

其中 canonical#1 下 stream#1（成功率 1.00）胜出、stream#2 降级，验证了「一 canonical 多 stream + 按稳定性选线」。

## 8. 验收目标逐项自检

| # | 验收目标 | 结果 | 证据 |
|---|---|---|---|
| 1 | 能导入一份本地 M3U | PASS | `import-m3u` + `test_repo.py` + §7.3 |
| 2 | 条目写入 SQLite | PASS | `source_channel` 6 行（§7.3 counts） |
| 3 | 两个 source_channel → 一个 canonical 绑定 | PASS | `channel_binding` 3 行；演示中 canonical#1 有 2 个来源绑定 |
| 4 | 一个 canonical 保存多条 stream | PASS | canonical#1 有 stream#1、#2（§7.3） |
| 5 | 能插入模拟 probe_result | PASS | `probe_result` 8 行（含成功/失败/超时） |
| 6 | 能按简单规则选出最佳 stream | PASS | `select` → `best_stream_id`（§7.3、§6） |
| 7 | 能生成标准 live.m3u | PASS | `generate-m3u` 输出 285 字节、校验和（§7.3） |
| 8 | 测试覆盖核心数据关系与生成逻辑 | PASS | 60 passed，见 §7.2 |
| 9 | 不用频道名或 URL 作主键 | PASS | 全表 `INTEGER PK AUTOINCREMENT`（§3） |
| 10 | 重要行为可经 CLI 完成 | PASS | 16 子命令（§4） |
| 11 | schema 与 DATA_MODEL_V1.md 一致 | PASS | 逐表比对（§3） |
| 12 | 代码不把 display name / URL / tvg-id 当主键 | PASS | `test_schema.py` 断言 + §3 |

## 9. 停止条件检查

| 关系 | 是否可表达 | 实现方式 |
|---|---|---|
| 一频道多 source_channel | ✅ 可表达 | `channel_binding` 多行指向同一 `canonical_channel_id` |
| 一频道多 stream | ✅ 可表达 | `stream.canonical_channel_id` 一对多 |
| 一 stream 多来源 | ✅ 可表达 | `stream_source` 多行指向同一 `stream_id` |
| stream × probe 历史 | ✅ 可表达 | `probe_result` 不设唯一约束，保留多时刻 |

**未发现任何一类无法表达的情况，未使用临时字段绕过。不触发停止条件。**

## 10. Git

- 分支：`main`
- 基线：`3af8c6e685b7a0cd5870d28a0cd3a051377f11db`
- 提交：本报告与实现代码同一提交；SHA = push 后 `main` tip（执行者在交接消息中报告，可用 `git log -1 --format=%H` 复核）
- push：成功（`git push origin main`）
- 工作区：`git status --short --branch` → clean

## 11. 已知问题 / 下一步

**事实记录（不扩展 TASK-002）：**

1. 本机为 **Windows + OneDrive 目录**，`import-m3u` 路径需注意中文/空格（代码用 `pathlib` 处理，未做特殊限制）。
2. `generate-m3u` 的 `out/` 已在 `.gitignore` 中忽略，输出文件不入库。
3. V1 选线为**规则**而非统计模型；`stream_score` 表按范围明确不做，分数每次现算。
4. `probe_result-add` 当前为手工/模拟写入；**多探针真实网络通信**不在本任务范围。
5. 未做 EPG / logo 抓取、Guovin 集成、Docker、定时任务 —— 均属「不包含」。

**下一步（等待大G指示，不自行推进）：** 等 TASK-001 Review 结论。
