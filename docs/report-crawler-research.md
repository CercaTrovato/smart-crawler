# 爬虫技术调研与对比实验（2026-07-05）

> 背景：采集器现状是 Node 裸 `fetch`（`tools/collector/lib/fetch-page.js`），遇到 JS 渲染页/反爬页抓不到。本报告①调研 GitHub 高星开源爬虫的**真实源码本质**（不看宣传看代码），②在试验区 `tools/crawler-lab/` 对四档抓取技术做**多地区实测对比**，给出选型证据。
> 守红线：只抓公开页面事实数据、不碰 PII、控频、采集产物仍须经后台人工审核。

---

## 第一部分：15 个开源项目的"源码本质"（戳穿宣传）

方法：6 路子代理浅克隆真源码 + Grep 核心文件 + GitHub API 核对真实 star。

### 核心心智图：任何爬虫都拆三层
1. **抓取/渲染层**：纯 HTTP（快、不跑 JS、TLS 指纹假）vs 真浏览器（跑 JS、真 TLS、慢）。**这是"抓不到"的胜负手。**
2. **反检测层**：只有遇到反爬才需要。分层：TLS/HTTP2 指纹 → WebDriver 协议 → headless 特征 → JS 指纹 → 行为。**JS 层反检测治不了 TLS 层拦截。**
3. **抽取层**：启发式正文（readability/trafilatura）vs CSS/XPath 选择器（精确字段最稳）vs LLM（乱页兜底，慢/贵/会幻觉）。

### 真实源码体检（真 star + 代码真身）

