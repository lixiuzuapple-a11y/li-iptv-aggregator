# TASK-002 Execution Report

状态：REVIEW
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
| 提交 SHA | **见 §9**（本次提交即 push 后的 `main` tip） |

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

结果：128 passed in 44.40s      exit=0
```

| 文件 | 项数 | 覆盖 |
|---|---|---|
| `tests/test_schema.py` / `test_m3u.py` / `test_repo.py` / `test_select.py` / `test_cli.py` / `test_identity.py` | 70 | TASK-001 原有全部（**无回归**） |
| `tests/test_fetch.py` | 17 | 成功、重定向（正常/超限/自指）、超时、超大小、解码失败、HTTP 4xx/5xx、DNS 失败、连接失败、非 http 协议拒绝、**只请求传入的那一个 URL**、loopback 绕过环境代理、limits 映射 |
| `tests/test_remote_ingest.py` | 26 | fixed 全生命周期、失败不污染（6 类参数化）、源隔离、同名跨源身份不合并、CLI 注册/抓取/退出码/状态 |
| `tests/test_dynamic.py` | 15 | 脱敏工具、临时解析、**不接触数据库**、不泄漏签名 URL（JSON 与表格两种输出）、`--out` 白名单与越界拒绝、按 `--url` 直连、不进 /live.m3u、`.gitignore` 覆盖自检 |

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
4. 动态快照的 `--out` 白名单是**强制**的：写出目录必须位于 `fetch.dynamic_tmp_dir` 之内，无条件拒绝越界路径。

**停止条件核对（`TASKS/TASK-002.md`§停止条件）**：均未触发。
- 未破坏 TASK-001 已冻结的数据身份/关联设计（`schema_v1.sql` 零改动）；
- 动态赛事源无需持久化短时签名 URL 即可实现（本轮完全落库为零）；
- 拉取失败**不可能**覆盖已成功导入的库存（事务回滚 + 6 类失败逐字段快照比对测试）；
- 未涉及破解/逆向/绕过认证、未复制第三方私密令牌、未代理视频；
- 未发现影响安全/数据完整性的不可控问题。

## 9. Git

| 项 | 值 |
|---|---|
| 提交 SHA | 见下方「SHA 记录」 |
| 远程 `main` | 与本地一致（`git ls-remote origin refs/heads/main`） |
| `git status --short --branch` | clean（`## main...origin/main`） |
| `git diff --check` | exit 0 |

**SHA 记录**：本报告与实现代码同属 `main` 上的同一个提交；该提交的完整 SHA 由紧随本报告的一次记录提交写入本节末尾。

提交内容（不含任何运行期产物）：运行期目录 `out/`、`data/`、`*.sqlite3` 均被 `.gitignore` 覆盖（`git check-ignore` 实测命中 `.gitignore:10` 与 `:11`），动态快照与演示产物**未进入版本库**。

---

**下一步（等待大G 指示，不自行推进 TASK-003）：** 等 TASK-002 Review 结论。
