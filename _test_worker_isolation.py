# 验证 🔴-1 修复：单目标 process 异常（注入 write_payload OSError）不拖垮整批（失败隔离）。
import asyncio
import scheduler
import output

# 假 escalate：直接返回 usable，不走网络。
async def fake_escalate(target, cfg, res):
    return {"usable": True, "text": "x" * 600, "tier_used": "http-tls", "status": 200,
            "ms_total": 10, "needs_manual": False, "reason": "", "final_url": target["url"],
            "attempts": 1, "tier_log": []}
scheduler.escalate = fake_escalate

# 假 build_envelope：返回最小合法信封（签名须跟随真实 build_envelope，含 profile/lang）。
def fake_build_envelope(extraction, target, source_type, final_url, fetch_meta=None,
                        profile="generic", lang="en"):
    return {"task_type": "collect_programmes", "target_name": "x", "source_summary": {},
            "data_confidence": "high", "items": [{}], "warnings": [], "conflicts": [],
            "missing_fields": [], "raw_evidence": [], "contains_privacy": False,
            "import_recommendation": "recommend_manual_review", "agent_notes": ""}
output.build_envelope = fake_build_envelope

# write_payload：对 key 含 'boom' 的目标抛 OSError（注入失败）。
def boom_write(envelope, out_dir, key):
    if "boom" in key:
        raise OSError("disk full (injected)")
    return "fake/path.json"
output.write_payload = boom_write

cfg = {"concurrency": {"global": 4, "min": 1, "max": 32, "browserCap": 4, "firecrawlCap": 2},
       "politeness": {"perDomain": 2, "minDelayMs": 10},
       "retry": {"perTier": 2, "backoffMs": 10, "maxAttemptsPerTarget": 6},
       "firecrawlBudget": {"maxCredits": 100}, "network": {"bypassProxyForFetch": False},
       "_available_tiers": ["http-tls"]}
targets = [{"url": "https://a%d.edu/x" % i, "type": "programme"} for i in range(5)]
targets[2]["url"] = "https://boom.edu/x"  # 该目标 task_key 含 'boom' → write_payload 抛错

def stub_extract(text, schema, instr, cfg):
    return {}
def stub_schema(t):
    return ({}, "")

res, meta = asyncio.run(scheduler.run_targets(targets, cfg, stub_extract, stub_schema, resume=False))
print("processed:", len(res), "/ targets:", len(targets))
for r in res:
    print(" -", r["target"]["url"], "usable=", r["fetch"]["usable"],
          "needs_manual=", r["fetch"].get("needs_manual"), "|", r.get("error", ""))
assert len(res) == 5, "❌ 隔离失败：注入异常拖垮整批（只完成 %d/5）" % len(res)
boom = [r for r in res if "boom" in r["target"]["url"]][0]
assert boom["fetch"]["needs_manual"] is True, "❌ 注入失败目标未标 needs_manual"
assert len([r for r in res if r["fetch"]["usable"]]) == 4, "❌ 其余 4 个未正常完成"
print("[OK] 隔离验证通过：5/5 全处理，注入失败目标被隔离成 needs_manual，其余 4 正常")
