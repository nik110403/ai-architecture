import { runContinuity } from "./bridge"
import { createContinuityRuntime } from "./continuity-core"
import { createHub } from "./hub-core"
import { hubTool } from "./hub-tool"

// OpenCode plugin entry for the continuity runtime (opt-in; see README.md).
export default async function plugin(input: any) {
  const run = (args: string[], stdin?: string) => runContinuity(args, stdin)
  const runtime = createContinuityRuntime({ client: input.client, directory: input.directory, run })
  const hub = createHub({ client: input.client, directory: input.directory, run, budgets: runtime.childBudget, pollMs: 2000 })
  return { ...runtime.hooks, ...(hub ? { tool: { hub: hubTool(hub) } } : {}) }
}
