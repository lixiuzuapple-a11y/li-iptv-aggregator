# TASK-013 — 服务器端阶段验收记录（2026-10-09）

状态：**SERVER_READY / APPLE_TV_PENDING**，尚非整项 ACCEPTED。

## 已完成（大G生产实测）
- Owner 明确同意 Tailscale 私人接入，并在 Tailscale 官方网页登录授权 VM-0-13-ubuntu；另在官方页面启用 Serve。
- Ubuntu 24.04 从官方 stable apt 仓库安装 Tailscale 1.104.1，tailscaled active。
- tailnet 内 IP：100.73.115.58；设备 DNS：`vm-0-13-ubuntu.tail8ca9c1.ts.net`。
- 配置命令：`sudo tailscale serve --bg --yes http://127.0.0.1:8080`。
- `tailscale serve status`：`https://vm-0-13-ubuntu.tail8ca9c1.ts.net` (tailnet only)，反代 `http://127.0.0.1:8080`。
- `tailscale funnel status` 亦明确显示 (tailnet only)，未启用 Funnel。
- HTTPS 独立测试通过（服务器 DNS 接管禁用，使用 curl `--resolve` 保留域名 TLS SNI，**没有**用 `-k` 跳过证书验证）：
  - `/healthz` HTTP 200，862 bytes，TLS 校验 0；
  - `/live.m3u` HTTP 200，59,988 bytes，TLS 校验 0；
  - `/epg.xml` HTTP 200，2,522,086 bytes，TLS 校验 0。
- `ip route default` 仍走 `eth0`，`RouteAll=false`，`CorpDNS=false`，`ExitNodeID=""`，未 advertise subnet route。
- IPTV 服务 active；原应用继续仅 `127.0.0.1:8080`，Serve HTTPS 只监听 `100.73.115.58%tailscale0:443`（以及 tailnet IPv6）。
- 安装前 EV-Lab `ev-lab-task0006.service` 已 inactive；本任务没有更改/启动该服务，不能宣称其由本任务验证正常。

## 实际地址（仅已授权 tailnet 设备可访问）
- M3U：`https://vm-0-13-ubuntu.tail8ca9c1.ts.net/live.m3u`
- EPG：`https://vm-0-13-ubuntu.tail8ca9c1.ts.net/epg.xml`

## 未完成
- 尚未使用 **第二台 tailnet 设备** 独立请求订阅（上面的 HTTPS 为生产主机通过本机 tailnet 地址的自测）。
- Apple TV 上的 Tailscale 登录、APTV 输入 URL、实际视频播放、EPG/Logo 呈现均待 Owner 实机操作。
- 不能把服务端 HTTP 200 当作 Apple TV 端播放成功。

## 回滚
- `sudo tailscale serve --https=443 off` 可撤销私人 HTTPS 入口（按安装版本 CLI 输出）；保持 IPTV 8080 回环监听。
- 删除尾网节点或卸载 tailscaled 需独立审查，不在本阶段自动做。

## 下一步
Owner Apple TV App Store 安装 Tailscale，以同一账号连接，再在 APTV URL 配置中输入 M3U 和 EPG。反馈至少一台固定频道实际播放与 EPG 显示结果，之后再做整项验收。