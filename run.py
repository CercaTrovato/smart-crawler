# -*- coding: utf-8 -*-
"""smart-crawler 入口（契约 §11-J / §11-I）。

用法：
  <PY> run.py --targets targets.json --concurrency 8 [--resume]

--targets：带类型 JSON `[{url,type:'university'|'programme',hint?,university_id?,source_type?}]`
--concurrency：默认 8，clamp[1,32]
--resume：默认关

入口必须设 WindowsProactorEventLoopPolicy（§11-I，Windows + asyncio + Playwright）。
"""
import argparse
import asyncio
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 代理 env 键（大小写两套）：抓取直连档不被本机 Clash/VPN 黑洞（§11-I）。
_PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)


def _strip_proxy_env_once():
    """启动时一次性清掉抓取用代理 env + 设 NO_PROXY='*'（全程不动，避免 per-fetch 跨线程竞态，§11-I 4🔴2）。

    Firecrawl 的 API 调用不受影响（它显式 proxy=FIRECRAWL_PROXY，参数式，读的是 env 变量值而非进程代理）。
    """
    for k in _PROXY_ENV_KEYS:
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"

# —— 抽取 schema（供 codex/openai-compat；键名即 output 层别名映射的锚点） —— #

_UNIVERSITY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name_cn": {"type": "string", "description": "院校简体中文名"},
        "name_en": {"type": "string", "description": "院校英文名"},
        "country": {"type": "string", "description": "国家（英文或代码 uk/hk/sg/us/au）"},
        "city": {"type": "string", "description": "所在城市"},
        "introduction": {"type": "string", "description": "院校简介，<=500 字"},
        "strength_subjects": {"type": "array", "items": {"type": "string"}, "description": "优势学科"},
        "official_website": {"type": "string", "description": "官网 URL"},
        "missing_fields": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["missing_fields"],
}

_PROGRAMME_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "description": "项目英文名"},
        "name_cn": {"type": "string", "description": "项目中文名"},
        "degree": {"type": "string", "description": "学位 MSc/MA/MS/LLM"},
        "direction": {"type": "string",
                      "description": "方向枚举 business/finance/cs/media/law/engineering/science/social_science/art/education/other"},
        "programme_category": {"type": "string"},
        "faculty": {"type": "string", "description": "所属学院/系"},
        "duration": {"type": "string", "description": "学制，如 '1 year' / '12 months'"},
        "study_mode": {"type": "string", "description": "full-time/part-time"},
        "programme_intro": {"type": "string", "description": "项目简介"},
        "academic_requirement": {"type": "string", "description": "学术/入学要求"},
        "min_grade_band": {"type": "string", "description": "最低成绩要求"},
        "language_note": {"type": "string", "description": "语言要求说明"},
        "ielts_total": {"type": ["number", "null"], "description": "雅思总分；无->null"},
        "ielts_sub_min": {"type": ["number", "null"], "description": "雅思小分要求；无->null"},
        "tuition_fee": {"type": ["number", "null"], "description": "学费数字"},
        "tuition_label": {"type": "string", "description": "学费原文串"},
        "deadline_label": {"type": "string", "description": "申请截止期"},
        "gre_gmat_requirement": {"type": "string", "description": "not_required/optional/required"},
        "official_url": {"type": "string", "description": "项目官网 URL"},
        "missing_fields": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["missing_fields"],
}

_UNIVERSITY_INSTR = (
    "You are a study-abroad university data extractor. Extract only facts present in the text. "
    "name_cn must be Simplified Chinese if present; otherwise omit and add to missing_fields. "
    "country can be English name. Never fabricate."
)
_PROGRAMME_INSTR = (
    "You are a study-abroad master programme data extractor. Map direction to enum "
    "(computer/computational/data science->cs, business/management->business, finance->finance, "
    "media/communication->media, law->law, engineering->engineering, science->science, "
    "social->social_science, art/design->art, education->education, else->other). "
    "Convert IELTS scores to numbers; none/not-required->null. "
    "gre_gmat_requirement use not_required/optional/required. Extract only facts in the text; never fabricate."
)


def schema_provider(ttype):
    if ttype == "university":
        return _UNIVERSITY_SCHEMA, _UNIVERSITY_INSTR
    return _PROGRAMME_SCHEMA, _PROGRAMME_INSTR


# —— run-report —— #

