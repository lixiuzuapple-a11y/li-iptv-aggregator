# TASK-002 Execution Report

状态：REVIEW（含 Review 01 / Review 02 定向返工，见 §10、§11）
Executor：小W（WorkBuddy）
Reviewer：大G

> 本报告与实现代码同属一个提交。下表「提交 SHA」在 push 后由紧随的一次记录提交补齐。

## 1. 环境、基线 HEAD 与变更范围

| 项 | 值 |
|---|---|
| 仓库 | `lixiuzuapple-a11y/li-iptv-aggregator` |
| 开工基线 | `15755c3d439bbc512d5ec2a2c2b13b738fc81fca`（`git pull --ff-only origin main` 后） |
| 工作分支 | `main` |
| Python | 3.13.14（venv）/ 3.13.12（managed），仅标准库运行代码 |
| pytest | 9.1.1 |
| 提交 SHA | `e90fdb2cfa86145e033b69e6893400cfa0e2317f` |

变更范围（6 个修改 + 9 个新增）：

```
 M README.md                   |  44 ++-
 M config/config.example.toml  |  38 ++
 M liptv/cli.py                | 366 ++++++++++++-
 M liptv/config.py             |  41 ++
 M liptv/repo.py               | 152 ++++++-
 M tests/conftest.py           |  60 ++
?? liptv/fetch.py               （213 行，新）
?? liptv/ingest.py              （389 行，新）
?? tests/test_fetch.py          （171 行，新）
?? tests/test_remote_ingest.py  （402 行，新）
?? tests/test_dynamic.py        （268 行，新）
?? tools/__init__.py            （2 行，新）
?? tools/mock_source_server.py  （302 行，新）
?? tools/smoke_jsnzkpg.py       （116 行，新）
?? tools/demo_local_pipeline.py （220 行，新）
```

**未新增任何数据库业务表，未修改 TASK-001 冻结的数据身份/关联设计**（`schema/schema_v1.sql` 零改动，`SCHEMA_VERSION` 仍为 1）。

## 2. fixed_m3u 获取、校验与来源生命周期

真实命令（本地 mock，无公网）：

```bash
python -m liptv init-db --config out/demo-task002/config.toml
python -m liptv source-register --from-config --config out/demo-task002/config.toml
python -m liptv fetch --all --config out/demo-task002/config.toml
python -m liptv source-status --config out/demo-task002/config.toml
```

`tools/demo_local_pipeline.py` 一次跑完全部生命周期场景，结果 **15/15 PASS**：

| 场景 | 观测 |
|---|---|
| 首次抓取 | `requested=1 created=3`，exit 0 |
| 禁用源 / 动态源被跳过 | `skipped_disabled=['demo-disabled']`、`skipped_dynamic=['demo-dynamic']` |
| 重复抓取（幂等） | `created=0 updated=3 deactivated=0`，无重复行，`first_seen_at` 保留 |
| 条目消失 | `deactivated=1`，该行 `active=0` 且**未被删除**，`first_seen_at` 仍为最初值 |
| 条目新增 | `created=1` |
| 条目恢复 | `reactivated=1`，**复用原 `source_channel.id`**（不新建行） |
| 库存统计 | `active=3 inactive=1 total=4` |
| 不同来源隔离 | A 源某行 `active=0` 时，B 源全部条目仍 `active=1`；两源同名的同 URL 条目 `id` 互不重叠 |
| 绑定与历史保留 | 条目置 inactive 后 `channel_binding` 仍在、`stream` 行仍在（仅 `status='stale'`），恢复后回到 `observed` |

失败分类 → `source.last_fetch_status` 的实际取值：

| 触发 | 分类 | 库存变化 |
|---|---|---|
| HTTP 500 | `HTTP_STATUS` | 零改动 |
| 空响应体 | `EMPTY_LIST` | 零改动 |
| 有 `#EXTM3U` 无有效条目 | `EMPTY_LIST` | 零改动 |
| HTML 错误页 | `INVALID_M3U` | 零改动 |
| 非法 UTF-8（声明 utf-8） | `DECODE_ERROR` | 零改动 |
| 慢响应（超时） | `TIMEOUT` | 零改动 |
| 超过 `max_bytes` | `RESPONSE_TOO_LARGE` | 零改动 |
| 重定向超限 / 自指循环 | `TOO_MANY_REDIRECTS` | 零改动 |

