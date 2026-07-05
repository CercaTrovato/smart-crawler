# -*- coding: utf-8 -*-
"""读 crawler.config.json + 能力自检（契约 config.py 段 / §11-I）。

原则：缺能力给明确报错，不静默假成功（§0.4）。
- 声明了某抓取档但依赖缺 → 从可用档里剔除并记 warning（升级链自动跳过）。
- llm=codex 但 codex 不在 PATH → 报错（抽取会失败，不装成功）。
- llm=openai-compat 但缺 baseUrl/apiKey env → 报错。
- firecrawl 声明但无 FIRECRAWL_API_KEY → 该档退化为本地 StealthyFetcher（patchright），记 warning（§fetchers 契约）。
凭据只读 env（配置只存变量名）。
"""
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "crawler.config.json")
EXAMPLE_PATH = os.path.join(HERE, "crawler.config.example.json")


class ConfigError(Exception):
    pass


def _clamp(v, lo, hi):
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = lo
    return max(lo, min(hi, v))


def _safe_int(v, default):
    """§QC-F23：容错取整（非数值/None → default），避免 config 显式写 null/字符串致 int() 抛异常冒泡。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default)


def _codex_available():
    return shutil.which("codex") is not None


def _has_env(name):
    return bool(name) and bool(os.environ.get(name))


def load_config(path=None, cli_concurrency=None, cli_profile=None, cli_lang=None):
    """加载配置并做能力自检。返回规范化后的 dict（含 _capabilities / _warnings）。

    cli_concurrency：命令行 --concurrency，若给则覆盖 concurrency.global（clamp 到 [1,32]）。
    cli_profile / cli_lang：命令行 --profile / --lang，覆盖 config 的 output.profile / output.lang。
        优先级 CLI > config output.* > 代码默认（profile=generic, lang=en）。
    """
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        # 便捷：无 config 时回退 example（setup.md 让用户 copy，但缺失也别硬崩）
        if os.path.exists(EXAMPLE_PATH):
            path = EXAMPLE_PATH
        else:
            raise ConfigError("找不到 crawler.config.json（也无 example 回退）：%s" % path)

    try:
        with open(path, "r", encoding="utf-8-sig") as f:  # §QC-F35 utf-8-sig 容忍手写文件的 BOM
            cfg = json.load(f)
    except (OSError, ValueError) as e:  # §QC-F30：坏/不可读 config 友好报错（ValueError 含 JSONDecodeError）
        raise ConfigError("crawler.config.json 读取/解析失败（%s）：%s" % (os.path.basename(path), str(e)[:100]))

    warnings = []

    # —— 抓取档能力自检 —— #
    fetch = cfg.setdefault("fetch", {})
    declared_tiers = list(fetch.get("tiers") or ["http-tls", "browser", "firecrawl"])
    available_tiers = []
    for t in declared_tiers:
        if t in ("http-tls", "browser"):
            # 两档同依赖 scrapling.fetchers（curl_cffi / Playwright 都在其中）。
            try:
                import scrapling.fetchers  # noqa: F401
                available_tiers.append(t)
            except Exception as e:  # pragma: no cover - 环境缺库
                warnings.append("%s 档不可用（scrapling 缺失：%s），已跳过" % (t, str(e)[:80]))
        elif t == "firecrawl":
            # 无 key → 契约 fetchers 段：退 StealthyFetcher/patchright。仍占该档，实现里兜底。
            fc = fetch.setdefault("firecrawl", {})
            key_env = fc.get("apiKeyEnv", "FIRECRAWL_API_KEY")
            if not _has_env(key_env):
                warnings.append(
                    "firecrawl 档无 %s，将退化为本地 StealthyFetcher（patchright）逃生档" % key_env
                )
            available_tiers.append(t)
        else:
            warnings.append("未知抓取档 '%s'，忽略" % t)

    if not available_tiers:
        raise ConfigError("没有任何可用抓取档（检查 scrapling 安装）。")

    cfg["_available_tiers"] = available_tiers

    # —— LLM 抽取后端能力自检 —— #
    llm = cfg.setdefault("llm", {})
    provider = llm.get("provider", "codex")
    if provider == "codex":
        if not _codex_available():
            raise ConfigError("llm.provider=codex 但 codex 不在 PATH。装 codex 或改用 openai-compat。")
    elif provider == "openai-compat":
        try:
            import httpx  # noqa: F401
        except Exception as e:  # §QC-F26：openai-compat 依赖 httpx，缺则明确报错（对称 scrapling 自检）
            raise ConfigError("openai-compat 需 httpx 但导入失败：%s（pip install httpx）。" % str(e)[:80])
        base_env = llm.get("baseUrlEnv") or llm.get("baseURLEnv")
        key_env = llm.get("apiKeyEnv")
        if not _has_env(base_env):
            raise ConfigError("openai-compat 缺 baseUrl env（%s 未设）。" % base_env)
        if not _has_env(key_env):
            # 本地无鉴权服务允许占位，但 env 必须存在（哪怕填任意值）
            raise ConfigError("openai-compat 缺 apiKey env（%s 未设；本地无鉴权可填占位）。" % key_env)
    else:
        raise ConfigError("未知 llm.provider '%s'（支持 codex / openai-compat）。" % provider)

    # §QC-F23：llm 数值字段统一兜底（原 extract.py 里 int(非数值) 在 try 外，坏配置会抛异常冒泡整批）。
    _def_to = 180000 if provider == "codex" else 60000
    llm["timeoutMs"] = _safe_int(llm.get("timeoutMs", _def_to), _def_to)
    llm["maxRetry"] = _clamp(llm.get("maxRetry", 2), 1, 5)

    # —— 并发参数规范化（clamp） —— #
    conc = cfg.setdefault("concurrency", {})
    cmin = _clamp(conc.get("min", 1), 1, 32)
    cmax = _clamp(conc.get("max", 32), cmin, 32)  # §QC-F31 上限 32（原 64 超 CLI/§11-J 承诺 clamp[1,32]）
    gval = cli_concurrency if cli_concurrency is not None else conc.get("global", 8)
    conc["global"] = _clamp(gval, cmin, cmax)
    conc["min"] = cmin
    conc["max"] = cmax
    conc["browserCap"] = _clamp(conc.get("browserCap", 4), 1, 4)  # §QC-F31 上限 4（§11-D browserSem≤4，弱机防 OOM）
    conc["firecrawlCap"] = _clamp(conc.get("firecrawlCap", 2), 1, 2)  # §QC-F31 上限 2（§11-D firecrawlSem≤2）

    # —— 礼貌 / 重试 / 预算 默认兜底 —— #
    pol = cfg.setdefault("politeness", {})
    pol["perDomain"] = _clamp(pol.get("perDomain", 2), 1, 8)
    pol["minDelayMs"] = _safe_int(pol.get("minDelayMs", 1500), 1500)  # §QC-F23 防 null/非数值 int() 崩
    rt = cfg.setdefault("retry", {})
    rt["perTier"] = _clamp(rt.get("perTier", 2), 1, 5)
    rt["backoffMs"] = _safe_int(rt.get("backoffMs", 1500), 1500)  # §QC-F23
    rt["maxAttemptsPerTarget"] = _clamp(rt.get("maxAttemptsPerTarget", 6), 1, 20)
    cfg.setdefault("firecrawlBudget", {}).setdefault("maxCredits", 200)
    cfg.setdefault("network", {}).setdefault("bypassProxyForFetch", True)

    # —— 输出档：profile（信封形态）+ lang（自由文本语言）—— #
    # profile=generic（默认，通用信封，无任何下游专属键）｜studycompass（内部 submitCollectionResult 契约）。
    # lang=en（默认，自由文本保源语言/原样）｜zh（自由文本转简体中文，数字/名称/枚举码/URL 仍保原样）。
    # 优先级：CLI > config output.* > 代码默认。凭据无关，纯输出成形开关。
    out = cfg.setdefault("output", {})
    profile = str(cli_profile or out.get("profile") or "generic").lower()
    if profile not in ("generic", "studycompass"):
        warnings.append("未知 output.profile '%s'，回退 generic" % profile)
        profile = "generic"
    lang = str(cli_lang or out.get("lang") or "en").lower()
    if lang not in ("en", "zh"):
        warnings.append("未知 output.lang '%s'，回退 en" % lang)
        lang = "en"
    out["profile"] = profile
    out["lang"] = lang

    cfg["_warnings"] = warnings
    return cfg
