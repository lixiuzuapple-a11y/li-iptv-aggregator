#!/usr/bin/env python3
"""离线替身 `systemctl`（TASK-006 §11）。

为什么要它：部署/升级/回滚流程必须能在**没有 systemd 的机器**（开发机、容器、CI）上
离线验证，且**绝不能**碰真实 ``/etc/systemd/system`` 或真实服务。

行为：

* 把每次调用**原样追加**到 ``$FAKE_SYSTEMCTL_LOG``（每行一个 JSON），供测试断言流程顺序；
* ``is-active`` / ``is-enabled`` 从 ``$FAKE_SYSTEMCTL_STATE``（JSON）里读答案；
* ``$FAKE_SYSTEMCTL_FAIL`` 里列出的 action 一律返回 1（用于注入失败路径）；
* 退出码遵循 systemctl 惯例：``is-active`` 非 active 返回 3，其余失败返回 1。

接缝说明：``liptv deploy --systemctl tools/fake_systemctl.py`` 会把 ``*.py`` 当成脚本、
用当前解释器执行（见 ``liptv/deploy._cmd_for``），所以 Windows 上也能跑。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

DEFAULT_STATE = {"active": "inactive", "enabled": "disabled"}


def _log(entry: dict) -> None:
    target = os.environ.get("FAKE_SYSTEMCTL_LOG")
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    if target:
        path = pathlib.Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
    else:
        print(line, file=sys.stderr)


def _state() -> dict:
    raw = os.environ.get("FAKE_SYSTEMCTL_STATE")
    if not raw:
        return dict(DEFAULT_STATE)
    path = pathlib.Path(raw)
    if not path.is_file():
        return dict(DEFAULT_STATE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_STATE)
    return {**DEFAULT_STATE, **(data if isinstance(data, dict) else {})}


def main(argv: list[str]) -> int:
    action = argv[0] if argv else ""
    unit = argv[1] if len(argv) > 1 else ""
    failing = {item.strip() for item in (os.environ.get("FAKE_SYSTEMCTL_FAIL") or "").split(",")}
    _log({"action": action, "unit": unit, "argv": argv})

    if action in failing:
        print(f"fake-systemctl: 注入失败 {action}", file=sys.stderr)
        return 1

    state = _state()
    if action == "is-active":
        value = str(state.get("active") or "inactive")
        print(value)
        return 0 if value == "active" else 3
    if action == "is-enabled":
        value = str(state.get("enabled") or "disabled")
        print(value)
        return 0 if value in {"enabled", "enabled-runtime", "static"} else 1

    known = {"daemon-reload", "enable", "disable", "start", "stop", "restart", "status"}
    if action not in known:
        print(f"fake-systemctl: 不支持的动作 {action!r}", file=sys.stderr)
        return 1
    print(f"fake-systemctl: {action} {unit}".strip())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
