import json
import os
import socket
import urllib.request
import urllib.error
from pathlib import Path

from . import _build_prompt, _parse_llm_text


def call_anthropic(
    tool_name: str,
    tool_input: dict,
    cwd: Path,
    config: dict,
) -> tuple[str | None, str]:
    llm_cfg  = config.get("llm", {})
    model    = llm_cfg.get("model")
    if not model:
        return None, "No LLM model configured — set llm.model in ~/.config/bouncer/config.yaml"
    base_url = llm_cfg.get("url", "https://api.anthropic.com").rstrip("/")
    timeout  = int(llm_cfg.get("timeout", 25))
    api_key  = llm_cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")

    system_text, user_text = _build_prompt(tool_name, tool_input, cwd, config)
    payload = {
        "model": model,
        "max_tokens": 80,
        "system": system_text,
        "messages": [{"role": "user", "content": user_text}],
    }
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if api_key:
        headers["x-api-key"] = api_key
    try:
        req = urllib.request.Request(
            base_url + "/v1/messages",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        response_text = body["content"][0]["text"].strip()
        return _parse_llm_text(response_text)
    except (TimeoutError, socket.timeout):
        return None, f"Anthropic API timed out after {timeout}s — service may be slow or overloaded"
    except urllib.error.URLError:
        return None, "Anthropic endpoint unavailable"
    except Exception as e:
        return None, f"Classifier error: {e}"
