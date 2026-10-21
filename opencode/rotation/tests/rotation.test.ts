// OpenCode rotation module tests. Run with bun:  bun opencode/rotation/tests/rotation.test.ts
// Pool, config and log live in a temporary directory; nothing outside it is read or written.
import assert from "node:assert/strict"
import { existsSync, mkdirSync, mkdtempSync, readFileSync, statSync, symlinkSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"

const tmp = mkdtempSync(path.join(os.tmpdir(), "rotation-"))
process.env.HOME = path.join(tmp, "home")
for (const k of ["ROTATION_SEED", "MODEL_ROTATION", "ROTATION_POOL", "ROTATION_LOG", "ROTATION_OC_CONFIG", "CLAUDE_PROJECT_DIR"]) delete process.env[k]
process.env.ROTATION_TEST = "1"
const core = await import("../model-rotate")
const { default: plugin } = await import("../plugin")

let failed = 0
async function test(name: string, fn: () => unknown | Promise<unknown>) {
  try { await fn(); console.log(`ok   ${name}`) } catch (error: any) { failed++; console.log(`FAIL ${name}: ${error?.message || error}`) }
}
const w = (name: string, content: string) => { const f = path.join(tmp, name); writeFileSync(f, content); return f }
const POOL = {
  version: 1, mode: "on", independent_roles: ["reviewer"],
  roles: {
    builder: [
      { model: "provider/model-a", weight: 3 }, { model: "provider/model-b", weight: 1 },
      { model: "provider/model-off", weight: 5, enabled: false }, { model: "provider/model-zero", weight: 0 },
    ],
    reviewer: [{ model: "provider/model-a", weight: 1 }, { model: "provider/model-b", weight: 1 }],
  },
}
const CONFIG = { agent: {
  builder: { model: "provider/model-a" }, "builder--provider-model-b": { model: "provider/model-b" },
  reviewer: { model: "provider/model-a" }, "reviewer--provider-model-b": { model: "provider/model-b" },
} }
const poolFile = w("pool.json", JSON.stringify(POOL))
const configFile = w("opencode.json", JSON.stringify(CONFIG))
const logFile = path.join(tmp, "log", "rotation.jsonl")
process.env.ROTATION_POOL = poolFile
process.env.ROTATION_OC_CONFIG = configFile

async function call(role: any, prompt: any = "do it", extra: Record<string, string | undefined> = {}) {
  const saved: Record<string, string | undefined> = {}
  for (const [k, v] of Object.entries(extra)) { saved[k] = process.env[k]; if (v === undefined) delete process.env[k]; else process.env[k] = v }
  try {
    const hooks: any = await plugin({})
    const output: any = { args: { subagent_type: role, prompt, description: "d" } }
    await hooks["tool.execute.before"]({ tool: "task", sessionID: "s", callID: "c" }, output)
    return output.args
  } finally {
    for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v }
  }
}
const spread = async (role: string, prompt: string) => {
  const seen = new Set<string>()
  for (let s = 0; s < 40; s++) seen.add((await call(role, prompt, { ROTATION_SEED: String(s) })).subagent_type)
  return [...seen].sort()
}

