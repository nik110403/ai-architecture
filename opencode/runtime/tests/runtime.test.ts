// OpenCode continuity runtime tests. Run with bun:  bun opencode/runtime/tests/runtime.test.ts
// A fake client stands in for OpenCode; the Python CLI is the real one, run in a temporary project with a
// temporary HOME and AI_ARCH_FREEZE_DIR. No model and no OpenCode server is ever started.
import assert from "node:assert/strict"
import { spawn, spawnSync } from "node:child_process"
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, symlinkSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { CONTINUITY, ledgerFile, runContinuity, type Run } from "../bridge"
import { createContinuityRuntime, decideFreeze, freezeRecordPath, loadFreeze } from "../continuity-core"
import { createHub, HARD_CAP, SOFT_CAP } from "../hub-core"

const tmp = realpathSync(mkdtempSync(path.join(os.tmpdir(), "runtime-")))
process.env.HOME = path.join(tmp, "home")
process.env.AI_ARCH_FREEZE_DIR = path.join(tmp, "freeze")
delete process.env.AI_ARCH_JOB_MARKER
delete process.env.XDG_STATE_HOME
mkdirSync(process.env.HOME, { recursive: true })

// a loaded machine can take longer than the 5 s production timeout to start Python
const real: Run = (args, input) => runContinuity(args, input, { timeoutMs: 60_000 })
let failed = 0
async function test(name: string, fn: () => unknown | Promise<unknown>) {
  try { await fn(); console.log(`ok   ${name}`) } catch (error: any) { failed++; console.log(`FAIL ${name}: ${error?.stack || error}`) }
}

// --- helpers ------------------------------------------------------------------------------------------------------
let n = 0
function project(init = true): string {
  const dir = path.join(tmp, `proj${++n}`)
  mkdirSync(dir, { recursive: true })
  writeFileSync(path.join(dir, "ev.txt"), "evidence\n")
  if (init) assert.equal(py(["init", "--project", dir]).code, 0)
  return dir
}
function py(args: string[]): { code: number; json: any } {
  const r = spawnSync("python3", ["-B", CONTINUITY, ...args], { encoding: "utf8", env: process.env })
  let json: any = null
  try { json = JSON.parse(r.stdout) } catch { /* leave null */ }
  return { code: r.status ?? -1, json }
}
const checkpoint = (dir: string, next: string) => assert.ok(py(["checkpoint", "--project", dir, "--state", "S", "--next", next, "--evidence", "ev.txt"]).json.revision >= 1)
const sessionView = (dir: string, id: string) => py(["status", "--project", dir, "--session", `opencode:${id}`]).json

function counting(inner: Run = real) {
  const calls: string[][] = []
  const run: Run = async (args, input) => { calls.push(args); return inner(args, input) }
  return { run, calls }
}

function fakeClient(opts: { parents?: Record<string, string> } = {}) {
  const parents: Record<string, string> = { ...(opts.parents ?? {}) }
  const busy: Record<string, { type: string }> = {}
  const messages: Record<string, any[]> = {}
  const order: string[] = []
  const aborted: string[] = []
  let count = 0
  const client: any = {
    session: {
      get: async ({ path: p }: any) => ({ data: { id: p.id, ...(parents[p.id] ? { parentID: parents[p.id] } : {}) } }),
      create: async ({ body }: any) => { const id = `sess-child${++count}x${n}`; parents[id] = body.parentID; order.push("create"); return { data: { id } } },
      promptAsync: async ({ path: p }: any) => { order.push("promptAsync"); busy[p.id] = { type: "busy" } },
      status: async () => ({ data: { ...busy } }),
      messages: async ({ path: p }: any) => ({ data: messages[p.id] ?? [] }),
      abort: async ({ path: p }: any) => { aborted.push(p.id); delete busy[p.id] },
    },
    config: {
      get: async () => ({ data: { agent: {
        researcher: { permission: { edit: "deny", bash: "deny" } },
        refuter: { permission: { edit: "deny", bash: { "*": "ask" } } },
        tribune: { permission: { edit: "deny", bash: "allow" } },
        "security-reviewer": { permission: { edit: "ask", bash: "deny" } },
        implementer: { permission: { edit: "deny", bash: "deny" } },
      } } }),
      providers: async () => ({ data: { providers: [{ id: "prov", models: { big: { limit: { context: 100000 } } } }] } }),
    },
  }
  const finish = (child: string, text: string, error = false) => {
    delete busy[child]
    messages[child] = [{ info: { role: "assistant", time: { completed: 1 }, ...(error ? { error: { name: "x" } } : {}) }, parts: [{ type: "text", text }] }]
  }
  return { client, finish, order, aborted, busy, parents }
}
const done = (sessionID: string, id: string, tokens: any) => ({
  type: "message.updated",
  properties: { info: { id, sessionID, role: "assistant", providerID: "prov", modelID: "big", time: { completed: 1 }, tokens } },
})
const full = (input: number, output: number, read = 0, write = 0) => ({ input, output, reasoning: 0, cache: { read, write } })
const runtimeFor = (dir: string, client: any, run: Run = real, env: any = process.env) =>
  createContinuityRuntime({ client, directory: dir, run, env, log: () => {} })
