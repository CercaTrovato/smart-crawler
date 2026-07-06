# smart-crawler

> 中文 · [English](README.en.md)

**面向公开网页的院校 / 硕士项目采集工具**：多档抓取自适应升级 + LLM 结构化抽取 + 严格白名单产出。输入一批 URL，输出结构化 JSON（院校简介、项目要求、学费、截止期、语言分等）。**抓不到就如实标 `needs_manual`，从不编造。**

核心约 1200 行 Python，第三方依赖只有 `scrapling`（内含 curl_cffi + Playwright + patchright）+ `httpx`。不打包 / 不 clone 任何其它爬虫仓库。

## 它是什么

一套「TLS 伪装 HTTP → 真浏览器 → Firecrawl 付费逃生」自适应升级的采集器：从最便宜档起跑、失败才升级；自带按域限流、断点续跑、按域记忆。抓到正文后交给 LLM（`codex` 或任意 OpenAI 兼容端点）抽取，再经严格白名单正过滤 + 别名归一，产出结构化信封。

- **输入**：带类型的 URL 列表 `[{url, type:"university"|"programme", ...}]`
- **输出**：每个目标一份 JSON 信封（`out/payload-*.json`）+ 一份 `run-report.md`

## 抓取策略（实测覆盖约 8/9，1 秒/页起）

| 档 | 引擎 | 何时用 |
|---|---|---|
| ① http-tls | curl_cffi TLS/JA3 伪装 | 主力，绝大多数公开页 |
| ② browser | Playwright 真浏览器 | 纯前端渲染 SPA |
| ③ firecrawl | Firecrawl `proxy:stealth`（付费，可选） | 封机房 IP 的硬站逃生；无此档时这类站标 `needs_manual` |

系统**自动**从 ① 起跑，失败才升 ②，再失败才升 ③——别手动强制全上浏览器 / Firecrawl，多数公开页第一档 1 秒就拿下。同域第二次直达上次成功档。

## 安装

需 Python **3.10+**（scrapling 硬要求）。

```bash
git clone https://github.com/CercaTrovato/smart-crawler.git && cd smart-crawler

# 一键装：建 .venv + 装依赖 + 下浏览器 + 从 example 生成 crawler.config.json
powershell -ExecutionPolicy Bypass -File setup.ps1   # Windows（只有 conda 的机器加 -Python <该 env 的 python.exe>）
bash setup.sh                                        # Linux / macOS
```

装的是 `scrapling[fetchers]` + `httpx`（仅此两个第三方包）+ Chromium / patchright 浏览器（约 150MB）。

## 抽取后端（二选一，必须有一个）

| 画像 | 抽取后端 | 硬站逃生 |
|---|---|---|
| **A** | `codex` CLI 在 PATH（`codex --version` 可用） | `FIRECRAWL_API_KEY`（付费，可选） |
| **B** | 任意 **OpenAI 兼容**端点（本地 Ollama / vLLM / 线上）：config `llm` 段改 `openai-compat`，设 env `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | 无 Firecrawl → config `fetch.tiers` 去掉 `"firecrawl"` |

> **密钥只走环境变量，绝不写进任何文件 / 仓库。**

## 快速上手

```bash
# targets.example.json（带类型的数组）：
# [{"url":"https://courses.leeds.ac.uk/.../data-science-msc","type":"programme"},
#  {"url":"https://en.wikipedia.org/wiki/University_of_Leeds","type":"university"}]