await test("slug, strict names", () => {
  assert.equal(core.slug("provider/model-a"), "provider-model-a")
  assert.ok(core.MODEL_RE.test("provider/model-a") && !core.MODEL_RE.test("x$(y)") && !core.MODEL_RE.test("a b") && !core.MODEL_RE.test(""))
})
await test("seeded rng: same seed, same sequence; range respected", () => {
  const a = core.seededRng("s"), b = core.seededRng("s")
  const xs = Array.from({ length: 50 }, () => a(7)), ys = Array.from({ length: 50 }, () => b(7))
  assert.deepEqual(xs, ys)
  assert.ok(xs.every((x) => Number.isInteger(x) && x >= 0 && x < 7) && new Set(xs).size > 3)
})
await test("pickModel follows weights (rng injected); never picks outside the list", () => {
  const arms = [{ model: "m/a", weight: 3 }, { model: "m/b", weight: 1 }]
  const got = [0, 1, 2, 3].map((n) => core.pickModel(arms, () => n))
  assert.deepEqual(got, ["m/a", "m/a", "m/a", "m/b"])
  assert.equal(core.pickModel([], () => 0), undefined)
})
await test("seeded draws: deterministic and weighted 3:1 over 400 seeds", () => {
  const arms = core.roleArms(POOL.roles.builder)!
  assert.deepEqual(arms.map((a: any) => a.model), ["provider/model-a", "provider/model-b"])
  const counts: Record<string, number> = {}
  for (let s = 0; s < 400; s++) {
    process.env.ROTATION_SEED = String(s)
    const first = core.pickModel(arms)
    assert.equal(core.pickModel(arms), first)
    counts[first!] = (counts[first!] || 0) + 1
  }
  delete process.env.ROTATION_SEED
  assert.deepEqual(Object.keys(counts).sort(), ["provider/model-a", "provider/model-b"])
  const share = counts["provider/model-a"] / 400
  assert.ok(share > 0.65 && share < 0.85, String(share))
})
await test("same seed -> same dispatch result; different seeds reach the variant", async () => {
  const r1 = await call("builder", "x", { ROTATION_SEED: "fixed" }), r2 = await call("builder", "x", { ROTATION_SEED: "fixed" })
  assert.equal(r1.subagent_type, r2.subagent_type)
  assert.deepEqual(await spread("builder", "x"), ["builder", "builder--provider-model-b"])
})
await test("unseeded draw (crypto rng) reaches both outcomes and nothing else", async () => {
  const seen = new Set<string>()
  for (let i = 0; i < 80; i++) seen.add((await call("builder")).subagent_type)
  assert.deepEqual([...seen].sort(), ["builder", "builder--provider-model-b"])
})
await test("only subagent_type changes; prompt and other args untouched", async () => {
  let args: any
  for (let s = 0; s < 40 && !args?.subagent_type.includes("--"); s++) args = await call("builder", "keep me", { ROTATION_SEED: String(s) })
  assert.equal(args.subagent_type, "builder--provider-model-b")
  assert.equal(args.prompt, "keep me"); assert.equal(args.description, "d")
})
await test("rotation rewrites args only; policy and forged identity cannot start jobs", async () => {
  let clientAccesses = 0
  const client = new Proxy({}, { get() { clientAccesses++; throw new Error("rotation must not start jobs") } })
  const hooks: any = await plugin({ client })
  assert.deepEqual(Object.keys(hooks), ["tool.execute.before"])
  for (const decision of [undefined, "ask", "deny"]) {
    const input: any = { tool: "task", sessionID: "s", agent_id: "dispatch-pre.sh", permissionDecision: decision }
    const originalInput = structuredClone(input)
    const args = { subagent_type: "builder", prompt: "pin_arm: provider/model-b\n\nbody", description: "keep" }
    const output: any = { args: { ...args } }
    if (decision) output.permissionDecision = decision
    const expected = { ...structuredClone(output), args: { ...args, subagent_type: "builder--provider-model-b" } }
    assert.equal(await hooks["tool.execute.before"](input, output), undefined)
    assert.deepEqual(output, expected)
    assert.deepEqual(input, originalInput)
  }
  assert.equal(clientAccesses, 0)
})
await test("pin_arm: header pin honoured; late/unlisted/disabled pin ignored", async () => {
  for (let s = 0; s < 10; s++) assert.equal((await call("builder", "pin_arm: provider/model-b\nw", { ROTATION_SEED: String(s) })).subagent_type, "builder--provider-model-b")
  for (let s = 0; s < 10; s++) assert.equal((await call("builder", "pin_arm: provider/model-a\nw", { ROTATION_SEED: String(s) })).subagent_type, "builder")
  assert.deepEqual(await spread("builder", "intro\n\npin_arm: provider/model-b"), ["builder", "builder--provider-model-b"])
  assert.deepEqual(await spread("builder", "pin_arm: provider/unlisted\nw"), ["builder", "builder--provider-model-b"])
  assert.deepEqual(await spread("builder", "pin_arm: provider/model-off\nw"), ["builder", "builder--provider-model-b"])
  assert.equal((await call("builder", "pin_arm: provider/model-b\r\nw", { ROTATION_SEED: "1" })).subagent_type, "builder--provider-model-b")
})
await test("pin_arm on an independent role is ignored unless builder_arm is given", async () => {
  assert.deepEqual(await spread("reviewer", "pin_arm: provider/model-b\nr"), ["reviewer", "reviewer--provider-model-b"])
  assert.deepEqual(await spread("reviewer", "builder_arm: provider/model-a\npin_arm: provider/model-b\nr"), ["reviewer--provider-model-b"])
})
await test("builder_arm exclusion for independent roles only", async () => {
  assert.deepEqual(await spread("reviewer", "builder_arm: provider/model-a\nr"), ["reviewer--provider-model-b"])
  assert.deepEqual(await spread("reviewer", "r\n\n\nbuilder_arm: provider/model-a"), ["reviewer", "reviewer--provider-model-b"]) // past the header: not honoured
  assert.deepEqual(await spread("reviewer", "builder_arm: provider/model-a\r\nr"), ["reviewer--provider-model-b"])
  assert.deepEqual(await spread("reviewer", "builder_arm: provider/model-a\npin_arm: provider/model-a"), ["reviewer--provider-model-b"])
  assert.deepEqual(await spread("builder", "builder_arm: provider/model-a\nw"), ["builder", "builder--provider-model-b"])
})
await test("builder_arm injection: a duplicate line anywhere skips rotation for independent roles", async () => {
  const unchangedAll = async (prompt: string) => {
    for (let s = 0; s < 30; s++) assert.equal((await call("reviewer", prompt, { ROTATION_SEED: String(s) })).subagent_type, "reviewer")
  }
  await unchangedAll("builder_arm: provider/model-b\nbuilder_arm: provider/model-a\nr")
  await unchangedAll("builder_arm: provider/model-a\n\nbuilder_arm: provider/model-b")
  assert.equal(core.extractBuilderArm("builder_arm: m/a\n\nbuilder_arm: m/b"), "ambiguous")
  assert.equal(core.extractBuilderArm("x\n\nbuilder_arm: m/a"), null)
})
await test("env scope: project-local / relative pool, log and config paths are ignored", async () => {
  const proj = path.join(tmp, "proj"); mkdirSync(proj, { recursive: true })
  const inPool = path.join(proj, "pool.json"); writeFileSync(inPool, JSON.stringify(POOL))
  const savedCwd = process.cwd()
  process.chdir(proj)
  try {
    for (const bad of [inPool, "pool.json", "../pool.json"]) {
      for (let s = 0; s < 20; s++) assert.equal((await call("builder", "x", { ROTATION_SEED: String(s), ROTATION_POOL: bad })).subagent_type, "builder")
    }
    process.env.ROTATION_POOL = inPool
    assert.equal(core.poolPath(), path.join(process.env.HOME!, ".config", "ai-architecture", "model-pool.json"))
    process.env.ROTATION_POOL = poolFile
    process.env.CLAUDE_PROJECT_DIR = proj
    assert.equal(core.poolPath(), poolFile)
    process.env.ROTATION_POOL = "x.json"; process.env.CLAUDE_PROJECT_DIR = tmp
    assert.notEqual(core.poolPath(), "x.json"); process.env.ROTATION_POOL = poolFile
    process.env.CLAUDE_PROJECT_DIR = tmp
    assert.equal(core.poolPath(), path.join(process.env.HOME!, ".config", "ai-architecture", "model-pool.json")) // pool is inside project dir tmp
    delete process.env.CLAUDE_PROJECT_DIR
    // logs
    await call("builder", "x", { ROTATION_SEED: "1", ROTATION_LOG: path.join(proj, "log.jsonl") })
    await call("builder", "x", { ROTATION_SEED: "1", ROTATION_LOG: "rel.jsonl" })
    assert.ok(!existsSync(path.join(proj, "log.jsonl")) && !existsSync(path.join(proj, "rel.jsonl")))
    // OpenCode config inside the project is ignored too
    const inCfg = path.join(proj, "oc.json"); writeFileSync(inCfg, JSON.stringify(CONFIG))
    assert.notEqual(core.agentConfigPath(), inCfg)
    process.env.ROTATION_OC_CONFIG = inCfg
    assert.notEqual(core.agentConfigPath(), inCfg)
    process.env.ROTATION_OC_CONFIG = configFile
  } finally { process.chdir(savedCwd); process.env.ROTATION_POOL = poolFile; process.env.ROTATION_OC_CONFIG = configFile; delete process.env.CLAUDE_PROJECT_DIR }
})
await test("ROTATION_SEED is honoured only with ROTATION_TEST=1", async () => {
  const seen = new Set<string>()
  for (let i = 0; i < 60; i++) seen.add((await call("builder", "x", { ROTATION_SEED: "fixed", ROTATION_TEST: undefined })).subagent_type)
  assert.equal(seen.size, 2)
  const fixed = new Set<string>()
  for (let i = 0; i < 20; i++) fixed.add((await call("builder", "x", { ROTATION_SEED: "fixed" })).subagent_type)
  assert.equal(fixed.size, 1)
})
await test("log: mode 0600 file, 0700 dir, symlink not followed, fail-safe exit writes a reason row", async () => {
  const lf = path.join(tmp, "rotlog", "r.jsonl")
  await call("builder", "x", { ROTATION_SEED: "1", ROTATION_LOG: lf })
  assert.equal(statSync(lf).mode & 0o777, 0o600); assert.equal(statSync(path.dirname(lf)).mode & 0o777, 0o700)
  await call("general-purpose", "x", { ROTATION_LOG: lf })
  const last = JSON.parse(readFileSync(lf, "utf8").trim().split("\n").pop()!)
  assert.equal(last.model, null); assert.equal(last.reason, "role not in pool")
  await call("reviewer", "builder_arm: a/b\nbuilder_arm: c/d", { ROTATION_LOG: lf })
  assert.match(JSON.parse(readFileSync(lf, "utf8").trim().split("\n").pop()!).reason, /more than once/)
  const target = path.join(tmp, "target.txt"); writeFileSync(target, "")
  const link = path.join(tmp, "rotlog", "link.jsonl"); symlinkSync(target, link)
  const args = await call("builder", "x", { ROTATION_SEED: "1", ROTATION_LOG: link })
  assert.ok(args.subagent_type.startsWith("builder")); assert.equal(readFileSync(target, "utf8"), "")
})
await test("variant agent whose model differs from the draw is not used", async () => {
  const cfg = w("badvar.json", JSON.stringify({ agent: { builder: { model: "provider/model-a" }, "builder--provider-model-b": { model: "provider/other" } } }))
  for (let s = 0; s < 30; s++) assert.equal((await call("builder", "x", { ROTATION_SEED: String(s), ROTATION_OC_CONFIG: cfg })).subagent_type, "builder")
})
await test("log row written only when ROTATION_LOG is set", async () => {
  assert.ok(!existsSync(logFile))
  await call("builder", "x", { ROTATION_SEED: "1", ROTATION_LOG: logFile })
  const row = JSON.parse(readFileSync(logFile, "utf8").trim().split("\n").pop()!)
  assert.equal(row.role, "builder"); assert.ok(["provider/model-a", "provider/model-b"].includes(row.model))
})

