"""One bounded model call; no automatic retries or paid fallback."""

import json
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AIUnavailable(RuntimeError):
    pass


def read_private_key(path):
    path = Path(path).expanduser()
    if path.stat().st_mode & 0o077:
        raise PermissionError("AI key file must be private (mode 0600)")
    key = path.read_text(encoding="utf-8").strip()
    if not key or "\n" in key or "\r" in key:
        raise ValueError("AI key file is empty or malformed")
    return key


def _post_json(url, headers, payload, timeout=180):
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        # The response can contain request details; never echo it or the key.
        raise AIUnavailable("Model request failed (HTTP %s); next scheduled run may retry" % exc.code) from None
    except (URLError, TimeoutError) as exc:
        raise AIUnavailable("Model service or network unavailable; next scheduled run may retry") from None


def generate_json(provider, model, key_file, prompt, schema, thinking="medium"):
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Set ai_model to a model ID supported by your provider")
    key = read_private_key(key_file)
    if provider == "gemini":
        if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
            raise ValueError("Invalid Gemini model name")
        response = _post_json(
            "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model,
            {"Content-Type": "application/json", "x-goog-api-key": key},
            {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
             "generationConfig": {"thinkingConfig": {"thinkingLevel": thinking},
                                  "responseFormat": {"text": {"mimeType": "application/json", "schema": schema}}}},
        )
        candidates = response.get("candidates") or []
        if not candidates or candidates[0].get("finishReason") not in ("STOP", None):
            raise AIUnavailable("Model returned no complete candidate")
        text = "".join(part.get("text", "") for part in candidates[0].get("content", {}).get("parts", [])
                       if not part.get("thought"))
    elif provider in ("groq", "openrouter"):
        if provider == "openrouter" and not model.endswith(":free"):
            raise ValueError("OpenRouter must use an explicit :free model; paid fallback is disabled")
        if not re.fullmatch(r"[A-Za-z0-9._:/-]+", model):
            raise ValueError("Invalid model name")
        base = "https://api.groq.com/openai/v1" if provider == "groq" else "https://openrouter.ai/api/v1"
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                   "response_format": {"type": "json_schema", "json_schema": {"name": "editorial_review", "strict": True, "schema": schema}}}
        if provider == "groq":
            payload["reasoning_effort"] = thinking
        else:
            payload["provider"] = {"allow_fallbacks": False, "require_parameters": True}
        response = _post_json(base + "/chat/completions",
                              {"Content-Type": "application/json", "Authorization": "Bearer " + key}, payload)
        choices = response.get("choices") or []
        text = choices[0].get("message", {}).get("content", "") if choices else ""
    else:
        raise ValueError("Unsupported AI provider")
    if not text:
        raise AIUnavailable("Model returned empty output")
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        raise AIUnavailable("Model returned invalid JSON") from None
    if not isinstance(result, dict):
        raise AIUnavailable("Model returned an unexpected JSON shape")
    return result
