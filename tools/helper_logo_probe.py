"""TASK-011 §8：logo 可用性校验工具。

**为什么需要它**：任务书 §8 要求 logo「HTTP(S) 可访问、content-type 是
image、**不允许 HTML 假图片**」。最后一条是真实踩过的坑 —— 有些站点对
不存在的图片返回 `200 text/html`（一个错误页），播放器会把它当 logo 显示，
界面上就是一片空白方块，比没有 logo 更糟。

本模块**只做分类判定**，不下载图片内容到磁盘（§22「不批量镜像 logo」）。
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from typing import Iterable

__all__ = ["classify_logo_response", "probe_logo_url", "probe_urls", "USER_AGENT"]

USER_AGENT = "liptv/1.0 (+personal IPTV aggregator; logo probe)"

#: 明确的图片类型前缀。``image/*`` 全收，不逐个枚举格式。
_IMAGE_PREFIX = "image/"


def classify_logo_response(status: int, content_type: str | None) -> tuple[bool, str]:
    """把 HTTP 状态 + Content-Type 判定成 logo 可用性。

    返回 ``(是否可用, 类型标签)``。类型标签取值：

    - ``image`` —— 是真图片，可用；
    - ``fake_html`` —— 200 但返回 HTML，**假图片，不可用**；
    - ``not_found`` —— 404 / 410；
    - ``http_error`` —— 其它 4xx/5xx；
    - ``no_content_type`` —— 200 但服务端没说类型，不可确认；
    - ``other_type`` —— 200 且是非图片类型（如text/plain）。

    这个函数**故意纯函数化**（不触网），这样才能被测试直接覆盖，
    也能在批量校验时复用同一套判定。
    """
    ctype = (content_type or "").split(";")[0].strip().lower()
    if status in (404, 410):
        return False, "not_found"
    if status >= 400:
        return False, "http_error"
    if status == 200:
        if ctype.startswith(_IMAGE_PREFIX):
            return True, "image"
        if "text/html" in ctype:
            return False, "fake_html"
        if not ctype:
            return False, "no_content_type"
        return False, "other_type"
    return False, "other_type"


def probe_logo_url(url: str, *, timeout: float = 12.0) -> dict:
    """探测单个 logo URL（只发HEAD，服务器不支持时退回 GET Range）。

    返回 ``{url, host, status, content_type, usable, kind, error}``。
    ``url`` 原样返回便于调用方核对，但 ``host`` 单独给出便于汇总。
    """
    from urllib.parse import urlsplit

    out = {
        "url": url,
        "host": urlsplit(url).hostname or "",
        "status": None,
        "content_type": None,
        "usable": False,
        "kind": "unknown",
        "error": None,
    }
    if not urlsplit(url).scheme.lower() in ("http", "https"):
        out["kind"] = "bad_scheme"
        out["error"] = "只接受 http/https"
        return out

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            out["status"] = int(getattr(resp, "status", 0) or 0)
            out["content_type"] = resp.headers.get("Content-Type")
    except urllib.error.HTTPError as exc:
        out["status"] = int(exc.code)
        out["content_type"] = exc.headers.get("Content-Type") if exc.headers else None
    except Exception as exc:  # noqa: BLE001 - 侦察工具必须永不崩
        out["error"] = f"{type(exc).__name__}: {exc}"

    usable, kind = classify_logo_response(
        out["status"] or 0, out["content_type"]
    )
    # HEAD 返回 405（部分静态服务器不支持）时退回一次 GET。
    if out["status"] in (0, 405, 501) and not out["error"]:
        try:
            req2 = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"}
            )
            with urllib.request.urlopen(req2, timeout=timeout) as resp:
                out["status"] = int(getattr(resp, "status", 0) or 0)
                out["content_type"] = resp.headers.get("Content-Type")
            usable, kind = classify_logo_response(out["status"], out["content_type"])
        except urllib.error.HTTPError as exc:
            out["status"] = int(exc.code)
            usable, kind = classify_logo_response(out["status"], None)
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"

    out["usable"] = usable
    out["kind"] = kind
    return out


def probe_urls(urls: Iterable[str], *, timeout: float = 12.0) -> list[dict]:
    """批量探测。**串行**执行，不并发 —— 避免把公开服务器当压测对象。"""
    return [probe_logo_url(u, timeout=timeout) for u in urls]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("用法: python tools/helper_logo_probe.py <logo-url> [more-urls...]", file=sys.stderr)
        print("只发 HEAD/Range 请求，不下载图片内容。", file=sys.stderr)
        return 2
    rows = probe_urls(args)
    for row in rows:
        mark = "OK  " if row["usable"] else "FAIL"
        print(f"{mark} {row['host']:<28} {str(row['status']):<5} "
              f"{row['kind']:<14} {row['content_type'] or ''}")
        if row["error"]:
            print(f"     error: {row['error']}")
    ok = sum(1 for r in rows if r["usable"])
    print(f"\n{ok}/{len(rows)} 可用")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())