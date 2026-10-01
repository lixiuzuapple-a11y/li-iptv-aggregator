# TASK-005 Execution Report

状态：PENDING_EXECUTOR
Owner：老李
Executor：小W
Reviewer：大G
基线：TASK-004 ACCEPTED（REVIEWS/TASK-004-REVIEW-03.md）

## 1. 基线与变更
填写基线 HEAD、实现 SHA、变更文件、明确未修改的冻结模块。

## 2. ffprobe 能力与调用
可执行文件发现、版本检查、shell=False、超时/终止、stdout/stderr 限制。

## 3. ProbeObservation 与字段映射
说明 success / error_type / startup_ms / 分辨率 / bitrate / protocol / ipv_family / HTTP/connect 字段为何写值或 NULL。

## 4. 错误分类
每类错误判定依据；说明不保存 stderr 全文，不把环境错误批量写成 stream failure。

## 5. 目标 stream 选择
active fixed inventory、disabled/stale/orphan/dynamic 的过滤证据。

## 6. 并发与 SQLite
worker 模型、max_concurrency、每 stream 每轮一次、写库线程边界、停止/清理。

## 7. Scheduler 集成
fetch → sync → probe → publish 顺序；probe disabled 兼容；环境级 probe 故障策略。

## 8. Selector 一致性
真实/fake subprocess observation 写库后如何由既有 selector 选线；声明是否修改过 selector。

## 9. 离线 E2E
tools/demo_probe_pipeline.py 的真实输出与退出码。

## 10. 测试
原 269 项、新增项、总数、分组命令、退出码、环境噪声。

## 11. 可选真实 ffprobe smoke
若执行必须脱敏；若未执行明确写“未作为自动验收条件”。

## 12. 风险与范围外
多地区探针、云部署、长时 QoE、EPG/Logo、转码等均不属于本轮。

## 13. Git & Gate
git diff --check、工作区、本地/远端 SHA；最终仅置 REVIEW 并停 Gate。