## 3. dynamic_event_m3u 临时获取与 JSNZKPG smoke

**手工 smoke（可选步骤，`tools/smoke_jsnzkpg.py`，2026-09-30 实测）**：

```
url           : https://jsnzkpg.de5.net/all.m3u
status        : OK
http_status   : 200
content_type  : text/plain (charset=utf-8)
bytes         : 21539
entry_count   : 63
has_header    : True
skipped       : {'ignored_directive': 3}
groups        : 东盟杯=2, 中冠=2, 印班超=2, 国际友谊=4, 澳NBL=2, 蒙古超=2,
                赛事回放=48, ✈️TG频道@stymei=1
```

字段样本（**播放地址已脱敏**，仅此 3 条）：

| # | 显示名 | group | tags | ext | 脱敏 URL |
|---|---|---|---|---|---|
| 2 | 东帝汶 vs 柬埔寨 1-2 [解说] | 东盟杯 | [解说] | .m3u8 | `https://hello.ooo0ooo.top/live/4639762.m3u8` |
| 3 | 东帝汶 vs 柬埔寨 1-2 [原声] | 东盟杯 | [原声] | .m3u8 | `https://pul-tenm.gkykp.com/live/hd-en-1-4639762.m3u8?<redacted:2-param(s)>` |
| 4 | 广州志诚 vs 重庆瀚达 4-1 [解说] | 中冠 | [解说] | .m3u8 | `https://hello.ooo0ooo.top/live/4601450.m3u8` |

- 本次**未播放验证任何线路**，未对条目里的播放地址发起任何请求，未核验赛事版权/重分发授权。
- 上游确实存在带短时签名参数的线路（`?<redacted:2-param(s)>` 即其 query 被整体丢弃），**未写入报告、未落库、未提交 Git**。
- 该脚本在公网不可达时输出 `NETWORK_UNAVAILABLE` 且**退出码仍为 0**；自动化测试完全不依赖它。

**动态源行为**：`persisted=False`；摘要每组字段为 `name / group_title / tags / url_redacted / url_ext / url_scheme`；`--out` 落盘的是原始 M3U，但**只允许**写入 `fetch.dynamic_tmp_dir`（默认 `out/tmp`）。

## 4. 失败隔离与事务/回滚

- **单一来源事务边界**：`ingest.ingest_fixed_source()` 用 `SAVEPOINT` 包裹「应用快照 + 写 fetch 状态」，任一步异常即 `ROLLBACK TO`，随后才单独记录失败状态。失败路径下库存零改动（测试 `test_fixed_failure_keeps_inventory_intact` 对 6 类失败逐字段比对库存快照，全部相等）。
- **绝不因失败判「消失」**：先成功、后 HTTP 500 时，已有条目全部保持 `active=1`（`test_fixed_failure_does_not_deactivate_on_http_error_after_success`）。
- **来源间互不阻塞**：`fetch --all` 逐源处理，一源失败不影响其他源；退出码：全部成功 `0`、存在失败 `1`。
- 实测退出码：`fetch` 成功 `0`、单源/批量存在失败 `1`、`dynamic-fetch` 失败 `1`、参数不合法 `SystemExit`（2）。

## 5. CLI 与配置

最短上手（全程不碰公网）：

```bash
python tools/mock_source_server.py --port 8800          # 另开窗口
cp config/config.example.toml config/config.toml
python -m liptv init-db
python -m liptv source-register --from-config           # 注册 3 个来源（2 个禁用）
python -m liptv source-add --name demo-fixed --kind fixed_m3u \
    --url http://127.0.0.1:8800/ok.m3u --enable
python -m liptv fetch --all
python -m liptv source-status
python -m liptv dynamic-fetch --source jsnzkpg-sports    # 只预览，不落库
```

