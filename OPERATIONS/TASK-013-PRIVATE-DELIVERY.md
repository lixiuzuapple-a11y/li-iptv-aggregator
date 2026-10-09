# TASK-013 — 私人 Apple TV 交付操作设计

## 方案
优先 Tailscale 客户端（Apple TV tvOS 17+）+ 私人 tailnet + Tailscale Serve HTTPS 反向代理给现有 `127.0.0.1:8080`。官方文档：
- https://tailscale.com/docs/install/appletv
- https://tailscale.com/docs/features/tailscale-serve
- https://tailscale.com/docs/reference/tailscale-cli/serve

**特别区分：Tailscale Serve 与 Funnel**。只允许 Serve；绝不执行 Funnel。

## 操作 Gate
1. Owner 确认采用 Tailscale 作为本次 IPTV 私人接入方式；Apple TV 安装 Tailscale，使用官方二维码加入私人 tailnet。
2. 生产机上的网络客户端安装/认证会改变网络环境，须确认与现有腾讯云共机 EV-Lab 的 VPN、路由、DNS、IPv6 无冲突。
3. 从已授权 tailnet 设备访问服务；不通过公网 IP 或放开防火墙。

## 在正式部署前
运行 `python3 tools/task013_delivery_preflight.py` 进行只读检查；检查公网 IP 对应端口不等于批准开启公网服务。

## 获授权后（命令均须按安装版本 `--help` 验证）
- 官方源安装 Tailscale，并确保 `tailscaled` 运行。
- 通过官方授权登录，并限制只加入 tailnet，不启用 exit node、subnet router、Funnel。
- 根据官方文档配置 `tailscale serve`，代理目标 `http://127.0.0.1:8080`。
- 最终地址形态（**仅示例，不能当实际 URL**）：
  - `https://<server>.<tailnet>.ts.net/live.m3u`
  - `https://<server>.<tailnet>.ts.net/epg.xml`
- 完成第二设备 HTTP/HTTPS + 内容校核后，才向 Owner 提供真实地址。

## Apple TV
1. App Store 下载 Tailscale；打开并允许 VPN 配置、点击 Connect，用手机扫码按官方网页授权。
2. 确认 Apple TV 与 IPTV 服务器位于同一 tailnet，Tailscale 内可看见服务器。
3. 在 APTV 新建 URL 订阅，输入**实际** M3U HTTPS URL；在可填写 EPG 的模式中填同域名 `/epg.xml`。
4. 验证频道名、tvg-logo、CCTV/卫视 EPG；动态赛事无需匹配 EPG。
5. 先固定频道实播，再体育赛事，记录网络是否需家庭 VPN；不要用上海 cloud fail 淘汰家庭可播的 KORICE。

## 回滚
先 `tailscale serve ... off` 或按当前 CLI 命令 reset 私人分享，确认 IPTV 仍 `127.0.0.1:8080`；如需移除 tailnet 客户端，单独确认对共机其他用途无依赖。绝不删除 IPTV 数据库，也不清理其他项目。

## 不可冒充的验证
上海 curl localhost 200 ≠ Apple TV 连通；
tailnet 上 HTTP 200 ≠ 视频线路可播放；
播放成功 ≠ EPG 一定匹配。
