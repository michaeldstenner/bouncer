import json
import os
import urllib.request
import urllib.error
from pathlib import Path

from . import _build_prompt, _parse_llm_text


def call_openai(
    tool_name: str,
    tool_input: dict,
    cwd: Path,
    config: dict,
) -> tuple[str | None, str]:
    """OpenAI chat completions — also handles openai_compatible providers (Groq, LM Studio, etc.)."""
    llm_cfg  = config.get("llm", {})
    model    = llm_cfg.get("model", "gpt-4o-mini")
    base_url = llm_cfg.get("url", "https://api.openai.com").rstrip("/")
    timeout  = int(llm_cfg.get("timeout", 25))
    api_key  = llm_cfg.get("api_key") or os.environ.get("OPENAI_API_KEY", "")

    system_text, user_text = _build_prompt(tool_name, tool_input, cwd, config)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user",   "content": user_text},
        ],
        "temperature": 0,
        "max_tokens": 80,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        req = urllib.request.Request(
            base_url + "/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        response_text = body["choices"][0]["message"]["content"].strip()
        return _parse_llm_text(response_text)
    except urllib.error.URLError:
        return None, "OpenAI endpoint unavailable"
    except Exception as e:
        return None, f"Classifier error: {e}"
