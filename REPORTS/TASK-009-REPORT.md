# TASK-009 Execution Report

状态：REVIEW
Owner：老李
Executor：小W
Reviewer：大G

> 按 `TASKS/TASK-009.md` 执行。完成后将本文件与 TASK 状态一并更新到 REVIEW。

## 0. 一句话结论

生产从 `fixed_count=0` 推到 **10 个 fixed canonical 真实入库 + 真机 ffprobe 两轮 + fixed/dynamic 共存发布 206 条**。
过程中修掉 **5 个真生产缺陷**（2 个是 TASK-005/008 遗留、3 个是真机 smoke 才暴露的），每一个都做了负向验证。

---

## 1. 基线与变更

| 项 | 值 |
|:--|:--|
| 起止 release | `840c243`（TASK-008）→ `5575400`（含本任务全部代码） |
| 生产实例 | `lhins-bukxxz3g` / ap-shanghai / Ubuntu 24.04.4 / Python 3.12.3 / ffprobe 6.1.1 |
| schema | **V1 未变**（`doctor` PASS；TASK-001 冻结遵守） |
| 8080 绑定 | 仍只 `127.0.0.1`（`ss -tlnp` 复核），**未对公网开放** |
| 部署方式 | `deploy plan` 预演 → `deploy upgrade`（自动 daemon-reload + start） |

**代码变更清单**

| 文件 | 性质 | 说明 |
|:--|:--|:--|
| `liptv/publish.py` | 【真缺陷修复】 | 跨源去重改按 `_source_slot`；新增 `fixed_summary` |
| `liptv/probe.py` | 【真缺陷修复】 | 移除 ffprobe 不存在的 `-nostdin` |
| `tools/build_fixed_seed.py` | 【新增+修复】 | seed 导入；新增 prune-unused、stream-sync、签名绑定处再拦 |
| `tools/selector_explain.py` | 【新增】 | selector 选择依据解释器（§7 验收项） |
| `tools/upsert_fixed_sources.py` | 【新增】 | 幂等写 fixed 源进 config |
| `tools/verify_live_m3u.py` | 【新增】 | 产物反向解析验收 |
| `tools/mock_source_server.py` | 【修改】 | 新增 fixed 变体端点，供离线测试 |
| `SOURCES/FIXED-SOURCE-RECON-20261005.md` | 【新增】 | 16 个公网候选取舍记录 |
| `config/fixed_seed_bindings.toml` | 【新增】 | 10 条 seed 绑定，零 stream URL |
| `tests/test_task009.py` | 【新增】 | 28 项离线测试 |
| `tests/test_probe.py` | 【修改】 | +2 项 argv 回归 |

---

## 2. 外部侦察（§3，≥10 候选）

完整记录见 `SOURCES/FIXED-SOURCE-RECON-20261005.md`（16 个候选）。

**采用 2 个独立公开源**

| 源 | 条目 | 说明 |
|:--|--:|:--|
| `iptv-org-cn` | 145 | `https://iptv-org.github.io/iptv/countries/cn.m3u` |
| `guovin-gd-ipv4` | 473 | `https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/ipv4/result.m3u` |

**淘汰的典型**

| 源 | 淘汰原因 |
|:--|:--|
| `guovin-gd` | 含 `msisdn` / `migutoken` **用户身份参数**（隐私红线） |
| `fanmingming-tv` | IPv6-only，生产主机 0/12 可达 |
| `fanmingming-global` / `YanG-1989/tvlist` | 404 |
| `iptv-org-index` | 11138 频道，违反 §4「小而真实的 seed inventory」 |

**侦察阶段抓到的两个陷阱**

1. **万能流（占位流）**：某 `gslb/*.m3u8` 被 **45 个 canonical 共用**。若不排除，会让 45 个频道全部「probe 通过」而选出的线路根本不是该频道。已加规则「同一 stream URL 被 ≥5 个 canonical 共用 ⇒ 判为占位流并排除」。
2. **本机探测 ≠ 生产主机可达**：某 `gslb/*.m3u8` 本机返回 `application/vnd.apple.mpegurl` 判为可用，生产主机复验 **404**；另一路本机 OK，生产主机返回 `text/html`。根因是本机未严格校验首个分片 TS sync byte，且 `urllib` 自动跟随重定向而 `curl` 不跟随造成口径差异。已改为严格 HLS 校验（跟重定向 → 解析 playlist → 抓首分片验 `0x47` @ 0/188）。

