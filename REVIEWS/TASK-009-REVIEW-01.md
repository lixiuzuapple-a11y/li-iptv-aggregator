# TASK-009 REVIEW-01 — ACCEPT

日期：2026-10-05  
Reviewer：大G  
结论：**ACCEPT**

## 1. 验收结论

TASK-009 达到任务书最低验收要求，且未发现阻断缺陷。

本轮实际完成：
- fixed source 侦察 16 个候选，采用 2 个独立公开源；
- 10 个 canonical 进入生产 fixed pipeline；
- 4 个 canonical 具备跨独立来源多 stream；
- 真机 ffprobe 两轮完成，45 stream × 2 = 90 条 probe_result；
- selector 的 PASS/PASS、PASS/FAIL、all-below-threshold 均有可解释证据；
- fixed + TASK-008 dynamic 已在生产共存；
- LKG、fixed source fetch failure、环境级 ffprobe failure 均按既有语义 fail-closed；
- TASK 状态只推进到 REVIEW 后停止，未启动 TASK-010。

## 2. 大G独立 QA

### 代码 / Git
- REVIEW 前工作区：clean，main...origin/main
- HEAD：611b302e3b6559474b699fcd777d5dca9336e62b
- git diff --check 047d9b4..611b302：PASS
- 变更范围：13 files；schema 未改。

### 独立测试
1. tests/test_task009.py + tests/test_probe.py
   - 收集：63 tests
   - 实跑：全部 PASS
2. TASK-008 + publish 回归（独立 basetemp、禁用外部 pytest plugin）
   - tests/test_task008.py tests/test_publish.py
   - **97 passed in 83.21s**

小W报告的全量 552 passed 仅作为执行证据记录；大G没有重复声称独立跑完 552 项。

## 3. 生产只读 spot-check

大G通过新工作区 E:\WebCodex-Workspace\li-iptv-aggregator 直接 SSH 到 evlab-cloud：

- li-iptv.service：active
- service：enabled
- /healthz：HTTP 200
- /live.m3u：HTTP 200
- M3U header：有效
- 当前 entries：208
- 当前 fixed：10
- 当前 dynamic：198
- 分组：fixed 10（地方台 6 + 教育/新闻/纪录片/音乐各 1），dynamic 198
- 监听：仅 127.0.0.1:8080

执行报告记录的 204/206 与当前 208 不冲突：dynamic source 是 current-fetch，赛事条目数随时间变化；fixed 10 条保持一致。

## 4. 重点审查结论

### A. ffprobe -nostdin
接受修复。-nostdin 不属于 ffprobe 可用参数；删除后生产真实 probe 恢复。新增严格参数回归测试合理。

TASK-009 之前生产没有真实 fixed inventory，因此本轮无需为现生产 DB 清理一批可用 fixed 的“旧假失败历史”；后续若发现其它历史数据库含修复前生成的 probe 结果，应视为无效历史，不用于健康结论。

### B. seed / 签名过滤
接受当前分层：build 阶段过滤用于计划与计数；bind 阶段再次过滤；sync_streams 只归集绑定后的 active channel。

scheduler 后续重新激活未绑定的 source_channel 会让 source inventory 统计偏大，但不会让其自动进入 stream / selector / publish，因此当前是统计噪声，不是正确性缺陷。

### C. fixed_summary
接受。fixed fetch 状态现在能进入 publish summary，解决“fixed_count 看起来正常但某源库存已陈旧”的可观测性缺口。

### D. TASK-008 isolate 回归
接受。按 _source_slot / source instance 处理重复动态来源，与 TASK-008 published accounting 口径一致；独立 TASK-008 + publish 回归 97 项通过。

## 5. 非阻断后续项

不反打 TASK-009，进入 TASK-010 或后续规划：
1. 手工 publish 不带 --dynamic 时不会抓动态源，容易把“未抓取”误读成“自然无赛事”；应改善 CLI/运维可用性。
2. 继续执行已冻结的 playback-context 原则：上海云 probe 只代表云端链路，不能等价于家庭/VPN/Apple TV 的真实播放可用性。
3. source inventory 的 active 统计与 seed 实际可发布集合可在后续优化可观测性，但不应破坏 TASK-002 生命周期语义。

## 6. 最终判定

**TASK-009：ACCEPTED。**