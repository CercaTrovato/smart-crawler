# smart-crawler — 可复用院校/项目采集系统（设计）

> 目标：把 2026-07-05 爬虫实测总结出的"多档抓取 + 自适应升级"打法，做成**可移植、稳定、高效、智能、便捷**的采集系统 + 技能。可拷到任意机器复用，适配两种机器画像。
> 红线不变：只抓公开数据、不碰 PII、不逆向签名/不用登录态、控频尊重目标站、产物必经人工审核。

---

## 0. 设计原则
1. **自适应 > 静态**：不预猜难度；每目标从最便宜档起跑、失败升级；按域名记住成功档，下次直达。
2. **能力自适配**：一份 `crawler.config.json` 声明"这台机有什么"（哪些抓取档、哪个 LLM、并发），同一套代码在任意机器自动适配。
3. **档位=并发泳道**：每档独立并发上限，按资源成本分配；再叠一层"按域"并发/延时（防封 + 守礼）。
4. **绝不假成功**：所有档打满仍失败 → 标 `needs_manual`（附原因），从不编造。
5. **可移植/便捷**：自包含目录，一条命令跑；凭据只走 env、绝不落文件。

---

## 1. 分层架构

```
targets.json ─┐
              ▼
      [调度器 Orchestrator]  ← crawler.config.json（能力/并发/礼貌）
     并发泳道 + 按域限流 + 断点续跑
              ▼  每个目标
      [升级引擎 Escalation]  ← domain-memory.json（按域成功档记忆）
   最便宜档起跑→打分→失败升级→重试→成功记忆 / 全挂标 needs_manual
        │            │             │
        ▼            ▼             ▼
   [抓取档适配器：统一 fetch(url)->{ok,status,html,ms,tier}]
   ① http-tls(impit/curl_cffi)  ② browser(playwright)  ③ firecrawl(stealth)   ← 任一缺失则跳过
              ▼  拿到 html
      [抽取层 extract(text,schema,instr)]  ← LLM 适配器
   codex / openai-compat(任意本地或线上 agent) / ollama
              ▼
      [产物：payload/*.json  +  run-report.md  +  state.json(续跑)]
              ▼
      （本项目）投喂 submitCollectionResult（可选，独立于核心）
```

---

## 2. 配置文件（可移植性的总开关）`crawler.config.json`

```jsonc
{
  "fetch": {
    "tiers": ["http-tls", "browser", "firecrawl"],   // 场景B 去掉 "firecrawl"
    "http-tls": { "engine": "impit" },                // 或 "curl_cffi"（需 Python 边车）
    "browser":  { "engine": "playwright", "waitMs": 2500 },
    "firecrawl":{ "apiKeyEnv": "FIRECRAWL_API_KEY", "proxy": "stealth", "proxyForApiEnv": "FIRECRAWL_PROXY" }
  },
  "llm": { "provider": "codex" },                     // 见 §5 三种 provider
  "concurrency": { "global": 8, "min": 1, "max": 16,
                   "laneCaps": { "http-tls": 16, "browser": 4, "firecrawl": 3 } },
  "politeness": { "perDomain": 2, "minDelayMs": 1500 },
  "retry": { "perTier": 2, "backoffMs": 1500 },
  "network": { "bypassProxyForFetch": true }          // 抓取绕开系统代理（本机 Clash 会黑洞直连档）
}
```
> **凭据只写 env 变量名**（如 `FIRECRAWL_API_KEY`），配置文件本身绝不含明文密钥。

---

## 3. 抓取档适配器（实测证据支撑）
每档实现统一接口 `async fetchOne(url,{timeoutMs}) -> {ok,status,html,ms}`，可缺省（缺则升级链跳过）。