const denies = async (fn: () => Promise<unknown>) => { try { await fn(); return null } catch (e: any) { return String(e.message) } }

// --- usage ---------------------------------------------------------------------------------------------------------
await test("usage: a root session's completed message is recorded (sum of the four counts, limit from the provider)", async () => {
  const dir = project()
  const { client } = fakeClient()
  const rt = runtimeFor(dir, client)
  await rt.hooks.event({ event: done("sess-root", "m1", full(1000, 200, 30, 4)) })
  const v = sessionView(dir, "sess-root")
  assert.equal(v.usage_tokens, 1234)
  assert.equal(v.limit_tokens, 100000)
  assert.equal(v.phase, "NORMAL")
})

await test("usage: a message that is not completed or not from the assistant records nothing", async () => {
  const dir = project()
  const { client } = fakeClient()
  const rt = runtimeFor(dir, client)
  const open = done("sess-a", "m1", full(5, 5)); open.properties.info.time = {}
  await rt.hooks.event({ event: open })
  const user = done("sess-a", "m2", full(5, 5)); user.properties.info.role = "user"
  await rt.hooks.event({ event: user })
  assert.equal(sessionView(dir, "sess-a").usage_tokens, null)
})

await test("usage: a missing token field is recorded as unknown and ready reports usage UNKNOWN", async () => {
  const dir = project()
  const { client } = fakeClient()
  const rt = runtimeFor(dir, client)
  checkpoint(dir, "N")
  await rt.hooks.event({ event: done("sess-u", "m1", { input: 10, output: 5, cache: { read: 1 } }) })
  const v = sessionView(dir, "sess-u")
  assert.equal(v.usage_tokens, null)
  assert.equal(v.phase, "UNKNOWN")
  const ready = py(["ready", "--project", dir, "--session", "opencode:sess-u"])
  assert.equal(ready.json.ready, false)
  assert.ok(ready.json.reasons.includes("usage UNKNOWN"), JSON.stringify(ready.json))
})

await test("usage: a child session's usage is kept in memory, not recorded in the ledger", async () => {
  const dir = project()
  const { client } = fakeClient({ parents: { "sess-kid": "sess-root" } })
  const rt = runtimeFor(dir, client)
  await rt.hooks.event({ event: done("sess-kid", "m1", full(7, 3)) })
  assert.equal(rt.childUsage.get("sess-kid"), 10)
  assert.equal(sessionView(dir, "sess-kid").usage_tokens, null)
})

await test("no ledger: ledger features are inactive and nothing is run", async () => {
  const dir = project(false)
  const { client } = fakeClient()
  const { run, calls } = counting()
  const rt = runtimeFor(dir, client, run)
  await rt.hooks.event({ event: done("sess-x", "m1", full(1, 1)) })
  const out: any = { system: [] }
  await rt.hooks["experimental.chat.system.transform"]({ sessionID: "sess-x" }, out)
  assert.equal(await denies(() => rt.hooks["tool.execute.before"]({ tool: "task", sessionID: "sess-x" }, { args: {} })), null)
  assert.equal(calls.length, 0)
  assert.deepEqual(out.system, [])
})

// --- resume --------------------------------------------------------------------------------------------------------
await test("resume: consumed once across two runtime instances; the second gets no Next; the prefix is byte-stable", async () => {
  const dir = project()
  checkpoint(dir, "NEXTMARK finish the parser")
  const { client } = fakeClient()
  const first = runtimeFor(dir, client)
  const out1: any = { system: ["base"] }
  await first.hooks["experimental.chat.system.transform"]({ sessionID: "sess-one" }, out1)
  assert.equal(out1.system.length, 2)
  assert.ok(out1.system[1].includes("NEXTMARK finish the parser"), out1.system[1])
  const again: any = { system: ["base"] }
  await first.hooks["experimental.chat.system.transform"]({ sessionID: "sess-one" }, again)
  assert.deepEqual(again.system, out1.system)
  // a restart is a fresh instance; the new session is not the one that consumed the Next
  const second = runtimeFor(dir, client)
  const out2: any = { system: ["base"] }
  await second.hooks["experimental.chat.system.transform"]({ sessionID: "sess-two" }, out2)
  assert.ok(!out2.system.join("\n").includes("NEXTMARK"), out2.system.join("\n"))
  assert.equal(py(["resume", "show", "--project", dir]).json.consumed_by, "opencode:sess-one")
})