新增/变更命令：`source-register`、`source-status`、`fetch --source|--all`、`dynamic-fetch --source|--url [--out]`；`source-add` 增加 `--enable/--disable`。`python -m liptv` 入口与全部 TASK-001 命令保持兼容。

**禁用来源不会被自动请求**（验收门槛 3）：

- `config/config.example.toml` 里 `demo-fixed`、`jsnzkpg-sports` **默认 `enabled = false`**；`DEFAULT_CONFIG["sources"]` 为**空列表**，照搬示例也不会产生任何默认公网请求。
- `fetch --all` 的查询条件是 `kind='fixed_m3u' AND enabled=1`；禁用源与动态源只会出现在输出的 `skipped_disabled` / `skipped_dynamic` 里。
- `fetch --source <动态源>` 直接 `SystemExit` 并提示改用 `dynamic-fetch`，避免误触发。

**动态源不会永久留在 /live.m3u**（验收门槛 3）：动态源从不写 `source_channel` / `canonical_channel` / `stream`；实测 `select --all` 返回 `selected=[] skipped=[]`，`generate-m3u` 产出 `channel_count=0` 且文件内容仅 `#EXTM3U`（`test_cli_dynamic_source_never_enters_live_m3u`）。

## 6. 逐条验收自检

| # | 验收门槛 | 结果 | 证据 |
|---|---|---|---|
| 1 | 原 70 个测试全部通过，无回归 | **PASS** | 全量 `128 passed`，其中 TASK-001 的 6 个测试文件 70 项全绿（§7） |
| 2 | 新增测试覆盖全部指定场景 | **PASS** | 新增 58 项：fixed 首抓/幂等/更新/消失/inactive/恢复/源隔离/失败不污染/大小与超时限制；dynamic 临时解析/拒绝入库/签名 URL 不泄漏（§7） |
| 3 | 两类源状态明确；禁用源不被 `fetch --all` 请求；动态源不进 /live.m3u | **PASS** | `source-status` 输出 kind+enabled+fetch 状态+active/inactive 统计；`skipped_disabled`/`skipped_dynamic` 实测；`select --all`/`generate-m3u` 无动态条目 |
| 4 | 操作可复现；失败有分类/摘要/退出码；单源更新有事务边界 | **PASS** | `tools/demo_local_pipeline.py` 15/15 PASS 可重复；8 类失败分类表；退出码 0/1 实测；`SAVEPOINT` + 回滚路径有专门测试 |
| 5 | `git diff --check` 干净；测试实际运行成功；推送后 main 与远端一致 | **PASS** | `git diff --check` exit 0（仅 LF/CRLF 提示）；`128 passed` exit 0；§9 的 SHA 比对 |

## 7. 测试

```
命令（本机受控，与 Reviewer 同构）：
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -o addopts="" -p no:cacheprovider \
    -q --basetemp=<工作区内目录>

首次交付：128 passed in 44.40s      exit=0
Review 01 返工后：141 passed in 47.13s      exit=0   （见 §10）
Review 02 返工后：151 passed in 49.47s      exit=0   （见 §11）
```

| 文件 | 项数 | 覆盖 |
|---|---|---|
| `tests/test_schema.py` / `test_m3u.py` / `test_repo.py` / `test_select.py` / `test_cli.py` / `test_identity.py` | 70 | TASK-001 原有全部（**无回归**） |
| `tests/test_fetch.py` | 17 | 成功、重定向（正常/超限/自指）、超时、超大小、解码失败、HTTP 4xx/5xx、DNS 失败、连接失败、非 http 协议拒绝、**只请求传入的那一个 URL**、loopback 绕过环境代理、limits 映射 |
| `tests/test_remote_ingest.py` | 26 | fixed 全生命周期、失败不污染（6 类参数化）、源隔离、同名跨源身份不合并、CLI 注册/抓取/退出码/状态 |
| `tests/test_dynamic.py` | 15 | 脱敏工具、临时解析、**不接触数据库**、不泄漏签名 URL（JSON 与表格两种输出）、`--out` 白名单与越界拒绝、按 `--url` 直连、不进 /live.m3u、`.gitignore` 覆盖自检 |
| `tests/test_review_qa002.py` | 13 | Review 01 回归：截断快照拒绝、库存不变、正常删台照旧、落盘路径 Git 忽略强制（§10） |
| `tests/test_review_qa002c.py` | 10 | Review 02 回归：**真实临时 Git 仓库**中已跟踪文件拒写、旧字节不变、索引回退、v4 索引、nested worktree（§11） |

