"""TASK-012: 拉取生产 reliability 采集脚本（sha 校验）。"""
import hashlib
import sys
import urllib.request

URL = ("https://raw.githubusercontent.com/lixiuzuapple-a11y/li-iptv-aggregator/"
       "main/tools/prod_reliability_t012.py")
DEST = "/home/ubuntu/t012/prod_reliability_t012.py"
SIZE = 2091
SHA = "c9186e6bd963"

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
print("OK", len(data), got)
