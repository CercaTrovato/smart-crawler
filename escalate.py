# -*- coding: utf-8 -*-
"""单目标升级链（契约 §4 + §11-D/G）。

流程（每目标）：
  tiers = 配置启用且可用的档，按成本升序；若 domain-memory 有该域成功档，则记忆档优先 + 其余档作 fallback 尾巴。
  for tier in tiers:
      for attempt in 1..(firecrawl 档=1，其余=retry.perTier)：
          （若 tier 是 browser/firecrawl，先过资源 sem；firecrawl 还要 credit 先扣后用）
          r = fetch_one(url, tier)
          s = score(r)
          if s.usable: 记忆 domain->tier；返回成功
          sleep(backoff)
  单目标总尝试封顶 maxAttemptsPerTarget（§11-G/D）
  命中验证码/challenge 页 → 天然 blocked → not usable → 升级链走完标 needs_manual（红线不破解满足）。
  全挂 → needs_manual（附最后原因，绝不假成功 §0.4）

并发正确性（§11-D）：本模块**不持有全局 Semaphore**（那在 scheduler）；只在 browser/firecrawl
  档内获取对应资源 sem，且获取顺序固定：host 限流（fetch 前，由 scheduler 注入的 limiter）→ 资源 sem。
  这里拿到的 resources 已是最内层，不再嵌套全局 slot，避免死锁。
"""
import asyncio
import random

from fetchers import fetch_one
from scoring import score, expect_for

# 成本升序：http-tls 最便宜 → browser → firecrawl 最贵。
_TIER_ORDER = ["http-tls", "browser", "firecrawl"]

# firecrawl stealth 单页 credit 估算（报告：约 5× 基础，stealth 更贵）。保守取 5。
_FIRECRAWL_STEALTH_COST = 5


def _order_tiers(available_tiers, start_tier=None):
    """按成本升序排列可用档；若 domain-memory 给了 start_tier，则「记忆档优先 + 其余档作 fallback 尾巴」。

    记忆档不切掉更便宜的档（避免 firecrawl 记忆档 credit denied 时无法回退到 http-tls/browser，
    修 domain-memory 在 credit 耗尽时放大失败面，§11-D）。
    """
    ordered = [t for t in _TIER_ORDER if t in available_tiers]
    if start_tier and start_tier in ordered:
        return [start_tier] + [t for t in ordered if t != start_tier]
    return ordered


class Resources:
    """升级链需要的共享资源（由 scheduler 注入）。"""

    def __init__(self, browser_sem, firecrawl_sem, credit_gate, host_limiter_ctx, memory):
        self.browser_sem = browser_sem            # asyncio.Semaphore
        self.firecrawl_sem = firecrawl_sem        # asyncio.Semaphore
        self.credit_gate = credit_gate            # CreditGate（含 asyncio.Lock）
        self.host_limiter_ctx = host_limiter_ctx  # async ctx manager 工厂：host_limiter_ctx(url)
        self.memory = memory                      # DomainMemory（get/remember）


async def _do_fetch_with_resources(url, cfg, tier, res):
    """单次抓取，按档套资源闸。返回 (r, denied_reason|None)。

    获取顺序固定（§11-D）：host 限流 → 资源 sem（仅 browser/firecrawl）。fetch 在最内层。
    """
    # host 限流上下文（守礼 + 防封）：进入即等待该域并发额度 + 随机间隔。
    async with res.host_limiter_ctx(url):
        if tier == "browser":
            async with res.browser_sem:
                return await fetch_one(url, cfg, tier), None
        elif tier == "firecrawl":
            # credit 先扣后用（§11-D）：进 T3 前原子 reserve；失败即拒并降级。
            reserved = await res.credit_gate.reserve(_FIRECRAWL_STEALTH_COST)
            if not reserved:
                return None, "firecrawl-credit-exhausted"
            async with res.firecrawl_sem:
                r = await fetch_one(url, cfg, tier)
            # credit 退还（§11-D 4②）：请求没真发出（error 非空）或走了 stealthy 兜底（没调 firecrawl API）→ 退。
            if r.get("error") or r.get("fallback") == "stealthy":
                await res.credit_gate.refund(_FIRECRAWL_STEALTH_COST)
            return r, None
        else:  # http-tls
            return await fetch_one(url, cfg, tier), None


