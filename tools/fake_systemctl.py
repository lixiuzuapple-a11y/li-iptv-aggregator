#!/usr/bin/env python3
"""离线替身 `systemctl`（TASK-006 §11）。

为什么要它：部署/升级/回滚流程必须能在**没有 systemd 的机器**（开发机、容器、CI）上
离线验证，且**绝不能**碰真实 ``/etc/systemd/system`` 或真实服务。

行为：

* 把每次调用**原样追加**到 ``$FAKE_SYSTEMCTL_LOG``（每行一个 JSON），供测试断言流程顺序；
* ``is-active`` / ``is-enabled`` 从 ``$FAKE_SYSTEMCTL_STATE``（JSON）里读答案；
* ``$FAKE_SYSTEMCTL_FAIL`` 里列出的 action 一律返回 1（用于注入失败路径）；
* 设 ``FAKE_SYSTEMCTL_MUTABLE=1`` 后 ``start`` / ``stop`` 会**改写** ``$FAKE_SYSTEMCTL_STATE``
  里的 ``active``（真实 systemctl 有此副作用；不打开时替身严格无状态）。QA-006B 的
  「stop 之后复查必须变成 inactive」就靠这个开关离线覆盖。
* ``$FAKE_SYSTEMCTL_STOP_STATE``（QA-007C）：指定 ``stop`` 之后写入的状态，
  用来覆盖「停机卡在 deactivating / unknown」——此时门禁**不得**判停机成功。
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


def _maybe_mutate(action: str) -> None:
    """``start`` / ``stop`` 真的改变状态（仅当 ``FAKE_SYSTEMCTL_MUTABLE=1``）。

    替身默认严格无状态，于是「stop 之后 ``is-active`` 必须变 inactive」这种断言
    无法离线覆盖。打开开关后，替身会像真实 systemctl 一样改写 state 文件。

    ``$FAKE_SYSTEMCTL_STOP_STATE``（QA-007C）：正常 stop 把状态写成 ``inactive``；
    设置该变量则写入它指定的值，用来离线覆盖「stop 之后服务仍卡在
    ``deactivating`` / ``unknown`` 等过渡态」——真实 systemctl 上就是会这样，
    此时停机**不得**被判为成功。
    """
    if action not in {"start", "stop"}:
        return
    if os.environ.get("FAKE_SYSTEMCTL_MUTABLE") != "1":
        return
    raw = os.environ.get("FAKE_SYSTEMCTL_STATE")
    if not raw:
        return
    path = pathlib.Path(raw)
    state = _state()
    if action == "stop":
        override = os.environ.get("FAKE_SYSTEMCTL_STOP_STATE")
        state["active"] = override if override is not None else "inactive"
    else:
        state["active"] = "active"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")


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
    _maybe_mutate(action)
    print(f"fake-systemctl: {action} {unit}".strip())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
