# -*- coding: utf-8 -*-
"""产物层：抽取结果 + 源信息 → 结构化信封。

两种输出 profile（由 build_envelope 的 profile 参数决定）：
  - generic（默认，面向公开）：干净通用信封 {target_name,type,source,data_confidence,items,
    missing_fields,warnings,evidence,contains_privacy,needs_manual,notes}，无任何下游专属键。
  - studycompass（内部契约）：submitCollectionResult 信封（task_type/import_recommendation/
    source_summary/agent_notes/university_id 父院校语义），供留学指南针入库管线。

两 profile 共享的归一逻辑：
  - 严格白名单**正过滤**（白名单外一律丢弃，尤其 status/review_status/is_simulated/submitted_by/
    is_platform_verified 等控制/运营字段，核心绝不产出）。
  - 别名映射（LLM 键名自由 → 白名单键名）；direction 枚举校验（不在枚举 → other + warning）。
  - CJK 分流 name/name_cn；toNum（雅思/学费等）；official_url/official_website 从落地 URL 回填。
  - data_confidence 硬枚举 high|medium|low，由**源类型**定，落盘前硬校验。
  - contains_privacy 恒 false（仅公开事实）。

抽不到的字段进 missing_fields，绝不编造。
"""
import hashlib
import json
import os
import re
import time

CONF_ENUM = ("high", "medium", "low")
DIRECTION_ENUM = [
    "business", "finance", "cs", "media", "law",
    "engineering", "science", "social_science", "art", "education", "other",
]
_CJK = re.compile(r"[一-鿿]")

# 源类型 → data_confidence 硬映射（官网=high、第三方结构源/百科=medium）。未知源默认 medium。
_SOURCE_CONF = {
    "official": "high",
    "official_website": "high",
    "third_party_education_site": "medium",
    "wikidata": "medium",
    "wikipedia": "medium",
}

# 国家关键词（移植 country.js）。
_COUNTRY_KW = [
    (re.compile(r"\b(united kingdom|u\.?k\.?|england|scotland|wales|northern ireland|britain|british)\b", re.I), "uk"),
    (re.compile(r"\bhong kong\b", re.I), "hk"),
    (re.compile(r"\bsingapore\b", re.I), "sg"),
    (re.compile(r"\b(united states|u\.?s\.?a?\.?|america)\b", re.I), "us"),
    (re.compile(r"\baustralia\b", re.I), "au"),
    (re.compile(r"\bcanada\b", re.I), "ca"),
    (re.compile(r"\b(japan|south korea|korea)\b", re.I), "jpkr"),
    (re.compile(r"\b(china|chinese)\b", re.I), "cn"),
]


def _country_from_text(text):
    s = str(text or "")
    for rx, code in _COUNTRY_KW:
        if rx.search(s):
            return code
    return ""


def _to_num(v):
    if v is None or v == "":
        return None
    # 取第一个数字片段（含千分位逗号 + 可选小数）再解析：避免区间 "12,500-15,000" 被剥成
    # "1250015000"（灾难性错误大数）、"7.5 (6.0)" 多点解析失败（§QC-F2/F3）。取区间下界，宁少勿错。
    mm = re.search(r"\d[\d,]*(?:\.\d+)?", str(v))
    if not mm:
        return None
    try:
        n = float(mm.group(0).replace(",", ""))
        return n if n == n and n not in (float("inf"), float("-inf")) else None
    except ValueError:
        return None


_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-z]+|(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])")


def _key_tokens(key):
    """键名 → token 集：按 `_`/非字母数字 与 驼峰边界切分（ethnicity 不含 city token）。"""
    # 先在驼峰边界插分隔（HelloWorld → Hello World），再按非字母数字/数字边界切，最后小写。
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(key))
    parts = _TOKEN_SPLIT_RE.split(spaced.lower())
    return {p for p in parts if p}


