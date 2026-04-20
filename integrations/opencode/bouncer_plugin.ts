/**
 * bouncer plugin for opencode
 *
 * Install:
 *   cp bouncer_plugin.ts ~/.config/opencode/plugins/bouncer.ts
 *   # In ~/.config/opencode/opencode.json:
 *   #   { "plugin": ["bouncer"] }
 *
 * Or per-project:
 *   cp bouncer_plugin.ts <project>/.opencode/plugins/bouncer.ts
 *   # In <project>/.opencode/opencode.json:
 *   #   { "plugin": ["bouncer"] }
 *
 * Requires: bouncer on PATH, .bouncer/config.yaml in the project tree.
 * If no .bouncer/config.yaml is found, bouncer exits 0 (pass-through).
 *
 * Uses --format plain so the plugin doesn't need to parse Claude Code's
 * hookSpecificOutput JSON envelope. Output is one of:
 *   allow
 *   deny\t<reason>   (exit 2)
 *   ask\t<reason>    (exit 0)
 */

import { execFileSync } from "child_process"

// opencode doesn't expose a session ID to plugins; derive a stable per-process ID.
const SESSION_ID = `opencode-${process.pid}`

export const BouncerPlugin = async () => {
  return {
    "tool.execute.before": async (
      input: { tool: string; args?: Record<string, unknown> },
      _output: { args: Record<string, unknown> },
    ): Promise<void> => {
      const toolInput = input.args ?? {}

      // bouncer's tool filter is case-insensitive, so pass tool name as-is.
      const bouncerPayload = JSON.stringify({
        tool_name: input.tool,
        tool_input: toolInput,
        cwd: process.cwd(),
        session_id: SESSION_ID,
        hook_event_name: "PreToolUse",
      })

      let stdout: string
      try {
        stdout = execFileSync("bouncer", ["classify", "--hook", "--format", "plain"], {
          input: bouncerPayload,
          encoding: "utf-8",
          stdio: ["pipe", "pipe", "pipe"],
        })
      } catch (err: unknown) {
        const e = err as { status?: number; stdout?: string }
        if (e.status === 2) {
          // DENY — stdout contains "deny\t<reason>"
          const reason = (e.stdout ?? "").replace(/^deny\t?/, "").trim()
          throw new Error(`bouncer: ${reason || "operation denied by policy"}`)
        }
        // bouncer unavailable or other error → fail open
        return
      }

      const line = stdout.trim()
      if (!line || line === "allow") return

      const [decision, ...rest] = line.split("\t")
      const reason = rest.join("\t")

      if (decision === "deny") {
        throw new Error(`bouncer: ${reason || "operation denied by policy"}`)
      }

      // "ask" = bouncer needs user approval; opencode has no mid-execution
      // escalation UI, so block with the reason (which bouncer's plain format
      // already annotates with escalation guidance for the agent).
      // To get a plain deny instead, set on_unsure: deny_with_message in config.
      if (decision === "ask") {
        const cmd = (toolInput as { command?: string }).command ?? input.tool
        throw new Error(
          `bouncer: needs user approval for "${cmd}"\n` +
            `${reason || "no reason provided"}`,
        )
      }

      // anything else → pass through
    },
  }
}
