"""按 sha256 校验从 GitHub raw 拉取指定工具脚本到本地目录。"""
import hashlib
import os
import sys
import urllib.request

BASE = "https://raw.githubusercontent.com/lixiuzuapple-a11y/li-iptv-aggregator/main/tools"
JOBS = [
    ("prod_stability_t012.py", 4475, "77b5a8423130"),
]
DEST = "/home/ubuntu/t012"
os.makedirs(DEST, exist_ok=True)
rc = 0
for name, size, sha in JOBS:
    try:
        with urllib.request.urlopen(f"{BASE}/{name}", timeout=120) as r:
            data = r.read()
    except Exception as exc:  # noqa: BLE001
        print("FAIL", name, type(exc).__name__, str(exc)[:80])
        rc = 1
        continue
    got = hashlib.sha256(data).hexdigest()[:12]
    if len(data) != size or got != sha:
        print("MISMATCH", name, len(data), got)
        rc = 1
        continue
    with open(os.path.join(DEST, name), "wb") as fh:
        fh.write(data)
    print("OK", name, len(data), got)
sys.exit(rc)