async def escalate(target, cfg, res):
    """对单个 target 跑升级链。

    target: {url, type, hint?, domain}
    返回：{
      usable: bool, text: str, tier_used: str|None, status, ms_total,
      needs_manual: bool, reason: str, final_url: str, attempts: int, tier_log: [..]
    }
    """
    url = target["url"]
    ttype = target.get("type", "programme")
    domain = target["domain"]
    expect = expect_for(ttype, target.get("hint"))

    per_tier = cfg["retry"]["perTier"]
    backoff_ms = cfg["retry"]["backoffMs"]
    max_attempts = cfg["retry"]["maxAttemptsPerTarget"]

    start = res.memory.get(domain)  # domain-memory 命中则直达该档
    tiers = _order_tiers(cfg["_available_tiers"], start_tier=start)

    attempts = 0
    ms_total = 0
    last_reason = "未知失败"
    last_status = 0
    last_final_url = url
    tier_log = []

    for tier in tiers:
        # firecrawl 档不做 per-tier 重试（该档 attempt 只跑 1 次，别烧 2×credit，§11-D 4①）。
        tier_retries = 1 if tier == "firecrawl" else per_tier
        for attempt in range(1, tier_retries + 1):
            if attempts >= max_attempts:
                last_reason = "达单目标尝试上限 maxAttemptsPerTarget=%d" % max_attempts
                return _fail(url, last_reason, last_status, ms_total, last_final_url, attempts, tier_log)
            attempts += 1

            r, denied = await _do_fetch_with_resources(url, cfg, tier, res)
            if denied:
                # credit 耗尽等资源拒绝 → 该档不可用，跳到下一档（降级/兜底）
                last_reason = denied
                tier_log.append({"tier": tier, "attempt": attempt, "denied": denied})
                break  # 换下一档

            ms_total += r.get("ms", 0)
            last_status = r.get("status", 0)
            last_final_url = r.get("final_url", url) or url
            s = score(expect, r["ok"], r["status"], r["html"])
            tier_log.append({
                "tier": tier, "attempt": attempt, "status": r["status"],
                "ok": r["ok"], "usable": s["usable"], "text_len": s["text_len"],
                "hits": s["hits"], "kw_total": s["kw_total"], "blocked": s["blocked"],
                "ms": r["ms"],
                "error": r.get("error", ""), "fallback": r.get("fallback"),
            })

            if s["usable"]:
                res.memory.remember(domain, tier)  # 记忆成功档，下次直达
                return {
                    "usable": True, "text": s["text"], "tier_used": tier,
                    "status": r["status"], "ms_total": ms_total, "needs_manual": False,
                    "reason": "", "final_url": last_final_url, "attempts": attempts,
                    "tier_log": tier_log,
                }

            # 记失败原因（供最终 needs_manual 说明）。命中验证码/challenge 页天然 blocked → 走此路标 needs_manual。
            if r.get("error"):
                last_reason = "%s 档报错：%s" % (tier, r["error"])
            elif s["blocked"]:
                last_reason = "%s 档被封（status=%s）" % (tier, r["status"])
            else:
                last_reason = "%s 档内容不足（正文 %d 字 / 命中 %d/%d，未达 usable）" % (
                    tier, s["text_len"], s["hits"], s["kw_total"])

            # 重试退避（反爬非确定性 §4）
            if attempt < tier_retries:
                await asyncio.sleep(backoff_ms / 1000.0 * (0.7 + 0.6 * random.random()))

    return _fail(url, last_reason, last_status, ms_total, last_final_url, attempts, tier_log)


def _fail(url, reason, status, ms_total, final_url, attempts, tier_log):
    return {
        "usable": False, "text": "", "tier_used": None, "status": status,
        "ms_total": ms_total, "needs_manual": True, "reason": reason,
        "final_url": final_url, "attempts": attempts, "tier_log": tier_log,
    }
