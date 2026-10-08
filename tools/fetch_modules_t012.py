"""从 GitHub raw 拉取本任务新增模块/工具并落盘（幂等，可重跑）。

TAT 环境里 git 协议连 GitHub 不稳（实测多次 fetch 拿不到 commit），
但 HTTPS 直连可用。逐个文件下载并**同时校验字节数与 sha256 前 12 位**，
任一不符即报错退出 —— 只校验字节数曾放过一次「长度对但内容被截断」的事故。

用法：
  python3 fetch_modules_t012.py            # 拉 liptv/ 下的模块
  python3 fetch_modules_t012.py tools      # 额外拉 tools/ 下的工具
"""
import hashlib
import os
import sys
import urllib.request

BASE = "https://raw.githubusercontent.com/lixiuzuapple-a11y/li-iptv-aggregator"
REF = "main"
DEST = "/home/ubuntu/t012pkg/liptv"
DEST_TOOLS = "/home/ubuntu/t012pkg/tools"

# (子目录, 文件名, 字节数, sha256 前 12 位)
FILES = [
    ("liptv", "errors.py", 13766, "06535bd5c70c"),
    ("liptv", "stability.py", 17329, "63e8c04deff9"),
    ("liptv", "preflight.py", 23072, "55880f3d0e6d"),
    ("liptv", "reliability.py", 21487, "2356be5d7ad1"),
    ("liptv", "cadence.py", 8748, "922fe0e25278"),
    ("liptv", "retention.py", 7540, "603fefadc699"),
]

TOOLS = [
    ("tools", "failover_verify_t012.py", 12786, "ce44d185f013"),
]


def fetch(subdir, name, size, sha):
    url = f"{BASE}/{REF}/{subdir}/{name}"
    dest_root = DEST if subdir == "liptv" else DEST_TOOLS
    target = os.path.join(dest_root, name)
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
    except Exception as exc:  # noqa: BLE001
        print("FAIL", name, type(exc).__name__, str(exc)[:90])
        return 1
    if len(data) != size:
        print("SIZE_MISMATCH", name, len(data), "!=", size)
        return 1
    got = hashlib.sha256(data).hexdigest()[:12]
    if got != sha:
        print("SHA_MISMATCH", name, got, "!=", sha)
        return 1
    with open(target, "wb") as fh:
        fh.write(data)
    print("OK", subdir + "/" + name, len(data), got)
    return 0


def main():
    os.makedirs(DEST, exist_ok=True)
    os.makedirs(DEST_TOOLS, exist_ok=True)
    rc = 0
    for item in FILES:
        rc |= fetch(*item)
    if "tools" in sys.argv:
        for item in TOOLS:
            rc |= fetch(*item)
    print("RC", rc)
    sys.exit(rc)


if __name__ == "__main__":
    main()