| 组 | 项目 | 真实★ | 语言/许可 | 代码里的真身（≠宣传） |
|---|---|---|---|---|
| AI爬虫 | Firecrawl | 144k | TS/**AGPL** | undici抓→Playwright微服务→**闭源 fire-engine**；强反爬全在闭源云服务，自建版缩水+AGPL传染 |
| AI爬虫 | Crawl4AI | 71k | Py/Apache-2.0 | **就是 Playwright**+html2text+**TF-IDF 统计判停**（"Adaptive Intelligence"名不副实）；LLM可选默认关 |
| AI爬虫 | ScrapeGraphAI | 28k | Py/MIT | langchain Chromium loader + 抓→解析→**LLM** 图；LLM强制；抓取层最弱 |
| 框架 | Scrapy | 63k | Py | Twisted异步HTTP+lxml；**不跑JS**（要外挂scrapy-playwright）；无反检测 |
| 框架 | Colly | 25k | Go | net/http+goquery；**不跑JS**；最快最省内存 |
| 框架 | Crawlee | 24k | **TS/Node** | **双栈**CheerioCrawler(HTTP)+PlaywrightCrawler(浏览器)；**指纹注入默认开**+队列/重试内建 |
| 引擎 | Puppeteer | 95k | TS/Node | CDP over WebSocket；默认`--enable-automation`→webdriver=true，几乎必配stealth；基本只Chrome |
| 引擎 | Playwright | 92k | TS(多语言) | CDP(remote-debugging-pipe更隐蔽)；**默认指纹最干净**；跨浏览器+网络拦截(可直取SPA的XHR) |
| 引擎 | Selenium | 34k | Java+ | W3C WebDriver(HTTP)；webdriver=true**规范强制**+cdc_泄漏；最重最易被识破 |
| 反检测 | undetected-chromedriver | 13k | Py | 给chromedriver二进制打补丁去cdc_；**已半停更、思路过时** |
| 反检测 | nodriver | 4.4k | Py | UC官方继任者·活跃；**纯CDP不用webdriver→源头无指纹**；暴露面最小 |
| 反检测 | puppeteer-extra-stealth | 7.4k | JS | 16个JS evasion；**已停更**；学指纹泄漏点最佳教材；只打JS层 |
| 反检测 | patchright | 3.7k | TS/Py·活跃 | "隐身版Playwright"；**留Playwright生态做反检测首选** |
| 反检测 | botasaurus | 5.5k | Py | CDP+**唯一自带拟人鼠标/延时**(行为层)+Cloudflare提示 |
| 抽取 | readability(Mozilla) | 11k | JS | Firefox阅读模式；DOM文本密度打分；**只抽正文不抓取** |
| 抽取 | trafilatura | 6.2k | Py | lxml启发式+级联；正文抽取学术标杆；**只抽正文不抓取** |
| 抽取 | jina-reader | 11k | TS | Puppeteer渲染+readability+→markdown(+可选LLM)；"渲染+抽取"标准栈SaaS化 |

### 国产/中文社区两利器
- **DrissionPage**(12k,Py)：SessionPage=requests直HTTP；ChromiumPage=**自撸CDP over websocket绕开Selenium**；两模式合一共享cookie。**"HTTP优先、浏览器兜底、同一API"是采集器该有的架构原则。**
- **MediaCrawler**(55k,Py)：开源版是**引流版**（强能力在闭源Pro）；套路=Playwright保登录态+**借站点自身JS算签名(x-s/a_bogus)再打私有JSON接口**。**只可借鉴"优先抓公开无签名JSON接口"这一点；逆向签名/登录态/账号池一律不碰（红线）。**

### 必须戳穿的神话
1. "AI爬虫更会爬"→假，爬的部分就是Playwright，AI只在抽取且多可选。
2. Firecrawl 14万星"开源"→最能打的闭源，自建缩水+AGPL。
3. "pass ALL bot systems"→营销；纯JS反检测碰不到TLS层；真浏览器强是因自带真实TLS指纹。
4. MediaCrawler 5.5万星→开源版被降配，核心套路对合规采集不能用。

---

## 第二部分：多地区实测对比实验

### 试验区
`tools/crawler-lab/`（不属生产/SSOT）。四档引擎统一 `fetchOne(url)->{html,status,ms}` 接口，产出过同一套"正文提取+打分"，保证只对比"抓取层"差异。

### 评估指标
- **有效产出率**（总分）：可达 && 未被封 && 正文>500字 && 关键词命中≥40%
- **关键数据可见度**：产出命中几个预期关键词（区分"真数据"vs"空壳"）
- **反爬拦截**：403/验证码/Cloudflare
- **耗时** / **正文体量** / **部署成本**（定性）

### 四档引擎
① node-fetch（裸HTTP·现状基线）② got-scraping（HTTP+拟真头/TLS）③ playwright（真无头浏览器）④ playwright-stealth（真浏览器+反检测）

### 最终结果（9页×4档，已修正URL+打分口径）

| 引擎 | 有效产出 | 关键词命中 | 被封 | 报错 | 平均耗时 |
|---|---|---|---|---|---|
| playwright | **8/9** | 87% | 1 | 0 | 4.4s |
| playwright-stealth | **8/9** | 87% | 1 | 0 | 5.0s |
| got-scraping | **7/9** | 76% | 1 | 1 | 3.5s* |
| node-fetch | **6/9** | 64% | 3 | 0 | 1.0s |

*got-scraping均值被HKUST一次21s超时拖高，正常约1s。

**逐页矩阵**（✓可用/关键词/正文体量/耗时；⛔封）

| 目标页 | 类型 | playwright | got-scraping | node-fetch |
|---|---|---|---|---|
| wiki-leeds | 纯静态 | ✓4/4 87k | ✓4/4 94k | ✓4/4 94k |
| uk-leeds-ds | SSR官网 | ✓4/5 | ✓4/5 | ✓4/5 |
| uk-ucl-dsml | 官网课程页 | ✗**403⛔** | ✓**5/5** | ✗403⛔ |
| au-melb-ds | SPA(JS重) | ✓4/5 | ✗403⛔(首轮✓) | ✗403⛔ |
| sg-nus-comp | 官网项目页 | ✓4/4 | ✓4/4 | ✓4/4 |
| hk-hkust-bdt | 官网项目页 | ✓4/4 28k | ✗超时(环境) | ✓4/4 23k |
| us-gt-omscs | 官网SSR | ✓4/4 | ✓4/4 | ✓4/4 |
| qs-oxford | 反爬CF+JS | ✓**4/4 54k** | ✓4/4(首轮✗) | ✗**403⛔** |
| cn-compass | 中文竞品 | ✓4/4 | ✓4/4 | ✓4/4 |

### 关键发现
1. **三层阶梯真实**：裸fetch 6/9 → 拟真HTTP 7/9 → 真浏览器 8/9，每档买到真实覆盖率。
2. **最便宜的大赢家=升级请求指纹**：got-scraping 把 UCL 从403救成200(5/5)，几乎零成本、比浏览器快3-4倍。**多数"抓不到"根子是Node裸fetch请求指纹一眼假，换会伪装的HTTP客户端即解决。**
3. **真浏览器不可替代=硬反爬+重JS**：QS(Cloudflare)只有真浏览器稳定突破(54k)。
4. **反直觉A：真浏览器非永远更强**：UCL两轮都把无头Playwright挡了(403)，却放行got-scraping——无头Chromium自带可识别指纹(headless+机房IP)。→ **正确架构是"HTTP优先、浏览器兜底"，不是"一律上浏览器"。**
5. **反直觉B：stealth在这批目标没加分**：playwright与playwright-stealth覆盖完全相同(都8/9,UCL都403)，stealth还略慢——它只抹JS层指纹，够不着UCL的握手/IP层拦截。**别迷信stealth。**
6. **反直觉C（复跑才现形）：反爬结果非确定性**：同一份got-scraping，首轮QS被封、Melbourne通过；复跑QS通过、Melbourne被封。**同代码不同时刻结果会翻**。→ 采集必须带**重试+多档降级/升级兜底**，别指望任何单一手段稳过。
7. **中文竞品(指南者)很好抓**：4档全拿下，连裸fetch都200(SSR)。成本很低。
8. **环境提示**：got-scraping对HKUST出现到`198.18.0.48`(保留段)的21s超时——本机代理/DNS的环境产物，非工具缺陷；提醒本机代理设置会影响采集结果。

---

## 第三部分：给采集器的落地方案

### 架构原则（学 DrissionPage）：HTTP优先、浏览器兜底、同一管线、带重试

留在 **Node**（贴合 `tools/collector/` + opencli；Playwright两端能力对等，不必为它引Python）。做成**三档自动降级/升级**，而非二选一：

| 场景 | 用哪档 | 依据 |
|---|---|---|
| 多数官网/SSR/中文竞品 | **拟真HTTP**（got-scraping 或 Crawlee CheerioCrawler） | 7/9、最快、够用 |
| 上面抓到空壳/JS渲染页 | **真浏览器**（Playwright） | 补齐SPA，8/9 |
| 硬反爬(Cloudflare等) | 真浏览器（必要时+patchright） | 只有它稳过QS |
| 每档都要 | **失败重试+换档** | 反爬非确定性(发现6) |

**两条落地路线**：
- **最省事**：直接上 **Crawlee** 一个库，双栈+指纹默认开+队列/重试全包。
- **最可控**：自己在采集器加 got-scraping（第一档）+ Playwright（兜底档），反爬页叠 patchright。

**抽取层**（已有codex）：精确字段(deadline/学费/雅思)优先每校写CSS选择器(零幻觉可复核)；正文用trafilatura式；**codex LLM只兜底且必人工审核**。

**合规**：只学MediaCrawler"优先抓公开无签名JSON接口"一点；逆向签名/登录态/账号池不碰；反检测到"真浏览器+轻量stealth+限速"为止，别破验证码、别高频压站。

### 复现
```
cd tools/crawler-lab && node run-bench.js   # 结果写 results/summary.md
```
（依赖 playwright/playwright-extra/stealth/crawlee 已装；Chromium 已下载。）

---

## 第四部分：Round-2 补测（视频「11 款高星爬虫」里没覆盖的 6 个 + 真实测试）

Owner 提供了一份"11 款高星爬虫"视频总结。核对后：其中 4 款（crawlee / scrapegraph-ai / crawl4ai / firecrawl）第一部分已覆盖；**6 款是新的**，逐个啃了真源码，并对可完整测的做了真实测试（守"不接受临时/半成品测试"）。

### 6 个新工具·源码定性

| 工具 | 真实★ | 层 | 源码本质（戳穿宣传） | 处置 |
|---|---|---|---|---|
| Scrapling | 68k Py | 抓取层 | 三档合一：curl_cffi(TLS/JA3伪装)+Playwright+patchright隐身；"自适应"= SQLite存元素特征 + `difflib.SequenceMatcher` 相似度重定位，**零大模型**（视频说对了） | ✅完整测 |
| spider | 2.5k Rust | 抓取层 | HTTP优先(reqwest)+可选chrome(CDP)；Node预编译绑定免装Rust；胜在Rust级并发，单页不比HTTP客户端强 | ✅完整测 |
| llm-scraper | 6.8k TS | 抽取层 | Playwright抓页 + AI SDK + zod schema → LLM抽字段（= Scrapegraph-ai 的 Node 版）；可接本地Ollama免key | ⏭️Owner定跳过（≈现有codex，要装2GB Ollama） |
| katana | 17k Go | 发现层 | URL/端点**发现器**（爬遍站点列链接），不抽字段；预编译二进制免装Go | ⚠️实测受阻于本机代理 |
| Agent-Reach | 50k Py | 编排器 | 自己不抓，路由给 Jina/gh/opencli；抓网页= Jina Reader（与现有同层）；5万星仅4个月靠推广 | ❌跳过（冗余） |
| BrowserAct | 3.6k(外壳) | SaaS | GitHub仓库只是"技能说明书"，真引擎闭源付费；反爬浏览器SaaS | ❌跳过（闭源付费+合规冲突） |

### 完整实测结果

**Node 5 档矩阵（同轮，spider 已修二进制）**：

| 引擎 | 有效产出 | 平均命中 | 平均耗时 | 只栽在 |
|---|---|---|---|---|
| playwright | 8/9 | 87% | 4.9s | UCL |
| playwright-stealth | 8/9 | 87% | 4.9s | UCL |
| got-scraping | 7/9 | 76% | 1.3s | QS(本轮) |
| node-fetch | 6/9 | 64% | 1.0s | UCL/Melb/QS |
| spider-rs | 4/9 | 44% | 0.9s | wiki/leeds/UCL/Melb/QS |

**Scrapling 三档**：

| 引擎 | 有效产出 | 平均命中 | 平均耗时 | 只栽在 |
|---|---|---|---|---|
| scrapling-http（curl_cffi TLS伪装） | **8/9** | 84% | **1.0s** ⭐ | UCL |
| scrapling-dynamic（Playwright） | 8/9 | 87% | 12.8s | UCL |
| scrapling-stealth（patchright隐身） | 8/9 | 87% | 15.9s | UCL |

### Round-2 关键结论（在第一部分基础上追加/修正）

1. **curl_cffi（会伪装 TLS/JA3 的纯 HTTP）是全场性价比之王**：8/9、1 秒/页，覆盖与真浏览器持平却快 12-16 倍，**还过了 QS Cloudflare**。→ Node 的 got-scraping（只换请求头、7/9、过不了 CF）不如它；差别就在**真伪装 TLS 指纹**。
2. **反爬墙 ≠ JS 墙（重要纠偏）**：这些"JS 重度"的大学/榜单页，只要过了反爬，数据其实就在 HTML 里——**真浏览器只对极少数纯前端渲染页才必需**，多数页便宜的 TLS-HTTP 就能拿下。
3. **没有单一工具通吃**：UCL 免费档里只有 got-scraping 的请求头组合能过（curl_cffi、所有浏览器全 403），付费里只有 **Firecrawl 住宅 stealth** 能过（见第五部分）；QS 只有 curl_cffi/浏览器/Firecrawl 能过 → 必须**多档 + 失败换档 + 重试**。
4. **stealth/patchright 名过其实**：patchright 隐身档没比普通 Playwright 多过任何一个（都 8/9），反而最慢；UCL 连它都 403。别盲上反检测。
5. **spider-rs 不是我们的菜**：极简 `Page()` API 不发浏览器 UA → 被 Wikipedia/QS/UCL/Melbourne 直接 403（4/9）。它的强项是 Rust 级**整站高速爬取**（Website 构建器 + 并发），不是"逐个项目页精抽"。
6. **katana 本机跑不通（环境不兼容，非工具缺陷；3 次尝试均失败）**：katana(Go) 与 spider(reqwest) 都读系统代理，本机 Clash/VPN（`127.0.0.1:7897`，fake-ip 模式）干扰其连接（曾见连到保留段 `198.18.0.48` 超时）。已试三法——清空代理+`NO_PROXY=*` / 显式 `-proxy` / 换多个目标(omscs 等)——**katana 在本机均无任何输出**，判定为本机网络栈不兼容。其价值（爬遍目录站自动发现所有项目 URL）已由源码确认；换一台无代理网络即可跑通。

> **环境提示（选型要考虑）**：本机存在一套代理/VPN，会干扰"读系统代理"的客户端（spider-rs、katana、偶发 got-scraping），但 Node fetch / Playwright / Python curl_cffi 正常。

### 最终落地建议（Round-2 收敛）

采集器（留 Node）做**三档 + 重试**：
1. **第一档·主力 = TLS 指纹伪装 HTTP**：Node 用 [`impit`](https://github.com/apify/impit)（Apify 出品，Rust，浏览器级 TLS 伪装）或 curl-impersonate 绑定；重活可起一个 **Python curl_cffi 边车**专抓硬页。实测这一档单独就 8/9、1 秒/页。
2. **第二档·兜底 = Playwright 真浏览器**：只对纯前端渲染页启用。
3. **失败换档 + 重试**：应对"反爬非确定性 + 无单一通吃"。
4. **发现层（可选增值）= katana**：换网络环境后接入，自动发现目录站的项目 URL。
5. **抽取层沿用 codex**（精确字段优先 CSS 选择器）。
6. 合规不变：只抓公开、不逆向签名/不用登录态、控频。

### 复现（Round-2 新增）
```
# Node 5 档（含 spider）
cd tools/crawler-lab && node run-bench.js
# Scrapling 三档（需 conda 环境 scrapling，用环境 python 直调，勿用 conda run 会卡交互提示）
D:\anaconda3\envs\scrapling\python.exe tools/crawler-lab/bench_scrapling.py
```
未实测项与原因已如实标注（llm-scraper=Owner 定跳过；katana=本机网络栈不兼容·3 次尝试均失败；Agent-Reach/BrowserAct=非本地可测的爬虫）——无一处用临时/替换方案冒充。

---

## 第五部分：Firecrawl 托管付费档实测（Owner 提供 credit）

Owner 提供了 Firecrawl 托管 API 的 credit——正好补上第一部分只能"看源码"的空白：Firecrawl 最能打的 **fire-engine 是闭源云服务**（自建版没有），现在能真金白银测它的天花板。用 `/v2/scrape` REST API 对同一 9 目标实测（**密钥只走 env、绝不落库**；调 API 走本机 Clash 代理，真正的抓取由 Firecrawl 云端完成）。

### 结果
| 档 | 有效产出 | UCL | QS(Cloudflare) | 耗时 |
|---|---|---|---|---|
| Firecrawl `proxy:auto` | **8/9** | ✗ 403（花 49s 也没救回） | ✓ 200,54k | 2.7–15s |
| Firecrawl `proxy:stealth`（住宅 IP，显式） | UCL 单测 **✓ 200,33k** | — | — | 17s |

### 关键结论
1. **付费≠更强（普通反爬）**：`auto` 档 8/9，并不比免费的 curl_cffi（8/9、1 秒/页）多拿一分，还更慢更花钱。日常公开数据用免费本地档即可。
2. **付费的真正价值＝住宅 IP stealth 攻硬骨头**：UCL 封机房/云 IP，免费本地（除住宅 got-scraping）+ Firecrawl basic/auto 全 403，**唯独 Firecrawl 显式 `proxy:"stealth"`（住宅代理）撬开了 UCL（200,33k）**——全场唯一攻破 UCL 的托管工具。对"封数据中心 IP"的站，这是免费云/本地工具给不了的逃生档。
3. **`auto` 不会真升级**：必须**显式 `proxy:"stealth"`** 才走住宅档（`auto` 在 UCL 上仍 403）。stealth 更贵（约 5× credit）、更慢，按需用。
4. **零维护**：Firecrawl 托管掉整个反爬/渲染/清洗，直接吐干净 markdown——省自己维护指纹/浏览器/重试的工程量，代价是 credit + 数据过第三方。

### 更新后的最终架构（四档 + 重试）
| 档 | 用什么 | 何时用 |
|---|---|---|
| ① 主力 | **TLS 指纹伪装 HTTP**（Node `impit` / Python `curl_cffi` 边车） | 绝大多数页；实测 8/9、1 秒/页 |
| ② 兜底 | **Playwright 真浏览器** | 纯前端渲染 SPA |
| ③ 硬骨头·逃生 | **Firecrawl 显式 stealth（付费）** | ①②全被挡的"封机房 IP"站（如 UCL）；按需付费、控量 |
| 贯穿 | **失败换档 + 重试** | 反爬非确定性、无单一通吃 |
| 抽取 | **codex**（精确字段优先 CSS 选择器） | — |

> 合规不变：只抓公开、不逆向签名/不用登录态、控频；Firecrawl 只用于抓公开页、credit 按需用。
> 复现：`$env:FIRECRAWL_API_KEY=<key>; $env:FIRECRAWL_PROXY='http://127.0.0.1:7897'; node tools/crawler-lab/bench_firecrawl.js`（密钥走 env、不入库；单测 stealth 见 UCL 命令）。