独立端到端演示（非 pytest，可手工复跑）：

```
python tools/demo_local_pipeline.py      →  15/15 PASS，exit 0
python tools/smoke_jsnzkpg.py            →  status OK（200 / 63 条），exit 0
```

## 8. 已知问题与停止条件

**未实现（本轮范围外，明确不自行扩展）**：统一 `/live.m3u` 合并发布、自动调度与定时任务、多节点真实 ffprobe 网络通信、腾讯云部署、EPG / Logo 抓取、自动赛事识别、历史动态赛事库、视频转码或代理、GUI、Dashboard。

**对本轮后续 TASK 的已知约束**（不是缺陷，供 Reviewer 参考）：

1. **动态源的「过期清理」尚未实现**。本轮动态源完全不落库，因此不存在陈旧条目问题；一旦后续要让动态条目参与发布，必须先解决「失效赛事不应无限使用 last-known-good」，见 `SOURCES/JSNZKPG-SPORTS.md` 第 4 条。
2. **拉取只有总超时**（`urllib` 语义），未拆分 connect / read 两段超时；TASK-002 §交付 2 允许「connect/read **或总超时**」，本轮选后者并全部配置化。
3. **本机 pytest 环境噪声**：直接 `python -m pytest -q` 在本机会因插件自动加载超时、并在清理临时目录时撞上沙箱的批量删除保护（测试全绿但退出码非 0）。已改用与 Reviewer 同构的受控命令（关自动插件 + 固定 `--basetemp`），结果干净稳定。与实现无关。
4. 动态快照的 `--out` 是**三重强制**的（Review 01 加强第 ② 条、Review 02 加强第 ③ 条，见 §10、§11）：
   ① 写出目录必须位于 `fetch.dynamic_tmp_dir` 之内；② 目标位于某个 Git 工作树内时必须被 `.gitignore`
   忽略（不在任何工作树内则允许）；③ 目标**不得已被该工作树跟踪**（在 Git 索引里）—— 因为
   `.gitignore` 对已跟踪文件不生效。任一不满足即拒绝，且不创建文件、不改动旧文件一个字节。

**停止条件核对（`TASKS/TASK-002.md`§停止条件）**：均未触发。
- 未破坏 TASK-001 已冻结的数据身份/关联设计（`schema_v1.sql` 零改动）；
- 动态赛事源无需持久化短时签名 URL 即可实现（本轮完全落库为零）；
- 拉取失败**不可能**覆盖已成功导入的库存（事务回滚 + 6 类失败逐字段快照比对测试）；
- 未涉及破解/逆向/绕过认证、未复制第三方私密令牌、未代理视频；
- 未发现影响安全/数据完整性的不可控问题。

## 9. Git

| 项 | 值 |
|---|---|
| 提交 SHA（首次实现 + 本报告） | `e90fdb2cfa86145e033b69e6893400cfa0e2317f` |
| Review 01 返工提交 SHA | `6aecd258c7008016b74a64223fa4d815ba8748a9` |
| Review 02 返工提交 SHA | 由紧随的记录提交写入本节（见 §11） |
| 远程 `main` | 与本地一致（`origin/main` = 同一 SHA） |
| `git status --short --branch` | clean（`## main...origin/main`） |
| `git diff --check` | exit 0 |

**SHA 记录**：`15755c3..e90fdb2` 已推送。本报告与实现代码同属提交 `e90fdb2`（`main` 上的单一提交）；SHA 由紧随的一次记录提交写入本节，以便报告内自引用。

