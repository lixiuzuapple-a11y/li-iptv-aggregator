"""离线 fake ffprobe —— 给 TASK-005 的自动测试与演示用。

存在的理由：TASK-005 §12 要求**自动测试必须离线**（不依赖公网、也不要求 CI 装了
真实 ffprobe）。本脚本按「像 ffprobe 一样」的方式读 argv、写 stdout/stderr、给出退出码，
因此 ``liptv.probe`` 走的是**完全真实的子进程调用路径**——argv 数组、shell=False、
管道读取上限、超时 terminate/kill 全部被真实执行，只是被执行的程序是它。

**它绝不访问网络**：所有输出都由 URL 里的一段「模式标记」决定（例如
``http://media.example/ok`` / ``.../dns`` / ``.../sleep``）。

用法（与真实 ffprobe 同样的位置参数形态）::

    python tools/fake_ffprobe.py -hide_banner -version
    python tools/fake_ffprobe.py -hide_banner -v error -print_format json \\
        -show_format -show_streams -analyzeduration 4000000 -probesize 2000000 \\
        http://media.example/ok

模式标记取 URL 最后几段里「去扩展名后」命中下表的那一段；也可用环境变量
``FAKE_FFPROBE_MODE`` 强制（会覆盖 URL 推断）。

若设置了 ``FAKE_FFPROBE_STATE``（指向一个 JSON 文件的路径），则**先查该文件**：它对
「这一个 URL」给出模式，用于模拟「URL 不变、但这条线路这一轮坏了 / 又好了」——真实
ffprobe 正是如此（同一个地址不同时刻结果可以不同）。文件内容形如::

    {"http://media.example/news/index.m3u8": "dns"}

可用的键（按优先级）：完整 URL、去掉 query 的 URL、最后一段去扩展名的文件名。
值必须是下表的模式标记之一；找不到就回落到 ``FAKE_FFPROBE_MODE`` / URL 推断。

  ok         成功：H.264 1920x1080 + AAC，format 提供 bit_rate
  audio      成功：纯 AAC（无宽高、无 format bit_rate，只有音频流 bit_rate）
  nobitrate  成功：结构完整但**任何地方都没有** bit_rate（→ bitrate_kbps 必须留 NULL）
  widthless  成功：有 video 流但缺 width/height（→ 分辨率留 NULL，但**不能**判失败）
  exit1      退出码 1，stderr 只有一句无法分类的抱怨（→ PROCESS_ERROR）
  dns        退出码 1，stderr 带 DNS 特征
  tls        退出码 1，stderr 带 TLS 特征
  http404    退出码 1，stderr 带 HTTP 404 特征
  invalid    退出码 1，stderr 带 invalid data 特征
  badjson    退出码 0，但 stdout 是被截断的 JSON
  empty      退出码 0，但没有任何 audio/video 流（空媒体）
  sleep      不退出，长睡（用来验证 timeout → terminate/kill，不留孤儿）
  oversize   往 stdout 写远超上限的垃圾后退出 0（验证输出上限 + 排水不堵）
  bignoise   往 stderr 写远超上限的文本后退出 1（验证 stderr 也受限）
  echo       退出码 1，stderr 里回显**完整 URL**（用来验证 token 脱敏）
  argv       退出码 0，把收到的 argv 原样打印成 JSON（用来证明 shell 元字符没有
             被任何 shell 解释、URL 恰好是**一个**参数）

退出码约定与真实 ffprobe 一致：正常 0，出错 1。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

VERSION_LINE = (
    "ffprobe version 6.1.1-fake-offline Copyright (c) 2007-2024 the FFmpeg developers"
)
VERSION_LINE_2 = "built with fake gcc (offline test double; never touches the network)"

SLEEP_SECONDS = 60.0
OVERSIZE_BYTES = 2_000_000
BIGNOISE_BYTES = 200_000

MODE_TOKENS = (
    "ok",
    "audio",
    "nobitrate",
    "widthless",
    "exit1",
    "dns",
    "tls",
    "http404",
    "invalid",
    "badjson",
    "empty",
    "sleep",
    "oversize",
    "bignoise",
    "echo",
    "argv",
)


def _state_override(url: str) -> str | None:
    """从 ``FAKE_FFPROBE_STATE`` 指定的 JSON 文件里查「这一个 URL」的模式。"""
    raw = (os.environ.get("FAKE_FFPROBE_STATE") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(pathlib.Path(raw).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    local = url.split("?")[0]
    keys = (url, local, local.rsplit("/", 1)[-1].split(".")[0])
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip().lower() in MODE_TOKENS:
            return value.strip().lower()
    return None


def detect_mode(argv: list[str]) -> str:
    # 最后一个参数就是 URL（与真实调用形态一致）
    url = argv[-1] if argv else ""
    per_url = _state_override(url)
    if per_url is not None:
        return per_url
    forced = (os.environ.get("FAKE_FFPROBE_MODE") or "").strip().lower()
    if forced in MODE_TOKENS:
        return forced
    for segment in reversed(url.lower().split("?")[0].split("/")):
        base = segment.split(".")[0].strip()
        if base in MODE_TOKENS:
            return base
    return "ok"


def _video_stream(**extra) -> dict:
    stream = {"index": 0, "codec_name": "h264", "codec_type": "video",
              "profile": "High", "width": 1920, "height": 1080}
    stream.update(extra)
    return stream


def _audio_stream(**extra) -> dict:
    stream = {"index": 1, "codec_name": "aac", "codec_type": "audio",
              "channels": 2, "sample_rate": "48000"}
    stream.update(extra)
    return stream


def _fmt(url: str, **extra) -> dict:
    fmt = {"filename": url, "nb_streams": 2, "format_name": "hls"}
    fmt.update(extra)
    return fmt


def payload_for(mode: str, url: str) -> dict:
    if mode == "audio":
        return {"streams": [_audio_stream(bit_rate="96000")],
                "format": _fmt(url, nb_streams=1, format_name="mp3")}
    if mode == "nobitrate":
        return {"streams": [_video_stream(), _audio_stream()], "format": _fmt(url)}
    if mode == "widthless":
        stream = {"index": 0, "codec_name": "h264", "codec_type": "video"}
        return {"streams": [stream, _audio_stream(bit_rate="128000")],
                "format": _fmt(url, bit_rate="2500000")}
    if mode == "empty":
        return {"streams": [], "format": {}}
    return {
        "streams": [
            _video_stream(bit_rate="6000000"),
            _audio_stream(bit_rate="128000"),
        ],
        "format": _fmt(url, bit_rate="6128000", duration="10.000000"),
    }


def _emit(text: str) -> None:
    sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
    sys.stdout.buffer.flush()


def main(argv: list[str]) -> int:
    if "-version" in argv:
        sys.stdout.write(VERSION_LINE + "\n" + VERSION_LINE_2 + "\n")
        sys.stdout.flush()
        return 0

    url = argv[-1] if argv else ""
    mode = detect_mode(argv)

    if mode == "sleep":
        time.sleep(SLEEP_SECONDS)
        return 0

    if mode == "oversize":
        sys.stdout.buffer.write(b"X" * OVERSIZE_BYTES)
        sys.stdout.buffer.flush()
        return 0

    if mode == "argv":
        _emit(json.dumps(sys.argv[1:], ensure_ascii=False))
        return 0

    if mode == "bignoise":
        sys.stderr.buffer.write(b"E" * BIGNOISE_BYTES)
        sys.stderr.buffer.flush()
        return 1

    if mode == "badjson":
        _emit('{"streams": [{"codec_type": "video", "width": 1920')
        return 0

    if mode == "echo":
        sys.stderr.write(
            f"[https @ 0x0] Error opening input {url}: Invalid data found when processing input\n"
        )
        sys.stderr.flush()
        return 1

    if mode in ("exit1", "dns", "tls", "http404", "invalid"):
        messages = {
            "exit1": "Error opening input: generic failure without any known signature\n",
            "dns": "[https @ 0x0] Failed to resolve hostname media.example: "
                   "Name or service not known\n",
            "tls": "[tls @ 0x0] TLS error: unable to negotiate TLS/SSL: "
                   "certificate verify failed\n",
            "http404": "[https @ 0x0] Server returned 404 Not Found\n",
            "invalid": "[hls @ 0x0] Invalid data found when processing input\n",
        }
        sys.stderr.write(messages[mode])
        sys.stderr.flush()
        return 1

    _emit(json.dumps(payload_for(mode, url), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
