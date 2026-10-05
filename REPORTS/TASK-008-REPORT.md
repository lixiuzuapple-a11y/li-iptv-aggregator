# TASK-008 Execution Report

状态：REVIEW
Owner：老李
Executor：小W
Reviewer：大G
基线：TASK-007 ACCEPTED（REVIEWS/TASK-007-REVIEW-04.md，commit `89e151e`）
任务书：`TASKS/TASK-008.md`（commit `da290a9`，511 行）

## 1. 基线与变更

| 项 | 值 |
|:--|:--|
| 基线 SHA | `da290a9`（TASK-008 任务书） |
| 实现 SHA | `2a2d0c5` → 修正 `840c243` |
| 远端 main | `840c243`（`git push` 直连成功，非 API 通道） |
| 主机源码树 | `/opt/liptv-src/repo` = `840c243`（fast-forward） |
| 生产 release | `840c2439bd2b-20261005T023339Z`（previous `2a2d0c5c39a3-20261005T021107Z`） |
| 变更规模 | 7 files changed, +1515 / −24 |

变更文件：

| 文件 | 性质 |
|:--|:--|
| `liptv/config.py` | 新增 `failure_policy` 默认值 + fail-fast 校验 |
| `liptv/publish.py` | 核心：isolate 隔离、跨源去重/标签、per-source 脱敏摘要、拒绝判据分流 |
| `liptv/runtime.py` | `dynamic_failure_policy` 透传；`dynamic_summary` 进轮次结果 |
| `liptv/cli.py` | `cmd_publish` / `_build_round_fn` 传递链 + printer 增策略摘要行 |
| `config/config.example.toml` | 示例配置补 `failure_policy` 及说明 |
| `tools/mock_source_server.py` | 新增 KORICE / 跨源冲突样本端点 |
| `tests/test_task008.py` | 新建，54 项离线测试 |

**冻结模块零改动**：`liptv/schema_v1.sql`、`liptv/m3u.py`、`liptv/ingest.py`、`liptv/fetch.py`、`liptv/probe.py`、`liptv/health.py`、`liptv/doctor.py`、`liptv/deploy.py`、`liptv/backup.py`、整个 `deploy/` 目录均未触碰。`SCHEMA_VERSION` 仍为 1（真机 upgrade 后 `deploy status` 复核确认）。

## 2. Dynamic failure policy

配置路径 `[publish.dynamic].failure_policy`，取值只有两个，**非法值一律抛错、绝不静默回落**：

| 取值 | 语义 | 退出码影响 |
|:--|:--|:--|
| `all_or_nothing`（默认） | TASK-003 冻结语义：任一动态源失败 ⇒ 本轮动态整体舍弃，只发固定频道 | `DEGRADED_FIXED_ONLY`（有固定）/ `DEGRADED_NO_PUBLISH`（无固定）|
| `isolate` | TASK-008 新增：一个源失败**只丢该源**，成功源的本轮赛事照常发布 | `DEGRADED_DYNAMIC_PARTIAL`（有动态）/ `DEGRADED_NO_PUBLISH`（全空）|

关键实现点：

- 默认值 `all_or_nothing` 写在 `config.DEFAULT_CONFIG`、`publish.DEFAULT_DYNAMIC_FILTERS` 两处，**旧配置零回归**（`test_failure_policy_defaults_to_all_or_nothing`）。
- `load_config()` 与 `normalize_dynamic_filters()` **双重校验**，写错策略名在加载配置时就炸（`test_invalid_failure_policy_fails_fast_at_config_load`）。
- 新状态 `DEGRADED_DYNAMIC_PARTIAL` 映射到 `EXIT_OK`（已发布的降级），但**绝不伪装成 `OK`** —— 降级事实必须留在状态与摘要里。

### 2.1 拒绝判据按策略分流（真机 smoke B 修正）

`publish()` 的「不发布」判据在两种策略下**必须不同**，这是本轮修掉的真实缺陷：