---

## 3. Canonical 与 binding（§5，零 fuzzy）

**10 个 canonical，53 条绑定，0 冲突，签名跳过 8 条。**

| canonical | 绑定 | 跨独立源多 stream |
|:--|--:|:--|
| CCTV-2 财经 | 6 | ✅ cn × guovin |
| CCTV-9 纪录 | 2 | ✅ cn × guovin |
| CCTV-15 音乐 | 2 | ✅ cn × guovin |
| CETV-1 中国教育 | 2 | ✅ cn × guovin |
| 湖南 / 浙江 / 东方 / 广东 / 山东 / 北京卫视 | 4~5 | guovin 单源多 stream |

**匹配规则唯一**：`config/fixed_seed_bindings.toml` 里只允许 `match = "exact_normalized"`；
`load_seed_bindings()` 对任何其他值**直接 `SystemExit`**。归一只剥末尾分辨率后缀
（`CCTV-2 (720p)` → `CCTV-2`）。**无 Levenshtein、无 NLP、无「名字看起来差不多」兜底。**

**自证断言**（防「假成功」）：`if matched_total and bound + conflicts < matched_total: raise SystemExit(...)`
—— 宁可炸也不写一份「看起来成功、实际 0 绑定」的结果。这个断言是在开发中真的抓到过
`bind_source_channel(canonical_id=...)` 参数名错误（实际是 `canonical_channel_id`），
当时裸 `except Exception: continue` 把 `TypeError` 吞掉，输出了一份 0 绑定的假成功。

**万能流排除**：seed 阶段排除 2 条被 ≥5 canonical 共用的 stream。

---

## 4. 签名与隐私（§11）

| 层 | 措施 |
|:--|:--|
| `build_plan` | `BANNED_QUERY_KEYS` 过滤（含 `token/auth/key/secret/msisdn/migutoken/sign/hdnts/expire/mdspid`） |
| `apply_plan` 第 4 步 | `prune-unused`：seed 用不到的条目置 `active=0`（**只置 inactive，绝不硬删** —— TASK-002 要求保留 `identity_hash` 可追溯） |
| **`apply_plan` 绑定循环** | 🚨 签名过滤**必须再拦一次**（见 §9 缺陷 3） |
| publish 摘要 | `fixed_summary` / `dynamic_summary` 的 URL 一律走 `redact_url_light`（只剩 `scheme://host`） |
| 产物校验 | `verify_live_m3u.py` 扫 `BANNED_QUERY_KEYS`，真机实测 **0 命中** |
| Git | `config/fixed_seed_bindings.toml` **零 stream URL**；本报告所有 URL 均脱敏 |

**真机实测结果**：`stream-list` 里带 `auth=` 的 stream **= 0**；
产物里带签名/身份 query 的条目 **= 0**（`?id=cctv9hd` 这类非签名参数不误报）。

---

## 5. 真机 ffprobe 两轮（§7）

**修掉一个 TASK-005 遗留的真缺陷后，probe 才第一次真正测到流。**

### 🚨 缺陷 4：`-nostdin` 让 ffprobe 100% 失败

`build_argv` 给 ffprobe 传了 `-nostdin` —— 那是 **ffmpeg** 的选项，ffprobe 没有。真机实测：

```
Failed to set value '-v' for option 'nostdin': Option not found
```

ffprobe 直接非 0 退出 ⇒ `classify_failure` 把 stderr 归成 `ERROR_HTTP`
⇒ **每一条** stream 都记成播放失败。运维看到的是「所有线路都 HTTP_ERROR」，
实际是 argv 本身就不合法，**一个流都没真的测过**。

手工对照（同一台机、同一 URL）：

| argv | 结果 |
|:--|:--|
| 带 `-nostdin` | `Failed to set value ... Option not found` |
| 去掉 | 正常返回 h264 / 1280x720 / `format protocol=http` |

