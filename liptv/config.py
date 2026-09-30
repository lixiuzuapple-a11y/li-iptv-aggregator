"""配置加载：TOML（stdlib tomllib），有默认值，配置文件缺失也能跑。"""

from __future__ import annotations

import pathlib
import tomllib
from typing import Any

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
        # 动态纳入规则（简单、配置化、可解释；默认只纳入明确标识为赛事的白名单分组）
        "dynamic": {
            "include_groups": ["正在直播", "即将开始", "赛事回放"],
            "exclude_groups": ["宣传", "公告", "推广"],
            "exclude_group_keywords": ["✈️", "TG频道", "TG 频道", "下载", "app"],
            "replay_groups": ["赛事回放", "回放"],
            "include_replay": False,
        },
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
    return _deep_merge(DEFAULT_CONFIG, user_cfg)


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