.venv\Scripts\python.exe run.py --targets targets.example.json --concurrency 8   # Windows
.venv/bin/python run.py --targets targets.example.json --concurrency 8           # Linux / macOS
```

产物：`out/payload-*.json`（每目标一份信封）+ `run-report.md`（每目标用了哪档 / 是否 `usable` / `needs_manual` 原因）。

## 命令行参数

| 参数 | 说明 |
|---|---|
| `--targets <file>` | **必填**。带类型的 JSON 数组，见「输入格式」 |
| `--concurrency <n>` | 全局并发，默认 8，clamp[1,32] |
| `--resume` | 断点续跑：重试上次失败的目标；再失败会在产物 warnings 里标明「第 N 次抓取失败」 |
| `--profile <generic\|studycompass>` | 输出信封形态，默认 `generic`，见「输出格式」 |
| `--lang <en\|zh>` | 自由文本语言：`en`（默认，保源语言原样）/ `zh`（转简体中文） |
| `--config <file>` | 配置文件路径，默认 `crawler.config.json` |

`--profile` / `--lang` 也可写进 `crawler.config.json` 的 `output` 段（`{"output":{"profile":"generic","lang":"en"}}`）；**命令行覆盖配置，配置覆盖默认**。

## 输入格式

`targets.json` 是一个数组，每个目标对象：

```jsonc
{
  "url": "https://courses.leeds.ac.uk/i071/data-science-msc",
  "type": "programme",          // 必填：university | programme
  "hint": "data science",       // 可选：给打分 / 国家推断的提示词
  "source_type": "official",    // 可选：显式源类型（official|wikipedia|third_party_education_site…，决定 data_confidence）
  "university_id": "LEEDS"       // 可选：项目的父院校 ID，透传进产物（generic 可选；studycompass 用作入库外键）
}
```

缺 scheme 会自动补 `https://`；`type` 非法即报错退出。

## 输出格式

每个目标产一份 JSON 信封。两种 profile：

### `generic`（默认，面向公开）

干净通用信封，无任何下游专属键：

```jsonc
{
  "target_name": "University of Leeds",
  "type": "university",
  "source": { "url": "...", "source_type": "wikipedia", "language": "en", "accessible": true },
  "data_confidence": "medium",           // high|medium|low，由源类型硬定，非 LLM 自报
  "items": [ { /* 白名单字段，见 OUTPUT-SCHEMA.md */ } ],
  "warnings": [],
  "missing_fields": ["name_cn"],         // 抽不到的字段，绝不编造
  "evidence": [ { "source_url": "...", "fetched_via": "http-tls", "fetched_at": "..." } ],
  "contains_privacy": false,             // 恒 false（仅公开事实）
  "needs_manual": false,                 // true = 关键信息没拿到（抓失败 / 无法识别实体），需人工
  "notes": "smart-crawler: tier=http-tls, attempts=1, ..."
}
```

### `studycompass`（内部契约，`--profile studycompass`）

留学指南针入库管线专用的 `submitCollectionResult` 信封（`task_type` / `import_recommendation` / `source_summary` / `agent_notes` / `university_id` 父院校语义）。去品牌改造前的行为在此 profile 下**完全保留**。

**两 profile 的完整字段规范见 [`docs/OUTPUT-SCHEMA.md`](docs/OUTPUT-SCHEMA.md)。**

## 当 Claude Code 技能用

把整个目录放进 `~/.claude/skills/smart-crawler/`（全局）或项目 `.claude/skills/smart-crawler/`，即可被自动发现触发。见 [`SKILL.md`](SKILL.md)。

## 红线（不可违反）

- 只抓**公开**页面事实；**不登录 / 不逆向签名 / 不抓 PII（个人隐私）**；产物 `contains_privacy` 恒 `false`。
- `data_confidence` 只 `high|medium|low`；核心**绝不产出** `status` / `is_simulated` 等控制 / 运营字段。
- 控频守礼；遇验证码 / challenge 自动停并标 `needs_manual`，**不破解**。
- 采集数据仅供参考，**入库 / 上线前应经人工审核**；请遵守目标站点的 robots 与服务条款。

## 目录说明

**入库**：`*.py` 源码 + `requirements.txt` + `crawler.config.example.json` + `setup.ps1/sh` + `setup.md` + `README*.md` + `DESIGN.md` + `SKILL.md` + `targets.example.json` + `_test_*.py`（测试）+ `docs/`。
**不入库**（`.gitignore` 挡掉，每台机自己生成 / 设置）：`.venv/`、`crawler.config.json`（setup 从 example 复制）、密钥（走 env）、`out/` / `state.json` / `domain-memory.json`（运行产物）。

## 测试

```bash
.venv\Scripts\python.exe _test_public.py            # 双 profile / --lang / 透传 / 回退 / 去品牌
.venv\Scripts\python.exe _test_qc_regression.py     # 质检回归（52 项）
.venv\Scripts\python.exe _test_worker_isolation.py  # 单目标失败隔离
```

## 设计与质量

架构、并发正确性、抓取升级链、以及经三段对抗式自检门禁打磨的记录见 [`DESIGN.md`](DESIGN.md)；抓取选型调研见 [`docs/report-crawler-research.md`](docs/report-crawler-research.md)。

## 许可

[MIT](LICENSE)