**为什么离线测试永远发现不了**：本机 PATH 没有 ffprobe（`FFPROBE_NOT_FOUND` 是预期），
而 `tools/fake_ffprobe.py` 替身**不校验选项合法性**，`-nostdin` 它照单全收 ⇒ 所有 TASK-005 探活用例全绿。
这是替身测试的天然盲区，**只有真机真二进制会当场翻脸**。

### 两轮实测（生产 DB，真 ffprobe 6.1.1）

| 轮 | requested | 成功 | 失败 | 写入 | 错误分布 |
|:--|--:|--:|--:|--:|:--|
| 1 | 45 | 38 | 7 | 45 | TIMEOUT 5 / CONNECT_ERROR 1 / INVALID_MEDIA 1 |
| 2 | 45 | 38 | 7 | 45 | 同上 |

两轮合计 **90 条 `probe_result`**，覆盖全部 45 条 stream。
错误类型**按 stream 不同分布**（不是一刀切），证明确实是逐条测出来的。

---

## 6. Selector 验证（§8）

工具：`tools/selector_explain.py`（只读）。它把「为什么选这条」摊成三段可核对的事实：
`probe_result` 原始历史 → 每条 stream 的评分与 `eligible` 理由 → 最终选择。

### 场景 A：多 stream 都 PASS ✅

CCTV-2 财经 6 条 stream 全部 `rate=1.00 / consec_fail=0 / eligible=True`：

| stream | startup | 分辨率 | 结果 |
|:--|--:|:--|:--|
| 2 | 2772ms | 1280x720 | **SELECTED** |
| 4 | 3070ms | 1280x720 | |
| 1 | 3147ms | 1280x720 | |
| 3 | 3972ms | 1280x720 | |
| 6 | 4523ms | 1920x1080 | |
| 5 | 5300ms | 1920x1080 | |

选 stream 2 —— **按启动时间最快**，不是随机、不是按来源顺序、不是「第一条」。

### 场景 B：一条 PASS、一条 FAIL ✅

**东方卫视**（3 条）：

| stream | probes | ok | rate | eligible | 理由 |
|:--|--:|--:|--:|:--|:--|
| 33 | 1 | 1 | 1.00 | True | — | **SELECTED** |
| 32 | 1 | 1 | 1.00 | True | — | |
| 31 | 1 | 0 | 0.00 | **False** | 窗口内成功次数 0 < 阈值 1 | |

**北京卫视**（4 条）同样形态：选 45（PASS），stream 44 `INVALID_MEDIA` 被排除。

⇒ **失败线路从未被选中**，判据来自 probe history 而非来源顺序。

### 场景 C：全部未达门槛 ✅

用 `--min-successes 3`（现有数据无人达到）：

```
entries=0  skipped=10
   SKIPPED CCTV-2 财经 / CCTV-9 纪录 / CCTV-15 音乐 / CETV-1 中国教育
   SKIPPED 湖南 / 浙江 / 东方 / 广东 / 山东 / 北京卫视
```

10 个 canonical 全部 skipped、**`entries=0`** ⇒ 达不到门槛就不发布。
**没有为了让 smoke 好看去临时降低门槛。**

### 场景 D：历史变化影响选择 ✅（离线测试 `test_selector_history_change_influences_choice`）

真机侧两轮 probe 已在同一 canonical 上产生不同历史（如 stream 7：`TIMEOUT`×1 后 `PASS`×1，
`rate` 从 0 升到 0.5 但仍 `consec_fail=0`）。**未要求一次失败就强制翻转** ——
按现有 `sort_key`（成功率 → 连续失败 → 最近成功 → 启动速度 → 清晰度）解释。

### 选择可由历史解释 ✅

`selector_explain.py` 输出的每一行都带 `probes / ok / rate / consec_fail / startup / 分辨率 / eligible / reason`，
运维不需要信任任何人的口头结论。

---

## 7. 生产 publish：fixed + dynamic 共存（§9）

### 真机结果

```
status        : OK
fixed/dynamic : 10 / 194  (total 204)     ← 首次不带 --dynamic 时是 10/0
live.m3u      : 43772 B
failure_policy: isolate  selected=2 ok=2 failed=0
dynamic src   : jsnzkpg-sports [ok] fetched=59  included=6   published=6
dynamic src   : korice-ppv     [ok] fetched=188 included=188 published=188
exit          : 0
```

