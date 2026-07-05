# -*- coding: utf-8 -*-
"""内容可见度打分（移植自 tools/crawler-lab/lib/util.js 的 htmlToText / looksBlocked / scoreCell）。

契约 §4 / §6：HTTP 200 不等于成功。要 正文 > 阈值 && 命中该类型预期关键词 && 未被封 才算 usable。
本模块纯函数、无副作用，供 escalate 判断是否升级、是否记忆成功档。
"""
import math
import re

# HTML → 纯正文（与 util.js htmlToText 逐条对齐，保证与试验区口径一致）。
_RE_SCRIPT = re.compile(r"<script[\s\S]*?</script>", re.I)
_RE_STYLE = re.compile(r"<style[\s\S]*?</style>", re.I)
_RE_NOSCRIPT = re.compile(r"<noscript[\s\S]*?</noscript>", re.I)
_RE_COMMENT = re.compile(r"<!--[\s\S]*?-->")
_RE_BLOCKTAG = re.compile(r"<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", re.I)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_NUMENT = re.compile(r"&#(\d+);")
_RE_WS = re.compile(r"[ \t]+")
_RE_NL_TRIM = re.compile(r" *\n *")
_RE_NL_COLLAPSE = re.compile(r"\n{3,}")


def html_to_text(html):
    if not html:
        return ""
    s = str(html)
    s = _RE_SCRIPT.sub(" ", s)
    s = _RE_STYLE.sub(" ", s)
    s = _RE_NOSCRIPT.sub(" ", s)
    s = _RE_COMMENT.sub(" ", s)
    s = _RE_BLOCKTAG.sub("\n", s)
    s = _RE_TAG.sub(" ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = _RE_NUMENT.sub(lambda m: chr(int(m.group(1))), s)
    s = _RE_WS.sub(" ", s)
    s = _RE_NL_TRIM.sub("\n", s)
    s = _RE_NL_COLLAPSE.sub("\n\n", s)
    return s.strip()


# 反爬拦截启发式：状态码 或 正文头部出现验证/盾页特征词（对齐 util.js BLOCK_RE）。
# 命中验证码/challenge 页 → 天然 blocked → not usable → 升级链走完标 needs_manual（红线「不破解」满足）。
_BLOCK_RE = re.compile(
    r"captcha|unusual traffic|are you a human|verify you are|access denied|"
    r"attention required|checking your browser|cloudflare|请完成验证|人机验证|"
    r"滑块|访问验证|拒绝访问",
    re.I,
)


def looks_blocked(status, text):
    if int(status or 0) in (401, 403, 429, 503):
        return True
    return bool(_BLOCK_RE.search((text or "")[:3000]))


def score(expect_keywords, ok, status, html):
    """综合打分。expect_keywords=该目标类型的预期关键词列表。

    返回 dict：{ok,status,text,text_len,hits,kw_total,blocked,usable}。
    usable = 可达 && 未被封 && 正文 > 500 && 命中 >= 下限。
    """
    text = html_to_text(html)
    low = text.lower()
    kw = [str(k) for k in (expect_keywords or [])]
    hits = [k for k in kw if k.lower() in low]
    kw_total = len(kw)
    blocked = looks_blocked(status, text)
    # 关键词表混合中英文，单语言页只能命中一半 → 用绝对下限 2（命中 2+ 即认页面相关），
    # 不用 40%×总数（会因跨语言稀释把真内容误判 not-usable，实测 bug）。
    need = min(2, kw_total) if kw_total else 1
    usable = bool(ok) and (not blocked) and len(text) > 500 and len(hits) >= need
    return {
        "ok": bool(ok),
        "status": int(status or 0),
        "text": text,
        "text_len": len(text),
        "hits": len(hits),
        "kw_total": kw_total,
        "blocked": blocked,
        "usable": usable,
    }


# 各目标类型的默认预期关键词（用于打分；hint 里可覆盖/追加）。
# 契约未强制关键词表——这是"命中该类型预期关键词"的合理默认，通用词以提高召回。
DEFAULT_EXPECT = {
    "university": ["university", "研究", "students", "campus", "faculty", "大学", "学院", "招生"],
    "programme": [
        "entry requirements", "english", "fees", "tuition", "modules",
        "申请", "要求", "学费", "课程", "雅思", "录取",
    ],
}


def expect_for(target_type, hint=None):
    """给定目标类型 + 可选 hint（str 或 list），产出预期关键词列表。

    命中判据是绝对下限（见 score()，命中≥2 即认相关）；hint 仅在缺省表基础上补充，避免稀释召回。
    """
    base = list(DEFAULT_EXPECT.get(target_type, DEFAULT_EXPECT["programme"]))
    if hint:
        extra = hint if isinstance(hint, (list, tuple)) else re.split(r"[,\s]+", str(hint))
        for e in extra:
            e = str(e).strip()
            if e and e not in base:
                base.append(e)
    return base
