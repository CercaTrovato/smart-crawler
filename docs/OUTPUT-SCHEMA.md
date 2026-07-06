# Input / Output Schema · 输入 / 输出规范

Field reference for `smart-crawler` inputs (`targets.json`) and outputs (`out/payload-*.json`).
`smart-crawler` 的输入（`targets.json`）与输出（`out/payload-*.json`）字段规范。

Descriptions are bilingual: **English. 中文。**

---

## 1. Input target · 输入目标

`targets.json` is a non-empty JSON array; each element: · `targets.json` 是非空 JSON 数组，每个元素：

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | ✅ | Target page URL. Missing scheme is auto-prefixed with `https://`. 目标页 URL；缺 scheme 自动补 `https://`。 |
| `type` | string | ✅ | `university` or `programme`. Invalid value aborts the run. 只能 `university` / `programme`，非法即报错退出。 |
| `hint` | string \| string[] | — | Hint words for content scoring and country inference. 供内容打分与国家推断的提示词。 |
| `source_type` | string | — | Explicit source type, overrides host-based inference; sets `data_confidence`. See §5. 显式源类型，覆盖按域推断，决定 `data_confidence`，见 §5。 |
| `university_id` | string | — | A programme's parent-university id. Passed through to the item. Optional in `generic`; used as a DB foreign key in `studycompass`. 项目的父院校 ID，透传进 item；`generic` 可选，`studycompass` 用作入库外键。 |

```json
[
  { "url": "https://courses.leeds.ac.uk/i071/data-science-msc", "type": "programme", "university_id": "LEEDS" },
  { "url": "https://en.wikipedia.org/wiki/University_of_Leeds", "type": "university", "source_type": "wikipedia", "hint": "uk" }
]
```

---

## 2. Envelope — `generic` profile (default) · 通用信封（默认）

Top-level keys of each `out/payload-*.json`. · 每份产物的顶层键。

| Key | Type | Description |
|---|---|---|
| `target_name` | string | Best-effort display name of the target. 目标的展示名（尽力而为）。 |
| `type` | string | `university` \| `programme`. |
| `source` | object | `{ url, source_type, language, accessible }` — where the data came from. 数据来源。 |
| `data_confidence` | enum | `high` \| `medium` \| `low`, decided by source type, **not** self-reported by the LLM. 由源类型硬定，非 LLM 自报。 |
| `items` | array | Exactly one extracted record (see §4). 恰好一条抽取记录（见 §4）。 |
| `warnings` | array | `{ warning_type, message }[]` — degradations / anomalies worth a human's attention. 降级 / 异常提示。 |
| `missing_fields` | string[] | Fields that could not be extracted. **Never fabricated.** 抽不到的字段，绝不编造。 |
| `evidence` | array | `{ source_url, fetched_via, fetched_at }[]` — provenance. 溯源。 |
| `contains_privacy` | bool | Always `false` (public facts only). 恒 `false`（仅公开事实）。 |
| `needs_manual` | bool | `true` = key info missing (fetch failed / entity unidentifiable), needs a human. `true` = 关键信息没拿到，需人工。 |
| `notes` | string | Free-text run note (tier used, attempts, degradation). 运行摘要（用了哪档、尝试次数、是否降级）。 |

---

## 3. Envelope — `studycompass` profile · 内部契约信封

`--profile studycompass`. The `submitCollectionResult` envelope for the StudyCompass ingestion pipeline. Differences from `generic`: · 留学指南针入库管线专用信封，与 `generic` 的差异：

| Key | Type | Description |
|---|---|---|
| `task_type` | string | `collect_universities` \| `collect_programmes`. |
| `source_summary` | object | `{ primary_source_url, source_type, source_language, source_accessible }` (replaces `source`). 取代 `source`。 |
| `import_recommendation` | enum | `recommend_manual_review` (required fields present) \| `manual_completion_required` (required missing). 必填齐→交审核；必填缺→需补全。 |
| `agent_notes` | string | Chinese human-facing summary (replaces `notes`); flags missing `university_id`. 中文摘要（取代 `notes`），点明缺 `university_id`。 |
| `raw_evidence` | array | Same shape as `evidence`. 同 `evidence`。 |

Shared with `generic`: `target_name`, `data_confidence`, `items`, `warnings`, `conflicts`, `missing_fields`, `contains_privacy`.
`generic` 独有 `type` / `source` / `needs_manual` / `notes`；`studycompass` 独有上表各键。Item 字段两 profile 一致。

---

## 4. Item fields · Item 字段

Empty / null fields are dropped before output (whitelist positive-filter + scalarization). Values that are objects/nested dicts are discarded — the core never emits control fields.
空值 / null 落盘前剔除（白名单正过滤 + 标量化）；值为对象 / 嵌套 dict 的一律丢弃，核心绝不产控制字段。

### 4a. `university` item