```python
no_publish = (
    (composition.dynamic_count == 0 and composition.fixed_count == 0)
    if policy == FAILURE_POLICY_ISOLATE
    else (dynamic_failed and composition.fixed_count == 0)
)
```

- `isolate`：**本轮还有没有任何可发布内容**。成功源写出来的赛事本身就是内容，不该因为没有固定频道而丢。
- `all_or_nothing`：保持 TASK-003 原样 —— 动态失败且无合格固定频道 ⇒ 无内容可发。

## 3. Multi-source composition

`Composition` 新增 5 个字段：`failure_policy`、`dynamic_sources_selected/succeeded/failed`、`cross_source_events`。

`build_composition()` 的 isolate 分支：先把失败源的 `included`/`discarded` 双双归零（失败源一条都不许进 playlist），再对**存活源的条目继续做跨源去重与标签**（哪怕有源失败也不能跳过），并追加显式 warning 说明「只丢该源、不复用上一次的签名线路」。

### 3.1 跨源完全相同 ⇒ 精确去重

判定键 `(url, 显示名, 分组)` 三者字节级一致且来源不同 ⇒ 保留首见，记录事件：

```json
{"reason": "exact_cross_source_duplicate",
 "kept_source": "...", "duplicate_source": "...", ...}
```

不做 fuzzy matching、不做赛事实体推断、不做球队名 NLP、不做时间窗猜测（任务书明确排除项，一条都没做）。

### 3.2 同名异 URL ⇒ 只加来源标签，绝不合并

判定键 `(normalize_channel_name(显示名), 分组)`。标签由 `source_label()` 取来源名第一个非字母数字段大写：`jsnzkpg-sports → JSNZKPG`、`korice-ppv → KORICE`；标签碰撞时补 `#2` / `#3`。

**同源内部**的同名多条（例如 KORICE 同一场的 pc/alt 两条线路）**不加标签** —— 那是上游自己的显示问题，不是多源聚合产生的冲突。这一点是真机 smoke B 迭代时改产品代码（而不是改断言）定下来的。

## 4. require_dynamic 双语义

| 策略 | 判定 | 理由 |
|:--|:--|:--|
| `all_or_nothing` | `dynamic_failed or not dynamic_report` | TASK-003 原判定，旧配置零回归 |
| `isolate` | `not (dynamic_count > 0)` | 「最终至少有 1 条**本轮新鲜**动态条目进入 playlist」|

因此 isolate 下「一源失败 + 另一源成功有条目」是**满足** require_dynamic 的；固定频道**永远不能**满足它（`test_require_dynamic_not_satisfied_by_fixed_entries`）。

## 5. 隐私与脱敏

`dynamic_summary` 进 `publish-summary.json` 与 `runtime-status.json`，per-source 字段：`name / ok / status / fetched / included / discarded / duration_ms / url / error / error_category`。

- source URL **只输出到 `scheme://host`**（比 `ingest.redact_url` 更窄一层，因为发布摘要与状态文件不需要知道路径）—— 这是按任务书 §6 改的产品代码。
- `error` 脱敏 + 限长 `MAX_DYNAMIC_ERROR_CHARS = 200`。
- 源清单 bounded：`MAX_DYNAMIC_SOURCES_IN_SUMMARY = 20`，超出置 `sources_truncated=true`。

真机核验（生产 release `840c2439bd2b`）：

| 检查 | 结果 |
|:--|:--|
| `publish-summary.json` 里 `txSecret` / `token=` / `authorization` / `cookie` | **全 0** |
| `runtime-status.json` 里 `txSecret` | **0** |
| `journalctl` 里 `txsecret` | **0** |
| Git 跟踪文件里 KORICE 真实中转域名 | **零命中** |
| Git 跟踪文件里的 `txSecret` | 仅出现在脱敏正则、说明文字、测试假令牌（`AAA111` @ `invalid.example`）|

说明：`live.m3u` 本身含真实签名 query —— 它是播放器要用的订阅文件，属设计必需，不入 Git、不进报告、不进状态文件。

