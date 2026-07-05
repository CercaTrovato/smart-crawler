#!/usr/bin/env bash
# smart-crawler 一键装（Linux/mac，best-effort）。 bash setup.sh [python3.10+路径]
# 做：找/用 Python 3.10+ → 建 .venv → 装 scrapling/httpx → 下浏览器 → 生成 crawler.config.json。
set -e
cd "$(dirname "$0")"

is_py310() { command -v "$1" >/dev/null 2>&1 && "$1" -c 'import sys; exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; }

PY=""
if [ -n "$1" ] && is_py310 "$1"; then PY="$1"; fi
if [ -z "$PY" ]; then
  for c in python3.12 python3.11 python3.10 python3; do
    if is_py310 "$c"; then PY="$c"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo "未找到 Python 3.10+。装 Python 3.10+，或 conda create -n smart-crawler python=3.11 -y 后： bash setup.sh <该env的python>"
  exit 1
fi
echo "用 Python: $PY"

[ -d .venv ] || "$PY" -m venv .venv
VENVPY=".venv/bin/python"
"$VENVPY" -m pip install -U pip
"$VENVPY" -m pip install -r requirements.txt
echo "下浏览器（Chromium+patchright，~150MB+）..."
.venv/bin/scrapling install
[ -f crawler.config.json ] || cp crawler.config.example.json crawler.config.json

echo "=== 装好了 === 环境 python: $VENVPY"
echo "画像A: export FIRECRAWL_API_KEY=fc-...（有代理再 export FIRECRAWL_PROXY=...）"
echo "画像B: 改 crawler.config.json 的 llm→openai-compat、tiers 去 firecrawl，export LLM_BASE_URL/LLM_MODEL/LLM_API_KEY"
echo "跑:   $VENVPY run.py --targets targets.json --concurrency 8"
