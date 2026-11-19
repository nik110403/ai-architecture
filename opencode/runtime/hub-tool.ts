// The `hub` tool for OpenCode: a thin wrapper over hub-core.ts (needs @opencode-ai/plugin, which OpenCode provides).
// Not unit tested: the behaviour lives in hub-core.ts and is tested there with a fake client.
import { tool } from "@opencode-ai/plugin"

export function hubTool(hub: { execute: (args: any, callerID: string) => Promise<unknown> }) {
  return tool({
    description:
      "Start and manage background READ-ONLY child sessions. Actions: start (agent, prompt, optional budget_tokens), status (optional job_id), wait (job_id, optional timeout_ms), cancel (job_id). Write roles are refused: use dispatch.py.",
    args: {
      action: tool.schema.enum(["start", "status", "wait", "cancel"]),
      agent: tool.schema.string().optional(),
      prompt: tool.schema.string().optional(),
      title: tool.schema.string().optional(),
      job_id: tool.schema.string().optional(),
      budget_tokens: tool.schema.number().int().positive().optional(),
      timeout_ms: tool.schema.number().int().positive().optional(),
    },
    async execute(args: any, ctx: any) {
      try {
        return JSON.stringify(await hub.execute(args, ctx.sessionID))
      } catch (error: any) {
        return JSON.stringify({ error: error?.message || String(error) })
      }
    },
  })
}