提交内容（不含任何运行期产物）：运行期目录 `out/`、`data/`、`*.sqlite3` 均被 `.gitignore` 覆盖（`git check-ignore` 实测命中 `.gitignore:10` 与 `:11`），动态快照与演示产物**未进入版本库**。

**返工 SHA 记录**：Review 01 定向返工对应的提交为 `6aecd258c7008016b74a64223fa4d815ba8748a9`
（`a3f16e8..6aecd25`，9 个文件，+787/−27）。

> 推送方式说明：本次执行期间本机到 `github.com:443` 完全不可达（`api.github.com` 正常，
> `github.com` 全部 IP 超时），常规 `git push` 无法完成。改走 GitHub 官方 REST API 通道
> **精确复刻同一提交**推送：逐文件上传 blob（9/9 SHA 与本地一致）→ 建 tree（SHA 与本地一致）
> → 建 commit（SHA 与本地一致）→ 最后才更新 `refs/heads/main`。
> **只有在 commit SHA 与本地 HEAD 完全相同时才会更新远程引用**，否则远程保持不动。
> 令牌仍只在内存中读取，未打印、未落盘、未写入 remote URL。

---

## 10. Review 01 定向返工结果（QA-002A / QA-002B）

返工基线：`a3f16e8`（Reviewer 驳回提交）。**仅定向修复两个阻断点，未重构无关模块**，
`schema/schema_v1.sql` 仍零改动，`SCHEMA_VERSION` 仍为 1。

### QA-002A — 截断的「部分成功前缀」被当成完整快照

**根因**：`liptv/ingest.py::validate_m3u_text()` 之前只要 `parsed.entry_count > 0` 就直接接受，
忽略了解析器**已经统计到**的 `skipped["extinf_without_url"]`。于是「前段完整 + 末尾 `#EXTINF` 缺 URL」
的截断正文被当作完整快照，交给 `apply_source_snapshot()` 把本次未出现的旧条目置 `active=0` —— 静默误下线。

**修复**：`entry_count > 0` **且**仍有未配播放地址的 `#EXTINF` 时，抛
`FetchError(INVALID_M3U)`，走既有失败路径（只写 `last_fetch_status`，库存逐字段零改动）。

刻意**不**用「条目数量变少」作判据，两条边界都保持原语义：

| 输入形态 | 结果 |
|---|---|
| 有条目但存在未配 URL 的 `#EXTINF`（截断） | `INVALID_M3U`，拒绝应用，库存不动 |
| 结构完整、确实缩减（正常删台） | 正常成功，消失条目照常置 `active=0` |
| 有 `#EXTM3U` 但零有效条目 | 仍为 `EMPTY_LIST`（原分类不回归） |

### QA-002B — 动态快照的 .gitignore 保护没有真正强制执行

**根因**：`liptv/ingest.py::write_dynamic_snapshot()` 只校验目标落在配置的 `allowed_dir` 之内，
而 `fetch.dynamic_tmp_dir` 可被配置覆盖成仓库内**未被忽略**的目录（如 `SOURCES/`），
带短时签名参数的完整快照就会进入 Git 跟踪范围。

**修复**：写盘前增加第二道强制校验 ——

* 目标位于某个 Git 工作树内 → 必须命中该树的 `.gitignore` 规则，否则抛 `ValueError`，**不创建任何文件**；
* 目标不在任何 Git 工作树内 → Git 不会跟踪它，允许写入（明确、可测试的策略，不是「默认放行」）。

`is_ignored_by_gitignore()` 由「只比较顶层目录名」的粗判升级为可用实现：读取工作树根及从根到目标父目录
链上每一级的 `.gitignore`，支持 `*` / `?` / `**` 通配、前导 `/` 锚定、目录模式、`!` 否定、`#` 注释，
按「最后一条适用规则」判定，并遵循「父目录被忽略 ⇒ 其中文件同样被忽略」。

### 反例重跑（`tools/qa002_repro.py`，完全离线，**6/6 PASS，exit 0**）

