#!/usr/bin/env python3
"""只读健康检查（TASK-006 §8）：只访问 localhost，只看两个只读路由。

    python tools/healthcheck.py --port 8080
    python tools/healthcheck.py --url http://127.0.0.1:8080 --json

退出码（可直接给监控用）：

* ``0`` 健康：``/healthz`` 业务状态 = ``ok``（**不是**只看 HTTP 200），且 ``/live.m3u``
  返回 200 且非空；
* ``1`` 降级：能连上，但业务状态是 ``stale`` / ``missing``，或 ``/live.m3u`` 返回 503；
* ``2`` 不可达：连不上 HTTP 服务；
* ``3`` 用法错误（地址非法 / 非 loopback 且未显式放行）。

``systemd active`` 不等于业务健康 —— 本脚本存在的意义就是把这两件事分开。
**永不打印播放列表正文**（里面含上游线路地址与短时签名）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from liptv import health as health_mod  # noqa: E402

EXIT_HEALTHY = 0
EXIT_DEGRADED = 1
EXIT_UNREACHABLE = 2
EXIT_USAGE = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="healthcheck", description="liptv 只读健康检查")
    parser.add_argument("--url", help="基地址（默认 http://127.0.0.1:<port>）")
    parser.add_argument("--host", default="127.0.0.1", help="主机（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8080, help="端口（默认 8080）")
    parser.add_argument("--health-path", default=health_mod.DEFAULT_HEALTH_PATH)
    parser.add_argument("--playlist-path", default=health_mod.DEFAULT_PLAYLIST_PATH)
    parser.add_argument("--timeout", type=float, default=3.0, help="单次请求超时秒数")
    parser.add_argument("--retries", type=int, default=0, help="额外重试次数（默认 0）")
    parser.add_argument("--interval", type=float, default=1.0, help="重试间隔秒数")
    parser.add_argument("--no-playlist", action="store_true",
                        help="不检查 /live.m3u（仅看 /healthz）")
    parser.add_argument("--allow-non-loopback", action="store_true",
                        help="允许检查非 loopback 地址（默认拒绝）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    base = args.url or f"http://{args.host}:{args.port}"
    try:
        if args.retries > 0:
            result = health_mod.wait_until_healthy(
                base, timeout=args.timeout * (args.retries + 1), interval=args.interval,
                health_path=args.health_path, playlist_path=args.playlist_path,
                request_timeout=args.timeout, require_playlist=not args.no_playlist,
                allow_non_loopback=args.allow_non_loopback,
            )
        else:
            result = health_mod.check_once(
                base, health_path=args.health_path, playlist_path=args.playlist_path,
                timeout=args.timeout, require_playlist=not args.no_playlist,
            )
    except health_mod.HealthError as exc:
        print(f"healthcheck: 用法错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    payload = result.to_dict()
    if result.ok:
        code = EXIT_HEALTHY
    elif result.health_http_status is None and result.playlist_http_status is None:
        code = EXIT_UNREACHABLE
    else:
        code = EXIT_DEGRADED
    payload["exit_code"] = code

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        label = {EXIT_HEALTHY: "HEALTHY", EXIT_DEGRADED: "DEGRADED",
                 EXIT_UNREACHABLE: "UNREACHABLE"}[code]
        print(f"liptv healthcheck: {label}  {result.base_url}")
        print(f"  /healthz  : http={result.health_http_status} status={result.health_status} "
              f"source={result.freshness_source}")
        if result.last_success_publish_at:
            print(f"  last ok   : {result.last_success_publish_at}  "
                  f"({result.seconds_since_last_success}s ago)")
        if not args.no_playlist:
            print(f"  /live.m3u : http={result.playlist_http_status} "
                  f"bytes={result.playlist_bytes}（不打印正文）")
        print(f"  attempts  : {result.attempts}  elapsed={result.elapsed_seconds}s")
        if result.detail:
            print(f"  detail    : {result.detail}")
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
