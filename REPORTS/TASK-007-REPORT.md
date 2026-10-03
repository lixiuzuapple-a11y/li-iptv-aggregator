# TASK-007 Execution Report

状态：PENDING_EXECUTOR
Owner：老李
Executor：小W
Reviewer：大G
基线：TASK-006 ACCEPTED（REVIEWS/TASK-006-REVIEW-02.md）

## 1. Phase A — 只读主机侦察
记录候选主机、OS/CPU/RAM/disk/Python/ffprobe/systemd/端口/现有服务/网络条件。明确证明 Phase A 期间 0 主机变更。

## 2. Owner 授权 Gate
记录是否获得老李对具体主机的明确部署授权。若没有，状态必须 BLOCKED / REVIEW，不得执行 Phase B。

## 3. 部署前快照
systemd active services、端口、目标目录、目标 unit、EV-Lab 状态与 checksum。

## 4. 实际变更清单
安装包、用户/组、目录、unit、配置、service start/enable。每项写命令、退出码和结果。

## 5. 权限矩阵
真实 stat：app/release/venv/config/data/unit 的 owner/group/mode。

## 6. doctor
真实 production config 下的检查结果；敏感字段必须脱敏。

## 7. ffprobe / fixed stream
probe-check、至少一条授权 fixed stream 真实 probe、probe_result 与 selector 证据。不得粘完整播放 URL。

## 8. systemd
enable/start/stop/restart、SIGTERM、EXIT_LOCKED、journal 摘要。

## 9. /healthz / live.m3u
状态、HTTP header、bytes、checksum、可解析性；不得粘完整 playlist。

## 10. upgrade / rollback
真实 A→B→A smoke；DB/live.m3u 保留证据。

## 11. EV-Lab 零伤害证明
如果共机，列部署前后 EV-Lab unit/PID/目录/unit checksum/端口对照。

## 12. 网络暴露
localhost / VPN / reverse proxy / public 的实际状态。未获 Owner 授权的外部访问必须 NOT EXECUTED。

## 13. 敏感信息检查
确认 Git、报告、日志没有 production config、真实 token、SSH key、云凭据、完整带 query stream URL。

## 14. 风险与未执行项
明确所有 NOT EXECUTED / BLOCKED 项，不得把 localhost 成功冒充公网成功。

## 15. Git & Gate
实现/部署记录 SHA、git diff --check、本地/远端一致；最终只置 REVIEW，禁止 TASK-008。