```
=== QA-002A：截断的部分成功前缀 ===
  ① 首次正常：created=3 active=3
  ② 截断响应：ok=False status=INVALID_M3U deactivated=0
  [PASS] 截断被拒绝（INVALID_M3U）: INVALID_M3U
  [PASS] 库存逐字段未变: rows=3
  [PASS] 无频道被静默下线: active=3/3
  ③ 完整缩减：status=ok deactivated=2
  [PASS] 完整缩减仍正常置 inactive: active={'演示新闻台': 1, '演示体育台': 0, '演示纪录台': 0}

=== QA-002B：快照落盘路径的 Git 忽略强制 ===
  ① 未忽略目录：rejected=True
     理由：拒绝写入 …\fake_repo\SOURCES\leak.m3u：该路径位于 Git 工作树 …\fake_repo 内，
           但未被 .gitignore 忽略；带短时签名参数的快照不得进入 Git 跟踪范围。…
  [PASS] 仓库内未忽略目录被拒绝且不落盘: exists=False
  ② 已忽略目录：git_worktree=…\fake_repo git_ignored=True
  [PASS] 已忽略目录允许写入: file=True

===== 汇总：6/6 通过 =====
```

### 新增永久回归（`tests/test_review_qa002.py`，13 项）

| # | 用例 | 覆盖 |
|---|---|---|
| 1 | `test_qa002a_truncated_prefix_rejected_and_inventory_intact` | 复刻 Reviewer 反例：截断 → `INVALID_M3U`，库存逐字段未变、无频道被下线 |
| 2 | `test_qa002a_truncated_via_mock_endpoint` | 同一反例走固定 mock 端点 `/truncated.m3u`，可手工复现 |
| 3 | `test_qa002a_complete_reduced_snapshot_still_applies` | **真正完整缩减仍正常置 inactive**（正常删台不能被打死） |
| 4 | `test_qa002a_validator_classification_unchanged_for_other_cases` | 分档：截断 `INVALID_M3U` / 零条目 `EMPTY_LIST` / 完整通过 |
| 5 | `test_qa002a_dynamic_preview_shares_the_same_guard` | 动态源复用同一校验，截断不作为有效预览 |
| 6 | `test_qa002a_cli_fetch_truncated_fails_without_touching_inventory` | CLI 端到端：exit 1 + 分类正确 + 库存不变 |
| 7 | `test_qa002b_rejects_unignored_dir_inside_worktree` | **负向**：仓库内未忽略目录被拒绝且不落盘（大G 要求的那条） |
| 8 | `test_qa002b_allows_ignored_dir_inside_worktree` | 已忽略目录仍允许（`out/tmp` 行为不回归） |
| 9 | `test_qa002b_path_outside_any_worktree_is_allowed` | 工作树外路径的明确安全策略 |
| 10 | `test_qa002b_allowed_dir_check_still_enforced` | 原 `allowed_dir` 边界检查未被削弱 |
| 11 | `test_qa002b_gitignore_matcher_basics` | 忽略判定本身（目录模式 / 命名目录 / 工作树外） |
| 12 | `test_qa002b_cli_rejects_unignored_tmp_dir_config` | CLI 端到端：配置覆盖成未忽略目录 → 拒绝且不落盘 |
| 13 | `test_qa002b_default_dynamic_tmp_dir_is_gitignored` | 默认 `out/tmp` 在真实仓库内确实被忽略 |

全部使用本机 mock HTTP 与临时假 Git 工作树（只建 `.git` 目录 + `.gitignore`，不触碰真实仓库），
**不依赖公网赛事源**。

### 测试结果

分组（各组独立全新 basetemp，与 Reviewer 同构的受控命令）：

| 组 | 文件 | 结果 | exit |
|---|---|---|---|
| 1 | TASK-001 六个文件 | `70 passed in 16.41s` | 0 |
| 2 | `test_fetch.py` + `test_dynamic.py` | `36 passed in 13.38s` | 0 |
| 3 | `test_remote_ingest.py` | `22 passed in 10.93s` | 0 |
| 4 | `test_review_qa002.py`（新增） | `13 passed in 2.19s` | 0 |

单命令全量（同一受控命令，固定 basetemp）：

```
141 passed in 47.13s      exit=0
```