await test("resume: compaction rebuilds the cached injection with source compact and consumes nothing", async () => {
  const dir = project()
  checkpoint(dir, "NEXTMARK after compact")
  const { client } = fakeClient()
  const rt = runtimeFor(dir, client)
  await rt.hooks.event({ event: { type: "session.compacted", properties: { sessionID: "sess-c" } } })
  const out: any = { system: [] }
  await rt.hooks["experimental.chat.system.transform"]({ sessionID: "sess-c" }, out)
  assert.ok(out.system[0].includes("NEXTMARK after compact") && out.system[0].includes("pending"), out.system[0])
  assert.equal(py(["resume", "show", "--project", dir]).json.consumed_by, null)
})

await test("resume: a child session gets no injection", async () => {
  const dir = project()
  checkpoint(dir, "NEXTMARK")
  const { client } = fakeClient({ parents: { "sess-kid": "sess-root" } })
  const rt = runtimeFor(dir, client)
  const out: any = { system: [] }
  await rt.hooks["experimental.chat.system.transform"]({ sessionID: "sess-kid" }, out)
  assert.deepEqual(out.system, [])
})

// --- freeze --------------------------------------------------------------------------------------------------------
await test("freeze: set in Python, denied by the TS guard for that session only; bash denied; check agrees on a path table", async () => {
  const dir = project()
  const root = path.join(tmp, "frozen-root")
  const outside = path.join(tmp, "outside")
  mkdirSync(path.join(root, "sub"), { recursive: true })
  mkdirSync(path.join(root, ".claude"), { recursive: true })
  mkdirSync(outside, { recursive: true })
  writeFileSync(path.join(outside, "f.txt"), "x")
  symlinkSync(path.join(outside, "f.txt"), path.join(root, "link.txt"))
  symlinkSync(outside, path.join(root, "outdir"))
  const set = py(["freeze", "set", "--harness", "opencode", "--session", "sess-f", "--root", root, "--bash", "deny"])
  assert.equal(set.code, 0, JSON.stringify(set.json))
  const { client } = fakeClient()
  const rt = runtimeFor(dir, client)
  const before = (tool: string, sessionID: string, args: any = {}) => rt.hooks["tool.execute.before"]({ tool, sessionID }, { args })
  assert.ok(await denies(() => before("write", "sess-f", { filePath: path.join(outside, "new.txt") })))
  assert.ok(await denies(() => before("bash", "sess-f", { command: "ls" })))
  assert.equal(await denies(() => before("write", "sess-f", { filePath: path.join(root, "ok.txt") })), null)
  assert.equal(await denies(() => before("write", "sess-other", { filePath: path.join(outside, "new.txt") })), null)
  assert.equal(await denies(() => before("bash", "sess-other", { command: "ls" })), null)
  assert.ok(await denies(() => before("apply_patch", "sess-f", { patchText: `*** Begin Patch\n*** Update File: ${path.join(outside, "f.txt")}\n*** End Patch` })))
  assert.equal(await denies(() => before("apply_patch", "sess-f", { patchText: `*** Begin Patch\n*** Add File: ${path.join(root, "p.txt")}\n*** End Patch` })), null)
  assert.ok(await denies(() => before("edit", "sess-f", {})), "no usable path denies")
  assert.equal(await denies(() => before("read", "sess-f", { filePath: path.join(outside, "f.txt") })), null)

  const record = loadFreeze(process.env.AI_ARCH_FREEZE_DIR!, "sess-f")!
  const table = [
    path.join(root, "a.txt"), path.join(root, "sub", "..", "b.txt"), path.join(root, "..", "out.txt"), "/etc/passwd", root,
    path.join(root, "link.txt"), path.join(root, "outdir", "x.txt"), path.join(root, "new", "deep", "f.txt"),
    path.join(root, ".claude", "settings.json"), path.join(root, ".claude", "settings.local.json"), path.join(root, ".claude", "other.json"),
    path.join(root, "sub", ".claude", "settings.json"), path.join(process.env.AI_ARCH_FREEZE_DIR!, "x.json"),
    "~/x", "$HOME/x", path.join(root, "a$b"), root + "-sibling/x", path.join(root, "sub") + "/",
  ]
  for (const p of table) {
    for (const tool of ["write", "edit"]) {
      const ref = py(["freeze", "check", "--harness", "opencode", "--session", "sess-f", "--tool", tool, "--path", p]).json
      const mine = decideFreeze(record, tool, p, undefined, process.env.AI_ARCH_FREEZE_DIR!)
      assert.equal(mine.allowed ? "allow" : "deny", ref.decision, `${tool} ${p}: TS ${mine.reason} vs CLI ${ref.reason}`)
    }
  }
  assert.equal(py(["freeze", "check", "--harness", "opencode", "--session", "sess-f", "--tool", "bash"]).json.decision, "deny")
})

await test("freeze: an unreadable or foreign record denies write and bash for that session", async () => {
  const dir = project()
  const { client } = fakeClient()
  const rt = runtimeFor(dir, client)
  mkdirSync(process.env.AI_ARCH_FREEZE_DIR!, { recursive: true })
  writeFileSync(freezeRecordPath(process.env.AI_ARCH_FREEZE_DIR!, "sess-bad"), "{not json")
  assert.ok(await denies(() => rt.hooks["tool.execute.before"]({ tool: "write", sessionID: "sess-bad" }, { args: { filePath: path.join(tmp, "x") } })))
  assert.ok(await denies(() => rt.hooks["tool.execute.before"]({ tool: "bash", sessionID: "sess-bad" }, { args: { command: "ls" } })))
})

