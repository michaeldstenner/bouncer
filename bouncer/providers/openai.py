import json
import os
import urllib.request
import urllib.error
from pathlib import Path

from . import _build_prompt, _parse_llm_text
from ..log import log_llm_debug


def _extract_response_text(body: dict) -> str:
    choices = body.get("choices") or []
    if not choices:
        raise ValueError("OpenAI-compatible response missing choices")

    choice = choices[0] or {}
    message = choice.get("message") or {}
    content = message.get("content")

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str) and part.strip():
                parts.append(part.strip())
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        if parts:
            return "\n".join(parts)

    for candidate in (
        choice.get("text"),
        body.get("output_text"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    raise ValueError("OpenAI-compatible response missing textual content")


def call_openai(
    tool_name: str,
    tool_input: dict,
    cwd: Path,
    config: dict,
) -> tuple[str | None, str]:
    """OpenAI chat completions — also handles openai_compatible providers (Groq, LM Studio, etc.)."""
    llm_cfg  = config.get("llm", {})
    model    = llm_cfg.get("model")
    if not model:
        return None, "No LLM model configured — set llm.model in ~/.config/bouncer/config.yaml"
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
        "max_tokens": 1000,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request_payload = {
        "url": base_url + "/v1/chat/completions",
        "headers": headers,
        "body": payload,
    }
    try:
        req = urllib.request.Request(
            base_url + "/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        response_text = _extract_response_text(body)
        log_llm_debug(str(cwd), config, llm_cfg.get("provider", "openai_compatible"), model,
                      request_payload, response_body=body, response_text=response_text)
        return _parse_llm_text(response_text)
    except urllib.error.URLError:
        log_llm_debug(str(cwd), config, llm_cfg.get("provider", "openai_compatible"), model,
                      request_payload, error="OpenAI endpoint unavailable")
        return None, "OpenAI endpoint unavailable"
    except Exception as e:
        log_llm_debug(str(cwd), config, llm_cfg.get("provider", "openai_compatible"), model,
                      request_payload, error=f"Classifier error: {e}")
        return None, f"Classifier error: {e}"