| 档 | 引擎 | 实测战绩 | 何时用 | 成本 |
|---|---|---|---|---|
| ① http-tls | **impit**(Node) 或 **curl_cffi**(Py) | curl_cffi 8/9、1s/页、过 Cloudflare | 主力，绝大多数页 | 极低 |
| ② browser | **Playwright** | 8/9 | 纯前端渲染 SPA | 高(内存/CPU) |
| ③ firecrawl | **Firecrawl `proxy:stealth`** | 唯一破 UCL(封机房IP站) | ①②全挂的硬站逃生 | 付费(约5×credit)+慢 |

> ⚠️ impit 的 TLS 伪装强度**待本系统实测验证**（curl_cffi 已验证）；若 impit 不达标则默认 curl_cffi 边车。见 §8 开放项。

---

## 4. 升级引擎（每目标）
```
tiers = 配置里启用且可用的档，按成本升序；若 domain-memory 有该域成功档，则从该档起跑
for tier in tiers:
    for attempt in 1..retry.perTier:
        r = tier.fetchOne(url)
        score = evaluate(r)              // §6 内容可见度 + 是否被封
        if score.usable:
            remember(domain -> tier); return r
        sleep(backoff)
mark needs_manual(url, 最后原因)          // 绝不假成功
```
- **content-visibility 打分**（复用试验区 util）：HTTP 200 不等于成功，要正文>阈值 && 命中该类型预期关键词 && 未被封，才算 usable。
- **重试**应对反爬非确定性（实测同代码复跑结果会翻）。

---

## 5. 抽取层（LLM provider 抽象——两场景的关键）
统一接口 `extract(text, jsonSchema, instructions) -> {..fields, missing_fields}`，适配器：
- **`codex`**：shell 调 `codex exec --output-schema`（复用现有 `tools/collector/lib/codex-extract.js` 思路）。
- **`openai-compat`**：HTTP POST 到任意 OpenAI 兼容端点（`{baseURLEnv, model, apiKeyEnv}`）。**这是场景B接入本地/线上 agent 的通道**——Ollama、LM Studio、DeepSeek、OpenAI、任何兼容 `/chat/completions` 或结构化输出的服务都能插。
- **`ollama`**：本地 Ollama 原生（便捷封装）。
> 场景B 用户只需在 config 填 `{"provider":"openai-compat","baseURLEnv":"LLM_BASE_URL","model":"qwen2.5","apiKeyEnv":"LLM_API_KEY"}` + 设两个 env，即可把自己的 agent 接进工作流。抽取 prompt/schema 与 provider 解耦。

---

## 6. 智能与稳定
- **按域记忆**：`domain-memory.json` 存 `域名→成功档`，跨运行持久，越跑越快。
- **内容可见度打分**：区分"真数据 / 空壳 / 封页"，驱动升级判断（非只看状态码）。
- **失败隔离**：一档崩溃（如浏览器 crash）不影响其他泳道；每档 try/catch + 超时。
- **断点续跑**：`state.json` 记已完成目标，重跑跳过；支持中断恢复。
- **礼貌/防封**：按域并发上限 + 最小间隔（守礼 + 降低被 ban，稳定性直接相关）。
- **绕代理**：`bypassProxyForFetch` 让直连档不被本机 Clash/VPN 黑洞（实测教训）。

---

## 7. 两种机器画像（明确落地）
**画像A：有 codex + 有 Firecrawl API（+ 可能其他 env）**
- `tiers:["http-tls","browser","firecrawl"]`，`llm:{provider:"codex"}`。
- 满配：硬站升级到 Firecrawl stealth；抽取用 codex。
- 需配 env：`FIRECRAWL_API_KEY`（+ 本机代理时 `FIRECRAWL_PROXY`）。

**画像B：无 codex（用本地/其他 agent API）+ 无 Firecrawl**
- `tiers:["http-tls","browser"]`（无 firecrawl 逃生档），`llm:{provider:"openai-compat",...}`。
- 抽取接本地/线上 agent（文档手把手：填 baseURL/model/key env）。
- 无 Firecrawl → 极少数"封机房IP硬站"（如 UCL）会标 `needs_manual`（附原因），而非假失败；实测覆盖仍≈8/9，够用。
- 文档明确：想补上那 ~1/9，可选装 curl-impersonate 住宅代理 或 后补 Firecrawl。