await test("freeze: --bash unscoped leaves bash allowed; edits outside are still denied", async () => {
  const dir = project()
  const root = path.join(tmp, "frozen-root2")
  mkdirSync(root, { recursive: true })
  assert.equal(py(["freeze", "set", "--harness", "opencode", "--session", "sess-un", "--root", root, "--bash", "unscoped"]).code, 0)
  const rt = runtimeFor(dir, fakeClient().client)
  assert.equal(await denies(() => rt.hooks["tool.execute.before"]({ tool: "bash", sessionID: "sess-un" }, { args: {} })), null)
  assert.ok(await denies(() => rt.hooks["tool.execute.before"]({ tool: "write", sessionID: "sess-un" }, { args: { filePath: "/tmp/elsewhere" } })))
})

// --- handoff gate, marker, child budget -----------------------------------------------------------------------------
await test("HANDOFF blocks task and hub start; other sessions and tools pass", async () => {
  const dir = project()
  assert.equal(py(["usage", "record", "--project", dir, "--session", "opencode:sess-h", "--tokens", "95000", "--limit", "100000"]).json.phase, "HANDOFF")
  const rt = runtimeFor(dir, fakeClient().client)
  const before = (tool: string, sessionID: string, args: any = {}) => rt.hooks["tool.execute.before"]({ tool, sessionID }, { args })
  assert.match((await denies(() => before("task", "sess-h")))!, /HANDOFF/)
  assert.match((await denies(() => before("hub", "sess-h", { action: "start" })))!, /HANDOFF/)
  assert.equal(await denies(() => before("hub", "sess-h", { action: "status" })), null)
  assert.equal(await denies(() => before("read", "sess-h")), null)
  assert.equal(await denies(() => before("task", "sess-fresh")), null)
})

await test("marker env disables the hooks, the hub and every CLI call", async () => {
  const dir = project()
  const { run, calls } = counting()
  const env = { ...process.env, AI_ARCH_JOB_MARKER: "job-1" }
  const { client } = fakeClient()
  const rt = runtimeFor(dir, client, run, env)
  assert.deepEqual(Object.keys(rt.hooks), [])
  assert.equal(createHub({ client, directory: dir, run, env, log: () => {} }), null)
  assert.equal(calls.length, 0)
})

await test("child budget: a hub child at or over its budget gets a stop error; below it passes", async () => {
  const dir = project()
  const { client } = fakeClient({ parents: { "sess-kid": "sess-root" } })
  const rt = runtimeFor(dir, client)
  rt.childBudget.set("sess-kid", 1000)
  await rt.hooks.event({ event: done("sess-kid", "m1", full(400, 100)) })
  assert.equal(await denies(() => rt.hooks["tool.execute.before"]({ tool: "read", sessionID: "sess-kid" }, { args: {} })), null)
  await rt.hooks.event({ event: done("sess-kid", "m2", full(900, 200)) })
  assert.match((await denies(() => rt.hooks["tool.execute.before"]({ tool: "read", sessionID: "sess-kid" }, { args: {} })))!, /partial result/)
  assert.equal(await denies(() => rt.hooks["tool.execute.before"]({ tool: "read", sessionID: "sess-root" }, { args: {} })), null)
})

// --- hub -----------------------------------------------------------------------------------------------------------
function hubFor(dir: string, fake = fakeClient(), run: Run = real, extra: any = {}) {
  const hub = createHub({ client: fake.client, directory: dir, run, log: () => {}, sleep: async () => {}, pid: process.pid, ...extra })!
  return { hub, fake }
}
const ledgerJobs = (dir: string) => py(["status", "--project", dir]).json.jobs
const ROOT = "sess-root"

await test("hub: refuses a write role and a role whose bash is allow, with the dispatch.py pointer for writers", async () => {
  const dir = project()
  const { hub, fake } = hubFor(dir)
  assert.match((await denies(() => hub.start({ agent: "implementer", prompt: "x" }, ROOT)))!, /use dispatch\.py: write roles run in an isolated worktree/)
  assert.match((await denies(() => hub.start({ agent: "tribune", prompt: "x" }, ROOT)))!, /permission\.bash/)
  assert.match((await denies(() => hub.start({ agent: "security-reviewer", prompt: "x" }, ROOT)))!, /permission\.edit/)
  assert.match((await denies(() => hub.start({ agent: "nonexistent", prompt: "x" }, ROOT)))!, /unknown role/)
  assert.deepEqual(fake.order, [])
  assert.deepEqual(ledgerJobs(dir), {})
})

