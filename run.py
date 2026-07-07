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
    # 上游接口收紧为 OpenAI 严格结构化输出：required 须列全所有键 + 字段可空（未抽到=null）。
    # output.py 已把 null/"" 视作缺失，故可空不破坏"不编造"链。
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name_cn": {"type": ["string", "null"], "description": "院校简体中文名"},
        "name_en": {"type": ["string", "null"], "description": "院校英文名"},
        "country": {"type": ["string", "null"], "description": "国家（英文或代码 uk/hk/sg/us/au）"},
        "city": {"type": ["string", "null"], "description": "所在城市"},
        "introduction": {"type": ["string", "null"], "description": "院校简介，<=500 字"},
        "strength_subjects": {"type": ["array", "null"], "items": {"type": "string"}, "description": "优势学科"},
        "official_website": {"type": ["string", "null"], "description": "官网 URL"},
        "missing_fields": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name_cn", "name_en", "country", "city", "introduction",
                 "strength_subjects", "official_website", "missing_fields"],
}

_PROGRAMME_SCHEMA = {
    # 上游接口收紧为 OpenAI 严格结构化输出：required 须列全所有键 + 字段可空（未抽到=null）。
    # output.py 已把 null/"" 视作缺失，故可空不破坏"不编造"链。
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": ["string", "null"], "description": "项目英文名"},
        "name_cn": {"type": ["string", "null"], "description": "项目中文名"},
        "degree": {"type": ["string", "null"], "description": "学位 MSc/MA/MS/LLM"},
        "direction": {"type": ["string", "null"],
                      "description": "方向枚举 business/finance/cs/media/law/engineering/science/social_science/art/education/other"},
        "programme_category": {"type": ["string", "null"]},
        "faculty": {"type": ["string", "null"], "description": "所属学院/系"},
        "duration": {"type": ["string", "null"], "description": "学制，如 '1 year' / '12 months'"},
        "study_mode": {"type": ["string", "null"], "description": "full-time/part-time"},
        "programme_intro": {"type": ["string", "null"], "description": "项目简介"},
        "academic_requirement": {"type": ["string", "null"], "description": "学术/入学要求"},
        "min_grade_band": {"type": ["string", "null"], "description": "最低成绩要求"},
        "language_note": {"type": ["string", "null"], "description": "语言要求说明"},
        "ielts_total": {"type": ["number", "null"], "description": "雅思总分；无->null"},
        "ielts_sub_min": {"type": ["number", "null"], "description": "雅思小分要求；无->null"},
        "tuition_fee": {"type": ["number", "null"], "description": "学费数字"},
        "tuition_label": {"type": ["string", "null"], "description": "学费原文串"},
        "deadline_label": {"type": ["string", "null"], "description": "申请截止期"},
        "gre_gmat_requirement": {"type": ["string", "null"], "description": "not_required/optional/required"},
        "official_url": {"type": ["string", "null"], "description": "项目官网 URL"},
        "missing_fields": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "name_cn", "degree", "direction", "programme_category", "faculty",
                 "duration", "study_mode", "programme_intro", "academic_requirement",
                 "min_grade_band", "language_note", "ielts_total", "ielts_sub_min",
                 "tuition_fee", "tuition_label", "deadline_label", "gre_gmat_requirement",
                 "official_url", "missing_fields"],
}

# 抽取指令 = 领域规则（base，语言无关）+ 语言指令（--lang 决定）。
# base 只讲"抽什么/怎么归一"；zh 追加"自由文本转简体中文"，en 追加"自由文本保源语言、不翻译"。
# 无论哪档，数字/分数/日期/货币/名称/枚举码/URL 一律保原样（只对自由文本切换语言）。
_UNIVERSITY_INSTR_BASE = (
    "You are a university data extractor. Extract only facts present in the text. "
    "name_cn must be Simplified Chinese if present; otherwise omit and add to missing_fields. "
    "country can be English name. Never fabricate; if not in the text, omit and add to missing_fields."
)
_UNIVERSITY_INSTR_ZH = (
    " Output introduction, city, and each item of strength_subjects in Simplified Chinese. "
    "Keep name_en and official_website verbatim (name_cn is already Chinese)."
)
_UNIVERSITY_INSTR_EN = (
    " Output free-text fields (introduction, city, strength_subjects) in the source's original "
    "language; do not translate. Keep name_en and official_website verbatim."
)

