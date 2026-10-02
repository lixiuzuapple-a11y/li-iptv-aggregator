#!/bin/sh
# Li IPTV Aggregator — Linux 部署入口（TASK-006 §3）
#
# 只是一个**薄包装**：真正的逻辑在 `python -m liptv deploy …`（可审计的 Python 代码，
# 不是 curl | bash 那种不可审计安装）。
#
#   sudo tools/deploy_linux.sh install  --service-manager systemd --create-user
#   sudo tools/deploy_linux.sh upgrade  --service-manager systemd
#   sudo tools/deploy_linux.sh status
#   sudo tools/deploy_linux.sh rollback
#   sudo tools/deploy_linux.sh backup
#   sudo tools/deploy_linux.sh restore-db /var/lib/li-iptv-aggregator/backups/xxx.sqlite3 --yes
#
# 离线验证（不需要 root、不碰真实 /etc 与 systemd）：
#   tools/deploy_linux.sh plan --root /tmp/liptv-root --no-venv --service-manager none
#
# 解释器可用 LIPTV_PYTHON 覆盖；`--root` 模式下不需要 root 权限。

set -eu

argv0=$0
case $argv0 in
    */*) here=$(CDPATH= cd -- "$(dirname -- "$argv0")" && pwd) ;;
    *)   here=$(pwd) ;;
esac
repo=$(dirname -- "$here")

PYTHON=${LIPTV_PYTHON:-python3}
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "找不到 Python 解释器：$PYTHON（可用 LIPTV_PYTHON 指定）" >&2
    exit 2
fi

# 让 `python -m liptv` 找到本仓库（部署机上是 git clone 出来的源码树）
PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

exec "$PYTHON" -m liptv deploy "$@"
