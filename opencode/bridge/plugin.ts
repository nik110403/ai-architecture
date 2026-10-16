import { createGatesBridge } from "./hooks-core"
import { createBashStructural } from "./bash-structural-hook"

// OpenCode plugin entry: bridges the gates plugin's hooks and the structural bash check.
// Only the default export takes part in automatic plugin discovery.
export default async function plugin(input: any) {
  const guards = await createGatesBridge(input)
  const structural = createBashStructural(input)
  return {
    "chat.message": guards["chat.message"],
    "shell.env": structural["shell.env"],
    "tool.execute.before": async (event: any, output: any) => {
      // Zero-pattern bash deny first (fails open only when the parser cannot load), then the gates hooks.
      await structural["tool.execute.before"](event, output)
      await guards["tool.execute.before"](event, output)
    },
    "tool.execute.after": async (event: any, output: any) => {
      try { await guards["tool.execute.after"](event, output) } catch {
        output.metadata = { ...output.metadata, hookTelemetry: "unavailable" }
      }
    },
    event: async (event: any) => {
      await guards.event(event)
      // Auto-approver for structurally read-only bash commands (internally never throws).
      await structural.event(event)
    },
  }
}
