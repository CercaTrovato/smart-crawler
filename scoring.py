# -*- coding: utf-8 -*-
"""内容可见度打分（移植自 tools/crawler-lab/lib/util.js 的 htmlToText / looksBlocked / scoreCell）。

契约 §4 / §6：HTTP 200 不等于成功。要 正文 > 阈值 && 命中该类型预期关键词 && 未被封 才算 usable。
本模块纯函数、无副作用，供 escalate 判断是否升级、是否记忆成功档。
"""
import html as _html_mod  # §QC-F11：用标准库 html.unescape 替代手写实体解码（含越界码点安全降级）
import math
import re

# HTML → 纯正文（与 util.js htmlToText 逐条对齐，保证与试验区口径一致）。
_RE_SCRIPT = re.compile(r"<script[\s\S]*?</script>", re.I)
_RE_STYLE = re.compile(r"<style[\s\S]*?</style>", re.I)
_RE_NOSCRIPT = re.compile(r"<noscript[\s\S]*?</noscript>", re.I)
_RE_COMMENT = re.compile(r"<!--[\s\S]*?-->")
_RE_BLOCKTAG = re.compile(r"<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", re.I)
# §QC-wbr（中文输出验证发现）：<wbr> 是零宽换行标记，应删除而非转空格——否则 wikipedia 用 <wbr> 分隔的
# URL（leeds<wbr>.ac<wbr>.uk）会被 _RE_TAG 拆成 "leeds .ac .uk"。unescape 后再清一遍同类零宽字符。
_RE_WBR = re.compile(r"</?wbr\s*/?>", re.I)  # <wbr> / <wbr/> / </wbr>（wikipedia 实际用成对 <wbr></wbr>，闭标签也要删）
_RE_ZEROWIDTH = re.compile(r"[​‌‍﻿­]")  # ZWSP/ZWNJ/ZWJ/BOM/soft-hyphen
_RE_TAG = re.compile(r"<[^>]{0,4096}+>")  # §QC-F14 possessive 限长：消除大量无闭合 < 串的 O(n²) 回溯（配合入口截断）
_RE_WS = re.compile(r"[^\S\n]+")  # §QC-F11修正：塌缩所有非换行空白（含 html.unescape 出的 &nbsp;→U+00A0），否则多词关键词子串匹配失配
_RE_NL_TRIM = re.compile(r" *\n *")
_RE_NL_COLLAPSE = re.compile(r"\n{3,}")


_MAX_HTML_LEN = 2_000_000  # §QC-F14：入口截断上限，防超长/无闭合标签致 _RE_TAG 回溯 O(n²) 卡死 worker


def html_to_text(html):
    if not html:
        return ""
    s = str(html)[:_MAX_HTML_LEN]  # §QC-F14 截断（正文关键字段通常在前部，对召回近乎无损）
    s = _RE_SCRIPT.sub(" ", s)
    s = _RE_STYLE.sub(" ", s)
    s = _RE_NOSCRIPT.sub(" ", s)
    s = _RE_COMMENT.sub(" ", s)
    s = _RE_BLOCKTAG.sub("\n", s)
    s = _RE_WBR.sub("", s)  # §QC-wbr：零宽换行标记删除（不转空格），保护 URL/长词不被拆开
    s = _RE_TAG.sub(" ", s)
    # §QC-F11：标准库 html.unescape 一次性解码命名/十进制/十六进制实体，越界码点安全降级 U+FFFD
    # （原手写 chr(int(&#N;)) 遇 N>0x10FFFF 抛 ValueError/OverflowError，冒泡出 score→escalate 无 try 包裹）。
    s = _html_mod.unescape(s)
    s = _RE_ZEROWIDTH.sub("", s)  # §QC-wbr：清理 unescape 出的零宽字符（ZWSP/BOM/soft-hyphen），同理不留痕
    s = _RE_WS.sub(" ", s)
    s = _RE_NL_TRIM.sub("\n", s)
    s = _RE_NL_COLLAPSE.sub("\n\n", s)
    return s.strip()


# 反爬拦截启发式：状态码 或 正文头部出现验证/盾页特征词（对齐 util.js BLOCK_RE）。
# 命中验证码/challenge 页 → 天然 blocked → not usable → 升级链走完标 needs_manual（红线「不破解」满足）。
_BLOCK_RE = re.compile(
    # §QC-F13：移除裸 "cloudflare"（全网 CDN 名，正常页脚/声明常见，误判率高）；保留更特异于
    # challenge 页的词，并在 looks_blocked 加"仅短正文生效"护栏，双保险防误判良性 200 页。
    r"captcha|unusual traffic|are you a human|verify you are|access denied|"
    r"attention required|checking your browser|请完成验证|人机验证|"
    r"滑块|访问验证|拒绝访问",
    re.I,
)


def looks_blocked(status, text):
    # 拦截型状态码：直接判封。
    if int(status or 0) in (401, 403, 429, 503):
        return True
    # §QC-F13：关键词仅在正文较短时生效——真封页/challenge 页正文都很短；长正文里出现
    # "access denied"/"checking your browser" 等多为正常页脚/声明，据此丢弃 200 好页属误判。
    t = text or ""
    if len(t) < 1500:
        return bool(_BLOCK_RE.search(t[:3000]))
    return False


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
