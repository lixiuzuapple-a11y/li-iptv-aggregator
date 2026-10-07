"""TASK-012 生产辅助：拉 soak 观测脚本（sha 校验）。"""
import hashlib
import os
import sys
import urllib.request

URL = ("https://raw.githubusercontent.com/lixiuzuapple-a11y/"
       "li-iptv-aggregator/main/tools/soak_observe_t012.py")
DEST = "/home/ubuntu/t012/soak_observe_t012.py"
SIZE = 6822
SHA = "515a112c60f7"

try:
    with urllib.request.urlopen(URL, timeout=120) as r:
        data = r.read()
except Exception as exc:  # noqa: BLE001
    print("FAIL", type(exc).__name__, str(exc)[:80])
    sys.exit(1)

if len(data) != SIZE:
    print("SIZE_MISMATCH", len(data), "!=", SIZE)
    sys.exit(1)
got = hashlib.sha256(data).hexdigest()[:12]
if got != SHA:
    print("SHA_MISMATCH", got, "!=", SHA)
    sys.exit(1)
with open(DEST, "wb") as fh:
    fh.write(data)
print("OK", len(data), got, DEST)
