# smart-crawler

**A reusable crawler for public university / master's-programme data · 可复用的院校/硕士项目公开数据采集系统**

📖 [English](#english) · [简体中文](#简体中文)

---

## English

A crawler built around an **adaptive tier-escalation** strategy: **TLS-impersonation HTTP (curl_cffi) → real browser (Playwright) → Firecrawl (paid escape hatch)**. It always starts from the cheapest tier and only escalates on failure. It ships with per-domain rate limiting, resumable runs, per-domain memory, and marks failures as `needs_manual` — it never fabricates data. Output is structured JSON (aligned with the "留学指南针" project's `submitCollectionResult` contract).

> **Lean by design:** ~1200 lines of core Python; the only third-party dependencies are `scrapling` (which bundles curl_cffi + Playwright + patchright) and `httpx`. It does **not** vendor or clone any other crawler repo. See `DESIGN.md` for design & review-gate notes, `SKILL.md` for the AI-skill description, and `docs/report-crawler-research.md` for the technology survey.

### Fetch strategy (~8/9 pages covered in testing, from ~1 s/page)

| Tier | Engine | When |
|---|---|---|
| ① http-tls | curl_cffi TLS/JA3 impersonation | The workhorse — the vast majority of public pages |
| ② browser | Playwright real browser | Purely client-side-rendered SPAs |
| ③ firecrawl | Firecrawl `proxy:stealth` (paid) | Escape hatch for hard sites that block datacenter IPs (Profile B lacks this tier → mark `needs_manual`) |

### Two machine profiles

- **Profile A:** has the `codex` CLI + `FIRECRAWL_API_KEY`. Full setup.
- **Profile B:** no codex → wire up a local/hosted agent (`LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`, OpenAI-compatible endpoint, Ollama included); no Firecrawl → drop `"firecrawl"` from `fetch.tiers` in the config.

### Quick start (incl. moving to a new machine / using it as a skill)

```
git clone <this repo> smart-crawler && cd smart-crawler

# 1. One-shot install (creates a Python 3.10+ venv + installs deps + downloads the browser + generates config)
powershell -ExecutionPolicy Bypass -File setup.ps1     # Windows (conda-only machines: add -Python <that env's python.exe>)
bash setup.sh                                          # Linux/macOS

# 2. Configure secrets (env only, never written to a file)
#    Profile A: $env:FIRECRAWL_API_KEY='fc-...'  (add $env:FIRECRAWL_PROXY='http://127.0.0.1:7897' if you run a local proxy)
#    Profile B: edit the llm block in crawler.config.json, set $env:LLM_BASE_URL / LLM_API_KEY

# 3. Run (use the python inside the .venv that setup created)
.venv\Scripts\python.exe run.py --targets targets.validate.json --concurrency 8
```

`targets.*.json` looks like `[{"url":"https://.../msc","type":"programme"},{"url":"...","type":"university"}]` (`type` is only `programme` / `university`). Add `--resume` to continue an interrupted run.

**Using it as a Claude Code skill:** drop the whole directory into `~/.claude/skills/smart-crawler/` (global) or a project's `.claude/skills/smart-crawler/` — it will be auto-discovered and triggered (see `SKILL.md`).

### Layout / portability notes (the key to "fully usable on a fresh machine")

- **Committed (present right after clone):** 8 `.py` files + `requirements.txt` + `crawler.config.example.json` + `setup.ps1/sh` + `setup.md` + `DESIGN.md` + `SKILL.md` + `README.md` + `targets.*.json` (examples) + `_test_worker_isolation.py` (regression test) + `docs/`.
- **Not committed (blocked by `.gitignore`, generated/set per machine):** `.venv/` (deps), `crawler.config.json` (setup copies it from the example), `.env` / secrets (env variables — hard rule: never committed), `out/` / `state.json` / `domain-memory.json` (run artifacts).

→ **Fully usable on a new machine = clone → run setup (pulls deps + browser) → set env secrets → run.** Zero hard-coded local paths in the code (audited); the clone→setup→run end-to-end flow has been tested on a fresh `.venv` and produces usable data.

### Output & ingestion ("留学指南针" project only, optional)

The output `out/payload-*.json` is a `submitCollectionResult` envelope. Ingestion is a **separate step**: first `admin.createCollectionTask` to get a `task_id`, then `admin.submitCollectionResult({task_id, ...payload})` → it lands as `pending_review` → materialized only after human review in the admin backend (upholding rule 14: collected data must always pass human review).

### Red lines

Only scrapes public facts; no login / no reverse-engineering signed APIs / no PII; `contains_privacy` is always false; output must pass human review; throttles politely, stops on CAPTCHA and never cracks it.

### Quality

Polished through a three-stage adversarial self-review gate (design / code / acceptance — each an independent adversarial subagent review): 6 🔴 defects were caught and fixed (concurrency-pool starvation / proxy race / broken host rate-limiting / double credit charge / worker isolation / contract envelope, etc.), and ~460 lines of over-engineering were cut per the Karpathy guidelines. Details in `DESIGN.md`.

---

## 简体中文

把"多档抓取 + 自适应升级"打法做成的采集器：**TLS 伪装 HTTP（curl_cffi）→ 真浏览器（Playwright）→ Firecrawl 付费逃生**，自动从最便宜档起跑、失败才升级；自带按域限流、断点续跑、按域记忆、失败标 `needs_manual` 从不编造。产物是结构化 JSON（可对齐留学指南针 `submitCollectionResult` 契约）。

> **很精瘦**：核心 ~1200 行 Python，第三方依赖**只有 `scrapling`（内含 curl_cffi+Playwright+patchright）+ `httpx`**。不打包/不 clone 任何其它爬虫仓库。设计与门禁记录见 `DESIGN.md`；AI 技能说明见 `SKILL.md`；选型调研见 `docs/report-crawler-research.md`。

### 抓取策略（实测覆盖约 8/9，1 秒/页起）

| 档 | 引擎 | 何时 |
|---|---|---|
| ① http-tls | curl_cffi TLS/JA3 伪装 | 主力，绝大多数公开页 |
| ② browser | Playwright 真浏览器 | 纯前端渲染 SPA |
| ③ firecrawl | Firecrawl `proxy:stealth`（付费） | 封机房 IP 的硬站逃生（画像B 无此档→标 needs_manual） |

### 两种机器画像

- **画像A**：有 `codex` CLI + `FIRECRAWL_API_KEY`。满配。
- **画像B**：无 codex → 接本地/线上 agent（`LLM_BASE_URL`/`LLM_MODEL`/`LLM_API_KEY`，OpenAI 兼容口，含 Ollama）；无 Firecrawl → config 的 `fetch.tiers` 去掉 `"firecrawl"`。

### 快速上手（含换新机器 / 当技能用）

```
git clone <本仓库地址> smart-crawler && cd smart-crawler

# 1. 一键装（建 Python3.10+ venv + 装依赖 + 下浏览器 + 生成 config）
powershell -ExecutionPolicy Bypass -File setup.ps1     # Windows（只有 conda 的机器加 -Python <该env的python.exe>）
bash setup.sh                                          # Linux/mac

# 2. 配密钥（只走 env，绝不写文件）
#    画像A: $env:FIRECRAWL_API_KEY='fc-...'  （本机有代理再 $env:FIRECRAWL_PROXY='http://127.0.0.1:7897'）
#    画像B: 改 crawler.config.json 的 llm 段，设 $env:LLM_BASE_URL / LLM_API_KEY

# 3. 跑（用 setup 建好的 .venv 里的 python）
.venv\Scripts\python.exe run.py --targets targets.validate.json --concurrency 8
```

`targets.*.json` 形如 `[{"url":"https://.../msc","type":"programme"},{"url":"...","type":"university"}]`（`type` 只能 programme/university）。断点续跑加 `--resume`。

**当 Claude Code 技能用**：把整个目录放进 `~/.claude/skills/smart-crawler/`（全局）或项目 `.claude/skills/smart-crawler/`，即可被自动发现触发（见 `SKILL.md`）。

### 目录 / 移植说明（"换台机器完整用"的关键）

- **入库（clone 就有）**：8 个 `.py` + `requirements.txt` + `crawler.config.example.json` + `setup.ps1/sh` + `setup.md` + `DESIGN.md` + `SKILL.md` + `README.md` + `targets.*.json`（示例）+ `_test_worker_isolation.py`（回归测试）+ `docs/`。
- **不入库（`.gitignore` 挡掉，每台机自己生成/设置）**：`.venv/`（依赖）、`crawler.config.json`（setup 从 example 复制）、`.env`/密钥（env 变量，红线不入库）、`out/`/`state.json`/`domain-memory.json`（运行产物）。

→ **新机器完整用 = clone → 跑 setup（补依赖+浏览器）→ 设 env 密钥 → 跑。** 代码零硬编码本机路径，已审计；已在全新 `.venv` 上实测 clone→setup→run 端到端产出可用数据。

### 产物与投喂（留学指南针项目专用，可选）

产物 `out/payload-*.json` 是 submitCollectionResult 信封。投喂是**独立一步**：先 `admin.createCollectionTask` 拿 `task_id`，再 `admin.submitCollectionResult({task_id, ...payload})` → 落 `pending_review` → 后台人工审核物化（守 rule14，采集数据必经人工审核）。

### 红线

只抓公开事实；不登录/不逆向签名/不抓 PII；`contains_privacy` 恒 false；产物必经人工审核；控频守礼、遇验证码停不破解。

### 质量

经三段对抗式自检门禁（设计/代码/验收，每段独立 subagent 对抗审查）打磨：共抓修 6 个 🔴（并发池饥饿/代理竞态/host 限流失效/credit 双扣/worker 隔离/契约信封等），并按 Karpathy 准则砍去 ~460 行过度设计。详见 `DESIGN.md`。