---

## 8. 技能封装
- `.claude/skills/smart-crawler/SKILL.md`：教 agent 何时用哪档（省 credit）、两画像怎么配、如何接 LLM、红线；触发词=采集院校/项目。
- `tools/smart-crawler/`：自包含、可整目录拷贝到别的机器；带 `config.example.json` + `README`（含两画像配置样例 + 一条命令跑）。
- 一条命令：`node crawl.js --targets targets.json --concurrency 8`（并发数命令行可覆盖，夹在 min/max 间）。

---

## 9. 开放项（待确认/待建时验证）
1. **impit vs curl_cffi**：建时先实测 impit 的 TLS 伪装（对 QS/Cloudflare）；达标→Node 单语言默认（最便携），否则默认 curl_cffi 边车（需目标机 Python）。
2. **并发默认**：global 默认 8、上限 16；泳道 http-tls≤16 / browser≤4 / firecrawl≤3；按域 2 并发/1.5s——可 config 覆盖。
3. **是否纳入 katana 发现层**：本机代理不兼容；设计留接口，换网络再接。

---

## 10. 复用/稳定/高效/智能/便捷 自评
- 复用：配置驱动 + 自包含目录 + 两画像文档 → 拷了就能跑。
- 稳定：失败隔离 + 超时 + 重试 + 断点续跑 + 礼貌限流 + 绕代理。
- 高效：并发泳道 + 便宜档优先 + 按域记忆（越跑越快）。
- 智能：自适应升级 + 内容可见度打分 + 记忆学习。
- 便捷：一条命令 + 命令行覆盖并发 + 清晰 run-report。

---

## 11. 自检修正与编码契约（v2 · 2026-07-05 两路对抗式自检 §16 后定稿）

> 本节是**权威编码契约**（覆盖前文冲突处）。合并了另一份 `docs/superpowers/2026-07-05-uni-crawler-系统设计spec.md`（已删，避免双设计）+ 两路对抗自检的 🔴🟡。

**A. 语言/引擎（§9.1 已实测定案）**：impit(Node) 实测 chrome/firefox/http3 三配置**全部 403 于 QS Cloudflare + 墨尔本 SPA**，弱于 curl_cffi（两者皆 200）。→ **http-tls 档 = Python curl_cffi（Scrapling Fetcher）；核心语言 Python**。放弃 Node 单语言（会丢反爬覆盖）。产物 JSON 跨语言喂 Node 采集器无碍。

**B. 产物 = 完整 submitCollectionResult 信封 + 投喂解耦（🔴）**：核心只产**本地通用 JSON** 信封 `{task_type,target_name,source_summary,data_confidence,items:[{白名单字段}],warnings,conflicts,missing_fields,raw_evidence,contains_privacy,import_recommendation,agent_notes}`。**task_id 不由核心背**（投喂前先 createCollectionTask 拿 id，独立一步）。字段走**严格白名单**（type 分院校/项目，见现有 `transform/*.js`），**白名单外一律丢弃**——尤其 `status/review_status/is_simulated/submitted_by/is_platform_verified` 等控制/运营字段**核心绝不产出**（用白名单正过滤，不用黑名单，天然焊死）。

**C. data_confidence 硬枚举（🔴）**：只 `high|medium|low`，由**采集源类型硬定**（官网=high、Wikidata 等第三方结构源=medium、竞品聚合=low），**丢弃 LLM 自报置信度**；落盘前断言 ∈ 枚举，否则报错不投喂（防后台兜底成 `simulated` 永久卡审，见 admin L976/L1458）。