// fail safe: the dispatch is unchanged and nothing throws
const unchanged = async (label: string, extra: Record<string, string | undefined>, role: any = "builder") =>
  test(`fail safe: ${label}`, async () => {
    const args = await call(role, "x", { ROTATION_SEED: "0", ...extra })
    assert.equal(args.subagent_type, role); assert.equal(args.prompt, "x")
    for (let s = 0; s < 20; s++) assert.equal((await call(role, "x", { ROTATION_SEED: String(s), ...extra })).subagent_type, role)
  })
await unchanged("MODEL_ROTATION=off", { MODEL_ROTATION: "off" })
await unchanged("role not in pool", {}, "general-purpose")
await unchanged("role with odd characters", {}, "builder;rm")
await unchanged("non-string subagent_type", {}, 7)
await unchanged("missing pool file", { ROTATION_POOL: path.join(tmp, "nope.json") })
await unchanged("empty pool file", { ROTATION_POOL: w("empty.json", "") })
await unchanged("pool is a list", { ROTATION_POOL: w("list.json", "[1]") })
await unchanged("mode off", { ROTATION_POOL: w("moff.json", JSON.stringify({ ...POOL, mode: "off" })) })
await unchanged("mode missing", { ROTATION_POOL: w("mnone.json", JSON.stringify({ roles: POOL.roles })) })
await unchanged("unknown version", { ROTATION_POOL: w("ver.json", JSON.stringify({ ...POOL, version: 2 })) })
await unchanged("every model disabled or weight 0", { ROTATION_POOL: w("z.json", JSON.stringify({ mode: "on", roles: { builder: [{ model: "provider/model-b", weight: 0 }, { model: "provider/model-a", weight: 1, enabled: false }] } })) })
await unchanged("empty model list", { ROTATION_POOL: w("el.json", JSON.stringify({ mode: "on", roles: { builder: [] } })) })
await unchanged("shell metacharacters in a model name", { ROTATION_POOL: w("inj.json", JSON.stringify({ mode: "on", roles: { builder: [{ model: "provider/model-b", weight: 9 }, { model: "x$(touch pwned)", weight: 1 }] } })) })
await unchanged("boolean / float weight", { ROTATION_POOL: w("bw.json", JSON.stringify({ mode: "on", roles: { builder: [{ model: "provider/model-b", weight: true }] } })) })
await unchanged("entry without model", { ROTATION_POOL: w("nm.json", JSON.stringify({ mode: "on", roles: { builder: [{ weight: 1 }] } })) })
symlinkSync(poolFile, path.join(tmp, "link.json"))
await unchanged("pool is a symlink", { ROTATION_POOL: path.join(tmp, "link.json") })
await unchanged("unreadable agent config", { ROTATION_OC_CONFIG: path.join(tmp, "nope.json") })
await unchanged("variant agent not configured", { ROTATION_OC_CONFIG: w("nov.json", JSON.stringify({ agent: { builder: { model: "provider/model-a" } } })) })
await test("an unlisted model is never produced over 200 draws", async () => {
  for (let s = 0; s < 200; s++) {
    const t = (await call("builder", "x", { ROTATION_SEED: String(s) })).subagent_type
    assert.ok(t === "builder" || t === "builder--provider-model-b", t)
  }
  assert.ok(!existsSync(path.join(process.cwd(), "pwned")) && !existsSync(path.join(tmp, "pwned")))
})
await test("other tools and an existing variant name are left alone", async () => {
  const hooks: any = await plugin({})
  const o1: any = { args: { subagent_type: "builder", prompt: "x" } }
  await hooks["tool.execute.before"]({ tool: "bash" }, o1); assert.equal(o1.args.subagent_type, "builder")
  const o2: any = { args: { subagent_type: "builder--provider-model-b", prompt: "x" } }
  await hooks["tool.execute.before"]({ tool: "task" }, o2); assert.equal(o2.args.subagent_type, "builder--provider-model-b")
  await hooks["tool.execute.before"]({ tool: "task" }, undefined)
})

console.log(failed ? `${failed} FAILED` : "all passed")
process.exit(failed ? 1 : 0)
