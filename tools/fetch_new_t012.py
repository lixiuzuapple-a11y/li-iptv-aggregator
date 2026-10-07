"""补拉 TASK-012 新增文件（幂等，sha 校验）。

生产机已通过 fetch_mods.py 拿到 errors/stability/preflight/reliability。
本脚本只补 cadence.py / retention.py / failover_verify_t012.py 三个，
并覆盖更新过的 reliability.py。
"""
import hashlib
import os
import sys
import urllib.request

BASE = "https://raw.githubusercontent.com/lixiuzuapple-a11y/li-iptv-aggregator/main"
LIP = "/home/ubuntu/t012pkg/liptv"
TLS = "/home/ubuntu/t012pkg/tools"

FILES = [
    (BASE + "/liptv/cadence.py", LIP, 8748, "922fe0e25278"),
    (BASE + "/liptv/retention.py", LIP, 7540, "603fefadc699"),
    (BASE + "/liptv/reliability.py", LIP, 21487, "2356be5d7ad1"),
    (BASE + "/tools/failover_verify_t012.py", TLS, 12786, "ce44d185f013"),
]

os.makedirs(LIP, exist_ok=True)
os.makedirs(TLS, exist_ok=True)
rc = 0
for url, folder, size, sha in FILES:
    name = url.rsplit("/", 1)[-1]
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
    except Exception as exc:  # noqa: BLE001
        print("FAIL", name, type(exc).__name__, str(exc)[:80])
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
    with open(os.path.join(folder, name), "wb") as fh:
        fh.write(data)
    print("OK", name, len(data), got)
print("RC", rc)
sys.exit(rc)
