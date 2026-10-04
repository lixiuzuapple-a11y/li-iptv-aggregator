# KORICE PPV 动态赛事 M3U — Source Registry

记录日期：2026-10-04
优先级：高
性质：动态赛事 / PPV 清单，不是固定频道
状态：**REGISTERED / DYNAMIC_EVENT_SOURCE**
主订阅：https://www.korice.eu.org/ppv_m3u.php

## 实测

大G于 2026-10-04 经 Windows WebCodex 直接 HTTP GET 主地址：

- HTTP 200；
- `text/plain; charset=utf-8`；
- 响应约 11.7 KB；
- 标准 `#EXTM3U` / `#EXTINF` 结构；
- 当前条目包含 NFL、大学橄榄球等赛事与频道；
- `group-title` 使用赛事类别，例如 `American Football`；
- 条目名称包含具体比赛与预计时间段；
- 底层播放 URL 本次只做结构核验，**未在文档/Git 中保存或展示**。

这些特征说明该源会随赛事时间变化，工程上归类为 `dynamic_event_m3u`，不得作为 `fixed_m3u` 使用。

## 接入规则

1. 每次需要时重新拉取当前 M3U；不把赛事条目长期 canonicalization。
2. 不进入固定 `stream` 库存，不写 `probe_result`，不参与固定频道 7 日 selector。
3. 不保存旧签名/旧赛事 URL，不从 last-known-good 回拼过期动态赛事。
4. 可与 JSNZKPG 一样通过 `dynamic-fetch` 临时预览，或显式 `publish --dynamic-source korice-ppv` 合入当轮 `/live.m3u`。
5. 默认 `enabled=false`，不会因为复制示例配置就自动联网。
6. 动态源失败继续沿用 TASK-003 fail-closed：默认整轮动态舍弃，只发布可用固定频道；若要求动态必需则整轮拒绝。

## 使用边界

本项目保持个人自用；公开可访问不等于拥有转播或再分发授权。
不破解、不逆向、不绕过 DRM/登录/认证、不提取或延长令牌、不做代理或转码、不公开再分发视频。
发现明确限制、失效、权利投诉或来源性质变化时停用并复核。