_PROGRAMME_INSTR_BASE = (
    "You are a master's programme data extractor. Map direction to enum "
    "(computer/computational/data science->cs, business/management->business, finance->finance, "
    "media/communication->media, law->law, engineering->engineering, science->science, "
    "social->social_science, art/design->art, education->education, else->other). "
    "Convert IELTS scores to numbers; none/not-required->null. "
    "gre_gmat_requirement use not_required/optional/required. "
    "Extract only facts in the text; never fabricate; if not in the text, omit and add to missing_fields."
)
_PROGRAMME_INSTR_ZH = (
    " Output the VALUES of these fields in Simplified Chinese (faithful translation/summary): "
    "programme_intro, academic_requirement, language_note, faculty. "
    "PRESERVE EXACTLY (do not alter when translating): all numbers, IELTS/GPA scores, dates, "
    "currency amounts, and proper names. "
    "For tuition_label and deadline_label: keep numbers/dates/currency verbatim, surrounding words may be Chinese. "
    "Keep VERBATIM / do NOT translate: name (official English name), degree, direction, study_mode, "
    "gre_gmat_requirement, min_grade_band codes (e.g. \"2:1\" / \"WAM 65%\"), "
    "ielts_total, ielts_sub_min, tuition_fee, official_url."
)
_PROGRAMME_INSTR_EN = (
    " Output free-text fields (programme_intro, academic_requirement, language_note, faculty, "
    "tuition_label, deadline_label) in the source's original language; do not translate. "
    "Keep all numbers, scores, dates, currency, proper names, enum codes and URLs verbatim."
)


