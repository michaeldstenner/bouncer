import json
import urllib.request
import urllib.error
from pathlib import Path

from . import _build_prompt, _parse_llm_text


def call_ollama(
    tool_name: str,
    tool_input: dict,
    cwd: Path,
    config: dict,
) -> tuple[str | None, str]:
    llm_cfg  = config.get("llm", {})
    model    = llm_cfg.get("model")
    if not model:
        return None, "No LLM model configured — set llm.model in ~/.config/bouncer/config.yaml"
    base_url = llm_cfg.get("url", "http://localhost:11434").rstrip("/")
    timeout  = int(llm_cfg.get("timeout", 25))

    system_text, user_text = _build_prompt(tool_name, tool_input, cwd, config)
    payload = {
        "model": model,
        "prompt": "\n\n---\n\n".join([system_text, user_text]),
        "stream": False,
        "keep_alive": "60m",
        "think": False,
        "options": {"temperature": 0, "num_predict": 80},
    }
    try:
        req = urllib.request.Request(
            base_url + "/api/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response_text = json.loads(resp.read()).get("response", "").strip()
        return _parse_llm_text(response_text)
    except urllib.error.URLError:
        return None, "Ollama unavailable"
    except Exception as e:
        return None, f"Classifier error: {e}"
