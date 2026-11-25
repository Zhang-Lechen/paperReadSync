#!/bin/sh
# Finder may launch this script from an unrelated working directory.
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
if [ ! -x "$ROOT/.venv/bin/python" ]; then
    printf '%s\n' '未找到 macOS Python 环境，请按 README.md 的 macOS 安装步骤创建 .venv。'
    printf '%s' '按回车关闭窗口…'
    read -r unused
    exit 1
fi
export PYTHONUTF8=1
"$ROOT/.venv/bin/python" "$ROOT/scripts/start_app.py" "$@"
status=$?
if [ "$status" -ne 0 ]; then
    printf '%s' '启动失败，按回车关闭窗口…'
    read -r unused
fi
exit "$status"