后续复核一次：`fixed/dynamic : 10 / 196 (total 206)`，仍 `status: OK` / exit 0。

> ⚠️ **一个必须记录的操作坑**：`publish` **不带 `--dynamic` 时根本不抓动态源**
> （`include_dynamic = bool(args.dynamic or args.require_dynamic or args.dynamic_source)`），
> `publish.dynamic_sources` 配置项只在这个开关为真时才被读到。
> 第一次跑出 `10 / 0` 时我差点把它当成「本轮自然无赛事」上报 —— 是 `dynamic_summary.sources == []`
> 暴露了「压根没去抓」。生产 scheduler 走 `run` 子命令，不受影响；
> 手工补跑必须带 `--dynamic`。

### 产物反向解析（`tools/verify_live_m3u.py`）

```
bytes            : 43772
has_header       : True
entries          : 204
banned hits      : 0        ← 无签名/身份 query
分组分布（6 组）:
      194  体育赛事（实时）     ← dynamic
        6  地方台                ← fixed
        1  教育 / 新闻 / 纪录片 / 音乐   ← fixed
expected total   : 204  (fixed 10 + dynamic 194)
live.m3u entries : 204        ← 一致
结论: PASS
```

**fixed 与 dynamic 的 group 完全可区分**（fixed 是频道分类，dynamic 是赛事分组）。

### 端点

| 检查 | 结果 |
|:--|:--|
| `/live.m3u` | **200**（43772 B） |
| `/healthz` | **200** |
| 8080 绑定 | `127.0.0.1:8080` only（`ss -tlnp` 复核） |
| `li-iptv.service` | `active` + `enabled` |

**不出现旧签名 dynamic 回流**：产物 `banned hits = 0`。

---

## 8. F1 / F2 / F3 真机验证（§10）

在 `/tmp/t009/f/` 隔离环境做（DB + config 全副本，路径全改指 `/tmp`），
**不改生产 DB、不改公网 DNS**。§10 明确「不要求故意破坏真实公网源；
可以使用隔离 DB / mock / 本机失败 endpoint 做受控验证」。

### F1：一个 fixed source fetch 失败 ✅

把 `guovin-gd-ipv4` 的 URL 改成 `http://127.0.0.1:9/gone.m3u`（未监听端口 ⇒ connection refused）：

| 判据 | 实测 |
|:--|:--|
| 其它 fixed source 正常继续 | `iptv-org-cn` `ok`（`updated=145`） |
| 失败源库存**不被清空** | `guovin-gd-ipv4` `created=0 updated=0 deactivated=0`；stream 仍 45、binding 仍 45；`active_channels=473` |
| 现有 stream 仍参与 selector | `fixed/dynamic : 10 / 0` ⇒ 10 个 canonical 照常发布 |
| **summary 明确 degraded/fetch failure** | `failed_sources: 1`；`guovin-gd-ipv4 [NETWORK_ERROR] active_channels=473 http://127.0.0.1` |

> 🚨 **F1 第 4 条此前是真缺口**：publish 阶段**从不读** fixed 源的抓取状态，
> 摘要只看 `fixed_count`。某个源抓挂了，运维只能看到「fixed_count 正常」，
> 库存正在悄悄变旧也没人知道。本任务新增 `payload["fixed_summary"]` 补上（只列已启用源、URL 脱敏）。

### F2：环境级 ffprobe 故障 ⇒ 0 新写入 ✅

把 `ffprobe_path` 指向 `/nonexistent/ffprobe`：

```
stage       : failed
ffprobe     : ok=False path=/nonexistent/ffprobe
requested   : 0   succeeded=0 failed=0 written=0 skipped=0
environment : 环境级故障（FFPROBE_NOT_FOUND）—— 本轮 0 条 probe_result；
             环境故障不是任何一条流的失败，也不会进线路健康历史
```

复核：`probe_result` 仍 **90 条**，最新时间戳仍是 F2 之前的 `14:25:16` ⇒ **旧历史完整保留、零污染**。

### F3：publish 结果变空 ⇒ 不覆盖 LKG ✅

用 `probe-result-add --fail` 对 45 条 stream 各写 4 条失败（超过 `max_consecutive_failures=3`），
使 `select_playlist` 得到 `entries=0 / skipped=10`，然后 publish：

