# -*- coding: utf-8 -*-
"""公开化改造回归测试：双 profile（generic/studycompass）+ --lang（en/zh）+ 去品牌 + 可选透传/回退。

跑法：<venv-python> _test_public.py
纯逻辑，不走真实网络/codex。改动前应多项 FAIL（红），改动后 ALL GREEN。
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_PASS, _FAIL = [], []


def check(name, cond, detail=""):
    (_PASS if cond else _FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + ("" if cond else "  -> " + str(detail)[:140]))


import output
import run
from output import build_envelope

FM = {"tier_used": "http-tls", "attempts": 1}

# ===== make_schema_provider：schema 与 lang 无关，指令按 lang 切换 =====
p_en, p_zh = run.make_schema_provider("en"), run.make_schema_provider("zh")
su_en, iu_en = p_en("university")
su_zh, iu_zh = p_zh("university")
sp_en, ip_en = p_en("programme")
sp_zh, ip_zh = p_zh("programme")
_ZH_UNI = "each item of strength_subjects in Simplified Chinese"
check("schema 与 lang 无关(同一对象)", su_en is su_zh and sp_en is sp_zh)
check("university en 指令=保原样不翻译", "do not translate" in iu_en and _ZH_UNI not in iu_en, iu_en[-80:])
check("university zh 指令=转简体中文", _ZH_UNI in iu_zh)
check("programme en 指令=保原样", "do not translate" in ip_en and "PRESERVE EXACTLY" not in ip_en)
check("programme zh 指令=转中文+保数字/名称", "Simplified Chinese" in ip_zh and "PRESERVE EXACTLY" in ip_zh)
check("programme base 领域规则两档都在", "direction to enum" in ip_en and "direction to enum" in ip_zh)

# ===== 大学 generic（默认）：干净信封，无专属键 =====
UNI = {"name_en": "University of Leeds", "country": "UK", "city": "Leeds",
       "introduction": "A public research university.", "strength_subjects": ["physics"]}
T_UNI = {"url": "https://en.wikipedia.org/wiki/University_of_Leeds", "type": "university"}
g = build_envelope(UNI, T_UNI, "wikipedia", T_UNI["url"], fetch_meta=FM)
_gk = set(g.keys())
check("generic 无 task_type/import_recommendation/agent_notes/source_summary",
      not (_gk & {"task_type", "import_recommendation", "agent_notes", "source_summary"}), sorted(_gk))
check("generic 有 type/source/needs_manual/notes", {"type", "source", "needs_manual", "notes"} <= _gk, sorted(_gk))
check("generic source_type 正确", g["source"]["source_type"] == "wikipedia" and g["type"] == "university")
check("generic 有 name_en → needs_manual False", g["needs_manual"] is False)
check("generic contains_privacy 恒 False", g["contains_privacy"] is False)
check("generic 仍如实记 name_cn 缺", "name_cn" in g["missing_fields"])
check("generic 不因 name_cn 缺报 missing_required 警告",
      not any(w.get("warning_type") == "missing_required" for w in g["warnings"]), g["warnings"])
check("generic data_confidence 硬枚举", g["data_confidence"] in ("high", "medium", "low"))

# ===== 大学 studycompass：submitCollectionResult 契约信封 =====
sc = build_envelope(UNI, T_UNI, "wikipedia", T_UNI["url"], fetch_meta=FM, profile="studycompass")
check("sc task_type=collect_universities", sc["task_type"] == "collect_universities")
check("sc 专属键齐全", {"import_recommendation", "agent_notes", "source_summary"} <= set(sc.keys()))
check("sc name_cn 缺 → manual_completion_required", sc["import_recommendation"] == "manual_completion_required")
check("sc name_cn 缺 → missing_required 警告", any(w.get("warning_type") == "missing_required" for w in sc["warnings"]))
check("sc 无 needs_manual/type/source(通用键)", not (set(sc.keys()) & {"needs_manual", "type", "source"}))

# ===== country 回退（非英美，不丢字段）=====
DE = {"name_en": "Technical University of Munich", "country": "Germany", "city": "Munich"}
gde = build_envelope(DE, {"url": "https://www.tum.de/en/", "type": "university"}, "official", "https://www.tum.de/en/", fetch_meta=FM)
check("country 内置码未命中→回退原文 germany", gde["items"][0].get("country") == "germany", gde["items"][0].get("country"))
check("回退后 country 不再计缺", "country" not in gde["missing_fields"])

# ===== 项目 university_id：可选透传 =====
PROG = {"name": "Data Science MSc", "direction": "cs", "degree": "MSc"}
T_PROG = {"url": "https://courses.leeds.ac.uk/i071/x", "type": "programme"}  # 无 university_id
gp = build_envelope(PROG, T_PROG, "official", T_PROG["url"], fetch_meta=FM)
check("generic 无 university_id → item 无该键", "university_id" not in gp["items"][0])
check("generic 不把 university_id 计缺", "university_id" not in gp["missing_fields"])
check("generic 有 name → needs_manual False(direction 有也不阻塞)", gp["needs_manual"] is False)

scp = build_envelope(PROG, T_PROG, "official", T_PROG["url"], fetch_meta=FM, profile="studycompass")
check("sc 缺 university_id → 计入 missing", "university_id" in scp["missing_fields"])
check("sc name+direction 齐 → recommend_manual_review", scp["import_recommendation"] == "recommend_manual_review")
check("sc agent_notes 含 university_id 提示(F9)", "university_id" in scp["agent_notes"])

scp2 = build_envelope(PROG, dict(T_PROG, university_id="LEEDS"), "official", T_PROG["url"], fetch_meta=FM, profile="studycompass")
check("提供 university_id → 透传进 item", scp2["items"][0].get("university_id") == "LEEDS")
check("有 university_id → 不计缺", "university_id" not in scp2["missing_fields"])

# ===== duration 跟 lang 走 =====
DUR = {"name": "X MSc", "direction": "cs", "duration": "12 Months (Full time)"}
d_en = build_envelope(DUR, T_PROG, "official", T_PROG["url"], fetch_meta=FM, lang="en")
check("lang=en duration 保原文", d_en["items"][0]["duration"] == "12 Months (Full time)")
d_zh = build_envelope(DUR, T_PROG, "official", T_PROG["url"], fetch_meta=FM, profile="studycompass", lang="zh")
check("lang=zh duration 转中文", "年" in d_zh["items"][0]["duration"], d_zh["items"][0]["duration"])

# ===== 去品牌：源置信度映射不含竞品键 =====
check("_SOURCE_CONF 去竞品(compassedu/competitor)", not (set(output._SOURCE_CONF) & {"compassedu", "competitor_aggregator"}), list(output._SOURCE_CONF))

# ===== F1 防线在两 profile 都在：白名单字段值为 dict 被丢弃 + 无控制字段落盘 =====
BAD = {"name": "X MSc", "direction": "cs", "faculty": {"is_simulated": True, "x": "y"}}
for prof in ("generic", "studycompass"):
    eb = build_envelope(BAD, dict(T_PROG, university_id="u1"), "official", T_PROG["url"], fetch_meta=FM, profile=prof)
    js = json.dumps(eb, ensure_ascii=False)
    check("F1 %s: dict 值丢弃且无 is_simulated 落盘" % prof, ("faculty" not in eb["items"][0]) and ("is_simulated" not in js))

# ===== 抓取失败降级：两 profile 各自需人工 =====
FAIL = {"__extract_error__": "fetch-failed:blocked"}
gf = build_envelope(FAIL, T_PROG, "official", T_PROG["url"], fetch_meta={"tier_used": None, "attempts": 3})
check("generic 抓取失败 → needs_manual True", gf["needs_manual"] is True)
scf = build_envelope(FAIL, T_PROG, "official", T_PROG["url"], fetch_meta={"tier_used": None, "attempts": 3}, profile="studycompass")
check("sc 抓取失败 → manual_completion_required", scf["import_recommendation"] == "manual_completion_required")

print("\n== 汇总 ==  PASS=%d  FAIL=%d" % (len(_PASS), len(_FAIL)))
if _FAIL:
    print("FAILED:", ", ".join(_FAIL)); sys.exit(1)
print("ALL GREEN"); sys.exit(0)
