# TASK-013 — Private Apple TV Delivery & Real Playback Verification

状态：SERVER_READY / APPLE_TV_PENDING（服务器私网 HTTPS 已验证，Apple TV 尚未实机验收）
Owner：老李
执行 / 架构 / QA：大G
基线：TASK-012 ACCEPTED，生产 IPTV localhost 127.0.0.1:8080，/live.m3u、/epg.xml、/healthz 200。

## 1. 用户目标
让 Apple TV 上的 APTV 通过**两个可达的 URL**获取自动更新的 M3U 和 XMLTV EPG；固定频道与动态赛事保留原有运行语义。生产公网端口仍不开放，不重写 IPTV 核心。

## 2. 推荐架构（待 Owner 身份授权）
Apple TV Tailscale App → 已授权的私人 tailnet → 生产 Linux tailscaled + Tailscale Serve（tailnet-only HTTPS）→ 127.0.0.1:8080 → /live.m3u 与 /epg.xml。

官方参考：
- https://tailscale.com/docs/install/appletv
- https://tailscale.com/docs/features/tailscale-serve
- https://tailscale.com/docs/reference/tailscale-cli/serve

**严格禁止** Funnel（公网发布）、公网 8080/security group 放行、HTTP 反代监听 0.0.0.0、视频转码或视频代理、KORICE cloud fail 全局淘汰。Tailscale Serve 仅转发订阅文件/EPG 等现有本地 HTTP 路径，并不代理直播视频流。

## 3. 当前环境已核实（2026-10-09）
- li-iptv.service active；/live.m3u、/epg.xml HTTP 200。
- 8080 only 127.0.0.1。
- tailscale 未安装；tailscaled 未激活。
- UFW inactive（不等于云安全组可达，也不得据此开放公网）。
- Apple TV tailnet 账户认证尚不可验证。
- 真正 Apple TV APTV 配置/画面仍未验证。

## 4. 第一阶段 — 可无人介入完成的工作
1. 固化私有传输架构与威胁模型。
2. 核验 repo/生产服务、M3U/XMLTV 完整性和当前来源。
3. 提供只读 preflight 脚本，检查 localhost 三端点、服务身份、版本、绑定地址、危险开放端口，以及 Tailscale 安装/运行状态。
4. 制定可回滚安装、验证、证据记录方案；所有配置命令使用官方当前 CLI 语义，以版本实测为准。
5. 编写 Apple TV APTV 端操作说明及故障分支。
6. 不额外创建服务、不新增 token、不开公网端口。

## 5. Owner Gate（当前停点）
需要 Owner 在 Apple TV 和服务器所属 tailnet 登录/授权。认证只通过官方 Tailscale 网页/二维码交互，凭据不得贴到 GitHub、报告或聊天。安装任何额外网络客户端前先确认服务器现有 VPN/路由冲突风险。

## 6. 第二阶段 — 取得授权后直接执行
1. 安装、验证 tailscaled，不启用 exit node/subnet router。
2. 用 Owner 授权流程把生产主机加入私人 tailnet。
3. 配置 **tailscale serve，禁止 funnel**，精确转发 127.0.0.1:8080。
4. 从第二个 tailnet 设备真实请求 /live.m3u、/epg.xml、/healthz，核状态、内容类型、非空、权限与 EPG 交集。
5. 复核公网端口不变、后台运行/重启恢复、日志及 ACL。
6. Apple TV Tailscale 应用加入同一 tailnet；APTV 输入私人 HTTPS M3U/XMLTV URL。
7. 真实播放 2 个固定台、1 个动态赛事（有赛事时）；校核节目单/Logo，并记录仅云端探测与家庭端播放差异。

## 7. 验收门槛
- 外部公网无法直接访问 8080，IPTV server 仍 localhost-only；
- 只有授权 tailnet 设备可请求订阅与 XMLTV；
- M3U+EPG HTTP 200 且内容完整，与生产 localhost 一致；
- APTV 能实际加载频道 + 至少一台 fixed 播放成功，EPG 至少一台匹配显示；
- dynamic 条目是当前轮，KORICE VPN 限制如实说明；
- 自动更新与重启持久性；
- 日志/报告无 signed URL 或 auth token；
- rollback 可验证，不触碰 EV-Lab；
- 真实 Apple TV 未测到时，不得写 ACCEPTED。

## 8. 交付
- TASKS/TASK-013.md
- OPERATIONS/TASK-013-PRIVATE-DELIVERY.md
- tools/task013_delivery_preflight.py
- REPORTS/TASK-013-REPORT.md（测试后填写）
- REVIEWS/TASK-013-REVIEW-01.md（独立验收后填写）

## 9. 当前边界
本阶段不进行生产网络客户端安装或账号授权；Apple TV 需 Owner 物理参与。达到 Gate 时如实停点，不以 localhost 可访问冒充 Apple TV 可访问。