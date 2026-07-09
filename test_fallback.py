import unittest
from pathlib import Path
from unittest.mock import patch

import bouncer.providers as providers_mod


class TestFallbackConfig(unittest.TestCase):
    def test_llm_fallbacks_inherit_aip_url_and_key(self):
        config = {
            "provider": "openai_compatible",
            "model": "gpt-oss-120b",
            "url": "https://models.example.test",
            "api_key": "dummy-token",
            "fallbacks": [
                {"model": "nemotron-3-ultra"},
            ],
        }

        configs = providers_mod._build_llm_configs(config)

        self.assertEqual([c.model for c in configs], [
            "gpt-oss-120b", "nemotron-3-ultra"
        ])
        self.assertEqual([c.provider for c in configs], [
            "openai_compatible", "openai_compatible"
        ])
        self.assertEqual(configs[1].url, "https://models.example.test")
        self.assertEqual(configs[1].api_key, "dummy-token")

    def test_cross_provider_fallback_does_not_inherit_url_or_key(self):
        config = {
            "provider": "ollama",
            "model": "qwen3:32b",
            "url": "http://localhost:11434",
            "api_key": "local-key-should-not-cross",
            "fallbacks": [
                {"provider": "anthropic", "model": "claude-haiku"},
            ],
        }

        configs = providers_mod._build_llm_configs(config)

        self.assertEqual(configs[1].provider, "anthropic")
        self.assertEqual(configs[1].model, "claude-haiku")
        self.assertEqual(configs[1].url, "")
        self.assertEqual(configs[1].api_key, "")

    def test_call_llm_uses_fallback_client_when_configured(self):
        captured = {}

        class FakeFallbackClient:
            def __init__(self, configs, abort_event=None, fallback_on=None):
                captured["configs"] = configs
                captured["fallback_on"] = fallback_on
                self.last_attempts = []

            def call(self, user, system=""):
                class Result:
                    text = "DECISION: ALLOW\nREASON: ok"
                    outcome = "success"
                    prompt_chars = len(user) + len(system)
                    prompt_tokens = None
                    call_s = 0.1
                    queue_snapshot = None
                return Result()

        config = {
            "llm": {
                "provider": "openai_compatible",
                "model": "gpt-oss-120b",
                "url": "https://models.example.test",
                "fallback_on": ["timeout*", "http_5*"],
                "fallbacks": [{"model": "nemotron-3-ultra"}],
            }
        }

        with patch("bouncer.llmclient.FallbackLLMClient", FakeFallbackClient):
            decision, reason, _, _snap = providers_mod.call_llm(
                "Bash", {"command": "pwd"}, Path("/tmp/project"), config,
            )

        self.assertEqual((decision, reason), ("ALLOW", "ok"))
        self.assertEqual([c.model for c in captured["configs"]], [
            "gpt-oss-120b", "nemotron-3-ultra"
        ])
        self.assertEqual(captured["fallback_on"], ["timeout*", "http_5*"])


if __name__ == "__main__":
    unittest.main()
