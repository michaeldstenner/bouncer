/**
 * bouncer plugin for opencode
 *
 * Install:
 *   cp bouncer_plugin.ts ~/.config/opencode/plugin/bouncer.ts
 *   # Files in this directory are auto-discovered by opencode.
 *
 * Or per-project:
 *   cp bouncer_plugin.ts <project>/.opencode/plugin/bouncer.ts
 *   # Files in this directory are auto-discovered by opencode.
 *
 * Requires: bouncer on PATH, .bouncer/config.yaml in the project tree.
 * If no .bouncer/config.yaml is found, bouncer exits 0 (pass-through).
 *
 * Bouncer reviews opencode's native permission prompts. ALLOW and DENY are
 * answered after the configured delay; ASK leaves opencode's prompt visible.
 *
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
  tui?: {
    showToast(input: {
      body: {
        title?: string
        message: string
        variant: "info" | "success" | "warning" | "error"
        duration?: number
      }
      query?: { directory?: string }
    }): Promise<unknown>
  }
}

type OpencodeClientV2 = {
  permission: {
    reply(
      input: {
        requestID: string
        directory?: string
        reply: "once" | "always" | "reject"
        message?: string
      },
      options?: { throwOnError?: boolean },
    ): Promise<unknown>
  }
  v2: {
    session: {
      messages(
        input: { sessionID: string; limit?: number; order?: "asc" | "desc" },
        options?: { throwOnError?: boolean },
      ): Promise<{
        data: {
          data: Array<{
            id: string
            type: string
            content?: Array<{
              type: string
              id?: string
              name?: string
              state?: { input?: string | Record<string, unknown> }
            }>
          }>
        }
      }>
      permission: {
        reply(
          input: {
            sessionID: string
            requestID: string
            reply: "once" | "always" | "reject"
            message?: string
          },
          options?: { throwOnError?: boolean },
        ): Promise<unknown>
      }
    }
  }
}

type PluginInput = {
  client: OpencodeClient
  clientV2?: OpencodeClientV2
  directory?: string
  serverUrl?: URL
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

type PermissionRequestV1 = {
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

type PermissionRequestV2 = {
  id: string
  sessionID: string
  action: string
  resources: string[]
  metadata?: Record<string, unknown>
  source?: {
    type: "tool"
    callID: string
    messageID: string
  }
}

type PermissionRequest = {
  generation: "v1" | "v2"
  id: string
  sessionID: string
  permission: string
  patterns: string[]
  metadata?: Record<string, unknown>
  callID?: string
  messageID?: string
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

function writeReplyAudit(entry: Record<string, unknown>) {
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

function permissionRequest(type: string, properties: unknown): PermissionRequest | undefined {
  if (type === "permission.asked") {
    const request = properties as PermissionRequestV1 | undefined
    if (!request?.id || !request.sessionID || !request.permission) return
    return {
      generation: "v1",
      id: request.id,
      sessionID: request.sessionID,
      permission: request.permission,
      patterns: request.patterns ?? [],
      metadata: request.metadata,
      callID: request.tool?.callID,
      messageID: request.tool?.messageID,
    }
  }
  if (type !== "permission.v2.asked") return
  const request = properties as PermissionRequestV2 | undefined
  if (!request?.id || !request.sessionID || !request.action) return
  return {
    generation: "v2",
    id: request.id,
    sessionID: request.sessionID,
    permission: request.action,
    patterns: request.resources ?? [],
    metadata: request.metadata,
    callID: request.source?.callID,
    messageID: request.source?.messageID,
  }
}

function parsedInput(input: string | Record<string, unknown> | undefined) {
  if (!input) return
  if (typeof input !== "string") return input
  try {
    const value = JSON.parse(input)
    if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>
  } catch {
    return
  }
}

async function resolveTool(request: PermissionRequest, cached: ToolArgs | undefined, client?: OpencodeClientV2) {
  if (cached) return { tool: cached.tool, input: cached.args }
  if (request.generation === "v2" && request.callID && client) {
    try {
      const result = await client.v2.session.messages(
        { sessionID: request.sessionID, limit: 50, order: "desc" },
        { throwOnError: true },
      )
      const message = request.messageID
        ? result.data.data.find((item) => item.id === request.messageID)
        : result.data.data.find((item) => item.type === "assistant")
      const part = message?.content?.find((item) => item.type === "tool" && item.id === request.callID)
      const input = parsedInput(part?.state?.input)
      if (part?.name && input) return { tool: part.name, input }
    } catch {
      // The permission event still contains enough information for a conservative fallback.
    }
  }
  return { tool: request.permission, input: permissionToolInput(request) }
}

async function createCurrentClient(serverUrl: URL | undefined, cwd: string) {
  if (!serverUrl) return
  try {
    const sdk = await import("@opencode-ai/sdk/v2")
    const serverPassword = process.env.OPENCODE_SERVER_PASSWORD
    const username = process.env.OPENCODE_SERVER_USERNAME ?? "opencode"
    return sdk.createOpencodeClient({
      baseUrl: serverUrl.toString(),
      directory: cwd,
      headers: serverPassword
        ? { Authorization: `Basic ${Buffer.from(`${username}:${serverPassword}`).toString("base64")}` }
        : undefined,
    }) as unknown as OpencodeClientV2
  } catch {
    return
  }
}

function accepted(result: unknown) {
  return !result || typeof result !== "object" || !("error" in result) || !result.error
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
  const clientV2 = pluginInput.clientV2 ?? await createCurrentClient(pluginInput.serverUrl, cwd)
  const showStatus = async (
    title: string,
    message: string,
    variant: "info" | "success" | "warning" | "error",
    duration: number,
  ) => {
    try {
      await pluginInput.client.tui?.showToast({
        body: { title, message, variant, duration },
        query: { directory: cwd },
      })
    } catch {
      // Headless clients and older opencode releases may lack TUI toasts.
    }
  }
  const reply = async (input: {
    generation: "v1" | "v2"
    sessionID: string
    requestID: string
    reply: "once" | "reject"
    message?: string
  }) => {
    try {
      if (input.generation === "v2") {
        if (!clientV2) return false
        await clientV2.v2.session.permission.reply(
          {
            sessionID: input.sessionID,
            requestID: input.requestID,
            reply: input.reply,
            message: input.message,
          },
          { throwOnError: true },
        )
        return true
      }
      if (clientV2) {
        await clientV2.permission.reply(
          {
            requestID: input.requestID,
            directory: cwd,
            reply: input.reply,
            message: input.message,
          },
          { throwOnError: true },
        )
        return true
      }
      const result = await pluginInput.client.postSessionIdPermissionsPermissionId({
        path: {
          id: input.sessionID,
          permissionID: input.requestID,
        },
        body: {
          response: input.reply,
        },
      })
      return accepted(result)
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
      event: { type: string; properties?: PermissionRequestV1 | PermissionRequestV2 | PermissionReply }
    }): Promise<void> => {
      if (input.event.type === "permission.replied" || input.event.type === "permission.v2.replied") {
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
      const request = permissionRequest(input.event.type, input.event.properties)
      if (!request) return
      observedRequests.set(request.id, Date.now())

      const cached = request.callID
        ? toolArgs.get(key(request.sessionID, request.callID))
        : undefined
      const resolved = await resolveTool(request, cached, clientV2)
      const tool = resolved.tool
      const toolInput = resolved.input
      const target = request.patterns?.[0]

      await showStatus(
        "Bouncer reviewing",
        `Analyzing ${tool}${target ? `: ${target}` : ""}. `
          + "No action is needed yet.",
        "info",
        60_000,
      )

      writeReplyAudit({
        event: "permission_asked",
        cwd,
        session_id: request.sessionID,
        permission_id: request.id,
        generation: request.generation,
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
      if (result.decision === "ask" || result.decision === "skip") {
        await showStatus(
          "Bouncer deferred",
          "Review complete. This permission decision is now yours.",
          "warning",
          60_000,
        )
        return
      }

      const remaining = configuredDelayMs - (Date.now() - started)
      if (remaining > 0) await sleep(remaining)

      const response = result.decision === "allow" ? "once" : "reject"
      const replyResult = reply({
        generation: request.generation,
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
      if (accepted) {
        await showStatus(
          result.decision === "allow"
            ? "Approved by Bouncer"
            : "Denied by Bouncer",
          result.decision === "allow"
            ? "OpenCode is continuing."
            : "The operation was blocked by policy.",
          result.decision === "allow" ? "success" : "error",
          2_500,
        )
      } else {
        await showStatus(
          "Bouncer reply not used",
          "The permission request is still pending or was answered elsewhere.",
          "info",
          5_000,
        )
      }
      writeReplyAudit({
        event: accepted ? "permission_replied" : "bouncer_reply_failed",
        source: "bouncer",
        outcome: accepted ? "accepted" : "not_accepted",
        cwd,
        session_id: request.sessionID,
        permission_id: request.id,
        generation: request.generation,
        response,
      })
    },
  }
}
