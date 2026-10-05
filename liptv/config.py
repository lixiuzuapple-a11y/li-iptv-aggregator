"""配置加载：TOML（stdlib tomllib），有默认值，配置文件缺失也能跑。"""

from __future__ import annotations

import pathlib
import tomllib
from typing import Any

#: TASK-008：多动态源失败策略。``all_or_nothing`` 是 TASK-003 冻结的既有语义（默认值，
#: 保证旧配置零回归）；``isolate`` 是 TASK-008 新增的生产多源模式。
FAILURE_POLICY_ALL_OR_NOTHING = "all_or_nothing"
FAILURE_POLICY_ISOLATE = "isolate"
FAILURE_POLICIES = (FAILURE_POLICY_ALL_OR_NOTHING, FAILURE_POLICY_ISOLATE)

DEFAULT_CONFIG: dict[str, Any] = {
    "database": {
        "path": "data/liptv.sqlite3",
    },
    "output": {
        "m3u_path": "out/live.m3u",
        "keep_previous": True,
    },
    "selection": {
        # 选线窗口与最低可用阈值（见 V1_RUNTIME_FLOW §5 / §6）
        "window_days": 7,
        "max_consecutive_failures": 3,
        "min_successes": 1,
    },
    "category_order": {
        "groups": [
            "体育",
            "新闻",
            "影视",
            "纪录片",
            "港澳台",
            "国际",
            "音乐",
            "动漫",
            "儿童",
            "地方台",
            "其他",
        ],
    },
    # 远程拉取限制（TASK-002）：全部可在 config.toml 覆盖
    "fetch": {
        "timeout_seconds": 10.0,
        "max_bytes": 5_000_000,
        "max_redirects": 3,
        "user_agent": "liptv/1.0 (+personal IPTV aggregator; python-urllib)",
        # 动态赛事快照只能落在这个目录（必须被 .gitignore 忽略）
        "dynamic_tmp_dir": "out/tmp",
    },
    # 来源注册清单：用 `source-register --from-config` 同步进数据库。
    # 默认**空**，避免任何默认公网请求；示例条目见 config/config.example.toml。
    "sources": [],
    # 统一发布（TASK-003）：`publish` 的一次性组合与安全发布策略
    "publish": {
        # 发布摘要（JSON，脱敏）。必须落在被 .gitignore 忽略的目录里。
        "summary_path": "out/publish-summary.json",
        # 动态赛事条目统一归入这个分组
        "dynamic_group_title": "体育赛事（实时）",
        # 未显式 --dynamic-source 时要用的动态来源名；默认空 = 用数据库里 enabled 的动态源
        "dynamic_sources": [],
        # TASK-010 §8：手工 publish 的动态源默认语义。
        #
        # 🚨 修复的真实运维坑（TASK-009 真机发现，`publish` 不带 --dynamic 时
        # dynamic_summary.sources = []，操作者会把「根本没抓」误读成「今天没赛事」）。
        #
        # tri-state 而不是布尔：
        #   "auto"（默认）—— config 里登记了 enabled 的动态源就默认抓；
        #                  一个都没登记则维持旧行为（不联网）。
        #   true        —— 强制抓（等价旧的 --dynamic）。
        #   false       —— 显式不抓（对应新增的 --no-dynamic）。
        #
        # 为什么不是简单布尔：默认 True 会让「照搬 config.example.toml 的空白项目」
        # 也开始联网，这是 TASK-003 明确避免的行为；默认 False 又正是本轮要修的坑。
        # auto 把「是否联网」绑定到「是否真的登记了动态源」，语义可解释且零意外请求。
        "dynamic_default": "auto",
        # 动态纳入规则（简单、配置化、可解释）
        # 默认「排除法」：剔掉宣传与回放，其余分组（= 各联赛名）保留。
        # 真实上游的 group-title 是联赛名，「正在直播/赛事回放」只是注释分区标记。
        "dynamic": {
            # 留空 = 不启用白名单（只按排除规则过滤）；填了则切严格白名单模式
            "include_groups": [],
            "exclude_groups": ["宣传", "公告", "推广", "广告"],
            "exclude_group_keywords": ["✈️", "TG频道", "TG 频道", "下载", "app"],
            "replay_sections": ["赛事回放", "回放", "录像", "重播"],
            "replay_groups": ["赛事回放", "回放", "录像", "重播"],
            "include_replay": False,
            # TASK-008：多动态源失败策略。
            #   all_or_nothing（默认，= TASK-003 既有语义）：任一动态源失败 ⇒ 本轮动态全部舍弃
            #   isolate（TASK-008 生产多源模式）：一源失败只丢该源，其它成功源本轮条目继续发布
            # 非法值在 load_config() 里 fail-fast，不静默回落。
            "failure_policy": FAILURE_POLICY_ALL_OR_NOTHING,
        },
    },
    # 本地调度运行期（TASK-004）。默认值与 liptv/runtime.RuntimeSettings 保持一致
    # （tests/test_runtime.py 有一致性断言，任一处改动必须同步另一处）。
    "runtime": {
        # 完整运行周期（秒）。示例默认 3 小时。
        "interval_seconds": 10800,
        # 进程启动后是否立刻跑第一轮（false = 先等一个周期）
        "run_on_start": True,
        # 单实例锁（运行期产物；必须落在被 .gitignore 忽略的目录里）
        "lock_path": "out/liptv.lock",
        # 脱敏的轮次状态 JSON（同样必须是被忽略的运行期产物）
        "status_path": "out/runtime-status.json",
        # 「多久没成功发布算 stale」的阈值；建议为 interval_seconds 的 2–3 倍
        "stale_after_seconds": 21600,
        # 状态文件里最多保留多少条最近轮次（其余丢弃，不做无限增长）
        "status_history": 5,
        # 每轮是否自动拉取动态赛事源。默认 false = 完全不碰公网动态源。
        # TASK-010 §8：手工 publish 已改为按 [publish].dynamic_default 判定（默认 auto）。
        # 这里的默认值保持 false —— scheduler 是**长期**在后台跑的，
        # 让它默认联网等于升级即产生公网请求，风险不对称。
        # 想让 scheduler 与手工 publish 语义一致，在部署配置里显式设 true 即可。
        "include_dynamic": False,
        # include_dynamic = true 时使用的已登记动态源名；留空 = 用数据库里 enabled 的动态源
        "dynamic_sources": [],
        # 动态失败时是否整轮拒绝（默认 false = 沿用 TASK-003 的降级为只发固定频道）
        "require_dynamic": False,
    },
    # 只读 HTTP 订阅服务（TASK-004）。默认关闭，且只绑 loopback。
    "server": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 8080,
        "playlist_path": "/live.m3u",
        "health_path": "/healthz",
    },
    # 真实流测活（TASK-005）。默认值与 liptv/probe.ProbeSettings 保持一致
    # （tests/test_probe.py 有一致性断言，任一处改动必须同步另一处）。
    # **默认 enabled = false**：升级后 scheduler 不会突然去请求全部播放流。
    "probe": {
        "enabled": False,
        # 测活节点名（对应 probe 表；schema_v1.sql 已预置 windows-local / shanghai-cloud）
        "name": "windows-local",
        "location": "Windows",
        # ffprobe 可执行文件；允许写成绝对路径。本工具**不**负责下载安装 ffmpeg。
        "ffprobe_path": "ffprobe",
        # 单条流的总超时（秒）：超过即 terminate/kill，绝不允许无限挂住。
        "timeout_seconds": 12.0,
        # 交给 ffprobe 自己的分析时长（秒）→ -analyzeduration，避免长时间拉流。
        "analyze_seconds": 4.0,
        # 并发 worker 数
        "max_concurrency": 4,
        # 0 = 不额外限制条数（仍受并发限制）；正数 = 每轮最多测这么多条
        "per_round_limit": 0,
    },
}

