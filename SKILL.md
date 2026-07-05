---
name: smart-crawler
description: |
  采集海外院校/硕士项目的公开网页数据（院校简介/项目要求/学费/截止期/语言分等），产出结构化 JSON（可对齐 submitCollectionResult 契约）。
  当需要「抓大学官网/项目页」「批量采集院校项目信息」「把某留学竞品公开页转成结构化数据」时用本技能。
  它是一套「TLS 伪装 HTTP → 真浏览器 → Firecrawl 付费逃生」自适应升级的采集器，自带按域限流、断点续跑、按域记忆、失败标 needs_manual 不编造。
  不用于：登录后内容 / 逆向签名接口 / 抓个人隐私(PII)——这些是红线，禁止。
---

# smart-crawler 采集技能（独立可复用）

**本目录即完整实现**（clone 本仓库即得代码+技能；当全局技能就整目录放进 `~/.claude/skills/smart-crawler/`，或项目 `.claude/skills/smart-crawler/`）。所有命令在本目录下跑。**动手前先读 `README.md` + `setup.md` 把环境装好。**

## 何时用
- 用户要采集院校/项目的**公开**事实数据（官网、项目页），要结构化 JSON。
- 已有一批目标 URL，或能先列出目标 URL。

## 前置（两种机器画像，二选一）
- **画像A**：有 `codex` CLI（抽取）+ `FIRECRAWL_API_KEY`（硬站逃生档）。
- **画像B**：无 codex → 用本地/线上 agent（`LLM_BASE_URL`/`LLM_MODEL`/`LLM_API_KEY`，OpenAI 兼容口，含 Ollama）；无 Firecrawl → `crawler.config.json` 的 `fetch.tiers` 去掉 `"firecrawl"`。
- 环境未装好时**先跑 `setup.ps1`/`setup.sh`**，别硬跑。**密钥只走 env，绝不写进任何文件/仓库/记忆。**

## 依赖清单与安装（agent 照此装齐 = 100% 功能；无需 clone 任何其它爬虫仓库）
**本系统不依赖任何需要单独 clone 的开源爬虫项目**——全部抓取能力由一个 pip 包 `scrapling` 提供（内含 curl_cffi + Playwright + patchright）。agent 按下面装齐即可：

**A. 必装（所有画像）**
1. Python **3.10+**（Scrapling 硬要求）。
2. `<env-python> -m pip install -r requirements.txt`（= `scrapling[fetchers]` + `httpx`，仅此两个第三方包）。
3. `<env-python 所在 venv>\Scripts\scrapling install`（下 Chromium + patchright 浏览器，约 150MB）。
> 上面 1-3 一条命令搞定：`setup.ps1`（Windows）/ `setup.sh`（Linux/mac），自动建 `.venv`。

**B. 抽取后端（二选一，必须有一个，否则只抓到 HTML 抽不出结构化字段）**
- 画像A：`codex` CLI 在 PATH（`codex --version` 可用）。
- 画像B：任意 **OpenAI 兼容** agent（本地 Ollama / vLLM / 线上）——改 `crawler.config.json` 的 `llm` 段为 `openai-compat`，设 env `LLM_BASE_URL`/`LLM_MODEL`/`LLM_API_KEY`。

**C. 硬站逃生档（决定"站点覆盖率是否 100%"，可选）**
- `FIRECRAWL_API_KEY`（付费；本机有代理再设 `FIRECRAWL_PROXY`）。
- **诚实边界**：curl_cffi + Playwright 两档免费、实测覆盖约 8/9；**极少数"封数据中心 IP"的硬站只有 Firecrawl 住宅 stealth 能破**。配 Firecrawl → 站点覆盖 100%；不配 → 这类站产 `needs_manual`（如实标注、绝不假成功——这是能力边界，非代码缩水）。

## 怎么用
1. 备目标清单 `targets.json`（带类型；`type` 只能 `programme`/`university`）：
   ```json
   [{"url":"https://courses.leeds.ac.uk/.../data-science-msc","type":"programme"},
    {"url":"https://www.xxx.edu/about","type":"university"}]
   ```
2. 跑（用 setup 建好的 `.venv` 里的 python，在本目录下；画像A 先在 shell 设 `FIRECRAWL_API_KEY`）：
   ```
   .venv\Scripts\python.exe run.py --targets targets.json --concurrency 8    # Windows
   .venv/bin/python run.py --targets targets.json --concurrency 8            # Linux/mac
   ```
   `--concurrency` 默认 8、夹在 [1,32]；断点续跑加 `--resume`。
3. 产物：`out/payload-*.json`（每目标一份信封）+ `run-report.md`（每目标用了哪档/是否 usable/needs_manual 原因）。
4. 解读：`usable` 的可用；`needs_manual` 的表示采集器没拿到/没确认（**从不编造**，缺字段进 `missing_fields`），转人工。

## 自适应策略（省 credit / 别乱升级）
- 系统**自动**从最便宜档（curl_cffi）起跑，失败才升 browser，再失败才升 Firecrawl（付费）。**别手动强制全上浏览器/Firecrawl**——多数公开页第一档 1 秒就拿下。
- 按域记忆：同域第二次直达成功档。

## 红线（不可违反）
- 只抓**公开**页面事实；不登录/不逆向签名/不抓 PII；产物 `contains_privacy` 恒 false；`data_confidence` 只 `high|medium|low`；核心**绝不产出** `status/is_simulated/is_platform_verified` 等控制字段。
- 采集产物**必经人工审核**才可能上线；控频守礼；遇验证码/challenge 自动停并标 needs_manual，**不破解**。

## 排错
- 报 `crawler.config.json 不存在` → 跑 setup 或 `cp crawler.config.example.json crawler.config.json`。
- 报缺 scrapling/浏览器 → `.venv` 里 `pip install -r requirements.txt` + `scrapling install`。
- 全 needs_manual 且原因被封 → 画像B 缺 Firecrawl 逃生档，或站点反爬强；换画像A 或降频。