```
status        : DEGRADED_NO_PUBLISH
fixed/dynamic : 0 / 0  (total 0)
reason        : failure_policy=isolate 且本轮没有任何可发布内容（动态 0 条 + 固定 0 条）：
                不发布（也不沿用旧的动态签名线路）
risk          : 已有 live.m3u 可能仍在被播放器读取，其动态线路随时失效；…
```

**LKG 指纹前后完全一致**：

```
before : 6babc5801dd116f0446e2af530bac7cb9df2991023eb62205e22bb922c505167
after  : 6babc5801dd116f0446e2af530bac7cb9df2991023eb62205e22bb922c505167
```

> 口径说明：状态是 `DEGRADED_NO_PUBLISH` 而不是 OK —— all_or_nothing/isolate 语义下
> 空组合本就不该伪装成成功（TASK-003 冻结语义，isolate 之外不许收窄）。
> **F3 要求的是「文件不被空结果覆盖」，这一条严格满足。**

---

## 9. 本任务修掉的真缺陷（5 个）

每一个都做了**负向验证**（换回旧实现 / 植入旧行为 ⇒ 对应测试必须 failed）。

### 缺陷 1：跨源去重按 `source_name` 判 ⇒ 同一源写两遍就整轮发布 exit 1

`resolve_cross_source_collisions` 用 `source_name` 判断「是否跨源」。
同一源被显式指定两次时（`--dynamic-source korice --dynamic-source korice`）**不去重**
⇒ 重复条目一路活到 `generate_text` 撞重复 key ⇒ `REJECTED_VALIDATION` / exit 1。
**配置里重复写一个源名就搞挂整轮发布**，且与 QA-008A 刚立的「published 按下标归属」自相矛盾。

修法：新增 `_source_instance()`，改按 `_source_slot` 判。
**负向**：换回按名字 ⇒ `test_dynamic_isolate_zero_regression` failed（1 failed / 21 passed）。

### 缺陷 2：publish 摘要里没有 fixed 源健康信息

见 §8 F1 第 4 条。新增 `fixed_summary`。
**负向**：删除该字段 ⇒ `test_one_fixed_source_failure_keeps_others` failed。

### 缺陷 3：签名过滤只作用于「绑定」，不适用于「入库」与「绑定路径」

真机 smoke **两次**才抓到：

1. **第一版**：`build_plan` 过滤签名只让**计划计数**正确；
   `apply_plan` 第 2 步 `ingest_fixed_source` 按 TASK-002 冻结语义把**整份**上游 M3U
   灌进库存（实测 618 条），签名 URL 留在 active 库存里。
   修法：新增第 4 步 `prune-unused`。
   **真机复验仍有 7 条 `?auth=` stream**（属于 CCTV-15 等 canonical）。
2. **第二版**：`apply_plan` 第 3 步绑定时是 `list_source_channels(conn, …)` **从库里重查**的，
   根本不走 `build_plan` 那份内存列表 ⇒ 签名条目照样被绑上。
   修法：绑定循环里**再拦一次**，并记 `signed_skipped` 计数（可审计，不静默丢弃）。

**负向**：把绑定处改成不过滤 ⇒ `test_apply_never_binds_signed_stream` failed（`bound 2 != 1`）。

同批还修了 `apply_plan` **根本不做 stream 归集**（必须手动补跑 `stream-sync` 才出现 stream）。

### 缺陷 4：`-nostdin` 让 ffprobe 100% 失败

见 §5。**负向**：加回 `-nostdin` ⇒ 2 failed，且错误信息**精确复现真机症状**
（`HTTP_ERROR + Failed to set value for option '-nostdin': Option not found`）。

### 缺陷 5：`upsert_fixed_sources` 不幂等（正则贪婪吞掉块尾换行）

第 1 次跑完文件末尾少一个换行，第 2 次跑又补回来，**字节一直漂**。
内容对，但运维没法用 checksum 判断「配置到底改没改」。
修法：匹配区间不吞尾换行；替换后按「块 + 恰好一个空行」写回，末尾块只补一个换行。

**负向**：退回贪婪 `sub` ⇒ `test_upsert_fixed_sources_is_byte_idempotent` failed
（1 failed / 3 passed），差异正是那个多出来的换行。

