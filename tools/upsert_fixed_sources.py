#!/usr/bin/env python3
"""把 fixed 源清单**幂等**写进 config.toml 的 ``[[sources]]`` 段（运维工具）。

用法::

    python tools/upsert_fixed_sources.py <config-path>
    python tools/upsert_fixed_sources.py <config-path> <name> <kind> <url>

* 不带 name/kind/url ⇒ 用内置的 :data:`SOURCE_URLS`（与 ``build_fixed_seed.py`` 同源）；
* 带三元组 ⇒ 只处理这一个源，名字/类型/URL 全部由调用方给死。

🚨 **文件路径永远是第 1 个参数（``argv[1]``）**，绝不在脚本里写死路径。
   写死路径的版本一旦参数传错，会**静默污染别的文件**（TASK-008 发生过）。

幂等语义：同名 ``[[sources]]`` 块**整块原地替换**，不追加重复条目。
重复跑一百遍，结果与跑一遍相同。

写完打印字节数变化与 CR 计数 —— CR 不为 0 说明有人用 CRLF 改过文件，
需要人工确认（仓库既有文件全是 LF）。
"""
from __future__ import annotations

import pathlib
import re
import sys

#: 内置 fixed 源清单。改这里就等于改所有用本工具的环境的默认清单。
SOURCE_URLS: dict[str, str] = {
    "iptv-org-cn": "https://iptv-org.github.io/iptv/countries/cn.m3u",
    "guovin-gd-ipv4": (
        "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/ipv4/result.m3u"
    ),
}

_BLOCK_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _block_pattern(name: str) -> re.Pattern[str]:
    """匹配「从 ``[[sources]]`` 起、到下一个段头为止、且 name 等于给定值」的整块。

    用否定前瞻 ``(?!\\n\\s*\\[)`` 保证不会跨段 —— 否则会把后面的
    ``[fetch]`` / ``[[dynamic_sources]]`` 一起吞掉。
    """
    cached = _BLOCK_RE_CACHE.get(name)
    if cached is None:
        cached = re.compile(
            r"\[\[sources\]\](?:(?!\n\s*\[).)*?name\s*=\s*\""
            + re.escape(name)
            + r"\"(?:(?!\n\s*\[).)*",
            re.S,
        )
        _BLOCK_RE_CACHE[name] = cached
    return cached


def render_block(name: str, kind: str, url: str, enabled: bool) -> str:
    return (
        "[[sources]]\n"
        f'name = "{name}"\n'
        f'kind = "{kind}"\n'
        f'url = "{url}"\n'
        f"enabled = {'true' if enabled else 'false'}\n"
    )


def upsert(text: str, name: str, kind: str, url: str, enabled: bool) -> tuple[str, str]:
    """返回 ``(新文本, 'replaced'|'appended')``。"""
    block = render_block(name, kind, url, enabled)
    pattern = _block_pattern(name)
    match = pattern.search(text)
    if match is None:
        if not text.endswith("\n"):
            text += "\n"
        return text + "\n" + block, "appended"

    # 🚨 替换必须**字节幂等**：重复跑 N 次 == 跑 1 次，否则运维没法用
    #    checksum 判断「配置到底改没改」。
    #    坑在尾随空白 —— 正则的贪婪匹配会连块尾的换行/空行一起吃掉，
    #    于是每跑一次就少一个换行、下次再补，字节一直漂。
    #    做法：匹配区间不吞尾换行；替换后把「块 + 恰好一个空行」写回去，
    #    末尾块则只补一个换行（不留空行）。
    head, tail = text[: match.start()], text[match.end():]
    stripped_tail = tail.lstrip("\n")
    is_last = stripped_tail == ""
    if is_last:
        # 文件里就是这个块：留一个结尾换行，不留空行
        return head + block, "replaced"

    # 后面还有别的段：块与下一个段之间保持恰好一个空行
    return head + block + "\n" + stripped_tail, "replaced"


def resolve_sources(argv: list[str]) -> list[tuple[str, str, str, bool]]:
    """按参数决定要写哪些源。四元组 = (name, kind, url, enabled)。"""
    if len(argv) >= 5:
        return [(argv[2], argv[3], argv[4], True)]
    return [(name, "fixed_m3u", url, True) for name, url in SOURCE_URLS.items()]


def apply_to_text(text: str, sources: list[tuple[str, str, str, bool]]) -> tuple[str, list[str]]:
    actions: list[str] = []
    for name, kind, url, enabled in sources:
        text, how = upsert(text, name, kind, url, enabled)
        actions.append(f"{how}: {name}")
    return text, actions


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    if len(args) < 2:
        print("usage: upsert_fixed_sources.py <config-path> [name kind url]")
        return 2

    path = pathlib.Path(args[1])
    original_bytes = path.read_bytes()
    text = original_bytes.decode("utf-8")
    new_text, actions = apply_to_text(text, resolve_sources(args))

    path.write_text(new_text, encoding="utf-8", newline="\n")
    written = path.read_bytes()

    for line in actions:
        print(line)
    print(f"bytes {len(original_bytes)} -> {len(written)}")
    print(f"CR count = {written.count(bytes([13]))}  (应为 0)")
    print(f"path = {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
