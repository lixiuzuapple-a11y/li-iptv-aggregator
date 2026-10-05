# TASK-010+ Planning Note — Playback-Context Probes

状态：PLANNING_NOTE_ONLY（非执行任务；TASK-009 完成前禁止启动）
记录日期：2026-10-05

## 背景

KORICE PPV 已确认存在“聚合器可达性”和“真实播放可达性”分离：

- 上海生产机可正常抓取 `https://www.korice.eu.org/ppv_m3u.php`；
- 上海生产机对抽样底层 PPV stream 的 ffprobe 失败（超时 / 5XX）；
- Owner 家庭网络在可用 VPN 下实际可播放 KORICE；
- WebCodex 本机经当前代理/VPN 网络直接 GET KORICE 底层 HLS，至少 `NFL Network` 返回 HTTP 200 + HLS playlist；
- 其它抽样赛事当时返回 502，可能与赛事未开播/已结束有关，不能据此判定整个来源不可用。

## 冻结认识

后续系统必须区分两类健康：

1. **Aggregator reachability**：聚合器/服务器能否抓取 source M3U。
2. **Playback reachability**：真实观看网络能否播放底层 stream。

二者不得混为一个 PASS/FAIL。

## KORICE 决策

- KORICE 保留为 `dynamic_event_m3u`；
- 上海服务器能抓列表即可满足聚合层职责；
- 不允许因为上海机房底层 stream probe 失败而自动从最终 M3U 删除 KORICE；
- 对 KORICE 的播放可用性，应以后由更接近真实观看环境的 probe 判断；
- 家庭 Windows / VPN / Apple TV 所在网络的 probe 权重应高于上海云 probe，因为播放器最终直接访问上游，不经上海转发。

## TASK-010 或后续应考虑

- 增加 probe context / probe location 概念，不改写现有事实记录；
- 至少支持 `shanghai-cloud` 与 `home-windows-vpn` 两类独立 probe；
- selector/动态可用性不得使用“上海 FAIL = 全局 FAIL”；
- 对动态赛事源，列表 fetch 成功与媒体 stream 健康分开记录；
- 允许 source policy 标注 `playback_requires_vpn` / `preferred_probe_context` 一类信息，但避免把具体 VPN 凭据写入 Git；
- 若自动化家庭 probe 成本过高，先允许人工确认/抽样，不为此阻断当前 MVP；
- 不做代理、不让视频经过上海云、不上传 VPN 配置/账号/密钥。

## 优先级

这不是 TASK-009 阻断项。TASK-009 继续完成 fixed inventory。
进入 TASK-010 设计时，大G应优先考虑“频道扩充 + 质量筛选”的产品目标；
只有当多环境 probe 能直接减少误杀/坏台时再并入 TASK-010，否则顺延到 TASK-011+。
