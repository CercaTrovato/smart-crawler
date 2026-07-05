# -*- coding: utf-8 -*-
"""调度器（契约 §11-D/E）：asyncio 队列消费者并发 + 每域限流 + 资源闸 + credit 先扣后用 + 断点续跑。

并发正确性（§11-D，编码契约）：
  - 队列消费者模型：所有目标塞 asyncio.Queue，起 global（clamp[min,max]）个 worker 协程循环消费。
    并发上限=worker 数，无全局 Semaphore（避免 worker 攥全局 slot 干等 host 额度致池利用率崩，§11-D 4🔴1）。
  - 资源上限：独立 browser_sem(≤browserCap)、firecrawl_sem(≤firecrawlCap)。
  - 礼貌：每域 hostLimiter（≤perDomain 并发 + 锁内预留发射时刻保证间隔递增不重叠 minDelayMs）。
  - **绝不嵌套持有 Semaphore**。固定获取顺序：(fetch 内层)host 限流 →(仅 browser/firecrawl)资源 sem。
    host 限流 + 资源 sem 在 escalate 内层获取，二者独立、不互锁。
  - credit 先扣后用：进 T3 前 asyncio.Lock 临界区 reserve（读余额→扣减原子），失败即拒并降级；
    请求没真发出/走本地兜底则 refund 退还。

断点续跑（§11-E）：
  - task_key = type + 归一 target + source_url。
  - "已完成"只在**产物成功落盘后**写 state.json；resume 只覆盖到"产物已落盘"，不横跨投喂。
  - domain-memory.json 单点更新（非 sqlite）。
"""
import asyncio
import contextlib
import json
import os
import random
import re
import sys
import time
from urllib.parse import urlparse

from escalate import Resources, escalate

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
STATE_PATH = os.path.join(HERE, "state.json")
MEMORY_PATH = os.path.join(HERE, "domain-memory.json")