| Field | Type | Description |
|---|---|---|
| `name_cn` | string | Simplified-Chinese name, if present in the source. Optional. 简体中文校名（源里有才产出）。 |
| `name_en` | string | English name. 英文校名。 |
| `country` | string | Built-in code (`uk`/`hk`/`sg`/`us`/`au`/`ca`/`cn`/`jpkr`) when matched, otherwise the raw extracted country string. 命中内置码则用码，否则回退原文国家串。 |
| `city` | string | City. In `--lang zh`, free-text; otherwise source language. 城市。 |
| `introduction` | string | ≤500 chars. Language follows `--lang`. 简介，≤500 字，语言随 `--lang`。 |
| `ranking_band` | string | Ranking band, if any. 排名区间。 |
| `qs_rank_label` | string | QS rank label, if any. QS 排名标签。 |
| `strength_subjects` | string[] | Up to 6. Language follows `--lang`. 优势学科，最多 6 个。 |
| `official_website` | string | Official site; backfilled from the landing URL when the page itself is the official site. 官网；官网页采集时从落地 URL 回填。 |
| `data_confidence` | enum | Mirror of envelope-level confidence. 同信封级置信度。 |
| `data_source_url` | string | Landing URL. 落地 URL。 |

### 4b. `programme` item

| Field | Type | Description |
|---|---|---|
| `university_id` | string | Parent-university id, passed through from the target if provided. 父院校 ID（target 提供才有）。 |
| `name` | string | Official English programme name. 项目英文名。 |
| `name_cn` | string | Chinese programme name, if present. 项目中文名（有才产）。 |
| `direction` | enum | One of the direction enum (§5); non-matching → `other` + warning. 方向枚举，不匹配落 `other` 并告警。 |
| `degree` | string | e.g. `MSc` / `MA` / `LLM`. 学位。 |
| `programme_category` | string | Category, if any. 项目类别。 |
| `faculty` | string | School / department. Language follows `--lang`. 所属学院 / 系。 |
| `duration` | string | e.g. `12 Months (Full time)`; in `--lang zh` normalized to `1 年（全日制）`. 学制；zh 档归一为中文。 |
| `study_mode` | string | full-time / part-time. 就读方式。 |
| `programme_intro` | string | Programme summary. Language follows `--lang`. 项目简介。 |
| `academic_requirement` | string | Entry / academic requirements. Language follows `--lang`. 学术 / 入学要求。 |
| `min_grade_band` | string | e.g. `2:1` / `WAM 65%`. Kept verbatim in both langs. 最低成绩要求，两档都保原码。 |
| `language_note` | string | Language requirement note. Language follows `--lang`. 语言要求说明。 |
| `ielts_total` | number \| null | IELTS overall; none → `null`. 雅思总分。 |
| `ielts_sub_min` | number \| null | IELTS sub-score minimum. 雅思小分。 |
| `tuition_fee` | number \| null | Numeric tuition (lower bound of a range). 学费数字（区间取下界）。 |
| `tuition_label` | string | Raw tuition string. Numbers/currency kept verbatim. 学费原文串，数字 / 货币保原样。 |
| `deadline_label` | string | Application deadline. Dates kept verbatim. 申请截止期，日期保原样。 |
| `gre_gmat_requirement` | string | `not_required` / `optional` / `required`. |
| `official_url` | string | Programme official URL; backfilled from landing URL. 项目官网 URL；从落地 URL 回填。 |
| `data_confidence` | enum | Mirror of envelope-level confidence. 同信封级置信度。 |
| `data_source_url` | string | Landing URL. 落地 URL。 |

> **`--lang` only switches free-text field *values*** (intro / requirement / note / faculty / city / subjects). Numbers, scores, dates, currency, proper names, enum codes and URLs are always kept verbatim. Envelope meta (`notes` / warnings) is not translated.
> **`--lang` 只切换自由文本字段的*值***；数字 / 分数 / 日期 / 货币 / 名称 / 枚举码 / URL 一律保原样；信封元信息（`notes` / warnings）不翻译。

---

## 5. Enums · 枚举

- **`direction`**: `business`, `finance`, `cs`, `media`, `law`, `engineering`, `science`, `social_science`, `art`, `education`, `other`.
- **`data_confidence`**: `high` (official site), `medium` (third-party / wiki), `low`. Decided by source type. 由源类型硬定。
- **`source_type`** → confidence: `official`/`official_website` → high; `wikipedia`/`wikidata`/`third_party_education_site` → medium; unknown → medium (conservative). Host `*.edu` / `*.ac.xx` auto-infers `official`; `wikipedia.org` → `wikipedia`; otherwise `third_party_education_site`. 未知域保守取 medium，不默认 official。

---

## 6. Guarantees · 保证

- **Never fabricates.** Anything not found goes to `missing_fields`. 抽不到进 `missing_fields`，绝不编造。
- **Whitelist positive-filter.** Only known fields are emitted; unknown keys and non-scalar values are dropped. Control/ops fields (`status`, `is_simulated`, `review_status`, …) are never produced. 白名单正过滤，控制 / 运营字段绝不产出。
- **`contains_privacy` is always `false`** — public facts only. 恒 false，仅公开事实。
- **`data_confidence` is a hard enum** decided by source type, asserted before write; the LLM's self-reported confidence is discarded. 硬枚举，落盘前校验，丢弃 LLM 自报置信度。
- On captcha / challenge the crawler stops and marks `needs_manual` — it does not solve them. 遇验证码停并标 `needs_manual`，不破解。
