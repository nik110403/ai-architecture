// Test helper (not a test): load a pool through the OpenCode rotation module and report what it sees.
// Usage: bun meval_bun_check.ts <pool> <opencode-config> ; env ROTATION_POOL / ROTATION_OC_CONFIG are set by the caller.
import { readFileSync } from "node:fs"
const core = await import("../../../opencode/rotation/model-rotate")
const { default: plugin } = await import("../../../opencode/rotation/plugin")
const pool = JSON.parse(readFileSync(process.argv[2], "utf8"))
const arms: Record<string, unknown> = {}
const picks: Record<string, string[]> = {}
for (const role of Object.keys(pool.roles)) {
  const a = core.roleArms(pool.roles[role]) ?? null
  arms[role] = a
  if (a) {
    const total = a.reduce((s: number, x: any) => s + x.weight, 0)
    picks[role] = Array.from({ length: total }, (_, n) => core.pickModel(a, () => n)!)
  }
}
const hooks: any = await plugin({})
const output: any = { args: { subagent_type: "builder", prompt: "pin_arm: provider/model-b\n\ndo it", description: "d" } }
await hooks["tool.execute.before"]({ tool: "task", sessionID: "s", callID: "c" }, output)
console.log(JSON.stringify({ arms, picks, pinned: output.args.subagent_type }))
