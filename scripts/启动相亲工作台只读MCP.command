#!/bin/zsh
set -e
ROOT='/Users/mac/Applications/AI文案工作台'
cd "$ROOT"
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo '未找到工作台虚拟环境，请先双击“启动 AI 文案工作台.command”完成初始化。'
  read -k 1
  exit 1
fi
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/mcp/matchmaking_readonly_mcp.py"