**D. 并发正确性（🔴，编码契约）**：**不嵌套持有 Semaphore**。全局一个 `Semaphore(N)`（N clamp[min,max]）+ 消费者协程跑所有目标；资源上限 = 独立 `browserSem(≤4)`、`firecrawlSem(≤2)`；礼貌 = 每域 `hostLimiter`（≤2 并发 + 随机间隔）。固定获取顺序：全局 slot →(fetch 内层)host 限流 →(仅 browser/firecrawl 档)资源 sem。**credit 先扣后用**：进 T3 前 `asyncio.Lock` 临界区 `reserve(cost)`（读余额→扣减原子），失败即拒并降级；事后阀不行。浏览器 context `try/finally` 必关 + 池化 + 超时强杀。

**E. 断点续跑幂等（🔴）**：每目标稳定 `task_key`(type+归一 target+source_url)；**"已完成"只在产物成功落盘后写 `state.json`**；resume 只覆盖到"产物已落盘"，**不横跨投喂**（投喂阶段自己去重）。route-memory = `domain-memory.json` 单点更新（**非 sqlite**）。

**F. official_url/official_website 期一必拿（🟡）**：官网抓取时落地 URL 本身即官网页，从 URL/canonical 直接回填（不等 CSS 注册表）；验收加"产物官网字段非空率"。

**G. 无-Firecrawl 兜底护栏（🟡，防越界）**：单域**总尝试封顶 ≤3 档 × ≤2 重试**；代理**不对同一 URL 无限轮换到过为止**；命中验证码/challenge 页**立即停并标 needs_manual**，不破解。

**H. 场景B 抽取水密（🟡）**：openai-compat config 增 `timeoutMs/jsonMode(openai_schema|ollama_format|plain)/maxRetry`；抽取失败**必降级**（退 CSS-only + missing_fields，禁编造）；文档诚实标注"本地弱模型严格出 schema 未验证，期一保通链路+降级、不保证抽全字段"；移植 `transform/*.js` 的别名映射+枚举校验+CJK 分流 name/name_cn+toNum。

**I. 环境/setup（🟡）**：Windows-first（不假装跨 OS，Linux/mac 期二 best-effort）；Python 3.10+ 硬要求；`scrapling install` 下 Chromium+patchright ~150MB+；**用环境内 python 直调、禁 `conda run`**（卡交互）；入口设 `WindowsProactorEventLoopPolicy`；`bypassProxyForFetch` 对**所有抓取档**（Playwright 传 `proxy=direct://`），仅 Firecrawl **API 调用**走 `FIRECRAWL_PROXY`。

**J. CLI（🟡）**：`--concurrency` 默认 8、clamp[1,32]；`--targets` = 带类型 JSON `[{url,type:'university'|'programme',hint?}]`；`--resume` 默认关。

**K. 期一范围（YAGNI 收敛）**：升级链(T1 curl_cffi→T2 Playwright→T3 Firecrawl/patchright)+重试+全局并发池+每域限流+browser/firecrawl 资源 sem+json 版 route-memory & 断点续跑+codex/openai-compat 两抽取后端(timeout/重试/降级)+严格白名单产物+run-report。**踢出期一**：探测分池/动态再分池（升级链替代）、sqlite、CSS 注册表铺开、SKILL.md 完善、katana。**期一验收**=本机真跑一批英国院校+项目，产可投喂 JSON（官网字段非空、confidence 合法、无控制字段）。

**L. 隐私边界（🟡）**：产物恒 `contains_privacy=false`（仅公开事实）；扩站外评价/案例(PII)须独立设计+Owner 拍板，不在本系统加 PII 抓取。

---

## 12. MVP 建成 + 真跑验证（2026-07-05）

`tools/smart-crawler/` 8 模块（config/scoring/fetchers/escalate/extract/output/scheduler/run）按 §11 契约建成。**门禁③真跑 5 校 = 5/5 usable / 52.8s**（Leeds/NUS/HKUST/墨尔本 http-tls 亚秒命中；UCL firecrawl 直达；route-memory 让 UCL 二次直达 firecrawl、credit 仅用 5/剩 195）。产物端到端正确（UCL/Leeds 真数据、data_confidence=high、无控制字段、official_url 回填、缺字段进 missing、duration 规范化"1 年（全日制）"）。

