# -*- coding: utf-8 -*-
"""质检回归测试：覆盖本轮修复的 bug（TDD 红→绿）。纯逻辑 + 轻量 asyncio mock，不走真实网络/codex。

跑法：<venv-python> _test_qc_regression.py
修复前应有多项 FAIL（红），修复后应 ALL GREEN。
"""
import sys, os, asyncio, json, tempfile, time as _t
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_PASS = []; _FAIL = []
def check(name, cond, detail=""):
    (_PASS if cond else _FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + ("" if cond else "  -> " + str(detail)[:120]))

# ================= output.py =================
import output
from output import _to_num, _normalize_duration, build_envelope

# F2 区间值不拼接成巨数
check("F2 _to_num区间取下界", _to_num("12,500-15,000") == 12500, _to_num("12,500-15,000"))
check("F2 _to_num英镑区间", _to_num("£12,500 - £15,000") == 12500, _to_num("£12,500 - £15,000"))
# F3 IELTS 总分(小分)
check("F3 _to_num总分(小分)", _to_num("7.5 (6.0)") == 7.5, _to_num("7.5 (6.0)"))
# 不回归
check("_to_num常规千分位", _to_num("25,000") == 25000 and _to_num("1,234.56") == 1234.56)
check("_to_num纯文字None", _to_num("N/A") is None)

# F8 小数 months 不回溯
check("F8 duration小数months", _normalize_duration("1.5 months") != "5 个月", _normalize_duration("1.5 months"))
check("F8 duration正常不回归", _normalize_duration("12 Months (Full time)") == "1 年（全日制）")

# F1 嵌套控制字段不入产物（白名单值层标量化）
env1 = build_envelope({"programme": "MSc Finance", "direction": "finance",
                       "degree": {"is_simulated": True, "status": "draft", "value": "MSc"}},
                      {"type": "programme", "university_id": "u1"}, "official", "https://x.ac.uk/p")
js1 = json.dumps(env1, ensure_ascii=False)
check("F1 嵌套控制字段不落盘", ("is_simulated" not in js1) and ("review_status" not in js1) and ("\"status\"" not in js1), js1)

# F6 name 提取不把 degree_name 当项目名
env6 = build_envelope({"degree_name": "MSc", "programme": "Financial Economics", "direction": "finance"},
                      {"type": "programme", "university_id": "u1"}, "official", "https://x.ac.uk")
check("F6 degree_name不当项目名", env6["items"][0].get("name") == "Financial Economics", env6["items"][0].get("name"))
env6b = build_envelope({"programme": "MSc Y", "direction": "cs", "degree_requirement": "Second-class honours degree"},
                       {"type": "programme", "university_id": "u1"}, "official", "https://x.ac.uk")
check("F6 degree不误取requirement", env6b["items"][0].get("degree") != "Second-class honours degree", env6b["items"][0].get("degree"))
# 不回归：合法项目名（含 School）不被误杀
env6c = build_envelope({"programme_name": "School of Data Science MSc", "direction": "cs"},
                       {"type": "programme", "university_id": "u1"}, "official", "https://x.ac.uk")
check("F6 合法School项目名不误杀", env6c["items"][0].get("name") == "School of Data Science MSc", env6c["items"][0].get("name"))

# F7 missing_fields 只保留字符串
env7 = build_envelope({"programme": "MSc", "direction": "cs", "missing_fields": [{"status": "x"}, 123, "real_field"]},
                      {"type": "programme", "university_id": "u1"}, "official", "https://x.ac.uk")
check("F7 missing_fields仅字符串", all(isinstance(x, str) for x in env7["missing_fields"]) and "status" not in json.dumps(env7["missing_fields"]), env7["missing_fields"])

# F10 非 dict extraction 有降级告警
env10 = build_envelope(None, {"type": "programme", "university_id": "u1"}, "official", "https://x.ac.uk")
check("F10 非dict抽取有降级告警", any(("degrad" in json.dumps(w, ensure_ascii=False)) or ("invalid" in json.dumps(w, ensure_ascii=False)) or ("降级" in json.dumps(w, ensure_ascii=False)) for w in env10["warnings"]), env10["warnings"])

# F4 幻觉 url 不顶替可信 final_url（去宽泛 exact 键）
env4 = build_envelope({"programme": "MSc X", "direction": "cs", "url": "https://hallucinated.evil/pdf"},
                      {"type": "programme", "university_id": "u1"}, "official", "https://real.ac.uk/prog")
