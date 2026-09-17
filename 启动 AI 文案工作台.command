#!/bin/zsh
# Portable macOS launcher for the local AI Copywriting Workbench.
set -u

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$APP_DIR/.venv"
PYTHON="$VENV_DIR/bin/python"

fail() {
  echo ""
  echo "启动没有完成：$1" >&2
  echo "请保留这个窗口，把上面的错误信息交给 Codex 处理。" >&2
  exit 1
}

find_python() {
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

if [[ ! -x "$PYTHON" ]]; then
  SYSTEM_PYTHON="$(find_python)" || fail "没有找到 Python 3.10 或更高版本。请先从 python.org 安装 Python 3。"
  echo "首次运行：正在创建工作台运行环境……"
  "$SYSTEM_PYTHON" -m venv "$VENV_DIR" || fail "创建 Python 运行环境失败。"
fi

if ! "$PYTHON" -c 'import fastapi, uvicorn, httpx, multipart, pydantic, pypdf, docx, openpyxl, bs4, playwright, PIL, pillow_heif' >/dev/null 2>&1; then
  echo "首次运行：正在安装所需组件，通常需要 1–5 分钟……"
  "$PYTHON" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt" || fail "组件安装失败，请检查网络后重新双击。"
fi

echo "运行环境已准备好。"
if [[ "${WORKBENCH_SETUP_ONLY:-0}" == "1" ]]; then
  exit 0
fi

cd "$APP_DIR" || fail "无法进入工作台目录。"
exec "$PYTHON" scripts/launch_workbench.py