def make_schema_provider(lang="en"):
    """按 --lang 组装 (schema, instructions) 的闭包；scheduler 仍以 schema_provider(ttype) 调用。

    lang=en（默认）自由文本保源语言原样；lang=zh 自由文本转简体中文（数字/名称/枚举码/URL 仍保原样）。
    """
    lang = "zh" if str(lang).lower() == "zh" else "en"

    def provider(ttype):
        if ttype == "university":
            tail = _UNIVERSITY_INSTR_ZH if lang == "zh" else _UNIVERSITY_INSTR_EN
            return _UNIVERSITY_SCHEMA, _UNIVERSITY_INSTR_BASE + tail
        tail = _PROGRAMME_INSTR_ZH if lang == "zh" else _PROGRAMME_INSTR_EN
        return _PROGRAMME_SCHEMA, _PROGRAMME_INSTR_BASE + tail

    return provider


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
    lines.append("| # | 类型 | URL | tier_used | usable | 尝试 | 耗时 | confidence | review | needs_manual 原因 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(results, 1):
        t = r["target"]
        if r.get("skipped"):
            lines.append("| %d | %s | %s | - | (resume 跳过) | - | - | - | - | %s |" % (
                i, t.get("type"), _cell(_short(t["url"])), _cell(r.get("reason", ""))))
            continue
        f = r.get("fetch", {})
        reason = f.get("reason", "") if f.get("needs_manual") else ""
        lines.append("| %d | %s | %s | %s | %s | %d | %.1fs | %s | %s | %s |" % (
            i, t.get("type"), _cell(_short(t["url"])), f.get("tier_used") or "-",
            "✓" if f.get("usable") else "✗", f.get("attempts", 0),
            (f.get("ms_total", 0) / 1000.0), r.get("data_confidence", "-"),
            r.get("review_status", "-"), _cell(reason)))
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


def _cell(v):
    """§QC-F33：markdown 表格单元格转义 |（URL/needs_manual 原因含竖线会破表列）。"""
    return str(v).replace("|", "\\|")


def main():
    # §QC-F36：Windows 控制台默认代码页（GBK/936）下中文 print 乱码；reconfigure stdout/stderr 为 utf-8。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    # §11-I：Windows + asyncio + Playwright 必设 Proactor 策略。
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    ap = argparse.ArgumentParser(description="smart-crawler：可移植院校/项目采集（升级链 + 打分 + 白名单信封）")
    ap.add_argument("--targets", required=True, help="带类型 JSON 文件 [{url,type,hint?,university_id?}]")
    ap.add_argument("--concurrency", type=int, default=None, help="全局并发，默认 8，clamp[1,32]")
    ap.add_argument("--resume", action="store_true", help="断点续跑（默认关）")
    ap.add_argument("--config", default=None, help="配置文件路径（默认 crawler.config.json）")
    ap.add_argument("--profile", choices=["generic", "studycompass"], default=None,
                    help="输出信封形态：generic（默认，通用）| studycompass（内部契约）。覆盖 config.output.profile")
    ap.add_argument("--lang", choices=["en", "zh"], default=None,
                    help="自由文本语言：en（默认，保原样）| zh（转简体中文）。覆盖 config.output.lang")
    args = ap.parse_args()

    from config import load_config, ConfigError
    from scheduler import run_targets
    from extract import extract

    # --concurrency 的 clamp 统一由 load_config 做（clamp 到 [min,max]，§11-J），此处不重复 clamp。
    try:
        cfg = load_config(args.config, cli_concurrency=args.concurrency,
                          cli_profile=args.profile, cli_lang=args.lang)
    except ConfigError as e:
        print("[配置错误] %s" % e, file=sys.stderr)
        sys.exit(2)

    # §11-I 4🔴2：bypassProxyForFetch 时启动一次性清代理 env（全程不动，避免 per-fetch 跨线程竞态）。
    if cfg.get("network", {}).get("bypassProxyForFetch", True):
        _strip_proxy_env_once()

    try:
        with open(args.targets, "r", encoding="utf-8-sig") as f:  # §QC-F35 容忍 BOM
            targets = json.load(f)
    except (OSError, ValueError) as e:  # §QC-F30：坏/缺 targets 文件友好报错（原裸 open+json.load 抛栈 exit1）
        print("[错误] targets 文件读取/解析失败：%s" % str(e)[:120], file=sys.stderr)
        sys.exit(2)
    if not isinstance(targets, list) or not targets:
        print("[错误] targets 必须是非空 JSON 数组", file=sys.stderr)
        sys.exit(2)

    # §11-J：校验每条 type ∈ {university, programme}（归一后），非法即报错退出，不静默降级 programme。
    from urllib.parse import urlparse
    _VALID_TYPES = ("university", "programme")
    for i, t in enumerate(targets):
        if not isinstance(t, dict) or not t.get("url"):
            print("[错误] targets[%d] 缺 url 或非对象：%r" % (i, t), file=sys.stderr)
            sys.exit(2)
        # §QC-F17：URL 须带 scheme + host，否则 urlparse().netloc 为空致按域限流塌缩进同一空桶（防封失效）。
        _u = urlparse(str(t["url"]))
        if not _u.scheme:
            t["url"] = "https://" + str(t["url"]).lstrip("/")  # 缺 scheme 自动补 https://
            _u = urlparse(t["url"])
        if _u.scheme not in ("http", "https") or not _u.netloc:
            print("[错误] targets[%d] url 非法（需 http(s)://host/...）：%r" % (i, t.get("url")), file=sys.stderr)
            sys.exit(2)
        raw_type = str(t.get("type", "")).strip().lower()
        if raw_type not in _VALID_TYPES:
            print("[错误] targets[%d] type=%r 非法（须 university 或 programme）：%s" % (
                i, t.get("type"), t.get("url")), file=sys.stderr)
            sys.exit(2)
        t["type"] = raw_type  # 归一写回，下游按归一值分流

    for w in cfg.get("_warnings", []):
        print("[能力自检] %s" % w)
    schema_provider = make_schema_provider(cfg["output"]["lang"])
    print("[启动] 目标 %d 个，并发 global=%d，抓取档 %s，抽取 %s，profile=%s，lang=%s，resume=%s" % (
        len(targets), cfg["concurrency"]["global"], cfg["_available_tiers"],
        cfg["llm"].get("provider"), cfg["output"]["profile"], cfg["output"]["lang"], args.resume))

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
