param([string]$Python = "")  # 可选：直接指定一个 Python 3.11+ 解释器（应对只有 conda、py 启动器看不到的机器）
# smart-crawler 一键装（Windows）。 powershell -ExecutionPolicy Bypass -File setup.ps1 [-Python <py3.11+路径>]
# 做：找/用 Python 3.11+ → 建 .venv → 装 scrapling/httpx → 下 Chromium/patchright → 生成 crawler.config.json。
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Test-Py311($exe, $preArgs) {
  try {
    $v = & $exe @preArgs --version 2>$null
    if ($v -match 'Python (\d+)\.(\d+)') { return ([int]$Matches[1] -gt 3) -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 11) }
  } catch {}
  return $false
}

# 找 Python 3.11+：显式 -Python > py 启动器 > python。（只有 conda 的机器：先 conda create -n smart-crawler python=3.11 -y，再 -Python <该env的python.exe>）
$pyExe = $null; $pyPre = @()
if ($Python) {
  if (Test-Py311 $Python @()) { $pyExe = $Python } else { Write-Error "-Python 指向的不是 Python 3.11+"; exit 1 }
}
if (-not $pyExe) {
  foreach ($ver in '-3.12', '-3.11') {
    if ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-Py311 'py' @($ver))) { $pyExe = 'py'; $pyPre = @($ver); break }
  }
}
if (-not $pyExe -and (Get-Command python -ErrorAction SilentlyContinue) -and (Test-Py311 'python' @())) { $pyExe = 'python' }
if (-not $pyExe) {
  Write-Error "未找到 Python 3.11+。装 Python 3.11+（python.org），或用 conda：``conda create -n smart-crawler python=3.11 -y`` 后重跑 ``setup.ps1 -Python <该env里的python.exe>``。"
  exit 1
}
"用 Python: $pyExe $($pyPre -join ' ')"

# 建 venv
if (-not (Test-Path ".venv")) { & $pyExe @pyPre -m venv .venv }
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

# 装依赖 + 下浏览器
& $venvPy -m pip install -U pip
& $venvPy -m pip install -r requirements.txt
"下浏览器（Chromium+patchright，~150MB+，慢请耐心）..."
& (Join-Path $PSScriptRoot ".venv\Scripts\scrapling.exe") install

# 生成 config（不覆盖已有）
if (-not (Test-Path "crawler.config.json")) { Copy-Item "crawler.config.example.json" "crawler.config.json"; "已生成 crawler.config.json（按画像改）" }

"`n=== 装好了 ===  环境 python: $venvPy"
"下一步（密钥只走 env，绝不写文件/仓库）："
"  画像A: `$env:FIRECRAWL_API_KEY='fc-...'   本机有代理再 `$env:FIRECRAWL_PROXY='http://127.0.0.1:7897'"
"  画像B: 改 crawler.config.json 的 llm→openai-compat、tiers 去掉 firecrawl，设 `$env:LLM_BASE_URL/LLM_MODEL/LLM_API_KEY"
"  跑:   & '$venvPy' run.py --targets targets.example.json --concurrency 8"
