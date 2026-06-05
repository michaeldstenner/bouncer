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
 * Bouncer reviews opencode's native permission prompts. ALLOW and DENY are
 * answered after the configured delay; ASK leaves opencode's prompt visible.
 *
 * Optional plugin config:
 *   { "plugin": [["bouncer", { "replyDelayMs": 10000 }]] }
 */

import { execFileSync } from "child_process"

type OpencodeClient = {
  permission: {
    reply(input: { requestID: string; reply: "once" | "always" | "reject"; message?: string }): Promise<unknown>
  }
}

type PluginInput = {
  client: OpencodeClient
  directory?: string
}

type PluginOptions = {
  replyDelayMs?: number
  replyDelaySeconds?: number
}

type ToolArgs = {
  tool: string
  args: Record<string, unknown>
  time: number
}

type PermissionRequest = {
  id: string
  sessionID: string
  permission: string
  patterns?: string[]
  metadata?: Record<string, unknown>
  tool?: {
    callID: string
    messageID?: string
  }
}

type BouncerDecision =
  | { decision: "allow"; reason: string }
  | { decision: "ask"; reason: string }
  | { decision: "deny"; reason: string }
  | { decision: "skip"; reason: string }

const toolArgs = new Map<string, ToolArgs>()

function key(sessionID: string, callID: string) {
  return `${sessionID}:${callID}`
}

function pruneToolArgs() {
  const cutoff = Date.now() - 10 * 60 * 1000
  for (const [k, value] of toolArgs) {
    if (value.time < cutoff) toolArgs.delete(k)
  }
}

function delayMs(options?: PluginOptions) {
  if (typeof options?.replyDelayMs === "number" && Number.isFinite(options.replyDelayMs)) {
    return Math.max(0, options.replyDelayMs)
  }
  if (typeof options?.replyDelaySeconds === "number" && Number.isFinite(options.replyDelaySeconds)) {
    return Math.max(0, options.replyDelaySeconds * 1000)
  }
  return 0
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function permissionToolInput(request: PermissionRequest, cached?: ToolArgs): Record<string, unknown> {
  if (cached?.args) return cached.args

  const metadata = request.metadata ?? {}
  if (typeof metadata.command === "string") return { ...metadata, command: metadata.command }

  if (request.permission === "bash") {
    return {
      ...metadata,
      command: (request.patterns ?? []).join("\n"),
    }
  }

  return {
    ...metadata,
    patterns: request.patterns ?? [],
  }
}

function parseJsonDecision(stdout: string): BouncerDecision {
  const text = stdout.trim()
  if (!text) return { decision: "skip", reason: "no bouncer decision" }

  const data = JSON.parse(text) as {
    hookSpecificOutput?: {
      permissionDecision?: string
      permissionDecisionReason?: string
    }
  }
  const output = data.hookSpecificOutput
  const decision = output?.permissionDecision
  const reason = output?.permissionDecisionReason ?? ""
  if (decision === "allow") return { decision: "allow", reason }
  if (decision === "ask") return { decision: "ask", reason }
  return { decision: "skip", reason: "no bouncer decision" }
}

function classify(
  tool: string,
  toolInput: Record<string, unknown>,
  sessionID: string,
  cwd: string,
): BouncerDecision {
  const bouncerPayload = JSON.stringify({
    harness: "opencode",
    tool_name: tool,
    tool_input: toolInput,
    cwd,
    session_id: sessionID,
    hook_event_name: "PermissionRequest",
  })

  try {
    const stdout = execFileSync("bouncer", ["classify", "--hook", "--format", "json"], {
      input: bouncerPayload,
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "pipe"],
    })
    return parseJsonDecision(stdout)
  } catch (err: unknown) {
    const e = err as { status?: number; stderr?: string }
    if (e.status === 2) {
      return { decision: "deny", reason: (e.stderr ?? "").trim() }
    }
    return { decision: "skip", reason: "bouncer unavailable" }
  }
}

export const BouncerPlugin = async (pluginInput: PluginInput, options?: PluginOptions) => {
  const configuredDelayMs = delayMs(options)
  const cwd = pluginInput.directory ?? process.cwd()
  const reply = async (input: { requestID: string; reply: "once" | "reject"; message?: string }) => {
    try {
      await pluginInput.client.permission.reply(input)
    } catch {
      // The user may answer before bouncer's delayed auto-reply. In that case
      // opencode has already removed the pending request, which is fine.
    }
  }

  return {
    "tool.execute.before": async (
      input: { tool: string; sessionID: string; callID: string; args?: Record<string, unknown> },
      output: { args?: Record<string, unknown> },
    ): Promise<void> => {
      pruneToolArgs()
      toolArgs.set(key(input.sessionID, input.callID), {
        tool: input.tool,
        args: {
          ...(input.args ?? {}),
          ...(output.args ?? {}),
        },
        time: Date.now(),
      })
    },

    event: async (input: { event: { type: string; properties?: PermissionRequest } }): Promise<void> => {
      if (input.event.type !== "permission.asked") return

      const request = input.event.properties
      if (!request?.id || !request.sessionID) return

      const cached = request.tool?.callID
        ? toolArgs.get(key(request.sessionID, request.tool.callID))
        : undefined
      const tool = cached?.tool ?? request.permission
      const toolInput = permissionToolInput(request, cached)

      const started = Date.now()
      const result = classify(tool, toolInput, request.sessionID, cwd)
      if (result.decision === "ask" || result.decision === "skip") return

      const remaining = configuredDelayMs - (Date.now() - started)
      if (remaining > 0) await sleep(remaining)

      if (result.decision === "allow") {
        await reply({
          requestID: request.id,
          reply: "once",
        })
        return
      }

      await reply({
        requestID: request.id,
        reply: "reject",
        message: result.reason || "operation denied by policy",
      })
    },
  }
}
