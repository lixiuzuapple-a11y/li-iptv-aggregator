"""从 GitHub raw 拉取本任务新增模块并落盘（幂等，可重跑）。

TAT 环境里 git 协议连 GitHub 不稳（实测多次 fetch 拿不到 commit），
但 HTTPS 直连可用。逐个文件下载并**校验字节数**，不符即报错退出。
"""
import hashlib
import os
import sys
import urllib.request

BASE = "https://raw.githubusercontent.com/lixiuzuapple-a11y/li-iptv-aggregator"
REF = "main"
DEST = "/home/ubuntu/t012pkg/liptv"

FILES = [
    ("errors.py", 13766, "06535bd5c70c"),
    ("stability.py", 17329, "63e8c04deff9"),
    ("preflight.py", 23072, "55880f3d0e6d"),
    ("reliability.py", 19249, "cd9755214c65"),
]

os.makedirs(DEST, exist_ok=True)
rc = 0
for name, size, sha in FILES:
    url = f"{BASE}/{REF}/liptv/{name}"
    target = os.path.join(DEST, name)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
    except Exception as exc:  # noqa: BLE001
        print("FAIL", name, type(exc).__name__, str(exc)[:90])
        rc = 1
        continue
    if len(data) != size:
        print("SIZE_MISMATCH", name, len(data), "!=", size)
        rc = 1
        continue
    got = hashlib.sha256(data).hexdigest()[:12]
    if got != sha:
        print("SHA_MISMATCH", name, got, "!=", sha)
        rc = 1
        continue
    with open(target, "wb") as fh:
        fh.write(data)
    print("OK", name, len(data), got)

sys.exit(rc)
