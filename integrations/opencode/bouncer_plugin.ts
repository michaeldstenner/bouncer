/**
 * bouncer plugin for opencode
 *
 * Install:
 *   cp bouncer_plugin.ts ~/.config/opencode/plugin/bouncer.ts
 *   # In ~/.config/opencode/opencode.json:
 *   #   { "plugin": ["bouncer"] }
 *
 * Or per-project:
 *   cp bouncer_plugin.ts <project>/.opencode/plugin/bouncer.ts
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

export const BouncerPlugin = async () => {
  return {
    "tool.execute.before": async (
      input: { tool: string; sessionID: string; callID: string; args?: Record<string, unknown> },
      output: { args?: Record<string, unknown> },
    ): Promise<void> => {
      const toolInput = {
        ...(input.args ?? {}),
        ...(output.args ?? {}),
      }

      // bouncer's tool filter is case-insensitive, so pass tool name as-is.
      const bouncerPayload = JSON.stringify({
        harness: "opencode",
        tool_name: input.tool,
        tool_input: toolInput,
        cwd: process.cwd(),
        session_id: input.sessionID,
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

      // Plain-format harnesses do not have ASK available. Bouncer currently
      // delivers both DENY and internal ASK outcomes outward as "deny" here.
      // Keep this branch as a defensive fallback in case an older bouncer
      // still returns "ask".
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