def write_run_report(results, meta, cfg, out_dir, elapsed_s):
    path = os.path.join(HERE, "run-report.md")
    n = len(results)
    ok = sum(1 for r in results if r.get("fetch", {}).get("usable"))
    manual = sum(1 for r in results if r.get("fetch", {}).get("needs_manual"))
    skipped = sum(1 for r in results if r.get("skipped"))
    lines = []
    lines.append("# smart-crawler run-report")
    lines.append("")
    lines.append("- 时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("- 目标数：%d（成功 usable %d / needs_manual %d / resume 跳过 %d）" % (n, ok, manual, skipped))
    lines.append("- 并发：global=%d, browserCap=%d, firecrawlCap=%d" % (
        cfg["concurrency"]["global"], cfg["concurrency"]["browserCap"], cfg["concurrency"]["firecrawlCap"]))
    lines.append("- 抽取后端：%s" % cfg["llm"].get("provider"))
    lines.append("- 可用抓取档：%s" % ", ".join(cfg["_available_tiers"]))
    lines.append("- Firecrawl credit 剩余：%s" % meta.get("credit_remaining"))
    lines.append("- 总耗时：%.1fs" % elapsed_s)
    if cfg.get("_warnings"):
        lines.append("- 能力自检 warnings：")
        for w in cfg["_warnings"]:
            lines.append("  - %s" % w)
    lines.append("")
    lines.append("## 逐目标")
    lines.append("")
    lines.append("| # | 类型 | URL | tier_used | usable | 尝试 | 耗时 | confidence | import_rec | needs_manual 原因 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(results, 1):
        t = r["target"]
        if r.get("skipped"):
            lines.append("| %d | %s | %s | - | (resume 跳过) | - | - | - | - | %s |" % (
                i, t.get("type"), _short(t["url"]), r.get("reason", "")))
            continue
        f = r.get("fetch", {})
        reason = f.get("reason", "") if f.get("needs_manual") else ""
        lines.append("| %d | %s | %s | %s | %s | %d | %.1fs | %s | %s | %s |" % (
            i, t.get("type"), _short(t["url"]), f.get("tier_used") or "-",
            "✓" if f.get("usable") else "✗", f.get("attempts", 0),
            (f.get("ms_total", 0) / 1000.0), r.get("data_confidence", "-"),
            r.get("import_recommendation", "-"), reason))
    lines.append("")
    lines.append("## 抓取档明细（tier_log）")
    lines.append("")
    for i, r in enumerate(results, 1):
        if r.get("skipped"):
            continue
        lines.append("### %d. %s" % (i, _short(r["target"]["url"])))
        for step in r.get("tier_log", []):
            if step.get("denied"):
                lines.append("- %s 尝试%s：拒绝（%s）" % (step["tier"], step.get("attempt"), step["denied"]))
            else:
                lines.append(
                    "- %s 尝试%s：status=%s usable=%s 正文=%d 命中=%s/%s%s ms=%s%s" % (
                        step["tier"], step.get("attempt"), step.get("status"), step.get("usable"),
                        step.get("text_len", 0), step.get("hits"), step.get("kw_total"),
                        " BLOCK" if step.get("blocked") else "",
                        step.get("ms"), (" ERR:" + step["error"]) if step.get("error") else ""))
        pay = r.get("payload")
        if pay:
            lines.append("- 产物：`%s`" % os.path.basename(pay))
            if r.get("missing_fields"):
                lines.append("- missing_fields：%s" % ", ".join(r["missing_fields"]))
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def _short(url):
    return url if len(url) <= 60 else url[:57] + "..."


def main():
    # §11-I：Windows + asyncio + Playwright 必设 Proactor 策略。
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    ap = argparse.ArgumentParser(description="smart-crawler：可移植院校/项目采集（升级链 + 打分 + 白名单信封）")
    ap.add_argument("--targets", required=True, help="带类型 JSON 文件 [{url,type,hint?,university_id?}]")
    ap.add_argument("--concurrency", type=int, default=None, help="全局并发，默认 8，clamp[1,32]")
    ap.add_argument("--resume", action="store_true", help="断点续跑（默认关）")
    ap.add_argument("--config", default=None, help="配置文件路径（默认 crawler.config.json）")
    args = ap.parse_args()

    from config import load_config, ConfigError
    from scheduler import run_targets
    from extract import extract

    # --concurrency 的 clamp 统一由 load_config 做（clamp 到 [min,max]，§11-J），此处不重复 clamp。
    try:
        cfg = load_config(args.config, cli_concurrency=args.concurrency)
    except ConfigError as e:
        print("[配置错误] %s" % e, file=sys.stderr)
        sys.exit(2)

    # §11-I 4🔴2：bypassProxyForFetch 时启动一次性清代理 env（全程不动，避免 per-fetch 跨线程竞态）。
    if cfg.get("network", {}).get("bypassProxyForFetch", True):
        _strip_proxy_env_once()

    with open(args.targets, "r", encoding="utf-8") as f:
        targets = json.load(f)
    if not isinstance(targets, list) or not targets:
        print("[错误] targets 必须是非空 JSON 数组", file=sys.stderr)
        sys.exit(2)

    # §11-J：校验每条 type ∈ {university, programme}（归一后），非法即报错退出，不静默降级 programme。
    _VALID_TYPES = ("university", "programme")
    for i, t in enumerate(targets):
        if not isinstance(t, dict) or not t.get("url"):
            print("[错误] targets[%d] 缺 url 或非对象：%r" % (i, t), file=sys.stderr)
            sys.exit(2)
        raw_type = str(t.get("type", "")).strip().lower()
        if raw_type not in _VALID_TYPES:
            print("[错误] targets[%d] type=%r 非法（须 university 或 programme）：%s" % (
                i, t.get("type"), t.get("url")), file=sys.stderr)
            sys.exit(2)
        t["type"] = raw_type  # 归一写回，下游按归一值分流

    for w in cfg.get("_warnings", []):
        print("[能力自检] %s" % w)
    print("[启动] 目标 %d 个，并发 global=%d，抓取档 %s，抽取 %s，resume=%s" % (
        len(targets), cfg["concurrency"]["global"], cfg["_available_tiers"],
        cfg["llm"].get("provider"), args.resume))

    t0 = time.time()
    results, meta = asyncio.run(
        run_targets(targets, cfg, extract, schema_provider, resume=args.resume))
    elapsed = time.time() - t0

    report_path = write_run_report(results, meta, cfg, "out", elapsed)
    ok = sum(1 for r in results if r.get("fetch", {}).get("usable"))
    manual = sum(1 for r in results if r.get("fetch", {}).get("needs_manual"))
    print("[完成] usable %d / needs_manual %d / 共 %d，耗时 %.1fs" % (ok, manual, len(results), elapsed))
    print("[产物] out/payload-*.json")
    print("[报告] %s" % report_path)


if __name__ == "__main__":
    main()