def _atomic_write_json(path, obj):
    """原子写盘：写临时文件 + os.replace（避免半截 JSON 损坏 state/memory，§11-E 韧性）。"""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_json_or_warn(path, what):
    """读 JSON；损坏时告警到 stderr 并返回空 dict（不静默清空，§11-E 韧性）。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        print("[警告] %s 损坏无法解析（%s）：%s —— 本次以空处理，不覆盖原文件直到有新写入"
              % (what, os.path.basename(path), str(e)[:80]), file=sys.stderr)
        return {}


# —— 断点续跑 state —— #

def _normalize_url(url):
    """归一 URL（去 fragment、末尾斜杠、小写 host），做稳定 task_key。"""
    try:
        u = urlparse(url)
        host = (u.netloc or "").lower()
        path = (u.path or "").rstrip("/")
        q = ("?" + u.query) if u.query else ""
        return "%s://%s%s%s" % (u.scheme or "https", host, path, q)
    except Exception:
        return url


def make_task_key(target):
    """task_key = type + 归一 target + source_url（§11-E）。"""
    ttype = target.get("type", "programme")
    nurl = _normalize_url(target["url"])
    return "%s|%s" % (ttype, nurl)


def _safe_task_key(target):
    """对可能畸形（非 dict / 缺 url）的 target 也稳定产 key（§QC-F15/F16 失败隔离健壮性）。"""
    if isinstance(target, dict) and target.get("url"):
        return make_task_key(target)
    if isinstance(target, dict):
        return "%s|(no-url)" % target.get("type", "programme")
    return "invalid|%r" % (target,)


class State:
    """已完成任务集合，落盘 state.json。只在产物落盘后标完成。"""

    def __init__(self, path, enabled):
        self.path = path
        self.enabled = enabled
        self.done = {}
        self._lock = asyncio.Lock()
        if enabled:
            self.done = _load_json_or_warn(path, "断点续跑 state")

    def is_done(self, key):
        # §QC-F20：只有"真抓到数据"(status=done)才算完成；失败记录(status=failed)不跳过、--resume 会重试。
        # 兼容旧格式（无 status 字段的记录视为 done）。
        if not self.enabled:
            return False
        v = self.done.get(key)
        return isinstance(v, dict) and v.get("status", "done") == "done"

    def prior_failures(self, key):
        """该 key 之前累计失败次数（§QC-F20，供产物标注"第 N 次抓取失败"）。"""
        v = self.done.get(key)
        if isinstance(v, dict) and v.get("status") == "failed":
            return int(v.get("attempts", 0) or 0)
        return 0

    async def mark_done(self, key, payload_path):
        if not self.enabled:  # §QC-F21：resume 关闭时不维护 state.json（否则下次 --resume 读到非预期完成记录被跳过）
            return
        async with self._lock:
            self.done[key] = {"status": "done", "payload": payload_path,
                              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            self._flush()

    async def mark_failed(self, key, attempts, reason):
        """§QC-F20：记录抓取失败（不算完成，--resume 会重试）；attempts 累计供产物标注"第 N 次失败"。"""
        if not self.enabled:
            return
        async with self._lock:
            self.done[key] = {"status": "failed", "attempts": int(attempts),
                              "last_reason": str(reason)[:150],
                              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            self._flush()

    def _flush(self):
        try:
            _atomic_write_json(self.path, self.done)
        except Exception as e:
            print("[警告] state.json 写盘失败：%s" % str(e)[:80], file=sys.stderr)


# —— domain-memory —— #

class DomainMemory:
    """按域记忆成功档，单点更新 domain-memory.json（非 sqlite，§11-E）。"""

    def __init__(self, path):
        self.path = path
        self.mem = _load_json_or_warn(path, "domain-memory")

    def get(self, domain):
        entry = self.mem.get(domain)
        return entry.get("tier") if isinstance(entry, dict) else None

    def remember(self, domain, tier):
        cur = self.mem.get(domain)
        if isinstance(cur, dict) and cur.get("tier") == tier:
            return  # 无变化，免写盘
        self.mem[domain] = {"tier": tier,
                            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        try:
            _atomic_write_json(self.path, self.mem)
        except Exception as e:
            print("[警告] domain-memory.json 写盘失败：%s" % str(e)[:80], file=sys.stderr)


# —— credit gate（firecrawl 先扣后用） —— #

class CreditGate:
    def __init__(self, max_credits):
        self.max_credits = int(max_credits)
        self.remaining = int(max_credits)
        self._lock = asyncio.Lock()

    async def reserve(self, cost):
        """原子：读余额→扣减。够则扣并返回 True，不够返回 False（拒绝并降级）。"""
        async with self._lock:
            if self.remaining >= cost:
                self.remaining -= cost
                return True
            return False

    async def refund(self, cost):
        """退还已 reserve 的 credit（请求没真发出/走了本地兜底，没调 firecrawl API，§11-D 4②）。"""
        async with self._lock:
            self.remaining = min(self.max_credits, self.remaining + cost)


# —— 每域限流器 —— #

class HostLimiter:
    """每域 ≤perDomain 并发 + 相邻抓取最小间隔（守礼 + 防封）。

    minDelay 并发正确性（§11-D 4🔴3）：在每域一把锁（_guard）下**预留下一发射时刻**
    （next = max(now, _last[host]) + min_delay；_last[host] = next），锁**外** sleep 到 next。
    保证 perDomain≥2 并发下每域请求间隔递增不重叠（不会读→sleep→写无锁把最小间隔吃掉）。
    """

    def __init__(self, per_domain, min_delay_ms):
        self.per_domain = per_domain
        self.min_delay = min_delay_ms / 1000.0
        self._sems = {}
        self._last = {}
        self._guard = asyncio.Lock()

    async def _sem_for(self, host):
        async with self._guard:
            if host not in self._sems:
                self._sems[host] = asyncio.Semaphore(self.per_domain)
                self._last[host] = 0.0
            return self._sems[host]

    async def _reserve_slot(self, host):
        """锁内预留下一发射时刻，返回需 sleep 到的绝对时间（锁外 sleep）。"""
        async with self._guard:
            now = time.time()
            # 抖动加在 min_delay 上，避免同域整点齐发被识别为脚本。
            jittered = self.min_delay * (0.8 + 0.4 * random.random())
            nxt = max(now, self._last.get(host, 0.0)) + jittered
            self._last[host] = nxt
            return nxt

    @contextlib.asynccontextmanager
    async def ctx(self, url):
        host = (urlparse(url).netloc or "").lower()
        sem = await self._sem_for(host)
        await sem.acquire()
        try:
            nxt = await self._reserve_slot(host)
            wait = nxt - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            yield
        finally:
            sem.release()


# —— 主调度 —— #

def _domain_of(url):
    return (urlparse(url).netloc or "").lower()


# 已知大学官网后缀白名单：命中才判 official/high，否则保守按第三方(medium)。宁低估不高估（§11-C）。
_OFFICIAL_HOST_RE = re.compile(
    r"(\.edu|\.edu\.\w+|\.ac\.\w+|\.uni-[\w-]+\.\w+)$", re.I,
)


def _source_type_for(target):
    """源类型（决定 data_confidence）。target 可显式给 source_type；否则按域判。

    保守低估（§11-C）：只有 target 显式给 source_type，或 host 命中已知大学官网白名单（.edu/.ac.xx 等），
    才判 official/high；未知域一律默认第三方(medium)，不默认 official。
    """
    if target.get("source_type"):
        return target["source_type"]
    host = _domain_of(target["url"])
    if re.search(r"compassedu", host):
        return "competitor_aggregator"
    if "wikipedia.org" in host or "wikidata.org" in host:
        return "wikipedia"
    if _OFFICIAL_HOST_RE.search(host):
        return "official"
    # 未知域：宁保守低估为第三方(medium)，不默认 official(high)。
    return "third_party_education_site"


async def run_targets(targets, cfg, extract_fn, schema_provider, resume=False, out_dir=None):
    """跑一批 targets，产物落盘。返回 per-target 结果列表（供 run-report）。

    extract_fn(text, schema, instructions, cfg) -> dict
    schema_provider(ttype) -> (schema_dict, instructions_str)
    """
    out_dir = out_dir or OUT_DIR
    from output import build_envelope, write_payload

    conc = cfg["concurrency"]
    browser_sem = asyncio.Semaphore(conc["browserCap"])
    firecrawl_sem = asyncio.Semaphore(conc["firecrawlCap"])
    credit_gate = CreditGate(cfg["firecrawlBudget"]["maxCredits"])
    host_limiter = HostLimiter(cfg["politeness"]["perDomain"], cfg["politeness"]["minDelayMs"])
    memory = DomainMemory(MEMORY_PATH)
    state = State(STATE_PATH, resume)

    res = Resources(browser_sem, firecrawl_sem, credit_gate,
                    host_limiter.ctx, memory)

    results = []
    results_lock = asyncio.Lock()

    async def process(target):
        target = dict(target)
        target["domain"] = _domain_of(target["url"])
        key = make_task_key(target)
        rec = {"target": target, "task_key": key}

        if state.is_done(key):
            rec.update({"skipped": True, "reason": "resume：产物已落盘，跳过"})
            async with results_lock:
                results.append(rec)
            return

        # 消费者模型（§11-D 4🔴1）：并发上限=worker 数，无全局 Semaphore（避免 worker 攥全局 slot
        # 干等 host 额度致池利用率崩）。host 限流 + 资源 sem 仍在 escalate 内层。
        fetch_res = await escalate(target, cfg, res)

        rec["fetch"] = {k: fetch_res[k] for k in
                        ("usable", "tier_used", "status", "ms_total", "needs_manual",
                         "reason", "final_url", "attempts")}
        rec["tier_log"] = fetch_res.get("tier_log", [])

        source_type = _source_type_for(target)
        prior_fail = state.prior_failures(key)  # §QC-F20：resume 时该目标此前累计失败次数

        if fetch_res["usable"]:
            schema, instructions = schema_provider(target.get("type", "programme"))
            # 抽取（同步子进程/HTTP，放 to_thread 避免阻塞事件循环）
            extraction = await asyncio.to_thread(
                extract_fn, fetch_res["text"], schema, instructions, cfg)
            rec["extract_error"] = extraction.get("__extract_error__") if isinstance(extraction, dict) else None
            envelope = build_envelope(
                extraction, target, source_type, fetch_res["final_url"],
                fetch_meta={"tier_used": fetch_res["tier_used"], "attempts": fetch_res["attempts"]})
        else:
            # 抓取失败 → 仍产 needs_manual 信封（空 item + 全 missing + 原因），绝不假成功（§0.4）。
            envelope = build_envelope(
                {"__extract_error__": "fetch-failed:%s" % fetch_res["reason"]},
                target, source_type, fetch_res["final_url"] or target["url"],
                fetch_meta={"tier_used": None, "attempts": fetch_res["attempts"]})
            envelope["import_recommendation"] = "manual_completion_required"
            # §QC-F20：累计失败次数，重试后仍失败则标"第 N 次抓取失败"，让验收 agent 知晓续跑重试仍未成。
            cur_fail = prior_fail + 1
            fail_msg = fetch_res["reason"] if cur_fail == 1 else (
                "第 %d 次抓取失败（含 --resume 重试）：%s" % (cur_fail, fetch_res["reason"]))
            envelope.setdefault("warnings", []).append(
                {"warning_type": "fetch_failed", "message": fail_msg})

        # 产物落盘 → 之后才更新 state（§11-E）
        payload_path = await asyncio.to_thread(write_payload, envelope, out_dir, key)
        rec["payload"] = payload_path
        rec["import_recommendation"] = envelope["import_recommendation"]
        rec["data_confidence"] = envelope["data_confidence"]
        rec["missing_fields"] = envelope["missing_fields"]
        # §QC-F20：仅"真抓到数据"标 done；失败的记 failed（不跳过），--resume 会重试。
        if fetch_res["usable"]:
            await state.mark_done(key, payload_path)
        else:
            await state.mark_failed(key, prior_fail + 1, fetch_res["reason"])

        async with results_lock:
            results.append(rec)

    # 队列消费者模型（§11-D 4🔴1）：所有 target 塞队列，起 global 个 worker 循环消费。
    queue = asyncio.Queue()
    for t in targets:
        queue.put_nowait(t)

    async def worker():
        while True:
            try:
                target = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await process(target)
            except Exception as e:
                # 失败隔离（§0.4/§11-D + §QC-F16）：单目标处理异常不拖垮整批；except 块本身不依赖
                # target 结构（target 可能非 dict/缺 url），避免二次抛异常击穿隔离。
                _t = target if isinstance(target, dict) else {}
                async with results_lock:
                    results.append({
                        "target": target,
                        "task_key": _safe_task_key(target),
                        "fetch": {"usable": False, "tier_used": None, "status": 0, "ms_total": 0,
                                  "needs_manual": True, "attempts": 0,
                                  "reason": "内部处理异常：%s" % str(e)[:150],
                                  "final_url": _t.get("url", "")},
                        "tier_log": [], "import_recommendation": "manual_completion_required",
                        "data_confidence": "", "missing_fields": [],
                    })
            finally:
                queue.task_done()

    n_workers = min(conc["global"], len(targets)) or 1
    await asyncio.gather(*(worker() for _ in range(n_workers)))
    # 按输入顺序稳定排序（完成序不定）。§QC-F15：用 _safe_task_key，缺 url/非 dict 不抛 KeyError 丢整批。
    order = {_safe_task_key(t): i for i, t in enumerate(targets)}
    results.sort(key=lambda r: order.get(r["task_key"], 1e9))
    return results, {"credit_remaining": credit_gate.remaining}
