#!/usr/bin/env python3
"""Deterministic OpenAI-compatible stub for agent-based bouncer tests.

bouncer's `openai_compatible` provider POSTs to `{url}/v1/chat/completions`
with the classifier prompt (system = policy + rubric, user = "Tool: …\nCommand:
…") and parses `choices[0].message.content` for a two-line reply:

    DECISION: <ALLOW|DENY|UNSURE>
    REASON: <one sentence>

This server makes that verdict *deterministic*: it scans the request for a
sentinel token embedded in the tool call and returns the matching decision, so
an agent-driven test exercises the harness <-> bouncer protocol without the
nondeterminism of a real model. It records every request so a test can assert
bouncer actually consulted it.

Sentinels (first match wins, DENY before ALLOW so a command can't sneak both):
    BNCR_DENY    -> DENY
    BNCR_UNSURE  -> UNSURE
    BNCR_ALLOW   -> ALLOW
    (no match)   -> default (UNSURE), surfaced in the reason for debugging

Run standalone (for manual harness poking):
    python bouncer-test/agent/stub_llm.py --port 8900
or embed it in-process via the StubLLM context manager (see run.py).
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Ordered: a command containing several tokens resolves to the most restrictive.
_SENTINELS = [
    ("BNCR_DENY", "DENY"),
    ("BNCR_UNSURE", "UNSURE"),
    ("BNCR_ALLOW", "ALLOW"),
]
_DEFAULT_DECISION = "UNSURE"


def decide(text: str) -> tuple[str, str]:
    """Map request text to (decision, reason)."""
    for token, decision in _SENTINELS:
        if token in text:
            return decision, f"stub: matched sentinel {token}"
    return _DEFAULT_DECISION, "stub: no sentinel matched (default)"


def _completion_body(decision: str, reason: str) -> dict:
    content = f"DECISION: {decision}\nREASON: {reason}"
    return {
        "id": "stub-cmpl",
        "object": "chat.completion",
        "model": "bouncer-stub",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@dataclass
class _State:
    requests: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, payload: dict, decision: str) -> None:
        user = ""
        for msg in payload.get("messages", []):
            if msg.get("role") == "user":
                user = msg.get("content") or ""
        with self.lock:
            self.requests.append({"user": user, "decision": decision})

    def find(self, sentinel: str) -> list[dict]:
        with self.lock:
            return [r for r in self.requests if sentinel in r["user"]]


def _make_handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr spam
            pass

        def _json(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                return self._json(200, {"ok": True})
            if self.path == "/requests":
                with state.lock:
                    return self._json(200, {"requests": list(state.requests)})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "bad json"})
            blob = "\n".join(
                str(m.get("content", "")) for m in payload.get("messages", [])
            )
            decision, reason = decide(blob)
            state.record(payload, decision)
            return self._json(200, _completion_body(decision, reason))

    return Handler


class StubLLM:
    """Background OpenAI-compatible stub. `url` is ready for bouncer's
    `llm.url`; `find(sentinel)` lets a test assert it was consulted."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.state = _State()
        self._httpd = ThreadingHTTPServer((host, port), _make_handler(self.state))
        self.host, self.port = self._httpd.server_address[0], self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def find(self, sentinel: str) -> list[dict]:
        return self.state.find(sentinel)

    def start(self) -> "StubLLM":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def __enter__(self) -> "StubLLM":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8900)
    args = ap.parse_args()
    stub = StubLLM(args.host, args.port).start()
    print(f"stub LLM on {stub.url}  (POST /v1/chat/completions)")
    print("sentinels: BNCR_DENY -> DENY, BNCR_UNSURE -> UNSURE, BNCR_ALLOW -> ALLOW")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        stub.stop()


if __name__ == "__main__":
    main()