await test("hub: permission.bash must be deny/ask or an object whose \"*\" is deny/ask", async () => {

  const cases: [any, boolean][] = [[{}, false], [{ "rm *": "deny" }, false], ["allow", false], [{ "*": "allow" }, false], ["deny", true], [{ "*": "ask", ls: "allow" }, true]]
  for (const [bash, ok] of cases) {
    const fake = fakeClient()
    const cfg = fake.client.config.get
    fake.client.config.get = async () => { const r: any = await cfg(); r.data.agent.researcher.permission.bash = bash; return r }
    const { hub } = hubFor(project(), fake)
    const err = await denies(() => hub.start({ agent: "researcher", prompt: "x" }, ROOT))
    if (ok) assert.equal(err, null, `accepted: ${JSON.stringify(bash)} (${err})`)
    else assert.match(err!, /permission\.bash/, `refused: ${JSON.stringify(bash)}`)
  }
})

await test("hub: unreadable roles file or agent config refuses (empty allowlist)", async () => {
  const dir = project()
  const { hub } = hubFor(dir, fakeClient(), real, { rolesPath: path.join(tmp, "missing-roles.json") })
  assert.match((await denies(() => hub.start({ agent: "researcher", prompt: "x" }, ROOT)))!, /roles file unreadable/)
  const broken = fakeClient()
  broken.client.config.get = async () => { throw new Error("down") }
  const again = hubFor(dir, broken)
  assert.match((await denies(() => again.hub.start({ agent: "researcher", prompt: "x" }, ROOT)))!, /config unreadable/)
})

await test("hub: only a root session may start jobs", async () => {
  const dir = project()
  const { hub } = hubFor(dir, fakeClient({ parents: { "sess-kid": ROOT } }))
  assert.match((await denies(() => hub.start({ agent: "researcher", prompt: "x" }, "sess-kid")))!, /only a root session/)
})

await test("hub: the job is registered in the ledger before the prompt is sent", async () => {
  const dir = project()
  const fake = fakeClient()
  const order = fake.order
  const run: Run = async (args, input) => {
    if (args[0] === "external" && args[1] === "register") order.push("register")
    return real(args, input)
  }
  const { hub } = hubFor(dir, fake, run)
  const started = await hub.start({ agent: "researcher", prompt: "look", budget_tokens: 5000 }, ROOT)
  assert.deepEqual(order, ["create", "register", "promptAsync"])
  assert.equal(ledgerJobs(dir)[started.job_id].state, "launched")
  assert.equal(ledgerJobs(dir)[started.job_id].owner.session_id, ROOT)
})

await test("hub: a failed register aborts the child, fails the start and never prompts", async () => {
  const dir = project()
  const fake = fakeClient()
  const { hub } = hubFor(dir, fake, async (args, input) => (args[1] === "register" ? { code: 2, json: { error: "draining" } } : real(args, input)))
  assert.match((await denies(() => hub.start({ agent: "researcher", prompt: "x" }, ROOT)))!, /ledger register failed/)
  assert.deepEqual(fake.order, ["create"])
  assert.equal(fake.aborted.length, 1)
  assert.deepEqual(ledgerJobs(dir), {})
})

await test("hub: a finished job is banked exactly once even when wait and status both see it", async () => {
  const dir = project()
  const { run, calls } = counting()
  const { hub, fake } = hubFor(dir, fakeClient(), run)
  const started = await hub.start({ agent: "researcher", prompt: "look" }, ROOT)
  assert.equal(ledgerJobs(dir)[started.job_id].state, "launched")
  fake.finish(started.session_id, "FINDINGS 42")
  const [w, s] = await Promise.all([hub.wait({ job_id: started.job_id }), hub.status({ job_id: started.job_id })])
  assert.equal(w.state, "finished")
  assert.equal(s.jobs[0].banked, true)
  await hub.status({})
  await hub.wait({ job_id: started.job_id })
  assert.equal(calls.filter((a) => a[0] === "external" && a[1] === "bank").length, 1)
  const row = ledgerJobs(dir)[started.job_id]
  assert.deepEqual([row.state, row.outcome, row.cause], ["banked", "succeeded", "completed"])
  assert.match(w.result, /FINDINGS 42/)
})

await test("hub: a failed bank leaves the job open and a later status retries it", async () => {
  const dir = project()
  let fail = true
  const { hub, fake } = hubFor(dir, fakeClient(), async (args, input) => (fail && args[1] === "bank" ? { code: 4, json: { error: "storage" } } : real(args, input)))
  const started = await hub.start({ agent: "researcher", prompt: "look" }, ROOT)
  fake.finish(started.session_id, "done")
  const first = await hub.status({ job_id: started.job_id })
  assert.equal(first.jobs[0].banked, false)
  assert.ok(first.jobs[0].bank_error)
  assert.equal(ledgerJobs(dir)[started.job_id].state, "launched")
  fail = false
  assert.equal((await hub.status({ job_id: started.job_id })).jobs[0].banked, true)
  assert.equal(ledgerJobs(dir)[started.job_id].state, "banked")
})