真机幂等复核：第二次跑 md5 完全相同（`65f97dd…`）、字节数不变（4071 → 4071）、
9 个配置段全部完好、`dynamic_sources` 未被动。

---

## 10. 自动测试（§12）

`tests/test_task009.py` **28 项，全部离线**（fixed 源打本机 mock HTTP 服务，公网只用于 smoke）。

| §12 场景 | 测试 |
|:--|:--|
| 1 两个 fixed source 导入 | `test_two_fixed_sources_import_and_bind` |
| 2 source A/B 同 canonical 不同 stream | `test_two_fixed_sources_import_and_bind` |
| 3 明确 mapping 成功 | 同上 |
| 4 不确定 mapping 保持 unbound | `test_uncertain_mapping_stays_unbound` / `test_build_plan_reports_ambiguous_as_unbound` |
| 5 source fetch 失败不清库存 | `test_fetch_failure_never_clears_inventory` / `test_failed_source_keeps_previous_inventory` |
| 6 消失 → inactive 不 hard delete | `test_missing_then_reappear_restores_identity` / `test_apply_prunes_unused_channels_and_syncs_streams` |
| 7 再出现恢复身份 | `test_missing_then_reappear_restores_identity` |
| 8 PASS/PASS 唯一选 1 | `test_selector_picks_exactly_one_when_both_pass` |
| 9 PASS/FAIL 选 PASS | `test_selector_prefers_pass_over_fail` |
| 10 全部低于门槛不发布 | `test_selector_publishes_nothing_when_all_below_threshold` |
| 11 环境级 probe failure 0 写入 | `test_environment_probe_failure_writes_no_history` |
| 12 fixed + dynamic 同时 publish | `test_fixed_and_dynamic_publish_together` |
| 13 dynamic isolate 零回归 | `test_dynamic_isolate_zero_regression` |
| 14 last-known-good 保护 | `test_empty_publish_keeps_last_known_good` |
| 15 report/status 不泄漏 URL/query | `test_report_and_status_never_leak_stream_url` |
| 补充 | 万能流排除、签名不绑定、build_plan 不读公网、upsert 幂等/不跨段/只改指定源/追加 |

`tests/test_probe.py` +2：`test_argv_uses_only_real_ffprobe_options`（清单比对）、
`test_probe_stream_survives_strict_ffprobe_option_check`（严格替身按 ffprobe 真实行为拒绝未知选项）。

### 测试数字（如实）

| 阶段 | 命令 | 结果 |
|:--|:--|:--|
| 本任务新增 | `pytest tests/test_task009.py` | **28 passed** |
| probe 回归 | `pytest tests/test_probe.py -k "argv or strict"` | **4 passed** |
| **全量** | `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -o addopts="" -p no:cacheprovider -q --basetemp=$TEMP/t009full4` | **552 passed in 249.94s** |

**522（TASK-008 基线）→ 552**，TASK-001~008 冻结语义零回归。

**负向验证 4 次（全部成立，如实记录数字）**

| 植入的旧行为 | 结果 |
|:--|:--|
| 跨源去重改回按 `source_name` | 1 failed / 21 passed |
| 绑定处不拦签名 | 1 failed / 23 passed（`bound 2 != 1`） |
| 加回 `-nostdin` | 2 failed（精确复现真机症状） |
| upsert 退回贪婪 `sub` | 1 failed / 3 passed |

全部用**反向 replace** 撤销，`git grep` 返 exit 1 确认清干净。

---

## 11. EV-Lab 零干扰（§13）

| 检查 | 结果 |
|:--|:--|
| `evlab-task0006.service` unit mtime | 2026-09-28 18:30（**未改**） |
| `evlab-task0006-health.timer` mtime | 2026-09-29 09:12（**未改**） |
| `ExecMainExitTimestamp` | 2026-10-03 18:31:58（计划内收工） |
| `ExecMainStatus` | 0 |
| `health.timer` | `active waiting` |
| `/home/ubuntu/evlab-data` | **305M**（零改动） |

TASK-009 全程**未触碰** EV-Lab 任何 unit / 数据 / timer。