## 6. Offline E2E

`tests/test_task008.py` 共 **54 项**，覆盖任务书 §14 十二场景：

| 分组 | 项数 | 要点 |
|:--|--:|:--|
| 配置与零回归护栏 | 8 | 默认值、白名单、fail-fast、非字符串、旧配置回落 |
| isolate 隔离 | 8 | HTTP 500 / timeout / malformed / 截断 各自只丢该源 |
| require_dynamic 语义 | 5 | isolate 与 all_or_nothing 判定分离、固定不可满足 |
| 跨源去重与标签 | 9 | 精确去重、标签稳定、标签碰撞补 `#2`、同源不加标签 |
| 脱敏与 bounded | 3 | 无 query/token、错误限长、源清单截断 |
| 旧 URL 不复用 | 2 | 失败源不回流、组合层不读上一版文件 |
| KORICE 兼容 | 2 | 赛事类别分组无需白名单、promo 走配置化排除 |
| 其余兼容性护栏 | 4 | 退出码映射、摘要落文件、裸 `--dynamic` 不被坏源带崩 |

离线 mock 新增两个端点：`/dynamic-korice.m3u`（5 条，1 条 `Promo` 被配置化排除 ⇒ 4 条合格）、`/dynamic-collide.m3u`（与 KORICE 首条同名同组、URL 不同）。

### 6.1 负向验证（全绿不算数，必须证明测试有牙齿）

共 4 次，每次都用**反向 replace** 恢复（不用 `git checkout`，避免丢未提交改动）：

| # | 植入的错误 | 结果 |
|:--|:--|:--|
| 1 | isolate 分支短路（`if False and ...`） | **8 failed / 44 passed** |
| 2 | 禁用跨源精确去重（`previous = None`） | **2 failed / 50 passed** |
| 3 | require_dynamic 退回旧判定 | **3 failed / 49 passed** |
| 4 | 拒绝判据换回 `dynamic_failed and fixed_count == 0` | **1 failed / 53 passed**（失败的正是新增 isolate 回归；all_or_nothing 那条仍 pass，说明它守的确实是旧语义）|

恢复后残留自检：`NEG` 残留 0、`if False and` 残留 0、CR/NUL/零宽/C0 全 0。

## 7. JSNZKPG real-source smoke

- 入口：`https://jsnzkpg.de5.net/all.m3u`（源入口 URL，非签名链接）
- 真实结构：`group-title` = 联赛名，靠 `# ===== 正在直播/赛事回放 =====` 注释分区
- 通用过滤器命中情况：推广入口 1 条、回放分组 46~47 条
- 计数示例：fetched 54~58 → included 6~10（随时间窗变化）

**未**把任何底层 stream URL / query / token 粘进本报告。

## 8. KORICE real-source smoke

- 入口：`https://www.korice.eu.org/ppv_m3u.php`
- 真实结构：`group-title` = 赛事类别（`American Football` / `Basketball`），显示名含比赛双方与开赛时间
- 实际底层 URL 走 `ppv.passwdwork.us.ci` 中转域名 —— **该域名零命中于 Git**
- 计数稳定：fetched 24 → included 24
- promo 分组走 `exclude_groups` 配置化排除，证明按通用规则过滤而非硬编码

兼容性结论：两个源的分组命名风格完全不同，但都**不需要**改过滤器白名单即可正确工作 —— 通用「排除法默认 + replay/promo 配置化」够用。

## 9. Production deployment

主机：`ev-lab-shanghai`（`lhins-bukxxz3g`，ap-shanghai，Ubuntu 24.04.4）。

生产配置（`/etc/li-iptv-aggregator/config.toml`，`0640 root:liptv`，**不入 Git**）：

```toml
[publish]
dynamic_sources = ["jsnzkpg-sports", "korice-ppv"]
[publish.dynamic]
failure_policy = "isolate"
[runtime]
interval_seconds = 900          # 15 分钟刷新
stale_after_seconds = 2700
include_dynamic = true
dynamic_sources = ["jsnzkpg-sports", "korice-ppv"]
```

