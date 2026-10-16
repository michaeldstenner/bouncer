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
 * With no .bouncer/config.yaml, bouncer still runs its hard guard;
 * anything else exits 0 (pass-through) unless `uninitialized: blocked`.
 *
 * Bouncer reviews opencode's native permission prompts. ALLOW and DENY are
 * answered after the configured delay; ASK leaves opencode's prompt visible.
 *
 * Optional plugin config:
 *   { "plugin": [["bouncer", { "replyDelayMs": 10000 }]] }
 */

import { execFileSync } from "child_process"
import { appendFileSync, mkdirSync } from "fs"
import { homedir } from "os"
import { dirname, join } from "path"

type OpencodeClient = {
  postSessionIdPermissionsPermissionId(input: {
    path: { id: string; permissionID: string }
    body: { response: "once" | "always" | "reject" }
  }): Promise<unknown>
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

type PermissionReply = {
  requestID?: string
  permissionID?: string
  reply?: "once" | "always" | "reject"
  response?: "once" | "always" | "reject"
  sessionID?: string
}

type BouncerDecision =
  | { decision: "allow"; reason: string }
  | { decision: "ask"; reason: string }
  | { decision: "deny"; reason: string }
  | { decision: "skip"; reason: string }

const toolArgs = new Map<string, ToolArgs>()
const observedRequests = new Map<string, number>()
const replyAttempts = new Map<
  string,
  { result: Promise<boolean>; time: number }
>()

function replyAuditPath() {
  const override = process.env.BOUNCER_OPENCODE_AUDIT_LOG
  if (override) return override
  return join(
    homedir(),
    ".local",
    "share",
    "bouncer",
    "opencode_replies.jsonl",
  )
}

export function writeReplyAudit(entry: Record<string, unknown>) {
  try {
    const path = replyAuditPath()
    mkdirSync(dirname(path), { recursive: true })
    appendFileSync(path, JSON.stringify({
      timestamp: new Date().toISOString(),
      ...entry,
    }) + "\n")
  } catch {
    // Audit failures must not interfere with permission handling.
  }
}

function key(sessionID: string, callID: string) {
  return `${sessionID}:${callID}`
}

function pruneToolArgs() {
  const cutoff = Date.now() - 10 * 60 * 1000
  for (const [k, value] of toolArgs) {
    if (value.time < cutoff) toolArgs.delete(k)
  }
  for (const [requestID, value] of replyAttempts) {
    if (value.time < cutoff) replyAttempts.delete(requestID)
  }
  for (const [requestID, time] of observedRequests) {
    if (time < cutoff) observedRequests.delete(requestID)
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
  const reply = async (input: {
    sessionID: string
    requestID: string
    reply: "once" | "reject"
    message?: string
  }) => {
    try {
      await pluginInput.client.postSessionIdPermissionsPermissionId({
        path: {
          id: input.sessionID,
          permissionID: input.requestID,
        },
        body: {
          response: input.reply,
        },
      })
      return true
    } catch {
      return false
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

    event: async (input: {
      event: { type: string; properties?: PermissionRequest | PermissionReply }
    }): Promise<void> => {
      if (input.event.type === "permission.replied") {
        const response = input.event.properties as PermissionReply | undefined
        const requestID = response?.requestID ?? response?.permissionID
        if (!requestID) return
        if (!observedRequests.has(requestID)) return

        const attempt = replyAttempts.get(requestID)
        const bouncerAccepted = attempt ? await attempt.result : false
        if (!bouncerAccepted) {
          writeReplyAudit({
            event: "permission_replied",
            source: "not_bouncer",
            cwd,
            session_id: response?.sessionID,
            permission_id: requestID,
            response: response?.reply ?? response?.response,
          })
        }
        replyAttempts.delete(requestID)
        observedRequests.delete(requestID)
        return
      }
      if (input.event.type !== "permission.asked") return

      const request = input.event.properties as PermissionRequest | undefined
      if (!request?.id || !request.sessionID) return
      observedRequests.set(request.id, Date.now())

      const cached = request.tool?.callID
        ? toolArgs.get(key(request.sessionID, request.tool.callID))
        : undefined
      const tool = cached?.tool ?? request.permission
      const toolInput = permissionToolInput(request, cached)

      writeReplyAudit({
        event: "permission_asked",
        cwd,
        session_id: request.sessionID,
        permission_id: request.id,
        permission: request.permission,
        patterns: request.patterns ?? [],
        tool,
      })

      const started = Date.now()
      const result = classify(tool, toolInput, request.sessionID, cwd)
      writeReplyAudit({
        event: "classified",
        cwd,
        session_id: request.sessionID,
        permission_id: request.id,
        decision: result.decision,
        reason: result.reason,
        elapsed_ms: Date.now() - started,
      })
      if (result.decision === "ask" || result.decision === "skip") return

      const remaining = configuredDelayMs - (Date.now() - started)
      if (remaining > 0) await sleep(remaining)

      const response = result.decision === "allow" ? "once" : "reject"
      const replyResult = reply({
        sessionID: request.sessionID,
        requestID: request.id,
        reply: response,
        message: response === "reject"
          ? result.reason || "operation denied by policy"
          : undefined,
      })
      replyAttempts.set(request.id, {
        result: replyResult,
        time: Date.now(),
      })
      const accepted = await replyResult
      writeReplyAudit({
        event: accepted ? "permission_replied" : "bouncer_reply_failed",
        source: "bouncer",
        outcome: accepted ? "accepted" : "not_accepted",
        cwd,
        session_id: request.sessionID,
        permission_id: request.id,
        response,
      })
    },
  }
}
