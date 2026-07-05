# -*- coding: utf-8 -*-
"""三档抓取适配器，统一接口 fetch_one(url, cfg, tier) -> {ok,status,html,ms,tier,error}。

档位（契约 §3 / §11-A/I）：
  http_tls  → Scrapling Fetcher（curl_cffi，TLS/JA3 伪装）——主力，绝大多数页。
  browser   → Scrapling DynamicFetcher（Playwright 真浏览器）——纯前端渲染 SPA。
  firecrawl → httpx POST Firecrawl /v2/scrape（proxy=stealth）；无 key 则退 StealthyFetcher（patchright）。

并发：Scrapling 是同步 API，用 asyncio.to_thread() 包装以并发（§fetchers 契约：别用 Scrapling async API 避坑）。

代理（§11-I，best-effort）：
  bypassProxyForFetch=true 时的代理 env 清理由 run.py main() 启动时一次性完成（清掉 HTTP(S)_PROXY/ALL_PROXY
  并设 NO_PROXY='*'），全程不动——避免 per-fetch 在 to_thread 多线程并发下 os.environ 互相踩（跨线程竞态）。
  唯独 Firecrawl 的 **API 调用**仍显式 proxy=FIRECRAWL_PROXY（参数式，不受启动清理影响；云端才是真正抓取方）。
  局限：Scrapling/curl_cffi 是否读 env 代理取决于其内部实现；进程 env 层清理属 best-effort，
        无法保证第三方库内部另有代理配置来源。
"""
import os
import time

# 浏览器档超时（ms）：防 network_idle 无限挂起（§11-I 韧性）。
_BROWSER_TIMEOUT_MS = 45000


def _get_html(page):
    """从 scrapling 返回对象取 HTML（对齐 bench_scrapling.get_html）。"""
    for attr in ("html_content", "body"):
        v = getattr(page, attr, None)
        if v:
            return v.decode("utf-8", "ignore") if isinstance(v, (bytes, bytearray)) else str(v)
    return str(page)


# —— 各档同步实现（在 to_thread 里跑） —— #

def _sync_http_tls(url, cfg):
    from scrapling.fetchers import Fetcher
    fc = cfg.get("fetch", {}).get("http-tls", {})
    impersonate = fc.get("impersonate", "chrome")
    p = Fetcher.get(url, impersonate=impersonate, timeout=30)
    return {
        "status": int(getattr(p, "status", 0) or 0),
        "html": _get_html(p),
        "final_url": getattr(p, "url", None) or url,
    }


def _sync_browser(url, cfg):
    from scrapling.fetchers import DynamicFetcher
    # 实测：传 proxy={'server':'direct://'} 会让 Scrapling/Chromium 报 ERR_PROXY_CONNECTION_FAILED；
    # 而 bench 证明"不传 proxy 参数"时 Chromium 能正常穿本机 Clash。故不传 proxy 参数。
    # network_idle 等前端 XHR 落定；headless 无头；timeout 防无限挂起（§11-I）。
    p = DynamicFetcher.fetch(url, headless=True, network_idle=True, timeout=_BROWSER_TIMEOUT_MS)
    return {
        "status": int(getattr(p, "status", 0) or 0),
        "html": _get_html(p),
        "final_url": getattr(p, "url", None) or url,
    }


def _sync_stealthy(url, cfg):
    """Firecrawl 无 key 时的本地逃生档：StealthyFetcher（patchright 隐身）。"""
    from scrapling.fetchers import StealthyFetcher
    # 同 _sync_browser：不传 direct:// proxy（会 ERR_PROXY_CONNECTION_FAILED）；timeout 防无限挂起。
    p = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=_BROWSER_TIMEOUT_MS)
    return {
        "status": int(getattr(p, "status", 0) or 0),
        "html": _get_html(p),
        "final_url": getattr(p, "url", None) or url,
    }