def _pick(obj, exact_keys, fuzzy_parts=None):
    """按候选键名（精确）+ 关键词（模糊）取值（移植 programme.js pick）。

    fuzzy 用 token 边界匹配（§11 修 🟡6）：要求每个 fuzzy_part 命中键名的**完整 token**，
    而非子串——避免 'city' 误伤 'ethnicity'、'fee' 误伤 'coffee' 等。
    """
    for k in exact_keys:
        if obj.get(k) not in (None, ""):
            return obj[k]
    if fuzzy_parts:
        for k in obj.keys():
            toks = _key_tokens(k)
            if all(p in toks for p in fuzzy_parts) and obj.get(k) not in (None, ""):
                return obj[k]
    return None


def _conf_from_source(source_type):
    """源类型 → 硬枚举 confidence（§11-C，丢弃 LLM 自报置信度）。"""
    return _SOURCE_CONF.get(str(source_type or "").lower(), "medium")


def _clean_item(item):
    """剔空 + 白名单值层标量化（§11-B / §QC-F1）：值只保留标量或标量列表；dict 或含非标量的
    复杂值一律丢弃——杜绝 LLM 让白名单字段藏嵌套 dict（其内部 is_simulated/status 等控制字段
    会随 json.dump 落盘，绕过只查顶层键的断言，文本性突破"绝不产控制字段"红线）。"""
    out = {}
    for k, v in item.items():
        if v in ("", None):
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif isinstance(v, (list, tuple)):
            elems = [e for e in v if isinstance(e, (str, int, float, bool)) and e not in ("", None)]
            if elems:
                out[k] = elems
        # dict / 其它复杂类型：丢弃，不落盘
    return out


# —— 院校信封（移植 university.js） —— #

def _build_university(raw, target, source_type, final_url):
    missing = []
    warnings = []

    # name_cn（必填）：优先简体；无中文→缺（不拿英文冒充）。
    name_cn = ""
    cand_zh = _pick(raw, ["name_zh_hans", "name_cn", "name_zh"], ["name", "cn"]) or ""
    if cand_zh and _CJK.search(str(cand_zh)):
        name_cn = str(cand_zh).strip()
    if not name_cn:
        # name_cn 是否「必须、需人工补」由 profile 决定：studycompass 要求中文名（在信封层加 warning），
        # generic 视 name_cn 为可选，缺失仅进 missing_fields、不加 warning（免每所英文院校都报中文名缺失）。
        missing.append("name_cn")

    name_en = _pick(raw, ["name_en", "name", "official_name"], ["name", "en"]) or ""

    # country（必填）：从描述/英文名解析；hint 兜底且留痕。
    country = _country_from_text(_pick(raw, ["country", "description", "intro", "introduction"], ["country"])) \
        or _country_from_text(name_en)
    if not country:
        hint = target.get("hint")
        if hint:
            hc = _country_from_text(hint) or (str(hint).lower() if str(hint).lower() in
                                              ("uk", "hk", "sg", "us", "au", "ca", "cn", "jpkr") else "")
            if hc:
                country = hc
                warnings.append({"warning_type": "country_from_hint",
                                 "message": "country 取自 hint（人工补，非采集），需人工核"})
    if not country:
        # 泛化：内置国家码（uk/hk/sg/us/au/ca/cn/jpkr）未命中时，保留 LLM 抽取的原始国家串，
        # 避免非英美院校（如 germany/france）整个 country 丢失（宁保原文不丢弃）。
        raw_country = _pick(raw, ["country"], ["country"])
        if raw_country and str(raw_country).strip():
            country = str(raw_country).strip().lower()
    if not country:
        missing.append("country")

    city = _pick(raw, ["city"], ["city"]) or ""
    if not city:
        missing.append("city")

    item = {
        "name_cn": name_cn,
        "name_en": name_en,
        "country": country,
        "city": (str(city).strip() if city else ""),
        "introduction": (str(_pick(raw, ["introduction", "intro", "summary"], ["intro"]) or "")[:500]),
        "ranking_band": _pick(raw, ["ranking_band"], ["ranking", "band"]) or "",
        "qs_rank_label": _pick(raw, ["qs_rank_label", "qs_rank"], ["qs"]) or "",
        "strength_subjects": _norm_subjects(_pick(raw, ["strength_subjects", "subjects"], ["strength", "subject"])),
        # §11-F：官网抓取时落地 URL 本身即官网页，从 URL 直接回填。
        "official_website": _pick(raw, ["official_website", "official_url"], ["official", "website"])  # §QC-F4 去宽泛键 website
        or final_url or "",
        "data_confidence": _conf_from_source(source_type),
        "data_source_url": final_url or _pick(raw, ["data_source_url"], []) or "",
    }
    item = _clean_item(item)

    # blocked（必填/阻塞判定）交由 build_envelope 按 profile 决定：studycompass 要求 name_cn，
    # generic 只要有任一名称即可识别实体。此处仅返回事实性 missing 与候选名。
    return item, warnings, missing, (name_en or target.get("target_name") or name_cn)


