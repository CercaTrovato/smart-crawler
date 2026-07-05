# -*- coding: utf-8 -*-
"""抽取层（契约 §5 + §11-H）：extract(text, json_schema, instructions, cfg) -> dict。

后端：
  codex        → subprocess 调 codex exec --output-schema（移植 tools/collector/lib/codex-extract.js）。
  openai-compat→ httpx POST /chat/completions（含 timeoutMs / maxRetry / jsonMode）。

红线：抽不到的字段进 missing_fields，绝不编造（§11-H）。
降级（§11-H 水密）：抽取失败必降级——返回 {}（让 output 层记 missing_fields），不装成功、不编造。
返回的是 LLM 抽出的**原始字段名对象**（键名可能近义/自由），统一归一交给 output 层（移植 transform 的别名映射）。
"""
import json
import os
import shutil
import subprocess
import tempfile


# —— codex 后端 —— #

def _extract_codex(text, schema, instructions, cfg):
    """subprocess 调 codex，正文走 stdin，schema/结果走临时文件（移植 codex-extract.js）。"""
    llm = cfg.get("llm", {})
    # Windows 下 codex 是 .CMD 包装，shell=False 需完整路径解析（否则 WinError 2）。
    codex_exe = shutil.which("codex") or "codex"
    timeout_s = int(llm.get("timeoutMs", 180000)) / 1000.0
    tmpdir = tempfile.mkdtemp(prefix="codex-x-")
    schema_file = os.path.join(tmpdir, "schema.json")
    out_file = os.path.join(tmpdir, "out.json")
    with open(schema_file, "w", encoding="utf-8") as f:
        json.dump(schema, f, ensure_ascii=False)

    prompt = (
        (instructions or "Extract fields from the <stdin> webpage text strictly per the JSON Schema.")
        + " CRITICAL: use EXACTLY the property names (keys) defined in the schema as the JSON keys —"
        + " do NOT rename, translate, abbreviate, or invent keys."
        + " Output ONLY JSON matching the schema. Put any schema field not found in the text into"
        + " missing_fields and omit that field. Never fabricate values."
    )

    # shell=False + 参数列表：避免 shell 注入面（prompt/schema 路径含引号/元字符时脆弱转义会破，🟡7）。
    cmd = [
        codex_exe, "exec", "--skip-git-repo-check", "--ephemeral",
        "-s", "read-only", "--color", "never",
        "--output-schema", schema_file, "-o", out_file, prompt,
    ]
    try:
        subprocess.run(
            cmd,
            input=text,
            timeout=timeout_s,
            shell=False,
            stdout=subprocess.DEVNULL,  # 丢弃 agent 过程输出，只用 -o 落盘结果
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="ignore",
        )
        with open(out_file, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        return json.loads(raw) if raw else {}
    except subprocess.TimeoutExpired:
        return {"__extract_error__": "codex-timeout"}
    except FileNotFoundError:
        return {"__extract_error__": "codex-no-output-file"}
    except json.JSONDecodeError as e:
        return {"__extract_error__": "codex-bad-json:%s" % str(e)[:60]}
    except Exception as e:
        return {"__extract_error__": "codex-failed:%s" % str(e)[:80]}
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


# —— openai-compat 后端 —— #

def _build_messages(text, schema, instructions):
    sys = (
        (instructions or "Extract structured fields from the webpage text strictly per the JSON Schema.")
        + " Use EXACTLY the schema property names as JSON keys. Do NOT rename/translate/invent keys."
        + " Output ONLY a JSON object. Any schema field not found in the text goes into missing_fields"
        + " and is omitted. Never fabricate values."
        + " JSON Schema: " + json.dumps(schema, ensure_ascii=False)
    )
    # 正文可能很长，截断到合理长度（弱模型 context 有限；正文关键字段通常在前部）
    body = text[:24000]
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": body},
    ]


def _extract_openai_compat(text, schema, instructions, cfg):
    import httpx
    llm = cfg.get("llm", {})
    base_env = llm.get("baseUrlEnv") or llm.get("baseURLEnv")
    key_env = llm.get("apiKeyEnv")
    base_url = os.environ.get(base_env, "").rstrip("/")
    api_key = os.environ.get(key_env, "")
    model = llm.get("model", "")
    timeout_s = int(llm.get("timeoutMs", 60000)) / 1000.0
    max_retry = int(llm.get("maxRetry", 2))
    json_mode = llm.get("jsonMode", "plain")  # openai_schema | ollama_format | plain

    messages = _build_messages(text, schema, instructions)
    payload = {"model": model, "messages": messages, "temperature": 0}
    if json_mode == "openai_schema":
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "schema": schema, "strict": False},
        }
    elif json_mode == "ollama_format":
        # Ollama /v1 兼容端点认 response_format=json_object；原生 format 走另一路，这里走兼容口。
        payload["response_format"] = {"type": "json_object"}
    # plain：不加约束，纯靠 prompt（最兜底）

    url = base_url + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key

    last_err = None
    for attempt in range(1, max_retry + 1):
        try:
            with httpx.Client(timeout=timeout_s) as client:
                resp = client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                last_err = "http-%s" % resp.status_code
                continue
            j = resp.json()
            content = j["choices"][0]["message"]["content"]
            return _parse_json_loose(content)
        except Exception as e:
            last_err = str(e)[:80]
            continue
    return {"__extract_error__": "openai-compat-failed:%s" % (last_err or "unknown")}


def _parse_json_loose(content):
    """从模型输出里尽量抠出 JSON（弱模型常包 ```json 或前后废话）。失败返回错误标记。"""
    if not content:
        return {"__extract_error__": "empty-content"}
    s = content.strip()
    # 去 code fence
    if s.startswith("```"):
        s = s.split("```", 2)
        s = s[1] if len(s) > 1 else content
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    # 抠第一个 { 到最后一个 }
    a = s.find("{")
    b = s.rfind("}")
    if a >= 0 and b > a:
        s = s[a:b + 1]
    try:
        return json.loads(s)
    except Exception as e:
        return {"__extract_error__": "parse-failed:%s" % str(e)[:60]}


def extract(text, json_schema, instructions, cfg):
    """统一抽取入口。返回 LLM 原始字段对象（键名自由）。

    失败降级：返回带 __extract_error__ 的 dict（output 层据此把该目标全字段计入 missing、绝不编造）。
    """
    provider = cfg.get("llm", {}).get("provider", "codex")
    if not text or len(text) < 50:
        return {"__extract_error__": "empty-or-tiny-text"}
    if provider == "codex":
        return _extract_codex(text, json_schema, instructions, cfg)
    elif provider == "openai-compat":
        return _extract_openai_compat(text, json_schema, instructions, cfg)
    return {"__extract_error__": "unknown-provider:%s" % provider}