DEFAULT_CONFIG_PATH = pathlib.Path("config/config.toml")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | pathlib.Path | None = None) -> dict[str, Any]:
    """读取配置；文件不存在时返回默认配置。"""
    cfg_path = pathlib.Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        return _deep_merge(DEFAULT_CONFIG, {})
    with cfg_path.open("rb") as handle:
        user_cfg = tomllib.load(handle)
    merged = _deep_merge(DEFAULT_CONFIG, user_cfg)
    _validate_failure_policy(merged)
    # TASK-010 §8：dynamic_default 同样 fail-fast，非法值绝不静默退回 "auto"。
    try:
        validate_dynamic_default(publish_settings(merged).get("dynamic_default", "auto"))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    return merged


def _validate_failure_policy(cfg: dict[str, Any]) -> None:
    """``[publish.dynamic].failure_policy`` 只接受白名单值，非法即报错。

    **刻意不静默回落默认值**：一个拼错的策略名如果悄悄退回 ``all_or_nothing``，
    生产多源就会退化成「一源失败全盘皆输」，而运维以为自己在跑 isolate ——
    这类「配置写了但没生效」是最难发现的故障，必须在加载时就炸。
    """
    raw = (cfg.get("publish", {}).get("dynamic", {}) or {}).get(
        "failure_policy", FAILURE_POLICY_ALL_OR_NOTHING
    )
    if not isinstance(raw, str) or raw.strip() not in FAILURE_POLICIES:
        raise ValueError(
            f"[publish.dynamic].failure_policy 只接受 {' / '.join(FAILURE_POLICIES)}，"
            f"收到 {raw!r}。请修正配置；本项目不会静默回落到默认值。"
        )