def _norm_subjects(v):
    if isinstance(v, (list, tuple)) and len(v):
        return [str(x).strip() for x in v if str(x).strip()][:6]
    return None


# —— 项目信封（移植 programme.js） —— #

def _build_programme(raw, target, source_type, final_url, lang="en"):
    missing = []
    warnings = []

    # name/name_cn：收集所有"项目名"候选（排除院校名/简介/要求），按是否含中文分流。
    name = None
    name_cn = None
    for k, v in raw.items():
        if not re.search(r"name|title|program|programme", k, re.I):
            continue
        if re.search(r"school|univ|college|institution|degree", k, re.I):  # §QC-F6 排除 degree_name 误当项目名
            continue
        if re.search(r"intro|summary|category|description|note|requirement|faculty", k, re.I):
            continue
        if not isinstance(v, str) or not v.strip():
            continue
        if _CJK.search(v):
            if not name_cn:
                name_cn = v.strip()
        else:
            if not name:
                name = v.strip()

    direction = _pick(raw, ["direction", "major_direction", "field"], ["direction"])
    if direction and str(direction).lower() not in DIRECTION_ENUM:
        warnings.append({"warning_type": "direction_not_enum",
                         "message": 'direction "%s" 不在枚举内，落为 other，需人工核' % direction})
        direction = "other"
    elif direction:
        direction = str(direction).lower()

    # university_id：可选透传（有则带上）。缺失是否算问题由 build_envelope 的 profile 决定——
    # studycompass 入库需父院校外键，generic 不关心。此处不硬报缺、不加 no_parent warning。
    university_id = target.get("university_id")
    if not name:
        missing.append("name")
    if not direction:
        missing.append("direction")

    item = {
        "university_id": university_id,
        "name": name,
        "name_cn": name_cn,
        "direction": direction,
        "degree": _pick(raw, ["degree", "degree_type"], None),  # §QC-F6 去 fuzzy，防误取 degree_requirement
        "programme_category": _pick(raw, ["programme_category", "project_category", "category"], ["category"]),
        "faculty": _pick(raw, ["faculty", "school", "department"], ["faculty"]),
        "duration": _normalize_duration(_pick(raw, ["duration", "length"], ["duration"]), lang),
        "study_mode": _pick(raw, ["study_mode"], ["study", "mode"]),
        "programme_intro": _pick(raw, ["programme_intro", "intro", "summary", "description"], ["intro"]),
        "academic_requirement": _pick(
            raw, ["academic_requirement", "application_requirements", "entry_requirements", "requirements"],
            ["requirement"]),
        "min_grade_band": _pick(raw, ["min_grade_band", "gpa_requirement", "grade"], ["grade"]),
        "language_note": _pick(raw, ["language_note", "language_requirement"], ["language", "note"]),
        "ielts_total": _to_num(_pick(raw, ["ielts_total", "ielts_overall", "ielts"], ["ielts", "total"])),
        "ielts_sub_min": _to_num(_pick(raw, ["ielts_sub_min", "ielts_subscore", "ielts_sub"], ["ielts", "sub"])),
        "tuition_fee": _to_num(_pick(raw, ["tuition_fee"], ["tuition", "fee", "amount"])),
        "tuition_label": _pick(raw, ["tuition_label", "tuition", "tuition_fee_label", "fee"], ["tuition"]),
        "deadline_label": _pick(raw, ["deadline_label", "application_deadline", "deadline"], ["deadline"]),
        # 'gre' 不用 fuzzy（会误匹配 deGREe）
        "gre_gmat_requirement": _pick(raw, ["gre_gmat_requirement", "gre_gmat", "gre", "gmat"], None),
        # §11-F：官网采集时页面本身即官网页。
        "official_url": _pick(raw, ["official_url", "programme_url"], ["official", "url"]) or final_url or "",  # §QC-F4 去宽泛键 url
        "data_confidence": _conf_from_source(source_type),
        "data_source_url": final_url or _pick(raw, ["data_source_url"], []) or "",
    }
    item = _clean_item(item)

    # 补记建议字段缺失（告知采集未拿到）
    for f in ("official_url", "ielts_sub_min", "min_grade_band", "deadline_label"):
        if item.get(f) is None and f not in missing:
            missing.append(f)
    # 合并 LLM 自报 missing
    raw_missing = raw.get("missing_fields")
    if isinstance(raw_missing, list):
        for f in raw_missing:
            if isinstance(f, str) and f and f not in missing:  # §QC-F7 只并入字符串，杜绝控制字段字样/垃圾串入
                missing.append(f)

    # blocked 交由 build_envelope 按 profile 决定（generic 只要有 name 即可识别）。
    return item, warnings, missing, (name or target.get("target_name") or name_cn or "")


