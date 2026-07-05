# smart-crawler — 可复用院校/项目采集系统

把"多档抓取 + 自适应升级"打法做成的采集器：**TLS 伪装 HTTP（curl_cffi）→ 真浏览器（Playwright）→ Firecrawl 付费逃生**，自动从最便宜档起跑、失败才升级；自带按域限流、断点续跑、按域记忆、失败标 `needs_manual` 从不编造。产物是结构化 JSON（可对齐留学指南针 `submitCollectionResult` 契约）。

> **很精瘦**：核心 ~1200 行 Python，第三方依赖**只有 `scrapling`（内含 curl_cffi+Playwright+patchright）+ `httpx`**。不打包/不 clone 任何其它爬虫仓库。设计与门禁记录见 `DESIGN.md`；AI 技能说明见 `SKILL.md`；选型调研见 `docs/report-crawler-research.md`。

## 抓取策略（实测覆盖约 8/9，1 秒/页起）
| 档 | 引擎 | 何时 |
|---|---|---|
| ① http-tls | curl_cffi TLS/JA3 伪装 | 主力，绝大多数公开页 |
| ② browser | Playwright 真浏览器 | 纯前端渲染 SPA |
| ③ firecrawl | Firecrawl `proxy:stealth`（付费） | 封机房 IP 的硬站逃生（画像B 无此档→标 needs_manual） |

## 两种机器画像
- **画像A**：有 `codex` CLI + `FIRECRAWL_API_KEY`。满配。
- **画像B**：无 codex → 接本地/线上 agent（`LLM_BASE_URL`/`LLM_MODEL`/`LLM_API_KEY`，OpenAI 兼容口，含 Ollama）；无 Firecrawl → config 的 `fetch.tiers` 去掉 `"firecrawl"`。

## 快速上手（含换新机器 / 当技能用）
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
**当 Claude Code 技能用**：把整个目录放进 `~/.claude/skills/smart-crawler/`（全局）或项目 `.claude/skills/smart-crawler/`，即可被自动发现触发（见 `SKILL.md`）。

## 目录 / 移植说明（"换台机器完整用"的关键）
**入库（clone 就有）**：8 个 `.py` + `requirements.txt` + `crawler.config.example.json` + `setup.ps1/sh` + `setup.md` + `DESIGN.md` + `SKILL.md` + `README.md` + `targets.*.json`（示例）+ `_test_worker_isolation.py`（回归测试）+ `docs/`。
**不入库（`.gitignore` 挡掉，每台机自己生成/设置）**：`.venv/`（依赖）、`crawler.config.json`（setup 从 example 复制）、`.env`/密钥（env 变量，红线不入库）、`out/`/`state.json`/`domain-memory.json`（运行产物）。
→ **新机器完整用 = clone → 跑 setup（补依赖+浏览器）→ 设 env 密钥 → 跑。** 代码零硬编码本机路径，已审计；已在全新 `.venv` 上实测 clone→setup→run 端到端产出可用数据。

## 产物与投喂（留学指南针项目专用，可选）
产物 `out/payload-*.json` 是 submitCollectionResult 信封。投喂是**独立一步**：先 `admin.createCollectionTask` 拿 `task_id`，再 `admin.submitCollectionResult({task_id, ...payload})` → 落 `pending_review` → 后台人工审核物化（守 rule14，采集数据必经人工审核）。

## 红线
只抓公开事实；不登录/不逆向签名/不抓 PII；`contains_privacy` 恒 false；产物必经人工审核；控频守礼、遇验证码停不破解。

## 质量
经三段对抗式自检门禁（设计/代码/验收，每段独立 subagent 对抗审查）打磨：共抓修 6 个 🔴（并发池饥饿/代理竞态/host 限流失效/credit 双扣/worker 隔离/契约信封等），并按 Karpathy 准则砍去 ~460 行过度设计。详见 `DESIGN.md`。