await test("hub: cancel aborts the child and banks cancelled once", async () => {
  const dir = project()
  const { run, calls } = counting()
  const { hub, fake } = hubFor(dir, fakeClient(), run)
  const started = await hub.start({ agent: "researcher", prompt: "look" }, ROOT)
  const c = await hub.cancel({ job_id: started.job_id })
  await hub.cancel({ job_id: started.job_id })
  assert.equal(c.state, "cancelled")
  assert.deepEqual(fake.aborted, [started.session_id])
  assert.equal(calls.filter((a) => a[1] === "bank").length, 1)
  const row = ledgerJobs(dir)[started.job_id]
  assert.deepEqual([row.state, row.outcome, row.cause], ["banked", "failed", "cancelled"])
})

await test("hub: a job from a previous process is aborted and banked orphaned_external", async () => {
  const dir = project()
  const fake = fakeClient()
  const first = hubFor(dir, fake).hub
  const started = await first.start({ agent: "researcher", prompt: "look" }, ROOT)
  // simulated restart: a new hub instance for the same project; the first instance's memory is gone
  const second = hubFor(dir, fake).hub
  await second.status({})
  assert.deepEqual(fake.aborted, [started.session_id])
  const row = ledgerJobs(dir)[started.job_id]
  assert.deepEqual([row.state, row.outcome, row.cause], ["banked", "failed", "orphaned_external"])
  const third = hubFor(dir, fake).hub
  await third.status({})
  assert.equal(fake.aborted.length, 1, "a banked orphan is not swept again")
})

await test("hub: a job whose owner process is still alive is not swept by another instance", async () => {
  const dir = project()
  const fake = fakeClient()
  const sleeper = spawn("sleep", ["300"], { stdio: "ignore" })
  try {
    const peer = hubFor(dir, fake, real, { pid: sleeper.pid }).hub
    const started = await peer.start({ agent: "researcher", prompt: "look" }, ROOT)
    const mine = hubFor(dir, fake).hub
    await mine.status({})
    assert.deepEqual(fake.aborted, [], "a live peer's child is not aborted")
    assert.equal(ledgerJobs(dir)[started.job_id].state, "launched", "and not banked")
    const file = JSON.parse(readFileSync(path.join(dir, ".ai-continuity", "hub-jobs.json"), "utf8"))
    assert.ok(file.jobs[started.job_id], "the live peer's entry survives the other instance's sweep and persist")
    sleeper.kill("SIGKILL")
    await new Promise((r) => sleeper.once("exit", r))
    await hubFor(dir, fake).hub.status({})
    assert.deepEqual(fake.aborted, [started.session_id], "after the peer dies a later sweep aborts its child")
    const row = ledgerJobs(dir)[started.job_id]
    assert.deepEqual([row.state, row.outcome, row.cause], ["banked", "failed", "orphaned_external"])
  } finally { sleeper.kill("SIGKILL") }
})

await test("hub: a job whose owner process has exited is swept as failed/orphaned_external", async () => {
  const dir = project()
  const fake = fakeClient()
  const sleeper = spawn("sleep", ["300"], { stdio: "ignore" })
  const exited = new Promise((r) => sleeper.once("exit", r))
  const peer = hubFor(dir, fake, real, { pid: sleeper.pid }).hub
  const started = await peer.start({ agent: "researcher", prompt: "look" }, ROOT)
  sleeper.kill("SIGKILL")
  await exited
  await hubFor(dir, fake).hub.status({})
  assert.deepEqual(fake.aborted, [started.session_id])
  const row = ledgerJobs(dir)[started.job_id]
  assert.deepEqual([row.state, row.outcome, row.cause], ["banked", "failed", "orphaned_external"])
})

// --- orphan sweep: unswept entries of dead peers (U04d fix 3) ---------------------------------------------------------
const hubFile = (dir: string) => path.join(dir, ".ai-continuity", "hub-jobs.json")
const hubEntries = (dir: string): Record<string, any> => JSON.parse(readFileSync(hubFile(dir), "utf8")).jobs
async function killed(p: ReturnType<typeof spawn>) {
  const gone = new Promise((r) => p.once("exit", r))
  p.kill("SIGKILL")
  await gone
}
const hasBank = (calls: string[][]) => calls.filter((a) => a[0] === "external" && a[1] === "bank").length