def category_order(cfg: dict[str, Any]) -> list[str]:
    """返回配置里的频道分类顺序。"""
    return list(cfg.get("category_order", {}).get("groups", []))


def source_entries(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """返回 [[sources]] 注册清单（规范化字段与默认值）。

    字段：name / kind / url / enabled（默认 False —— 未显式启用的源不会被 fetch --all 请求）。
    """
    raw = cfg.get("sources") or []
    entries: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        entries.append(
            {
                "name": name,
                "kind": str(item.get("kind", "fixed_m3u")).strip() or "fixed_m3u",
                "url": (str(item.get("url")).strip() if item.get("url") else None),
                "enabled": bool(item.get("enabled", False)),
            }
        )
    return entries


def fetch_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """返回 [fetch] 段（已与默认值合并）。"""
    return dict(cfg.get("fetch") or {})


def publish_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """返回 [publish] 段（已与默认值合并，含 [publish.dynamic] 子表）。"""
    merged = dict(DEFAULT_CONFIG["publish"])
    section = cfg.get("publish") or {}
    for key, value in section.items():
        if key == "dynamic" and isinstance(value, dict):
            merged["dynamic"] = {**merged["dynamic"], **value}
        else:
            merged[key] = value
    return merged


#: ``[publish].dynamic_default`` 的合法取值（TASK-010 §8）。
DYNAMIC_DEFAULTS = ("auto", True, False)


def validate_dynamic_default(value: Any) -> bool | str:
    """校验 ``[publish].dynamic_default``，非法值**直接抛错**。

    与 :func:`_validate_failure_policy` 同样的理由：策略名拼错却悄悄退回 "auto"，
    等于把「本轮要不要联网」的决定权交给一个没人注意的 typo。

    返回规范化后的值（``"auto"`` 或布尔）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() == "auto":
        return "auto"
    raise ValueError(
        f"[publish].dynamic_default 只允许 auto / true / false，收到 {value!r}"
    )


def resolve_include_dynamic(
    *,
    dynamic_default: Any,
    has_dynamic_sources: bool,
    cli_dynamic: bool = False,
    cli_no_dynamic: bool = False,
    require_dynamic: bool = False,
    dynamic_source_tokens: list[str] | None = None,
) -> tuple[bool, str]:
    """TASK-010 §8：决定本轮 publish 是否抓动态源，并给出**可审计的理由**。

    返回 ``(include_dynamic, reason)``。``reason`` 会进发布摘要与 CLI 输出，
    这样「为什么这轮没抓动态」永远有明确答案，而不是靠猜。

    优先级（显式 > 配置 > 自动）：

    1. ``--no-dynamic`` ⇒ 永远不抓（即使 config 说抓），理由记 ``cli_no_dynamic``；
    2. ``--dynamic`` / ``--require-dynamic`` / ``--dynamic-source`` ⇒ 抓，
       理由记 ``cli_explicit``；
    3. 配置 ``dynamic_default``：

       * ``true`` ⇒ 抓，理由 ``config_forced``；
       * ``false`` ⇒ 不抓，理由 ``config_disabled``；
       * ``"auto"`` ⇒ **只有真的登记了动态源才抓**，理由 ``auto_sources_present``
         或 ``auto_no_sources``。

    ``auto`` 的意义：一个动态源都没登记时不联网（保持 TASK-003 的零意外请求），
    登记了却因为忘了 flag 没抓才是本轮要修的坑。
    """
    tokens = list(dynamic_source_tokens or [])
    if cli_no_dynamic:
        return False, "cli_no_dynamic"
    if cli_dynamic or require_dynamic or tokens:
        return True, "cli_explicit"
    flag = validate_dynamic_default(dynamic_default)
    if flag is True:
        return True, "config_forced"
    if flag is False:
        return False, "config_disabled"
    return (True, "auto_sources_present") if has_dynamic_sources \
        else (False, "auto_no_sources")


def runtime_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """返回 [runtime] 段（已与默认值合并）。"""
    return {**DEFAULT_CONFIG["runtime"], **(cfg.get("runtime") or {})}


def server_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """返回 [server] 段（已与默认值合并）。"""
    return {**DEFAULT_CONFIG["server"], **(cfg.get("server") or {})}


def probe_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """返回 [probe] 段（已与默认值合并）。"""
    return {**DEFAULT_CONFIG["probe"], **(cfg.get("probe") or {})}
