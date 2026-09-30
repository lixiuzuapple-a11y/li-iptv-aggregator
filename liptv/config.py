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