await test("hub: a dead peer's entry survives a live hub's later persist and is banked once, never swept twice", async () => {
  const dir = project()
  const fake = fakeClient()
  const a = spawn("sleep", ["300"], { stdio: "ignore" })
  const b = spawn("sleep", ["300"], { stdio: "ignore" })
  try {
    const peerA = hubFor(dir, fake, real, { pid: a.pid }).hub
    const a1 = await peerA.start({ agent: "researcher", prompt: "look" }, ROOT)
    const hubB = hubFor(dir, fake, real, { pid: b.pid }).hub
    await hubB.status({}) // B sweeps while A is alive: nothing to do
    await killed(a)
    await hubB.start({ agent: "researcher", prompt: "more" }, ROOT) // B persists after A died, unswept
    assert.ok(hubEntries(dir)[a1.job_id], "B's persist keeps the dead peer's unbanked entry")
    await hubFor(dir, fake).hub.status({})
    assert.deepEqual(fake.aborted, [a1.session_id], "C aborts a1 exactly once")
    const row = ledgerJobs(dir)[a1.job_id]
    assert.deepEqual([row.state, row.outcome, row.cause], ["banked", "failed", "orphaned_external"])
    assert.equal(hubEntries(dir)[a1.job_id], undefined, "a1 is gone from hub-jobs.json once C banked it")
    await hubFor(dir, fake).hub.status({})
    assert.equal(fake.aborted.length, 1, "D makes no further abort")
    assert.equal(hubEntries(dir)[a1.job_id], undefined, "a1 is gone from hub-jobs.json")
  } finally { a.kill("SIGKILL"); b.kill("SIGKILL") }
})

await test("hub: an orphan whose ledger row is already banked is dropped without a CLI call or an abort", async () => {
  const dir = project()
  const fake = fakeClient()
  const a = spawn("sleep", ["300"], { stdio: "ignore" })
  const peer = hubFor(dir, fake, real, { pid: a.pid }).hub
  const a1 = await peer.start({ agent: "researcher", prompt: "look" }, ROOT)
  await killed(a)
  const res = path.join(dir, "res.txt")
  writeFileSync(res, "banked elsewhere\n")
  assert.equal(py(["external", "bank", "--project", dir, "--job-id", a1.job_id, "--outcome", "failed", "--cause", "orphaned_external", "--result-file", res]).code, 0)
  const spool = path.join(dir, ".ai-continuity", "ledger", "spool", `${a1.job_id}.external.txt`)
  const before = readFileSync(spool)
  const { run, calls } = counting()
  await hubFor(dir, fake, run).hub.status({})
  assert.equal(hasBank(calls), 0)
  assert.deepEqual(fake.aborted, [])
  assert.deepEqual(readFileSync(spool), before, "the spool evidence is untouched")
  assert.equal(hubEntries(dir)[a1.job_id], undefined, "the entry is removed")
})

await test("hub: a failed first bank of an orphan is retried as orphaned_external, not cancelled", async () => {
  const dir = project()
  const fake = fakeClient()
  const a = spawn("sleep", ["300"], { stdio: "ignore" })
  const peer = hubFor(dir, fake, real, { pid: a.pid }).hub
  const a1 = await peer.start({ agent: "researcher", prompt: "look" }, ROOT)
  await killed(a)
  let failedOnce = false
  const flaky: Run = async (args, input) => {
    if (args[0] === "external" && args[1] === "bank" && !failedOnce) { failedOnce = true; return { code: 1, json: { error: "boom" } } as any }
    return real(args, input)
  }
  const hub = hubFor(dir, fake, flaky).hub
  await hub.status({})
  assert.ok(failedOnce, "the first bank call failed")
  await hub.status({}) // the retry
  const row = ledgerJobs(dir)[a1.job_id]
  assert.deepEqual([row.state, row.outcome, row.cause], ["banked", "failed", "orphaned_external"])
})

await test("hub: an unreadable ledger defers the orphan: no abort, no bank, entry kept; readable again it is banked", async () => {
  if (process.getuid?.() === 0) return // chmod 000 does not stop root
  const dir = project()
  const fake = fakeClient()
  const a = spawn("sleep", ["300"], { stdio: "ignore" })
  const peer = hubFor(dir, fake, real, { pid: a.pid }).hub
  const a1 = await peer.start({ agent: "researcher", prompt: "look" }, ROOT)
  await killed(a)
  const lf = ledgerFile(dir)
  chmodSync(lf, 0)
  try {
    const { run, calls } = counting()
    await hubFor(dir, fake, run).hub.status({})
    await hubFor(dir, fake, run).hub.status({}) // another persist
    assert.equal(hasBank(calls), 0)
    assert.deepEqual(fake.aborted, [])
    assert.ok(hubEntries(dir)[a1.job_id], "the entry is still in the file")
  } finally { chmodSync(lf, 0o600) }
  await hubFor(dir, fake).hub.status({})
  assert.deepEqual(fake.aborted, [a1.session_id])
  const row = ledgerJobs(dir)[a1.job_id]
  assert.deepEqual([row.state, row.outcome, row.cause], ["banked", "failed", "orphaned_external"])
})

const ledgerCancel = (dir: string, id: string) => py(["cancel", "--project", dir, "--job", id, "--session", `opencode:${ROOT}`])

