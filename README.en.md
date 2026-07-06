# smart-crawler

> [中文](README.md) · English

**A crawler for public university / master's-programme web pages**: adaptive multi-tier fetching + LLM structured extraction + strict-whitelist output. Feed it a list of URLs, get structured JSON back (university intro, programme requirements, tuition, deadlines, language scores, …). **If it can't get a field, it says so via `needs_manual` — it never fabricates.**

~1200 lines of core Python; the only third-party dependencies are `scrapling` (which bundles curl_cffi + Playwright + patchright) and `httpx`. It does not vendor or clone any other crawler project.

## What it is

An escalation-tier crawler — **TLS-impersonating HTTP → real browser → paid Firecrawl fallback** — that starts on the cheapest tier and only escalates on failure. It has per-domain rate limiting, resumable runs, and per-domain memory. Once it has the page text, an LLM (`codex` or any OpenAI-compatible endpoint) extracts fields, which are then run through a strict whitelist positive-filter + alias normalization into a structured envelope.

- **Input**: a typed list of URLs — `[{url, type:"university"|"programme", ...}]`
- **Output**: one JSON envelope per target (`out/payload-*.json`) plus a `run-report.md`

## Fetch strategy (~8/9 real-world coverage, from 1 s/page)

| Tier | Engine | When |
|---|---|---|
| ① http-tls | curl_cffi TLS/JA3 impersonation | Default; the vast majority of public pages |
| ② browser | Playwright real browser | Client-rendered SPAs |
| ③ firecrawl | Firecrawl `proxy:stealth` (paid, optional) | Datacenter-IP-blocking hard sites; without this tier such sites are marked `needs_manual` |

The system **automatically** starts on ① and only escalates — don't force browser/Firecrawl for everything. Second hit on the same domain goes straight to the last successful tier.

## Install

Requires Python **3.10+** (a hard requirement of scrapling).

```bash
git clone https://github.com/CercaTrovato/smart-crawler.git && cd smart-crawler

# One-shot: create .venv + install deps + download browser + generate crawler.config.json from the example
powershell -ExecutionPolicy Bypass -File setup.ps1   # Windows (conda-only machines: add -Python <that env's python.exe>)
bash setup.sh                                        # Linux / macOS
```

This installs `scrapling[fetchers]` + `httpx` (the only two third-party packages) plus the Chromium / patchright browser (~150 MB).

## Extraction backend (pick one, at least one is required)

| Profile | Extraction backend | Hard-site fallback |
|---|---|---|
| **A** | `codex` CLI on PATH (`codex --version` works) | `FIRECRAWL_API_KEY` (paid, optional) |
| **B** | Any **OpenAI-compatible** endpoint (local Ollama / vLLM / hosted): set config `llm` to `openai-compat`, and env `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | No Firecrawl → remove `"firecrawl"` from config `fetch.tiers` |

> **Secrets live in environment variables only — never written to any file or committed.**

## Quick start

```bash
# targets.example.json (a typed array):
# [{"url":"https://courses.leeds.ac.uk/.../data-science-msc","type":"programme"},
#  {"url":"https://en.wikipedia.org/wiki/University_of_Leeds","type":"university"}]

.venv\Scripts\python.exe run.py --targets targets.example.json --concurrency 8   # Windows
.venv/bin/python run.py --targets targets.example.json --concurrency 8           # Linux / macOS
```

Outputs: `out/payload-*.json` (one envelope per target) + `run-report.md` (which tier was used per target / `usable` / `needs_manual` reason).

## CLI options

| Flag | Meaning |
|---|---|
| `--targets <file>` | **Required.** Typed JSON array, see "Input format" |
| `--concurrency <n>` | Global concurrency, default 8, clamped to [1,32] |
| `--resume` | Resume: retry previously failed targets; a second failure is recorded in the envelope warnings as "failed N times" |
| `--profile <generic\|studycompass>` | Output envelope shape, default `generic`, see "Output format" |
| `--lang <en\|zh>` | Free-text language: `en` (default, keep the source language verbatim) / `zh` (translate to Simplified Chinese) |
| `--config <file>` | Config file path, default `crawler.config.json` |

`--profile` / `--lang` can also live in the `output` section of `crawler.config.json` (`{"output":{"profile":"generic","lang":"en"}}`). **CLI overrides config, config overrides the built-in default.**

## Input format

`targets.json` is an array; each target object:

```jsonc
{
  "url": "https://courses.leeds.ac.uk/i071/data-science-msc",
  "type": "programme",          // required: university | programme
  "hint": "data science",       // optional: hint words for scoring / country inference
  "source_type": "official",    // optional: explicit source type (official|wikipedia|third_party_education_site…, sets data_confidence)
  "university_id": "LEEDS"       // optional: a programme's parent-university id, passed through (optional in generic; a DB foreign key in studycompass)
}
```

A missing scheme is auto-prefixed with `https://`; an invalid `type` aborts with an error.

## Output format

Each target produces one JSON envelope. Two profiles:

### `generic` (default, public-facing)

A clean, generic envelope with no downstream-specific keys:

```jsonc
{
  "target_name": "University of Leeds",
  "type": "university",
  "source": { "url": "...", "source_type": "wikipedia", "language": "en", "accessible": true },
  "data_confidence": "medium",           // high|medium|low, decided by source type (not self-reported by the LLM)
  "items": [ { /* whitelisted fields, see OUTPUT-SCHEMA.md */ } ],
  "warnings": [],
  "missing_fields": ["name_cn"],         // fields that couldn't be extracted; never fabricated
  "evidence": [ { "source_url": "...", "fetched_via": "http-tls", "fetched_at": "..." } ],
  "contains_privacy": false,             // always false (public facts only)
  "needs_manual": false,                 // true = key info missing (fetch failed / entity unidentifiable), human needed
  "notes": "smart-crawler: tier=http-tls, attempts=1, ..."
}
```

### `studycompass` (internal contract, `--profile studycompass`)

The `submitCollectionResult` envelope for the StudyCompass ingestion pipeline (`task_type` / `import_recommendation` / `source_summary` / `agent_notes` / `university_id` parent semantics). Behavior under this profile is **exactly as before** the public generalization.

**Full field reference for both profiles: [`docs/OUTPUT-SCHEMA.md`](docs/OUTPUT-SCHEMA.md).**

## Use as a Claude Code skill

Drop the whole directory into `~/.claude/skills/smart-crawler/` (global) or a project's `.claude/skills/smart-crawler/` and it is auto-discovered. See [`SKILL.md`](SKILL.md).

## Red lines (never crossed)

- Public page facts only; **no login / no signature reverse-engineering / no PII**; `contains_privacy` is always `false`.
- `data_confidence` is only `high|medium|low`; the core **never emits** control/ops fields like `status` / `is_simulated`.
- Polite rate limiting; on a captcha / challenge it stops and marks `needs_manual` — **it does not solve them**.
- Collected data is for reference; **review it before importing / publishing**, and respect each site's robots.txt and terms of service.

## Testing

```bash
.venv\Scripts\python.exe _test_public.py            # dual profile / --lang / passthrough / fallback / de-branding
.venv\Scripts\python.exe _test_qc_regression.py     # QA regression (52 checks)
.venv\Scripts\python.exe _test_worker_isolation.py  # per-target failure isolation
```

## Design & quality

Architecture, concurrency correctness, the fetch escalation chain, and the adversarial three-gate self-review record are in [`DESIGN.md`](DESIGN.md); the fetch-stack research is in [`docs/report-crawler-research.md`](docs/report-crawler-research.md).

## License

[MIT](LICENSE)