两个源已登记进数据库（`jsnzkpg-sports` id=1、`korice-ppv` id=2，均 `enabled=1`）。原始配置备份在 `config.toml.bak-t008`。

部署链路：

```
git push main (840c243)
  → 主机 /opt/liptv-src/repo fetch + ff-only
  → deploy upgrade（doctor → resolve-release → backup → release → health）
  → systemctl daemon-reload + restart
  → unit PYTHONPATH 指向 releases/840c2439bd2b-20261005T023339Z
```

- upgrade 结果：`OK`，18 个路径 changed / 10 skipped，DB 备份已生成（`bytes=106496 kept=5`），schema 仍 V1
- 服务：`active/running`、`NRestarts=0`、`Result=success`
- 端口：8080 仍只绑 `127.0.0.1`，**未**改安全组 / DNS / 公网暴露

### 9.1 一次配置误操作及其修复（如实记录）

在准备隔离 smoke 环境时，我误把 `/tmp/cfgup.py` 的参数顺序当成「(段, 键, 值)」，实际是「(文件, 段, 键, 值)」，结果在该脚本里**写死的生产 config 路径**上误追加了 5 行垃圾（`database = path` 等裸行）。

- 发现方式：upsert 返回 ok 但 grep 不到预期值 ⇒ 立即比对字节数（3791 → 3881）
- 修复：从 `config.toml.bak-t008` 重建 + 用参数正确的 `cfgup2.py` 重放 TASK-008 的 6 项真实改动 ⇒ 恢复副本 3791 字节、无残留裸行，与污染前**逐字节同尺寸**
- 换回后再次核对：字节数、`0640 root:liptv`、文件尾部、关键键值全部正确
- 之后所有隔离 smoke 改用参数正确的脚本与**独立的 `/tmp/t008s/` 配置**，不再触碰生产 config
- 生产配置内容最终未受污染，但**事故过程与修复步骤如实记录在此**

## 10. Production dynamic smoke

四组 smoke 全部在真机执行。B/C/D 跑在**隔离环境**（`/tmp/t008s/`：独立 DB 副本 + 独立 config + 独立输出目录），通过把 DB 副本里某个源的 URL 指向本机明确失败端点 `http://127.0.0.1:9/...`（connection refused）制造故障 —— **不改公网 DNS、不改生产 DB、不改生产 config**。

### smoke A · 双源真实抓取（生产 scheduler 自动轮次 + 隔离手工轮次）

| 项 | 值 |
|:--|:--|
| status | `OK`，exit 0，published=true |
| JSNZKPG | ok，fetched 57 → included 10，9742 ms |
| KORICE | ok，fetched 24 → included 24，883 ms |
| cross_source_events | 0 |
| live.m3u | 34 条 / 6930 字节，全部在「体育赛事（实时）」分组 |
| `[解说]` / `[原声]` | 都保留，未合并 |

隔离基线轮同样 `OK`、`isolate selected=2 ok=2 failed=0`。

### smoke B · 单源失败（JSNZKPG 失败，KORICE 存活）—— **本轮发现真实缺陷**

第一次跑出 `status=DEGRADED_NO_PUBLISH`（exit 2），KORICE 已抓到的 24 条赛事被整个丢弃。根因见 §2.1：拒绝判据的前半段是 all_or_nothing 旧条件，没有对 isolate 收窄；而生产现状恰恰没有合格固定频道，等于 isolate 在真机上完全失效。

修正后重跑：

| 项 | 值 |
|:--|:--|
| status | **`DEGRADED_DYNAMIC_PARTIAL`**，exit **0** |
| JSNZKPG | `NETWORK_ERROR` / Connection refused，fetched 0 / included **0** |
| KORICE | ok，fetched 24 / included **24**，全部发布 |
| live.m3u | 24 条 / 4600 字节 |
| 失败源 URL 回流 | **0 行**（`grep -c jsnzkpg` = 0）|
| fixed_count | 0（仍然照常发布，证明修正生效）|