def _normalize_duration(raw, lang="en"):
    """学制规范化。lang=zh 时 "12 Months (Full time)" → "1 年（全日制）"；lang=en（及其它）保留原文。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    if lang != "zh":
        return s  # 保留源语言原文（如 "12 Months (Full time)"），不强转中文
    if _CJK.search(s):
        return s
    ft = "（全日制）" if re.search(r"full[\s-]*time", s, re.I) else (
        "（非全日制）" if re.search(r"part[\s-]*time", s, re.I) else "")
    mm = re.search(r"(\d+(?:\.\d+)?)\s*months?", s, re.I)  # §QC-F8 支持小数，防 "1.5 months" 回溯抓 "5 months"
    if mm:
        fnum = float(mm.group(1))
        if fnum > 0 and fnum == int(fnum) and int(fnum) % 12 == 0:
            base = "%d 年" % (int(fnum) // 12)
        else:
            base = "%g 个月" % fnum
        return base + ft
    my = re.search(r"(\d+(?:\.\d+)?)\s*years?", s, re.I)
    if my:
        return ("%s 年" % my.group(1)) + ft
    return s


# —— 顶层组装 —— #

def build_envelope(extraction, target, source_type, final_url, fetch_meta=None,
                   profile="generic", lang="en"):
    """把抽取结果 + 源信息组装为结构化信封。

    extraction：extract() 返回的原始字段对象（可能含 __extract_error__）。
    target：{url,type,hint?,university_id?,target_name?}
    source_type：源类型（决定 data_confidence 硬枚举）。
    final_url：落地 URL（官网字段/来源回填）。
    fetch_meta：{tier_used, attempts, ...}（进 notes/evidence）。
    profile：generic（默认，通用信封）| studycompass（submitCollectionResult 契约信封）。
    lang：en（默认，自由文本保原样）| zh（自由文本已由 LLM 转中文；本层仅影响学制格式）。
    """
    fetch_meta = fetch_meta or {}
    ttype = target.get("type", "programme")
    extract_error = extraction.get("__extract_error__") if isinstance(extraction, dict) else None
    extraction_invalid = not isinstance(extraction, dict)  # §QC-F10：非 dict（None/list/str）也是降级
    # 抽取失败/非结构化 → 降级：raw 视为空，全字段进 missing（绝不编造 §11-H）
    raw = {} if (extract_error or extraction_invalid) else extraction

    src_lang = "en"  # targets 主为英文官网页
    if ttype == "university":
        item, warnings, missing, target_name = _build_university(raw, target, source_type, final_url)
        task_type = "collect_universities"
        if profile == "studycompass":
            blocked = ("name_cn" in missing) or ("country" in missing)
        else:
            # generic：只要有任一名称即可识别实体；country 缺失仅进 missing，不阻塞。
            blocked = (not item.get("name_en")) and (not item.get("name_cn"))
    else:
        item, warnings, missing, target_name = _build_programme(raw, target, source_type, final_url, lang)
        task_type = "collect_programmes"
        if profile == "studycompass":
            blocked = (not item.get("name")) or (not item.get("direction"))
        else:
            blocked = not item.get("name")  # generic 只在完全无名（无法识别）时判需人工

    if extract_error:
        warnings.append({"warning_type": "extract_degraded",
                         "message": "抽取降级（%s）：未抽到结构化字段，全部计入 missing_fields，未编造" % extract_error})
    elif extraction_invalid:
        warnings.append({"warning_type": "extract_degraded",
                         "message": "抽取返回非结构化结果（%s），已降级：全部计入 missing_fields，未编造" % type(extraction).__name__})

    # data_confidence 硬枚举，落盘前硬校验。§QC-F5：用 raise 非 assert（防 python -O 剥离掉硬保证）。
    conf = _conf_from_source(source_type)
    if conf not in CONF_ENUM:
        raise ValueError("data_confidence 非法：%r" % conf)

    if profile == "studycompass":
        envelope = _envelope_studycompass(
            task_type, target_name, final_url, src_lang, source_type, conf,
            item, warnings, missing, fetch_meta, extract_error, blocked)
    else:
        envelope = _envelope_generic(
            ttype, target_name, final_url, src_lang, source_type, conf,
            item, warnings, missing, fetch_meta, extract_error, blocked)
    _assert_no_control_fields(envelope)
    return envelope


def _envelope_generic(ttype, target_name, final_url, src_lang, source_type, conf,
                      item, warnings, missing, fetch_meta, extract_error, blocked):
    """通用信封（面向公开）：无任何下游专属键。needs_manual 表「关键信息没拿到，需人工」。"""
    notes = ("smart-crawler: tier=%s, attempts=%s, source_type=%s. "
             "Fields are whitelisted and alias-normalized; unextracted fields go to "
             "missing_fields and are never fabricated."
             % (fetch_meta.get("tier_used"), fetch_meta.get("attempts"), source_type))
    if extract_error:
        notes += " (extraction degraded: %s)" % extract_error
    return {
        "target_name": target_name or "",
        "type": ttype,
        "source": {
            "url": final_url or "",
            "source_type": source_type,
            "language": src_lang,
            "accessible": bool(final_url),
        },
        "data_confidence": conf,
        "items": [item],
        "warnings": warnings,
        "conflicts": [],
        "missing_fields": missing,
        "evidence": _build_evidence(final_url, fetch_meta),
        "contains_privacy": False,  # 仅公开事实，恒 false
        "needs_manual": bool(blocked),
        "notes": notes,
    }


def _envelope_studycompass(task_type, target_name, final_url, src_lang, source_type, conf,
                           item, warnings, missing, fetch_meta, extract_error, blocked):
    """submitCollectionResult 契约信封（留学指南针内部入库管线，行为与去品牌前一致）。"""
    # university_id 缺失（仅项目）：入库需 published 父院校外键回填。不 block，仅点明（§QC-F9）。
    if task_type == "collect_programmes" and not item.get("university_id"):
        if "university_id" not in missing:
            missing.append("university_id")
        warnings.append({"warning_type": "no_parent",
                         "message": "缺 university_id，物化前须先有 published 父院校并回填"})
    # 大学缺中文校名：studycompass 视为必填缺失（generic 不加此 warning，见 _build_university）。
    if task_type == "collect_universities" and "name_cn" in missing:
        warnings.append({"warning_type": "missing_required",
                         "message": "未抽到中文校名，name_cn 缺，需人工补"})
    agent_notes = (
        "smart-crawler 采集：tier=%s，尝试 %s 次，源类型=%s。"
        % (fetch_meta.get("tier_used"), fetch_meta.get("attempts"), source_type)
    )
    if extract_error:
        agent_notes += "（抽取降级：%s）" % extract_error
    agent_notes += " 字段经严格白名单正过滤 + 别名映射归一；抽不到进 missing_fields，未编造；以官网为准。"
    if "university_id" in missing:  # §QC-F9：缺父院校关联时点明，便于监督 agent 一眼留意（不强行 block）
        agent_notes += " ⚠️ 项目缺父院校关联 university_id，入库前须补。"
    return {
        "task_type": task_type,
        "target_name": target_name or "",
        "source_summary": {
            "primary_source_url": final_url or "",
            "source_type": source_type,
            "source_language": src_lang,
            "source_accessible": bool(final_url),
        },
        "data_confidence": conf,
        "items": [item],
        "warnings": warnings,
        "conflicts": [],
        "missing_fields": missing,
        "raw_evidence": _build_evidence(final_url, fetch_meta),
        "contains_privacy": False,  # 产物恒 false（仅公开事实）
        # 必填齐→交审核员；必填缺→manual_completion_required（决策权仍在审核员，rule14）
        "import_recommendation": "manual_completion_required" if blocked else "recommend_manual_review",
        "agent_notes": agent_notes,
    }


def _build_evidence(final_url, fetch_meta):
    ev = []
    if final_url:
        ev.append({"source_url": final_url, "fetched_via": fetch_meta.get("tier_used"),
                   "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return ev


# 控制/运营字段黑名单——核心绝不产出（§11-B）。白名单正过滤已天然焊死，这里是落盘前双保险断言。
_FORBIDDEN_ITEM_FIELDS = (
    "status", "review_status", "is_simulated", "submitted_by",
    "is_platform_verified", "_id", "moderation_status", "verification_status",
)


def _assert_no_control_fields(envelope):
    """落盘前硬校验 item（含嵌套 dict/list）不含控制/运营字段（§11-B）。
    §QC-F5：用 raise 非 assert（防 -O 剥离）；§QC-F1：递归扫嵌套键（与 _clean_item 标量化双保险）。"""
    def _scan(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in _FORBIDDEN_ITEM_FIELDS:
                    raise ValueError("产物含控制/运营字段 '%s'（核心绝不产出，§11-B）" % k)
                _scan(v)
        elif isinstance(obj, (list, tuple)):
            for e in obj:
                _scan(e)
    for it in envelope.get("items", []):
        _scan(it)


def write_payload(envelope, out_dir, task_key):
    """落盘 out/payload-*.json（原子写）。返回文件路径。"""
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", task_key)[:80]
    # §QC-F18：文件名并入 task_key 短哈希，防 safe[:80] 截断碰撞 / 秒级时间戳同秒覆盖丢产物。
    h = hashlib.sha1(task_key.encode("utf-8")).hexdigest()[:8]
    fname = "payload-%s-%s-%s.json" % (safe, time.strftime("%Y%m%d-%H%M%S"), h)
    path = os.path.join(out_dir, fname)
    # §QC-F18：tmp + os.replace 原子落盘（对齐 scheduler._atomic_write_json，防半截/并发同名共享冲突）。
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(envelope, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path
