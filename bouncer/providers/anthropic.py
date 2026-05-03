import json
import os
import socket
import time
import urllib.request
import urllib.error
from pathlib import Path

from . import _build_prompt, _parse_llm_text
from ..log import log_llm_debug


def call_anthropic(
    tool_name: str,
    tool_input: dict,
    cwd: Path,
    config: dict,
) -> tuple[str | None, str, int | None]:
    llm_cfg  = config.get("llm", {})
    model    = llm_cfg.get("model")
    if not model:
        return None, "No LLM model configured — set llm.model in ~/.config/bouncer/config.yaml", None
    base_url = llm_cfg.get("url", "https://api.anthropic.com").rstrip("/")
    timeout  = int(llm_cfg.get("timeout", 25))
    api_key  = llm_cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")

    system_text, user_text = _build_prompt(tool_name, tool_input, cwd, config)
    prompt_chars = len(system_text) + len(user_text)
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
    request_payload = {"url": base_url + "/v1/messages", "headers": headers, "body": payload}
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            base_url + "/v1/messages",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        elapsed = time.monotonic() - t0
        response_text = body["content"][0]["text"].strip()
        log_llm_debug(str(cwd), config, "anthropic", model,
                      request_payload, response_body=body, response_text=response_text,
                      elapsed_s=elapsed)
        decision, reason = _parse_llm_text(response_text)
        return decision, reason, prompt_chars
    except (TimeoutError, socket.timeout):
        elapsed = time.monotonic() - t0
        msg = f"Anthropic API timed out after {timeout}s — service may be slow or overloaded"
        log_llm_debug(str(cwd), config, "anthropic", model,
                      request_payload, error=msg, elapsed_s=elapsed)
        return None, msg, prompt_chars
    except urllib.error.URLError:
        elapsed = time.monotonic() - t0
        log_llm_debug(str(cwd), config, "anthropic", model,
                      request_payload, error="Anthropic endpoint unavailable", elapsed_s=elapsed)
        return None, "Anthropic endpoint unavailable", prompt_chars
    except Exception as e:
        elapsed = time.monotonic() - t0
        log_llm_debug(str(cwd), config, "anthropic", model,
                      request_payload, error=f"Classifier error: {e}", elapsed_s=elapsed)
        return None, f"Classifier error: {e}", prompt_chars
