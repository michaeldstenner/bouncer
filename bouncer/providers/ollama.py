import json
import socket
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

from . import _build_prompt, _parse_llm_text
from .._abort import ABORT_EVENT
from ..log import log_llm_debug


def _check_ollama_busy(base_url: str, model: str) -> bool:
    """True if Ollama is up and the model is currently loaded (busy, not down)."""
    try:
        req = urllib.request.Request(base_url + "/api/ps")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        loaded = [m.get("model", "") for m in data.get("models", [])]
        return any(m == model or m.startswith(model.split(":")[0]) for m in loaded)
    except Exception:
        return False


def call_ollama(
    tool_name: str,
    tool_input: dict,
    cwd: Path,
    config: dict,
) -> tuple[str | None, str, int | None]:
    llm_cfg  = config.get("llm", {})
    model    = llm_cfg.get("model")
    if not model:
        return None, "No LLM model configured — set llm.model in ~/.config/bouncer/config.yaml", None
    base_url = llm_cfg.get("url", "http://localhost:11434").rstrip("/")
    timeout  = int(llm_cfg.get("timeout", 25))

    system_text, user_text = _build_prompt(tool_name, tool_input, cwd, config)
    prompt_chars = len(system_text) + len(user_text)
    payload = {
        "model": model,
        "prompt": "\n\n---\n\n".join([system_text, user_text]),
        "stream": False,
        "keep_alive": "60m",
        "think": False,
        "options": {"temperature": 0, "num_predict": 80},
    }
    request_payload = {"url": base_url + "/api/generate", "body": payload}
    t0 = time.monotonic()

    result: dict = {}

    def _do_request():
        try:
            req = urllib.request.Request(
                base_url + "/api/generate",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result["text"] = json.loads(resp.read()).get("response", "").strip()
        except Exception as exc:
            result["error"] = exc

    t = threading.Thread(target=_do_request, daemon=True)
    t.start()

    while t.is_alive():
        t.join(1.0)
        if ABORT_EVENT.is_set():
            elapsed = time.monotonic() - t0
            log_llm_debug(str(cwd), config, "ollama", model,
                          request_payload, error="user aborted", elapsed_s=elapsed)
            return None, "user aborted", prompt_chars

    elapsed = time.monotonic() - t0

    if "error" in result:
        exc = result["error"]
        if isinstance(exc, (TimeoutError, socket.timeout)):
            if _check_ollama_busy(base_url, model):
                msg = (f"Ollama busy: model loaded but did not respond within "
                       f"{timeout}s — consider increasing llm.timeout")
            else:
                msg = ("Ollama timed out and model is not loaded — "
                       "Ollama may be overloaded or restarting")
            log_llm_debug(str(cwd), config, "ollama", model,
                          request_payload, error=msg, elapsed_s=elapsed)
            return None, msg, prompt_chars
        if isinstance(exc, urllib.error.URLError):
            log_llm_debug(str(cwd), config, "ollama", model,
                          request_payload, error="Ollama unavailable", elapsed_s=elapsed)
            return None, "Ollama unavailable", prompt_chars
        log_llm_debug(str(cwd), config, "ollama", model,
                      request_payload, error=f"Classifier error: {exc}", elapsed_s=elapsed)
        return None, f"Classifier error: {exc}", prompt_chars

    response_text = result.get("text", "")
    log_llm_debug(str(cwd), config, "ollama", model,
                  request_payload, response_text=response_text, elapsed_s=elapsed)
    decision, reason = _parse_llm_text(response_text)
    return decision, reason, prompt_chars