check("F4 幻觉url不顶final_url", env4["items"][0].get("official_url") == "https://real.ac.uk/prog", env4["items"][0].get("official_url"))
# 不回归：明确 official_url 仍生效
env4b = build_envelope({"programme": "MSc X", "direction": "cs", "official_url": "https://real.ac.uk/official"},
                       {"type": "programme", "university_id": "u1"}, "official", "https://real.ac.uk/prog")
check("F4 明确official_url生效", env4b["items"][0].get("official_url") == "https://real.ac.uk/official", env4b["items"][0].get("official_url"))
# 不回归：wikipedia 官网取 LLM 抽的真官网
env4c = build_envelope({"name_zh_hans": "利兹大学", "name_en": "University of Leeds", "official_website": "leeds.ac.uk", "country": "UK"},
                       {"type": "university"}, "wikipedia", "https://en.wikipedia.org/wiki/University_of_Leeds")
check("F4 wikipedia官网不回归", env4c["items"][0].get("official_website") == "leeds.ac.uk", env4c["items"][0].get("official_website"))

# 不回归：contains_privacy 恒 false，raw 塞控制字段进不了顶层
env_pv = build_envelope({"programme": "MSc", "direction": "cs", "contains_privacy": True, "is_simulated": True, "status": "approved"},
                        {"type": "programme", "university_id": "u1"}, "official", "https://x.ac.uk")
check("契约 contains_privacy恒false", env_pv["contains_privacy"] is False)
check("契约 顶层无控制字段泄漏", "is_simulated" not in json.dumps(env_pv["items"][0]))

# ================= scoring.py =================
import scoring
from scoring import html_to_text, looks_blocked, score, expect_for

# F11 chr 越界不崩
try:
    html_to_text("<p>x</p>&#1114112; more text")
    html_to_text("<p>y</p>&#99999999999; more text")
    check("F11 畸形数字实体不崩", True)
except Exception as e:
    check("F11 畸形数字实体不崩", False, "%s:%s" % (type(e).__name__, e))
# score 不冒泡
try:
    s = score(expect_for("programme"), True, 200, "<p>data science fees tuition modules english entry requirements</p>&#1114112;" + "x" * 600)
    check("F11 score不冒泡异常", True)
except Exception as e:
    check("F11 score不冒泡异常", False, "%s:%s" % (type(e).__name__, e))
# 不回归：正常实体解码
check("html_to_text正常实体不回归", "&" in html_to_text("A &amp; B") and "A" in html_to_text("<p>A &amp; B</p>"))

# F13 长正文含 cloudflare 不误判 blocked
long_ok = "Welcome. This site is protected by Cloudflare. " + "University programme details and entry requirements. " * 100
check("F13 长正文cloudflare不误判", looks_blocked(200, long_ok) == False, "blocked=%s len=%d" % (looks_blocked(200, long_ok), len(long_ok)))
# 不回归：真封页仍 blocked（状态码 + 短 challenge 页）
check("F13 真封页状态码仍blocked", looks_blocked(403, "x") == True)
check("F13 短challenge页仍blocked", looks_blocked(200, "Attention Required! Please verify you are a human.") == True)
# F11修正（验收发现的回归）：&nbsp; 须归一为普通空格，否则 "entry requirements" 等多词关键词子串失配
_nbsp_txt = html_to_text("<p>Entry&nbsp;requirements and English&nbsp;language.</p>")
check("F11修正 nbsp归一为空格", ("entry requirements" in _nbsp_txt.lower()) and ("\xa0" not in _nbsp_txt), repr(_nbsp_txt))

# ================= config.py =================
import importlib
import config
def _write_cfg(d):
    p = os.path.join(tempfile.mkdtemp(prefix="qccfg-"), "crawler.config.json")
    open(p, "w", encoding="utf-8").write(json.dumps(d))
    return p

# F31 clamp 上限对齐契约 32/4/2
c31 = config.load_config(_write_cfg({"fetch": {"tiers": ["http-tls"]}, "llm": {"provider": "codex"},
                                     "concurrency": {"max": 64, "global": 64, "browserCap": 16, "firecrawlCap": 8}}))
check("F31 global上限32", c31["concurrency"]["global"] <= 32, c31["concurrency"]["global"])
check("F31 browserCap上限4", c31["concurrency"]["browserCap"] <= 4, c31["concurrency"]["browserCap"])
check("F31 firecrawlCap上限2", c31["concurrency"]["firecrawlCap"] <= 2, c31["concurrency"]["firecrawlCap"])