### smoke C · 单源失败（对称：KORICE 失败，JSNZKPG 存活）

| 项 | 值 |
|:--|:--|
| status | **`DEGRADED_DYNAMIC_PARTIAL`**，exit **0** |
| KORICE | 失败，fetched 0 / included 0 |
| JSNZKPG | ok，fetched 56 → included 8，1872 字节 |
| 失败源 URL 回流 | **0 行**（KORICE 中转域名 `passwdwork` 计数 0）|

### smoke D · 双源全失败（last-known-good）

| 项 | 值 |
|:--|:--|
| status | **`DEGRADED_NO_PUBLISH`**，exit **2** |
| reason | `failure_policy=isolate 且本轮没有任何可发布内容（动态 0 条 + 固定 0 条）：不发布（也不沿用旧的动态签名线路）` |
| dynamic_summary | `failed_sources=2`、`published_entries=0`、两源均 `ok=false` |
| **LKG live.m3u** | sha256 `1af2dc90…`、1394 字节、mtime `10:32:40` —— **轮次前后完全一致，一字节未动** |
| source URL 脱敏 | 摘要里只剩 `http://127.0.0.1`（到 host 为止）|

## 11. Last-known-good

| 事实 | 证据 |
|:--|:--|
| 双源全失败时不覆盖当前文件 | smoke D：sha256 / 字节数 / mtime 三者均未变 |
| 不把上一轮动态 URL 当本轮结果 | smoke B/C：失败源域名在新文件里 0 命中；离线测试 `test_compose_layer_never_reads_previous_live_m3u`、`test_isolate_failure_does_not_reuse_previous_dynamic_urls` |
| `last_success_publish_at` 只在真正发布成功时推进 | smoke D 走 `DEGRADED_NO_PUBLISH`，不写成功时间戳（离线 `test_isolate_both_fail_fixed_zero_keeps_last_known_good` 覆盖）|
| 组合层绝不读取旧 `live.m3u` 回填 | 代码层无此路径；静态守护测试锁死 |

LKG 与「失败源回填」是两件不同的事，本轮实现严格区分：**允许旧文件留在磁盘上给播放器继续读，但组合层绝不从它取任何条目**。

## 12. EV-Lab zero-harm

| 检查 | 结果 | 与基线对比 |
|:--|:--|:--|
| `evlab-task0006.service` unit sha256 | `e9328102f97984726812ee56fd80da81e5ea5e620e832417adde845a86b28eb4` | **未变** |
| `evlab-task0006.service` 状态 | `inactive/dead`，`Result=success` | 2026-10-03 计划内 deadline 收工，未被本轮打扰 |
| `evlab-task0006-health.timer` | 08:00:01 跑过，下次 14:00:00 | 正常 |
| `evlab-task0006-health.service` | `Result=success`，`ExecMainStatus=0` | 正常 |
| 数据目录 | `/home/ubuntu/evlab-data` 305M、`evlab-ops` 40K | 与基线一致，未增长/未删改 |
| 用户/目录/端口/unit 隔离 | `liptv ≠ ubuntu`、8080 只绑 `127.0.0.1`、`li-iptv.service ≠ evlab-*`、未装 Docker | 全部保持 |

本轮全程未触碰 EV-Lab 的任何 unit、文件、数据库或进程。

## 13. Tests

| 项 | 值 |
|:--|:--|
| 基线 | 461 passed |
| 新增 | 54（`tests/test_task008.py`）|
| **全量总数** | **515 passed，0 failed，0 skipped**（228.75 s）|
| 命令 | `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -o addopts="" -p no:cacheprovider -q --basetemp="$TEMP/t008all"` |
| TASK-008 文件单跑 | 54 passed（16.91 s）|
| flaky | 未观察到（同一提交重复跑结果一致）|
| 字符卫生 | `publish.py` 50984 B、`test_task008.py` 45328 B，CRLF/NUL/零宽/C0 **全部 clean** |

