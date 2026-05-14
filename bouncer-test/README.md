# bouncer-test

Small integration-style smoke tests for Bouncer behavior outside the unit test
suite.

Run from the repository root:

```sh
uv run python bouncer-test/run.py
```

The runner creates disposable fixture projects under `bouncer-test/work/` and
executes `python -m bouncer` against them. The default suite is offline: it
does not call an LLM endpoint. It checks activation, dry-run output, tool-filter
skips, escalation formatting, plain-format no-ASK behavior, and unavailable-LLM
fallback.

To include real LLM round trips through the user-level LLM configuration:

```sh
uv run python bouncer-test/run.py --live
```

The live cases inherit provider, model, URL, and API key from the normal
user-level Bouncer config. They verify both `bouncer check --llm` and the hook
`classify --hook` path.

`bouncer-test/work/` is generated and can be deleted at any time.