def _sync_firecrawl(url, cfg):
    """Firecrawl /v2/scrape（proxy:stealth）。API 调用走 FIRECRAWL_PROXY（云端抓，本地只调 API）。

    无 key → 抛给上层由 fetch_one 转 StealthyFetcher。
    返回 markdown 当 html（下游 html_to_text 对 markdown 无害，正文/关键词照样命中）。
    """
    import httpx
    fc = cfg.get("fetch", {}).get("firecrawl", {})
    key_env = fc.get("apiKeyEnv", "FIRECRAWL_API_KEY")
    api_key = os.environ.get(key_env)
    if not api_key:
        raise RuntimeError("no-firecrawl-key")  # 上层捕获后走 patchright 兜底
    proxy_env = fc.get("proxyForApiEnv", "FIRECRAWL_PROXY")
    api_proxy = os.environ.get(proxy_env)  # API 调用（非抓取）走代理
    body = {
        "url": url,
        "formats": ["markdown"],
        "proxy": fc.get("proxy", "stealth"),
    }
    # httpx 用 proxy= 让 API 请求走 Clash（云端真正抓取不受此影响）
    client_kwargs = {"timeout": 120.0}
    if api_proxy:
        client_kwargs["proxy"] = api_proxy
    with httpx.Client(**client_kwargs) as client:
        resp = client.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={"Authorization": "Bearer %s" % api_key, "Content-Type": "application/json"},
            json=body,
        )
    status = resp.status_code
    md = ""
    final_url = url
    if status == 200:
        try:
            j = resp.json()
            data = j.get("data", {}) if isinstance(j, dict) else {}
            md = data.get("markdown", "") or ""
            meta = data.get("metadata", {}) or {}
            final_url = meta.get("sourceURL") or meta.get("url") or url
            # Firecrawl 成功但内容为空时，用 200 表意但 html 空 → 打分自然 not usable
        except Exception:
            md = resp.text
    return {"status": status, "html": md, "final_url": final_url}


_SYNC_IMPL = {
    "http-tls": _sync_http_tls,
    "browser": _sync_browser,
    "firecrawl": _sync_firecrawl,
}


async def fetch_one(url, cfg, tier):
    """统一异步接口。返回 {ok,status,html,ms,tier,error,final_url}。绝不抛异常（失败进 error）。"""
    import asyncio
    t0 = time.time()
    impl = _SYNC_IMPL.get(tier)
    if impl is None:
        return {"ok": False, "status": 0, "html": "", "ms": 0, "tier": tier,
                "error": "unknown-tier:%s" % tier, "final_url": url}
    try:
        r = await asyncio.to_thread(impl, url, cfg)
    except RuntimeError as e:
        if str(e) == "no-firecrawl-key":
            # firecrawl 档无 key → 本地 StealthyFetcher/patchright 兜底（契约 fetchers 段）
            try:
                r = await asyncio.to_thread(_sync_stealthy, url, cfg)
                r["_fallback"] = "stealthy"
            except Exception as e2:
                ms = (time.time() - t0) * 1000
                return {"ok": False, "status": 0, "html": "", "ms": int(ms), "tier": tier,
                        "error": "stealthy-fallback-failed:%s" % str(e2)[:100], "final_url": url}
        else:
            ms = (time.time() - t0) * 1000
            return {"ok": False, "status": 0, "html": "", "ms": int(ms), "tier": tier,
                    "error": str(e)[:120], "final_url": url}
    except Exception as e:
        ms = (time.time() - t0) * 1000
        return {"ok": False, "status": 0, "html": "", "ms": int(ms), "tier": tier,
                "error": str(e)[:120], "final_url": url}

    ms = (time.time() - t0) * 1000
    status = r.get("status", 0)
    ok = 200 <= int(status or 0) < 400
    out = {
        "ok": ok,
        "status": int(status or 0),
        "html": r.get("html", "") or "",
        "ms": int(ms),
        "tier": tier,
        "error": "",
        "final_url": r.get("final_url", url) or url,
    }
    if r.get("_fallback"):
        out["fallback"] = r["_fallback"]
    return out
