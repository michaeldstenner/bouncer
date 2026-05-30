# Alpha Quickstart

This is the short path for alpha testing bouncer, aimed at a comfortable
AI-native coder.

## 1. Install bouncer

Clone the repo and install it as an editable CLI tool:

```sh
git clone <repo-url> ./bouncer
cd ./bouncer
uv tool install --editable .
bouncer --help
command -v bouncer
```

If you do not use `uv`, install bouncer in a Python environment whose `bouncer`
command will be on `PATH` for your agent harness. A venv only works if you
start the agent from that same activated venv:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
bouncer --help
command -v bouncer
```

## 2. Create user config and wire your harness

Create `~/.config/bouncer/` and choose which detected harnesses or shim to wire:

```sh
bouncer -g init
```

This modifies local harness config under your home directory, such as
`~/.codex/hooks.json`, `~/.claude/settings.json`, or
`~/.config/opencode/opencode.json`.

## 3. Install the shared config

Replace or add the files provided by the maintainer:

```text
~/.config/bouncer/config.yaml       # incl. any provider api_key/url sections
~/.config/bouncer/policy.md
~/.config/bouncer/system_prompt.txt # if provided
```

Do not commit these files into project repos. They are user-level local config.

Confirm bouncer can read the config:

```sh
bouncer status -v
```

## 4. Enable a project

Inside any repo where bouncer should be active:

```sh
cd <project>
bouncer init    # enable bouncer and create project-level files
bouncer policy
bouncer status
```

Use `bouncer policy` to describe what is safe, routine, risky, and forbidden
for that project. Clearer policies produce fewer `UNSURE` results, but
focus on intent rather than brittle command rules. For example:

```text
The agent may create, read, update, and delete config data in
~/.config/myproject/.
```

## 5. Smoke test

From the project directory:

```sh
bouncer check 'pwd'
bouncer check --llm 'pwd'
```

Expected result: the first command shows the project and policy context; the
second calls the configured LLM and should return an `ALLOW`, `DENY`, or
`UNSURE` decision.

Then start your agent in that project and run a command that normally asks for
approval. Bouncer should pre-triage that approval request.

## 6. Normal operation

Useful commands:

```sh
bouncer status
bouncer status -v
bouncer log
bouncer log --tail
bouncer log --filter unsure
bouncer review
```

In harnesses that support ASK, the agent can retry the exact command with an
escalation header. Bouncer will skip the LLM and ask the user directly.

```sh
# ESCALATE: explain why this specific command should be allowed
<same command>
```

Escalation asks the human; it is not an automatic override.

## 7. Disable bouncer

For one project:

```sh
cd <project>
bouncer config -d
```

Re-enable it later:

```sh
bouncer config -e
```

For all projects using the shared user config:

```sh
bouncer -g config -d
```

## 8. What to send when something fails

Send the maintainer:

```sh
bouncer status -v
bouncer check --llm 'pwd'
tail -n 20 .bouncer/log.jsonl
tail -n 20 ~/.local/share/bouncer/log.jsonl
```

Also include which harness you were using: Codex, Claude Code, opencode, or
the shell shim.
