# Li IPTV Aggregator — V1 Runtime Flow

## 1. Fetch

定时获取所有 enabled source。

输出：
- source fetch 状态
- 新增/消失的 source_channel

## 2. Normalize

按优先级尝试：
1. 已有 binding
2. 精确 tvg-id / external id
3. alias 规则
4. normalized name
5. fuzzy candidate
6. 未识别则暂存，不强绑

## 3. Stream Inventory

从 source_channel 提取 URL。

规则：
- URL 去重；
- provenance 保留；
- 新 stream 进入观察期；
- 旧 stream 若长期不再出现，不立即删除，只标记 stale。

## 4. Probe

对需要测试的 stream 生成任务。

V1 建议分层频率：

- 新 stream：较高频，快速建立可信度；
- 稳定 stream：降低频率；
- 连续失败 stream：指数退避；
- 即将用于正式 M3U 的候选 stream：发布前优先复核。

避免所有线路固定频率全量 ffprobe。

## 5. Score

按历史生成简单分数。

初版规则：
- 7 日成功率高优先；
- 连续失败强惩罚；
- 最近成功时间过久降级；
- 启动速度为次要因素；
- 清晰度/码率只在稳定性相近时参与。

## 6. Select

每个 canonical_channel：
- 只选择 1 条最佳 stream；
- 若无达到最低可用阈值的线路，则暂不输出；
- 可设置少量“必须频道”后续特殊处理。

## 7. Generate

生成临时 M3U：
- 基础语法校验；
- 无重复 canonical channel；
- 每个条目 URL 非空；
- 输出数量合理；
- 必要时抽样复核。

## 8. Publish

成功：
- 原子替换 current live.m3u；
- 记录 publication；
- 保留上一版用于回滚。

失败：
- 不覆盖 current；
- 记录失败原因；
- 播放器继续读取 last-known-good。

# 建议初始频率

为避免过度设计，V1 暂定：

- Source fetch：每 3 小时
- 新线路测活：每 30–60 分钟
- 稳定线路：每 6 小时
- 失败线路：按 1h → 3h → 6h → 12h 退避
- 正式 M3U 发布：每 3 小时，或有有效变化时

这些都应配置化，后续按真实效果调整。

