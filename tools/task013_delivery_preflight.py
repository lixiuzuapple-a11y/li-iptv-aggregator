#!/usr/bin/env python3
"""TASK-013: read-only audit for private Apple TV delivery. No secrets printed."""
from __future__ import annotations
import json
import shutil
import socket
import subprocess
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"

def request_status(path: str) -> dict:
    try:
        req = urllib.request.Request(BASE + path, method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as response:
            return {"path": path, "status": response.status}
    except urllib.error.HTTPError as exc:
        # /healthz may not implement HEAD: retry bounded GET.
        if exc.code != 405:
            return {"path": path, "status": exc.code}
    except Exception as exc:
        return {"path": path, "error_type": type(exc).__name__}
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as response:
            response.read(128)
            return {"path": path, "status": response.status}
    except Exception as exc:
        return {"path": path, "error_type": type(exc).__name__}

def command(argv: list[str]) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
        return (result.stdout.strip() or result.stderr.strip())[:300]
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"

def main() -> int:
    results = { "http": [request_status(x) for x in ("/live.m3u", "/healthz", "/epg.xml")],
        "tailscale_installed": shutil.which("tailscale") is not None,
        "tailscaled_state": command(["systemctl", "is-active", "tailscaled"]),
        "iptv_state": command(["systemctl", "is-active", "li-iptv.service"]),
        "listeners_8080": [] }
    if shutil.which("ss"):
        text = command(["ss", "-ltn"]) or ""
        results["listeners_8080"] = [line.split()[3] for line in text.splitlines()
                                      if len(line.split()) >= 4 and line.split()[3].endswith(":8080")]
    if not results["listeners_8080"]:
        try:
            sock = socket.create_connection(("127.0.0.1", 8080), timeout=1)
            sock.close()
            results["listeners_8080"] = ["localhost-reachable; ss unavailable"]
        except OSError:
            pass
    print(json.dumps(results, ensure_ascii=False, indent=2))
    expected = all(x.get("status") == 200 for x in results["http"])
    safe = not any(s.startswith(("0.0.0.0:", "[::]:", "*:")) for s in results["listeners_8080"])
    return 0 if expected and safe else 2

if __name__ == "__main__":
    raise SystemExit(main())
