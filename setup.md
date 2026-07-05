# smart-crawler setup（Windows 优先；Linux/mac 期二 best-effort）

## 1. Python 环境（3.10+ 硬要求 —— Scrapling 依赖 match/case 等）
用 conda 或 venv 建一个 3.10+ 环境。**建完一律用"环境内 python 直调"，勿用 `conda run`（实测会卡交互提示挂住）。**
```
conda create -n smart-crawler python=3.11 -y
# 记下环境 python 绝对路径，如 D:\anaconda3\envs\smart-crawler\python.exe，下文记作 <PY>
```

## 2. 装依赖 + 下浏览器（~150MB+，预留时间/磁盘）
```
<PY> -m pip install -r requirements.txt
# 下 Chromium + patchright：用 venv 里的 scrapling console script（注意 `-m scrapling` 不可用，scrapling 无 __main__）
<PY 同目录>\scrapling install      # Windows；Linux/mac：<PY 同目录>/scrapling install
```

## 3. 配置（两种机器画像）
```
copy crawler.config.example.json crawler.config.json
```
- **画像A（有 codex + Firecrawl）**：用默认 config；设 env `FIRECRAWL_API_KEY=fc-...`；本机有 Clash 代理时再设 `FIRECRAWL_PROXY=http://127.0.0.1:7897`（Firecrawl 是云端抓，本地只调 API，需走代理）。
- **画像B（无 codex，用本地/线上 agent；无 Firecrawl）**：config 里 `fetch.tiers` 去掉 `"firecrawl"`；`llm` 改成 `{"provider":"openai-compat","baseUrlEnv":"LLM_BASE_URL","model":"<模型>","apiKeyEnv":"LLM_API_KEY","jsonMode":"openai_schema|ollama_format|plain","timeoutMs":60000,"maxRetry":2}`；设 env `LLM_BASE_URL`（如 `http://localhost:11434/v1` 接 Ollama）、`LLM_API_KEY`（本地无鉴权可填任意占位）。**远程端点需代理出网时**（bypassProxyForFetch 会清掉进程代理并设 NO_PROXY），config 里加 `"proxyEnv":"LLM_PROXY"` 并设该 env（本地/直连端点无需）。
  - ⚠️ 诚实提示：本地弱模型严格出 JSON schema **未经本项目验证**；抽不准时系统会**降级为 CSS-only + 标 missing_fields，绝不编造**，但不保证抽全字段。

## 4. 跑
```
<PY> run.py --targets targets.json --concurrency 8
# targets.json 形如：[{"url":"https://courses.leeds.ac.uk/...","type":"programme"},{"url":"...","type":"university"}]
# 断点续跑：加 --resume（默认关）
```

## 注意（实测踩过的坑）
- **本机 Clash fake-ip 代理会黑洞"直连抓取"**（Rust/Go 类客户端超时到保留段 IP）→ config `network.bypassProxyForFetch=true`（默认开），入口一次性清进程代理 env 并设 `NO_PROXY='*'`（Playwright/curl_cffi 直连穿透；实测传 `proxy=direct://` 反而 ERR_PROXY_CONNECTION_FAILED，已不传）。**唯独 Firecrawl 的 API 调用要走代理**（`FIRECRAWL_PROXY`）；**远程 openai-compat 抽取**若需代理见画像B 的 `proxyEnv`。
- Windows + asyncio + Playwright：入口已设 `WindowsProactorEventLoopPolicy`。
- 产物只落 `out/*.json`（通用信封）；投喂 `submitCollectionResult` 是**独立一步**（需先 createCollectionTask 拿 task_id），不由采集核心编排。
