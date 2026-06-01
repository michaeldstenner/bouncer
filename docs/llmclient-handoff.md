# llmclient Handoff Notes

## Circuit breaker scope is too broad

llmclient's circuit breaker currently appears to key state by `caller` only.
In bouncer, all classifier calls use `log_caller="bouncer"`, so failures from
one backend/model can suppress unrelated later calls.

Observed sequence from `~/.local/share/llmclient/llmclient_log.jsonl`:

```text
caller=bouncer provider=openai_compatible model=unreachable-test-model outcome=error:unreachable
caller=bouncer provider=openai_compatible model=unreachable-test-model outcome=error:unreachable
caller=bouncer provider=openai_compatible model=unreachable-test-model outcome=error:unreachable
caller=bouncer provider=openai_compatible model=gpt-oss-120b outcome=circuit_open
caller=bouncer provider=openai_compatible model=gpt-oss-120b outcome=circuit_open
```

The first model was intentionally unreachable in bouncer's offline smoke tests.
That opened the circuit for `caller=bouncer`, and then the live smoke test using
`gpt-oss-120b` was skipped before making a real request.

Recommended fix: key circuit state by at least:

```text
caller + provider + model
```

For `openai_compatible`, consider including `url` as well, because the same
model name can exist behind different endpoints.

Current relevant code in the vendored copy:

- `bouncer/llmclient/_queue.py`: `circuit_state` has `caller TEXT PRIMARY KEY`
- `circuit_check(cfg)` queries `WHERE caller=?`
- `circuit_record(cfg, outcome, is_probe)` updates `WHERE caller=?`

A migration path could add a `scope` or `circuit_key` column rather than making
a composite primary key if compatibility matters. For example:

```python
def circuit_key(cfg):
    return f"{cfg.log_caller}|{cfg.provider}|{cfg.model}|{cfg.url or ''}"
```

## DB open failures can still escape

In restricted Codex sessions, direct `bouncer check --llm ...` hit:

```text
sqlite3.OperationalError: unable to open database file
```

The failure came from opening `~/.local/share/llmclient/queue.db`. The intended
behavior in `circuit_check()` says it fails open on DB errors, but `_open()` is
called before the `try`, so an `_open()` failure escapes.

Current pattern:

```python
conn = _open()
try:
    ...
except Exception:
    return "proceed"
```

Recommended fix:

```python
try:
    conn = _open()
    ...
except Exception:
    return "proceed"
```

The same audit is worth doing for `circuit_record()` and queue acquisition
paths. For bouncer, an llmclient state DB problem should degrade to a normal
provider failure result, not crash the CLI.

## bouncer test workaround

bouncer's smoke tests now set:

```yaml
llm:
  circuit_n: 0
```

for both the intentionally unreachable fixture and the live fixture. That keeps
bouncer's smoke tests from poisoning shared llmclient state, but it is only a
test workaround. The production fix should be in llmclient's circuit keying and
DB error handling.