## 14. Risks / NOT EXECUTED

明确**没有**做、也不宣称做了：

- 未开放公网 8080、未改安全组、未改 DNS（云安全组仍只放行 22/TCP + ICMP）
- 未接入任何 fixed 频道源（生产 `fixed_count = 0`），因此 `DEGRADED_FIXED_ONLY` 路径只有离线测试覆盖，**真机上从未触发**
- 未做反向代理（Caddy/Nginx 全未装）
- 未做 EPG / 频道 Logo / 录制 / 定时停播
- 未验证 TLS 证书细节（走 HTTPS 默认校验，未做证书 pinning）
- 未做 fuzzy event matching / 赛事实体模型 / 球队名 NLP（任务书明确排除）
- 未安装 Docker
- 未触碰 EV-Lab、未续费 Lighthouse（实例 2026-10-24 到期，仍需手动续）
- 未抓取、未播放、未留存任何底层视频内容；未破解任何 DRM/登录/认证

已知遗留风险：

1. **两源都是「短时签名」型订阅**：`live.m3u` 是单次快照，签名过期后必须等下一轮 scheduler（15 分钟）刷新。这是任务书已知限制，不是本轮引入。
2. **跨源同名冲突靠标签区分**：若两源真的报同一场比赛，播放列表里会是两条带不同来源标签的线路，用户需自行选择。这是任务书明确选择（不做 fuzzy 合并）。
3. **`jsnzkpg` 的 `included` 在 6~10 条间波动**：赛事时间窗自然变化，非缺陷。
4. 实例 2026-10-24 到期，到期后 scheduler 与订阅服务会一起停。

## 15. Git & Gate

### 15.1 提交链

| SHA | 内容 |
|:--|:--|
| `da290a9` | TASK-008 任务书（大G） |
| `2a2d0c5` | feat: TASK-008 production multi-source dynamic aggregation with isolate policy |
| `840c243` | fix(publish): isolate 策略在 fixed=0 时也发布存活源赛事（真机 smoke B 回归） |

### 15.2 blob 对照表（本地 HEAD / 远端 main / 主机工作树 三方一致）

| 文件 | blob SHA |
|:--|:--|
| `liptv/config.py` | `097a18a8f41575fe49234059c74d8c3ab53678f5` |
| `liptv/publish.py` | `9fe96af988c7fb38421c5267a6d76ca76bd8979a` |
| `liptv/runtime.py` | `ff2b8c9db17fc7f7e525da8c7d90e267ab4f40e9` |
| `liptv/cli.py` | `adbf891ced41bc0b379778746448443787e5f810` |
| `config/config.example.toml` | `80fa12565fb839e772ac43d445feee0ecce85c06` |
| `tools/mock_source_server.py` | `30680b529282773fda22cbb597c3fa99a198f928` |
| `tests/test_task008.py` | `0ef2babdef6a819fe3bc80d90cd11a9a41a711a4` |

### 15.3 Gate

- `TASKS/TASK-008.md` 状态已置 **REVIEW**
- 状态只到 REVIEW，**未**自行推进下一任务
- **TASK-009 未启动**，等大G 独立验收

## 16. 需要大G 重点复核的三点

1. **§2.1 的判据分流是否认可**：`isolate` 下「有可发布内容就发」，`all_or_nothing` 保持「无固定频道就不发」。这条是本轮真机 smoke B 逼出来的，属于对任务书 §3 的必要补齐，请确认语义无误。
2. **`DEGRADED_DYNAMIC_PARTIAL` 映射到 exit 0 是否认可**：理由是「成功源的赛事确实写出文件了」，属已发布的降级；但它**不是** `OK`，状态与摘要都明确标注降级，绝不掩盖。
3. **跨源同名冲突只加标签不合并是否符合预期**：同源内部同名条目**不加**标签（KORICE 的 pc/alt 两条），只有真正的跨源冲突才加。若大G 认为同源也该标注，改动只在 `resolve_cross_source_collisions()` 第 2 轮的一个 `continue` 判断。