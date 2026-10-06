# TASK-010 REVIEW-01 — ACCEPT

日期：2026-10-06
Reviewer：大G
结论：**ACCEPT**

## 1. 总结

TASK-010 达到核心产品目标：fixed 从 10 个 seed 扩到 43 个 canonical，生产当前保持 40+ 条 fixed 可发布；动态双源仍工作；手工 publish 默认动态抓取语义已修复；playback context 已正式与 aggregator reachability 分离。

小W交付已进入 REVIEW，未启动 TASK-011。大G独立 QA 未发现代码层阻断缺陷。

## 2. 独立 QA

### Git / 变更范围
- REVIEW HEAD：48b5111
- 变更 23 个文件，核心代码涉及 config / cli / publish / source_policy / seed 工具与测试；schema 仍为 V1。
- 初次 `git diff --check 86ca9b0..48b5111` 发现 `TASKS/TASK-010.md` 状态行 trailing whitespace；属于收口文档缺陷，Reviewer 本轮直接修复，不要求执行者返工。

### 独立测试
- 受影响测试集合：`tests/test_task010.py + test_publish.py + test_task008.py + test_task009.py`
- 收集：192 tests
- 实跑：全部 PASS，exit 0。

## 3. 生产只读 spot-check

大G从 `E:\WebCodex-Workspace\li-iptv-aggregator` 直接 SSH 到 `evlab-cloud`：

- `li-iptv.service`：active / enabled
- `/healthz`：HTTP 200
- `/live.m3u`：HTTP 200
- M3U header：有效
- 当前总 entries：254
- 当前 dynamic：213
- 当前 fixed：41
- fixed 分组：央视 12 / 卫视 23 / 教育 2 / 新闻 1 / 纪录片 1 / 音乐 1 / 儿童 1
- 监听仍为 `127.0.0.1:8080`

当前 fixed 41 与执行报告的 42 不冲突：selector 会随新增 probe history 自然跳过退化线路，仍显著高于最低 24。

## 4. Reviewer 补充验证

### KORICE 家庭/VPN context
TASK-010 任务书要求家庭/VPN smoke 至少抽 KORICE 3–5 条；执行报告展示不充分。大G补测当前 KORICE 前 5 条：

- 3 条 timeout
- 2 条 HTTP 502
- 当前样本 0/5 可播

这不是“全局不可用”结论，也不推翻 Owner 之前家庭 VPN 实际可播的观察；它只代表当前 `home-windows-vpn` context、当前时点的 5 条样本。KORICE 继续保留，`cloud_probe_authoritative=false` 的设计正确。

### 国际源上海复验
小W把部分国际新闻候选留为“待生产复验”，大G补做上海只读检查：

- France 24：HTTP 200，HLS playlist 可达
- NHK World：HTTP 206，HLS playlist 可达
- DW 旧入口：HTTP 404
- Al Jazeera / CGTN / CCTV direct 候选：当前上海解析/连接链路失败

这些结果应作为 TASK-011 或后续频道扩充的侦察输入，不阻断 TASK-010。

## 5. 重点审查结论

### Dynamic 默认语义
接受。`CLI 显式 > config > auto`，并增加 `--no-dynamic` 与 `dynamic_decision_reason`；避免“未执行抓取”被误读为“自然无赛事”。

### Playback Context
接受。`source_policies.toml` 只保存上下文 metadata，不含 VPN 凭据、Cookie、Authorization 或私有订阅；KORICE 明确 `playback_requires_vpn=true`、`cloud_probe_authoritative=false`。

### Fixed 扩充与坏台治理
接受。43 canonical、168 stream，坏流按真实 probe 与家庭 smoke 分类；selector 自然排除退化频道，不通过降低门槛凑数量。

### 报告数字口径
执行报告的 214 PASS 与坏台报告的 213 PASS 属不同统计口径/轮次记录，不作为阻断；生产当前状态由 Reviewer spot-check 单独记录。

## 6. 非阻断后续

1. 把 France 24 / NHK World 作为后续国际频道优先候选。
2. 继续观察家庭 context 发现的 segment 404 线路；在决定永久排除前用上海 ffprobe/实际播放二次验证。
3. 观察单 host `120.76.248.139` 的持续超时；只有形成长期证据后才在 seed 层主动排除。
4. KORICE 家庭/VPN 可用性具有明显时变性，不应把任何单次 5 条 smoke 作为全局 source 开关。

## 7. 最终判定

**TASK-010：ACCEPTED。**

允许后续定义 TASK-011。