**128 项原有测试零回归，合计 141 项全绿，单命令 exit 0。**

### 变更范围

```
 M liptv/ingest.py                    （QA-002A 校验 + QA-002B 落盘安全，净增约 100 行）
 M tools/mock_source_server.py        （新增 /truncated.m3u 端点与常量）
 A tools/qa002_repro.py               （反例证据脚本）
 A tests/test_review_qa002.py         （13 项永久回归）
 M REPORTS/TASK-002-REPORT.md
 M TASKS/TASK-002.md
 M config/config.example.toml / README.md / PROJECT_LOG.md（说明与进度）
```

---

## 11. Review 02 定向返工结果（QA-002C）

返工基线：`03354e8`（Reviewer 的 Review 02 报告提交，`TASKS/TASK-002.md` 被置为 REJECTED）。
**仅定向关闭 QA-002C 这一个安全边界，未改动网络抓取、数据模型或原任务范围**，
`schema/schema_v1.sql` 仍零改动，`SCHEMA_VERSION` 仍为 1，未引入任何 Git 框架。

### QA-002C — 已跟踪文件不能靠 .gitignore 保护

**根因**：`liptv/ingest.py::write_dynamic_snapshot()` 把「目标路径匹配 `.gitignore`」等同于
「Git 不会跟踪该文件」。但 Git 对**已经进入索引**的文件**不再应用** `.gitignore`：
先 `git add -f out/tmp/signed.m3u`，之后再往同名文件写，新的动态签名内容会直接成为
**待提交的已跟踪变更** —— 违反 TASK-002「动态快照不得误入 Git 版本库」的实际目标。

**修复**：在工作树内的目标上，**在原有 `.gitignore` 校验之上再增加一道「索引校验」**：

| 判定 | 行为 |
|---|---|
| 目标在某个 Git 工作树内，且**已被该工作树跟踪** | **拒绝写入**，不创建/不改动文件 |
| 目标在某个 Git 工作树内，未被跟踪且被 `.gitignore` 忽略 | 允许写入（默认 `out/tmp` 行为不变） |
| 目标在某个 Git 工作树内，未被跟踪但**未被忽略** | 拒绝（Review 01 的既有规则，未削弱） |
| 目标不在任何 Git 工作树内 | 允许（Git 不会跟踪它，保持原行为） |
| 在工作树内但**无法判定**是否被跟踪 | **拒绝**（无法证明安全时不放行） |

判定实现（`ingest.is_tracked_by_git()`，返回 `True` / `False` / `None`）：

1. **优先问 git 自己**：`git -C <root> ls-files --error-unmatch -- <相对路径>`。
   `stdout/stderr` 一律丢弃（`DEVNULL`）—— 判定完全不依赖任何输出内容，因此不会把仓库里的
   文件名/内容带进日志或报告。路径以**独立 argv 参数**传入（不经 shell），
   带空格的 OneDrive 路径安全；`-C <root>` 使附属工作树天然正确。
2. **回退到直接解析索引**：找不到 git 可执行文件时，读 `<git_dir>/index`（支持 v2 / v3 / v4
   路径前缀压缩，并识别 `gitdir:` 指针形态的附属工作树）。遇到不认识的版本或 split index
   （`link` 扩展）一律返回 `None` —— **读不懂就拒绝，不静默放行**。
3. `LIPTV_GIT_EXECUTABLE` / `GIT_EXECUTABLE` 环境变量可显式指定 git 路径（本机 git 不在 PATH）。

### 反例重跑（`tools/qa002_repro.py`，完全离线，**10/10 PASS，exit 0**）

```
=== QA-002C：已跟踪文件不能靠 .gitignore 保护 ===
  BEFORE_TRACKED False  (ignored=True)
  AFTER_GIT_ADD_F True
  [PASS] 被强制加入索引后判定为已跟踪: tracked=True
  WRITE_ALLOWED False
     理由：拒绝写入 …\real_repo\out\tmp\signed.m3u：该路径**已被 Git 工作树 …\real_repo 跟踪**
           （存在于其索引中）。.gitignore 对已跟踪文件不生效，…
  [PASS] 已跟踪的（虽被忽略）快照文件写入被拒绝: rejected=True
  [PASS] 旧文件原字节不变: bytes=4
  [PASS] 未跟踪且被忽略的文件仍可写: git_tracked=False

===== 汇总：10/10 通过 =====
```

