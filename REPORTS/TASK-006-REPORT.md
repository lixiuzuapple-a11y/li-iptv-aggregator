# TASK-006 Execution Report

状态：PENDING_EXECUTOR
Owner：老李
Executor：小W
Reviewer：大G
基线：TASK-005 ACCEPTED（REVIEWS/TASK-005-REVIEW-02.md）

## 1. 基线与变更
基线 HEAD、实现 SHA、变更文件、明确未修改的冻结模块。

## 2. Linux 目录与权限
应用/配置/数据库/runtime 路径、用户/组、读写边界。

## 3. install / upgrade / rollback
脚本入口、幂等、失败语义、dry-run/plan、release 目录布局。

## 4. systemd
unit 内容、非 root、Restart、SIGTERM、hardening 与实际验证。

## 5. production config / doctor
绝对路径、ffprobe 条件检查、端口/目录/schema/锁检查，确认 doctor 无业务副作用。

## 6. SQLite backup
一致性备份方式、保留策略、恢复验证。

## 7. 网络暴露
localhost 默认、私网/VPN/反代方案；如果提供 Caddy/nginx 示例，列出安全边界。

## 8. 健康与日志
/healthz、/live.m3u、journald、脱敏验证。

## 9. 离线部署 E2E
install → start → health → backup → upgrade → rollback 的真实离线输出与退出码。

## 10. 测试
原 305 项、新增项、总数、分组命令、退出码、环境噪声。

## 11. 实机部署
若有 Linux VM：记录 distro、Python、ffprobe、systemd、实际 service/health 结果；不得粘贴密钥、完整 token 或私密 URL。
若没有主机权限：明确标记 NOT EXECUTED，不得伪造。

## 12. 风险与范围外
多探针、K8s、Docker、云厂商 IaC、EPG/Logo、视频代理等不属于本轮。

## 13. Git & Gate
`git diff --check`、工作区、本地/远端 SHA；最终只置 REVIEW 并停 Gate。
