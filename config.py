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


def _codex_available():
    return shutil.which("codex") is not None


def _has_env(name):
    return bool(name) and bool(os.environ.get(name))


def load_config(path=None, cli_concurrency=None):
    """加载配置并做能力自检。返回规范化后的 dict（含 _capabilities / _warnings）。

    cli_concurrency：命令行 --concurrency，若给则覆盖 concurrency.global（clamp 到 [1,32]）。
    """
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        # 便捷：无 config 时回退 example（setup.md 让用户 copy，但缺失也别硬崩）
        if os.path.exists(EXAMPLE_PATH):
            path = EXAMPLE_PATH
        else:
            raise ConfigError("找不到 crawler.config.json（也无 example 回退）：%s" % path)

    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

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
        base_env = llm.get("baseUrlEnv") or llm.get("baseURLEnv")
        key_env = llm.get("apiKeyEnv")
        if not _has_env(base_env):
            raise ConfigError("openai-compat 缺 baseUrl env（%s 未设）。" % base_env)
        if not _has_env(key_env):
            # 本地无鉴权服务允许占位，但 env 必须存在（哪怕填任意值）
            raise ConfigError("openai-compat 缺 apiKey env（%s 未设；本地无鉴权可填占位）。" % key_env)
    else:
        raise ConfigError("未知 llm.provider '%s'（支持 codex / openai-compat）。" % provider)

    # —— 并发参数规范化（clamp） —— #
    conc = cfg.setdefault("concurrency", {})
    cmin = _clamp(conc.get("min", 1), 1, 32)
    cmax = _clamp(conc.get("max", 32), cmin, 64)
    gval = cli_concurrency if cli_concurrency is not None else conc.get("global", 8)
    conc["global"] = _clamp(gval, cmin, cmax)
    conc["min"] = cmin
    conc["max"] = cmax
    conc["browserCap"] = _clamp(conc.get("browserCap", 4), 1, 16)
    conc["firecrawlCap"] = _clamp(conc.get("firecrawlCap", 2), 1, 8)

    # —— 礼貌 / 重试 / 预算 默认兜底 —— #
    pol = cfg.setdefault("politeness", {})
    pol["perDomain"] = _clamp(pol.get("perDomain", 2), 1, 8)
    pol["minDelayMs"] = int(pol.get("minDelayMs", 1500))
    rt = cfg.setdefault("retry", {})
    rt["perTier"] = _clamp(rt.get("perTier", 2), 1, 5)
    rt["backoffMs"] = int(rt.get("backoffMs", 1500))
    rt["maxAttemptsPerTarget"] = _clamp(rt.get("maxAttemptsPerTarget", 6), 1, 20)
    cfg.setdefault("firecrawlBudget", {}).setdefault("maxCredits", 200)
    cfg.setdefault("network", {}).setdefault("bypassProxyForFetch", True)

    cfg["_warnings"] = warnings
    return cfg
