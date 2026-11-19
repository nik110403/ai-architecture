// A4: broken hooks through the OpenCode bridge. Fixture hooks.json files call the failing .py directly, so
// decideGuard's timeout / non-zero / malformed-output branches really run. A fail-closed hook that cannot run
// blocks the tool call; a hook proven advisory is allowed and logged; an unusable hooks.json denies everything.
import assert from "node:assert/strict"
import { chmodSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"

const tmpHome = mkdtempSync(path.join(os.tmpdir(), "u11-bridge-"))
process.env.HOME = tmpHome
process.env.AIARCH_HOOKS_TIMEOUT_MS = "4000" // read at import time: must be set before the dynamic import
delete process.env.AIARCH_CONFIG
process.env.AIARCH_ADVISORY_HOOKS = "raises.py" // no env var may be able to fill ADVISORY_HOOKS
const work = path.join(tmpHome, "work")
mkdirSync(path.join(work, ".git"), { recursive: true })
const core = await import("../bridge/hooks-core")

let failed = 0
async function test(name: string, fn: () => unknown | Promise<unknown>) {
  try { await fn(); console.log(`ok   ${name}`) } catch (error: any) { failed++; console.log(`FAIL ${name}: ${error?.message || error}`) }
}
async function denies(p: Promise<unknown>, pattern?: RegExp) {
  try { await p } catch (error: any) { if (pattern) assert.match(String(error?.message), pattern); return }
  assert.fail("expected the call to be denied")
}

const hooksDir = path.join(tmpHome, "hooks")
mkdirSync(hooksDir)
const scripts: Record<string, string> = {
  "raises.py": "raise RuntimeError('boom')\n",
  "sleeps.py": "import time\ntime.sleep(60)\n",
  "hello.py": "print('hello')\n",
  "oops.py": "print('{oops')\n",
  "array.py": "print('[]')\n",
  "unreadable.py": "print('{}')\n",
  "ok.py": "pass\n",
}
for (const [name, body] of Object.entries(scripts)) writeFileSync(path.join(hooksDir, name), body)
const isRoot = process.getuid?.() === 0
if (!isRoot) chmodSync(path.join(hooksDir, "unreadable.py"), 0)

function fixture(script: string, matcher = "Bash") {
  const file = path.join(tmpHome, `hooks-${script}.json`)
  writeFileSync(file, JSON.stringify({ hooks: { PreToolUse: [{ matcher, hooks: [{ type: "command", command: `python3 ${path.join(hooksDir, script)}`, timeout: 10 }] }] } }))
  return file
}
async function bridgeFor(script: string) {
  const config = core.loadHookConfig(fixture(script))
  return core.createGatesBridge({ client: {}, directory: work }, config)
}
const bash = (bridge: any, id: string) => bridge["tool.execute.before"]({ tool: "bash", sessionID: id }, { args: { command: "ls" } })

await test("ADVISORY_HOOKS is empty: no env var or config can fill it", () => {
  assert.deepEqual(core.ADVISORY_HOOKS, [])
  const config = core.loadHookConfig(fixture("raises.py"))
  assert.ok(config.PreToolUse.every(entry => entry.failClosed))
})
await test("a healthy hook still allows (non-vacuous)", async () => { await bash(await bridgeFor("ok.py"), "a0") })
await test("hook that raises -> tool call blocked", async () => { await denies(bash(await bridgeFor("raises.py"), "a1"), /exited 1/) })
await test("hook that sleeps past the cap -> tool call blocked (timeout branch)", async () => { await denies(bash(await bridgeFor("sleeps.py"), "a2"), /timed out/) })
await test("hook printing non-JSON -> tool call blocked", async () => { await denies(bash(await bridgeFor("hello.py"), "a3"), /malformed/) })
await test("hook printing a truncated JSON object -> tool call blocked", async () => { await denies(bash(await bridgeFor("oops.py"), "a4"), /malformed/) })
await test("hook printing a JSON array -> tool call blocked", async () => { await denies(bash(await bridgeFor("array.py"), "a5"), /lacks permissionDecision/) })
await test(isRoot ? "SKIP unreadable hook: running as root, mode 000 is not enforced (skipped, not passed)" : "unreadable hook (mode 000) -> tool call blocked", async () => {
  if (!isRoot) await denies(bash(await bridgeFor("unreadable.py"), "a6"), /exited/)
})
await test("empty PATH -> tool call blocked", async () => {
  const bridge = await bridgeFor("ok.py")
  const saved = process.env.PATH
  process.env.PATH = ""
  try { await denies(bash(bridge, "a7")) } finally { process.env.PATH = saved }
})
await test("an advisory hook that crashes is allowed and logged", async () => {
  core.ADVISORY_HOOKS.push("raises.py") // in-process injection only
  try {
    const bridge = await bridgeFor("raises.py")
    const logged: string[] = []
    const warn = console.warn
    console.warn = (...a: any[]) => { logged.push(a.join(" ")) }
    try { await bash(bridge, "a8") } finally { console.warn = warn }
    assert.ok(logged.some(l => /raises\.py exited 1; fail-open/.test(l)), logged.join("|"))
  } finally { core.ADVISORY_HOOKS.length = 0 }
})
await test("missing hooks.json denies every tool call", async () => {
  const saved = process.env.AIARCH_GATES_ROOT
  process.env.AIARCH_GATES_ROOT = path.join(tmpHome, "absent-gates")
  try {
    const dead = await core.createGatesBridge({ client: {}, directory: work })
    await denies(dead["tool.execute.before"]({ tool: "bash", sessionID: "b1" }, { args: { command: "ls" } }), /did not load/)
    await denies(dead["tool.execute.before"]({ tool: "read", sessionID: "b1" }, { args: { filePath: "x" } }), /did not load/)
  } finally { if (saved === undefined) delete process.env.AIARCH_GATES_ROOT; else process.env.AIARCH_GATES_ROOT = saved }
})
for (const [label, body] of [["unparseable", "{not json"], ["an array", "[]"], ["hooks without PreToolUse", '{"hooks":{"Stop":[]}}'], ["PreToolUse of the wrong type", '{"hooks":{"PreToolUse":"x"}}']]) {
  await test(`hooks.json ${label} denies every tool call`, async () => {
    const root = mkdtempSync(path.join(tmpHome, "gates-"))
    mkdirSync(path.join(root, "hooks"))
    writeFileSync(path.join(root, "hooks/hooks.json"), body)
    const saved = process.env.AIARCH_GATES_ROOT
    process.env.AIARCH_GATES_ROOT = root
    try {
      const dead = await core.createGatesBridge({ client: {}, directory: work })
      await denies(dead["tool.execute.before"]({ tool: "bash", sessionID: "b2" }, { args: { command: "ls" } }), /did not load/)
      await denies(dead["tool.execute.before"]({ tool: "read", sessionID: "b2" }, { args: { filePath: "x" } }), /did not load/)
    } finally { if (saved === undefined) delete process.env.AIARCH_GATES_ROOT; else process.env.AIARCH_GATES_ROOT = saved }
  })
}
if (!isRoot) chmodSync(path.join(hooksDir, "unreadable.py"), 0o644)
console.log(failed ? `${failed} FAILED` : "u11 bridge tests passed")
process.exit(failed ? 1 : 0)