# F23 llm.timeoutMs/maxRetry 被规范为数值
c23 = config.load_config(_write_cfg({"fetch": {"tiers": ["http-tls"]}, "llm": {"provider": "codex", "timeoutMs": "abc", "maxRetry": None}, "concurrency": {}}))
check("F23 llm.timeoutMs规范数值", isinstance(c23["llm"].get("timeoutMs"), (int, float)), c23["llm"].get("timeoutMs"))
check("F23 llm.maxRetry规范数值", isinstance(c23["llm"].get("maxRetry"), (int, float)), c23["llm"].get("maxRetry"))
# F23b 非数值 politeness/retry 不崩
try:
    config.load_config(_write_cfg({"fetch": {"tiers": ["http-tls"]}, "llm": {"provider": "codex"}, "politeness": {"minDelayMs": None}, "retry": {"backoffMs": "x"}}))
    check("F23b 非数值politeness/retry不崩", True)
except Exception as e:
    check("F23b 非数值politeness/retry不崩", False, "%s:%s" % (type(e).__name__, e))

# ================= extract.py =================
from extract import _parse_json_loose
def _is_err(x): return isinstance(x, dict) and x.get("__extract_error__") is not None
check("F25 裸null返错误标记", _is_err(_parse_json_loose("null")), _parse_json_loose("null"))
check("F25 裸标量返错误标记", _is_err(_parse_json_loose("123")), _parse_json_loose("123"))
check("F25 数组返错误标记", _is_err(_parse_json_loose("[1,2,3]")), _parse_json_loose("[1,2,3]"))
check("F25 正常dict不回归", _parse_json_loose('{"a":1}') == {"a": 1})
check("F25 code-fence包裹不回归", _parse_json_loose('```json\n{"a":1}\n```') == {"a": 1})

# ================= scheduler.py (asyncio) =================
import scheduler
def _stub_extract(t, s, i, c): return {}
def _stub_schema(t): return ({}, "")
_CFG = {"concurrency": {"global": 4, "min": 1, "max": 32, "browserCap": 4, "firecrawlCap": 2},
        "politeness": {"perDomain": 2, "minDelayMs": 10}, "retry": {"perTier": 2, "backoffMs": 10, "maxAttemptsPerTarget": 6},
        "firecrawlBudget": {"maxCredits": 100}, "network": {"bypassProxyForFetch": False}, "_available_tiers": ["http-tls"]}
async def _sched_tests():
    async def fake_esc(target, cfg, res):
        return {"usable": True, "text": "x" * 600, "tier_used": "http-tls", "status": 200, "ms_total": 10,
                "needs_manual": False, "reason": "", "final_url": target.get("url", ""), "attempts": 1, "tier_log": []}
    scheduler.escalate = fake_esc
    # F16 None target 不崩整批
    try:
        r, _ = await scheduler.run_targets([{"url": "https://a.edu/x", "type": "programme"}, None, {"url": "https://b.edu/y", "type": "programme"}], _CFG, _stub_extract, _stub_schema)
        check("F16 None target不崩整批", len(r) >= 2, "len=%d" % len(r))
    except Exception as e:
        check("F16 None target不崩整批", False, "%s:%s" % (type(e).__name__, e))
    # F15 缺 url target 不崩整批
    try:
        r, _ = await scheduler.run_targets([{"url": "https://a.edu/x", "type": "programme"}, {"type": "programme"}], _CFG, _stub_extract, _stub_schema)
        check("F15 缺url target不崩整批", len(r) >= 1, "len=%d" % len(r))
    except Exception as e:
        check("F15 缺url target不崩整批", False, "%s:%s" % (type(e).__name__, e))
asyncio.run(_sched_tests())

# F21 resume=False 不写 state.json
_sp1 = os.path.join(tempfile.mkdtemp(prefix="qcst-"), "state.json")
_st1 = scheduler.State(_sp1, enabled=False)
asyncio.run(_st1.mark_done("k1", "/p"))
check("F21 resume=False不写state", not os.path.exists(_sp1), "exists=%s" % os.path.exists(_sp1))
# 不回归：resume=True 写 state
_sp2 = os.path.join(tempfile.mkdtemp(prefix="qcst-"), "state.json")
_st2 = scheduler.State(_sp2, enabled=True)
asyncio.run(_st2.mark_done("k1", "/p"))
check("F21 resume=True仍写state", os.path.exists(_sp2))

print("\n== 汇总 ==  PASS=%d  FAIL=%d" % (len(_PASS), len(_FAIL)))
if _FAIL:
    print("FAILED:", ", ".join(_FAIL)); sys.exit(1)
print("ALL GREEN"); sys.exit(0)