**门禁③抓修 2 bug（过度设计致）**：① 打分关键词中英混杂 → `need=ceil(40%×11)=5` 对单语言页不可达（英文页最多命中 5 英文词）→ 改**绝对下限 2**；② browser/stealthy 传 `proxy={'server':'direct://'}` → Chromium `ERR_PROXY_CONNECTION_FAILED` → **删该参数**（bench 证明不传即可穿本机 Clash）。

**期二待办**：CSS 选择器注册表铺开、SKILL.md 打包、无-Firecrawl 降级打磨（代理池）、katana 发现层（换无代理网络）。

**两路对抗自检修复（2026-07-05，§16 后）**：已落实 🔴 全局池饥饿→队列消费者模型（删 global_sem）、`_proxy_bypassed` 跨线程 env 竞态→run.py 启动一次性清理、HostLimiter minDelay 并发失效→锁内预留发射时刻、credit 不退/双扣→firecrawl 档不重试 + CreditGate.refund + 记忆档 credit denied 可回退；🟡 `_source_type_for` 默认第三方(medium 不高估)、`_pick` fuzzy 改 token 边界（防 city 误伤 ethnicity）、codex shell=False、state/memory 原子写盘 + 损坏告警、run.py type 校验、浏览器档显式 timeout；砍镀金 删 challenge 整条 / config 合并重复分支 / kw_ratio / engine·waitMs 死配置。

---

## 13. 公开化改造（2026-07-06 · 面向公开的通用工具）

把本工具从「留学指南针专属」泛化为**面向公开的院校/项目采集工具**，§11 编码契约（红线不变量）不破。

**输出双 profile**（`build_envelope(..., profile, lang)`，由 config `output.profile` 或 CLI `--profile` 决定）：
- `generic`（默认，面向公开）：干净通用信封 `{target_name,type,source,data_confidence,items,missing_fields,warnings,evidence,contains_privacy,needs_manual,notes}`，无任何下游专属键。必填/阻塞判定放宽——university 只要有任一名称即可识别、programme 只要有 name；`name_cn`/`university_id` 视为可选（缺失只进 `missing_fields`，不阻塞、不告警）。
- `studycompass`（`--profile studycompass`）：§11 的 `submitCollectionResult` 信封原样（`task_type`/`import_recommendation`/`source_summary`/`agent_notes`/`university_id` 父院校语义），行为与改造前**完全一致**——§11 契约对此 profile 继续生效。

**自由文本语言 `--lang`**（config `output.lang`）：`en`（默认，自由文本保源语言原样）/ `zh`（转简体中文）。抽取指令拆 base（领域规则，语言无关）+ 语言尾（`make_schema_provider(lang)`）；`_normalize_duration` 跟 lang 走。**数字/分数/日期/货币/名称/枚举码/URL 两档一律保原样**，只切换自由文本字段的值。

**去品牌**：`_SOURCE_CONF`/`_source_type_for` 去 compassedu 等竞品判定；country 内置码未命中回退 LLM 原文（全球院校不丢 country）；`name_cn` 缺失的 `missing_required` 警告仅 studycompass 产。优先级 **CLI > config `output.*` > 代码默认（generic/en）**——studycompass 部署只需在自己的 `crawler.config.json` 写 `output.profile=studycompass, lang=zh`，无需改调用。

**测试**：新增 `_test_public.py`（36 项：双 profile/--lang/透传/回退/去品牌）；回归 `_test_qc_regression.py`（52 项）、`_test_worker_isolation.py` 全绿；真跑 smoke 2/2 usable（generic 默认、英文自由文本、`official_website` 无空格）。

面向公开的用户文档见 `README.md` / `README.en.md`；输入输出字段规范见 `docs/OUTPUT-SCHEMA.md`。