await test("hub: a ledger cancel request during a running child aborts it and banks failed/cancelled, never succeeded", async () => {
  const dir = project()
  const { hub, fake } = hubFor(dir)
  const started = await hub.start({ agent: "researcher", prompt: "look" }, ROOT)
  assert.equal((await hub.status({ job_id: started.job_id })).jobs[0].state, "running")
  const c = ledgerCancel(dir, started.job_id)
  assert.equal(c.json.signalled, false)
  const seen = (await hub.status({ job_id: started.job_id })).jobs[0]
  assert.deepEqual([seen.state, seen.banked], ["cancelled", true])
  assert.deepEqual(fake.aborted, [started.session_id])
  fake.finish(started.session_id, "late text")
  await hub.status({})
  const row = ledgerJobs(dir)[started.job_id]
  assert.deepEqual([row.state, row.outcome, row.cause], ["banked", "failed", "cancelled"])
})

await test("hub: the background poll honours a ledger cancel without any hub call", async () => {
  const dir = project()
  const { hub, fake } = hubFor(dir, fakeClient(), real, { pollMs: 50 })
  const started = await hub.start({ agent: "researcher", prompt: "look" }, ROOT)
  ledgerCancel(dir, started.job_id)
  for (let i = 0; i < 100 && ledgerJobs(dir)[started.job_id].state !== "banked"; i++) await new Promise((r) => setTimeout(r, 100))
  hub.stop()
  const row = ledgerJobs(dir)[started.job_id]
  assert.deepEqual([row.state, row.outcome, row.cause], ["banked", "failed", "cancelled"])
  assert.deepEqual(fake.aborted, [started.session_id])
})

await test("hub: a ledger cancel after the child completed keeps the succeeded record", async () => {
  const dir = project()
  const { hub, fake } = hubFor(dir)
  const a = await hub.start({ agent: "researcher", prompt: "a" }, ROOT)
  const b = await hub.start({ agent: "researcher", prompt: "b" }, ROOT)
  fake.finish(a.session_id, "A done")
  fake.finish(b.session_id, "B done")
  await hub.status({ job_id: a.job_id }) // a is banked before the flag
  ledgerCancel(dir, a.job_id)
  ledgerCancel(dir, b.job_id) // b completed but was not yet observed: completion is seen first
  await hub.status({})
  for (const id of [a.job_id, b.job_id]) {
    const row = ledgerJobs(dir)[id]
    assert.deepEqual([row.state, row.outcome, row.cause], ["banked", "succeeded", "completed"])
  }
  assert.deepEqual(fake.aborted, [])
})

await test("hub: a child that ends with an error is banked failed, not succeeded", async () => {
  const dir = project()
  const { hub, fake } = hubFor(dir)
  const started = await hub.start({ agent: "researcher", prompt: "look" }, ROOT)
  fake.finish(started.session_id, "partial", true)
  await hub.status({})
  const row = ledgerJobs(dir)[started.job_id]
  assert.deepEqual([row.state, row.outcome], ["banked", "failed"])
})

await test("hub: caps are per coordinator session (soft flag at 4, refused at 15)", async () => {
  const dir = project()
  const hub = hubFor(dir, fakeClient(), async (args) => ({ code: 0, json: {} })).hub
  const flags: boolean[] = []
  for (let i = 0; i < HARD_CAP; i++) flags.push(!!(await hub.start({ agent: "researcher", prompt: "x" }, ROOT)).over_soft_cap)
  assert.deepEqual(flags.slice(0, SOFT_CAP), [false, false, false, false])
  assert.ok(flags.slice(SOFT_CAP).every(Boolean))
  assert.match((await denies(() => hub.start({ agent: "researcher", prompt: "x" }, ROOT)))!, /hard cap/)
  assert.equal((await hub.start({ agent: "researcher", prompt: "x" }, "sess-other_root")).over_soft_cap, undefined)
})

// --- bridge failure ------------------------------------------------------------------------------------------------
await test("bridge failure (python missing): ledger unchanged, hub start denied, task goes through", async () => {
  const dir = project()
  const noPython: Run = (args, input) => runContinuity(args, input, { env: { ...process.env, PATH: path.join(tmp, "empty-bin") } })
  assert.deepEqual(await noPython(["status"]), { code: -1, json: null })
  const before = py(["status", "--project", dir]).json
  const fake = fakeClient()
  const rt = runtimeFor(dir, fake.client, noPython)
  assert.equal(await denies(() => rt.hooks["tool.execute.before"]({ tool: "task", sessionID: "sess-b" }, { args: {} })), null)
  assert.match((await denies(() => rt.hooks["tool.execute.before"]({ tool: "hub", sessionID: "sess-b" }, { args: { action: "start" } })))!, /hub start refused/)
  await rt.hooks.event({ event: done("sess-b", "m1", full(5, 5)) })
  const { hub } = hubFor(dir, fake, noPython)
  assert.match((await denies(() => hub.start({ agent: "researcher", prompt: "x" }, ROOT)))!, /ledger register failed/)
  assert.deepEqual(fake.order, ["create"])
  assert.deepEqual(py(["status", "--project", dir]).json, before)
})

console.log(failed ? `${failed} FAILED` : "all passed")
process.exit(failed ? 1 : 0)
