# TASK-011 REVIEW-02 — ACCEPT

日期：2026-10-07
Reviewer：大G
结论：**ACCEPT**

## 1. REVIEW-01 返工结论

REVIEW-01 的唯一阻断项——生产 `/epg.xml` 实际 404——已修复，并且修复落到了部署模型与回归测试，不是只手工改生产。

本轮接受 TASK-011。

## 2. 大G独立生产 spot-check

通过新 WebCodex 工作区直接 SSH 到 `evlab-cloud`：

- `li-iptv.service`：active + enabled
- service User/Group：`liptv:liptv`
- `NeedDaemonReload=no`
- EPG 文件：`/var/lib/li-iptv-aggregator/epg.xml`
- EPG 权限：`liptv:liptv 0640`
- `sudo -u liptv test -r epg.xml`：PASS
- `/epg.xml`：HTTP 200
- Content-Type：`application/xml; charset=utf-8`
- `/live.m3u`：HTTP 200
- `/healthz`：HTTP 200，status=ok
- 监听：仅 `127.0.0.1:8080`

XMLTV 实测：
- 42 channels
- 11059 programmes
- XML parse PASS

M3U 实测：
- fixed with tvg-id：43
- dynamic without tvg-id：181（动态条目随时点变化）
- tvg-id 与 XMLTV channel id 交集：41
- 已知未命中：`France24.fr`、`FujianStraitsTV.cn`

上述两个未命中均是已知无 EPG 频道，不属于 mapping 缺陷。

## 3. 返工设计审查

接受以下修复：

1. EPG 运行期产物迁移到 `/var/lib/li-iptv-aggregator/`，与 live.m3u 同生命周期。
2. production config template 显式加入 `[epg]`，enabled=true、LKG/output/status 路径均落持久数据目录。
3. 主力 EPG 使用已验证的 fanmingming jsDelivr 入口，避免生产 raw.githubusercontent.com 网络阻断。
4. deploy Layout 增加 EPG output/status 路径，防止未来 install/upgrade 再遗漏。
5. 文档原文已直接修正，不再仅依赖 ERRATA。

## 4. 大G独立测试

### TASK-011 + deploy
- 独立 basetemp
- 禁用外部 pytest plugin
- **184 passed in 58.36s**

### server
- **25 passed in 19.60s**

### runtime 定向回归
覆盖 config defaults / server settings / health / binding 等 8 项：
- **8 passed**

组合测试曾出现无失败输出的长挂；未把挂起结果计为通过，而是拆分后独立重跑上述集合。

## 5. REVIEW-01 裁决维持

### 国际频道
接受本轮只新增 France 24，不要求为了数量凑第二个国际频道。真实可播门槛优先。

### 120.76.248.139
继续不做 host 级永久封禁。保留 probe history，用跨轮稳定证据决定后续精确排除。

### epg.pw
TASK-011 不做 42 条人工 id 映射。当前主力 EPG 覆盖与 LKG 足够，灾备映射留待真实需求出现后再做。

### Apple TV / APTV
不解除公网冻结，不开放 8080，不改安全组/DNS/TLS。Apple TV 实机 EPG 显示继续诚实标记为未验证。

APTV 的具体“本地配置 + EPG 输入”能力仍以 Owner 实机 UI 为最终判据；这不阻断 TASK-011 的后端交付验收。

## 6. 最终判定

**TASK-011：ACCEPTED。**

EPG、Logo、metadata、M3U↔XMLTV 关联、France 24、生产 `/epg.xml`、部署模型与回归测试均完成闭环。

允许后续定义 TASK-012；不得把 Apple TV 实机显示写成已验证事实。