**未做（§13 明令禁止）**：未改安全组 / 防火墙 / DNS / TLS；未对公网开放 8080；
未删除或 restore 生产 DB；未做系统级大版本升级；未付费购买；未使用私密/盗取订阅。

---

## 12. 已知限制与如实说明

1. **`publish` 不带 `--dynamic` 不抓动态源**（§7 已记录）。手工补跑必须带 `--dynamic`。
2. **prune-unused 的效果会被生产 scheduler 冲掉**：`ingest_fixed_source` 按 TASK-002
   「本轮出现的条目恢复 `active=1`」，而 scheduler 每 15 分钟 fetch 一次。
   所以生产库 `fixed_summary.active_channels` 显示 145 / 473（源的总条目数）而不是 53。
   **但这不影响正确性**：真正的闸门是 `stream-sync` 只认「active **且已绑定**」，
   而绑定只有 45 条 ⇒ 多出的 active 但未绑定条目**永远进不了 stream 表、进不了产物**
   （真机复核：stream 45 条、签名 stream 0 条）。这是 TASK-002 冻结语义与 seed 收敛的
   设计冲突，**我选择不动冻结语义**，只如实记录。
3. **`guovin-gd-ipv4` 抓取会 TIMEOUT**（网络抖动）。TASK-002 语义正确：失败库存零改动。
   重试即可恢复（实测重试第 1 次即恢复 53 条）。**不是代码问题。**
4. **本机探测不可信**：本机 PATH 无 ffprobe，且 fake 替身不校验选项合法性。
   凡「工具 CLI 参数合法性」「真实网络可达性」类结论，一律以真机为准。
5. **手抄 base64 分块传输会出错**：本次实测第 3 块 sha256 与本机不符
   （`0b0b1b6a` vs `7ce9f460`），靠逐块校验抓出来。
   **传文件必须走 `raw.githubusercontent` 并核对 sha256**，不要手抄长 base64。
6. **「静态文件」≠「24 小时订阅」**：产物是单次快照，下一轮 publish 前不会自动过期。

---

## 13. Git

| SHA | 内容 |
|:--|:--|
| `077bd50` | seed 阶段：侦察记录 / seed 绑定 / 缺陷 1 / 缺陷 2 |
| `3438e62` | prune-unused + stream-sync |
| `608679d` | 签名过滤在绑定处再拦一次 |
| `5575400` | 缺陷 4：移除 `-nostdin` |
| `394fe32` | `selector_explain.py` |
| `3e62738` | `upsert_fixed_sources.py` + 缺陷 5 |
| `269d351` | `verify_live_m3u.py` |
| `b0e67f1` | verify 的 summary 字段读法修正 |

推送后均用 `git ls-remote` **独立复核**远端 SHA（不信 push 自己的输出）。
本机 `core.autocrlf=true` 且仓库无 `.gitattributes` ⇒ **入库对象 CR 计数逐文件核对为 0**。

**TASK 状态只到 `REVIEW`；已停止。未启动 TASK-010。**

---

## 14. 需要大G 重点复核的五点

1. **缺陷 4（`-nostdin`）的影响面** —— 这个缺陷让 TASK-005 以来**所有** probe 结果都不可信。
   修复后线上线路健康度从「全部 HTTP_ERROR」变成「38/45 可播」。请确认历史 probe 数据
   是否需要清理或标注（我未动历史，只新增记录）。
2. **缺陷 3（签名两处漏）** —— 我改了 `build_fixed_seed.py` 的**两处**过滤点（prune + 绑定）。
   请确认「prune 只清未使用、绑定清已使用」这个分工是否与您的意图一致。
3. **限制 2（prune 被 scheduler 冲掉）** —— 我选择**不动 TASK-002 冻结语义**，
   改为如实记录。请确认这个取舍；替代方案是给 seed 绑定加一张白名单表让
   `sync_streams` 额外过滤，但那会动到 `repo` 层。
4. **`publish` 的 `--dynamic` 开关** —— 是否应该改成「配置里有 dynamic_sources 就自动抓」，
   避免手工补跑时误判成「本轮无赛事」。
5. **两个新增运维工具的权限边界** —— `upsert_fixed_sources.py` 与 `verify_live_m3u.py`
   都会读写生产 config / 产物。是否需要收进 `deploy` 的权限门禁（现在是独立脚本）。