（QA-002A / QA-002B 的 6 项用例同脚本内一并复跑，全部仍为 PASS。）

### 新增永久回归（`tests/test_review_qa002c.py`，10 项，**全部使用真实临时 Git 仓库**）

| # | 用例 | 覆盖 |
|---|---|---|
| 1 | `test_qa002c_real_repo_ignored_but_tracked_file_is_rejected` | **Reviewer 反例的真实仓库版**：`git add -f out/tmp/signed.m3u` 后写入被拒、旧字节不变、无新增待提交改动 |
| 2 | `test_qa002c_real_repo_untracked_ignored_file_still_writable` | 正向：未跟踪且被忽略的文件仍可写（`out/tmp` 行为不回归） |
| 3 | `test_qa002c_guard_follows_the_index_not_the_file_name` | `git rm --cached` 后同一路径恢复可写 ⇒ 判定跟着索引，不是按文件名一刀切 |
| 4 | `test_qa002c_rejects_tracked_path_with_spaces` | 仓库根/子目录/文件名**都含空格**的 OneDrive 风格路径同样拦住 |
| 5 | `test_qa002c_nested_worktree_is_detected` | `git worktree add` 出的**附属工作树**（`.git` 是文件）也能判定为已跟踪 |
| 6 | `test_qa002c_index_fallback_detects_tracked_without_git_binary` | 拿不到 git 可执行文件时，直接解析 `.git/index` 仍认得出已跟踪 |
| 7 | `test_qa002c_index_fallback_handles_v4_index` | `update-index --index-version 4` 后的 v4 索引同样正确解析 |
| 8 | `test_qa002c_unresolvable_index_state_is_refused` | 既无 git、又读不到索引 ⇒ **保守拒绝**且不落盘 |
| 9 | `test_qa002c_cli_rejects_tracked_snapshot_path` | CLI 端到端：`--out` 指向已跟踪文件 → `SystemExit` 且旧字节不变 |
| 10 | `test_qa002c_cli_writes_to_untracked_ignored_path` | CLI 端到端正向：同仓库里未跟踪的 `out/tmp` 目标仍写得进去 |

临时仓库一律建在 pytest 临时目录下，**绝不触碰本项目仓库的任何文件**；不依赖公网赛事源。

### 测试结果

分组（各组独立全新 basetemp，与 Reviewer 同构的受控命令）：

| 组 | 文件 | 结果 | exit |
|---|---|---|---|
| 1 | TASK-001 六个文件 | `70 passed in 16.42s` | 0 |
| 2 | `test_fetch.py` + `test_dynamic.py` | `36 passed in 19.00s` | 0 |
| 3 | `test_remote_ingest.py` | `22 passed in 11.47s` | 0 |
| 4 | `test_review_qa002.py` | `13 passed in 2.94s` | 0 |
| 5 | `test_review_qa002c.py`（新增） | `10 passed in 4.04s` | 0 |

单命令全量（同一受控命令，固定 basetemp）：

```
151 passed in 49.47s      exit=0
```

**既有 141 项零回归，合计 151 项全绿，单命令 exit 0。**

### 变更范围

```
 M liptv/ingest.py                    （QA-002C 索引守卫 + git/索引判定辅助函数）
 A tests/test_review_qa002c.py        （10 项真实临时 Git 仓库回归）
 M tools/qa002_repro.py               （新增 QA-002C 反例重跑，6/6 → 10/10）
 M REPORTS/TASK-002-REPORT.md
 M TASKS/TASK-002.md                  （状态回 REVIEW）
 M config/config.example.toml / README.md / PROJECT_LOG.md（说明与进度）
```

---

**下一步（等待大G 指示，不自行推进 TASK-003）：** 等 TASK-002 Review 02 返回后的第三轮独立验收。
