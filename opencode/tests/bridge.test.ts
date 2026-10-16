// Bridge unit tests. Run with bun:  bun opencode/tests/bridge.test.ts
// They spawn the real gates hooks from plugins/gates with an empty temporary HOME.
import assert from "node:assert/strict"
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"

const tmpHome = mkdtempSync(path.join(os.tmpdir(), "bridge-home-"))
process.env.HOME = tmpHome
delete process.env.AIARCH_CONFIG
const work = path.join(tmpHome, "work")
mkdirSync(path.join(work, ".git"), { recursive: true })

const core = await import("../bridge/hooks-core")
const structural = await import("../bridge/bash-structural")
const { createBashStructural } = await import("../bridge/bash-structural-hook")

let failed = 0
async function test(name: string, fn: () => unknown | Promise<unknown>) {
  try { await fn(); console.log(`ok   ${name}`) } catch (error: any) { failed++; console.log(`FAIL ${name}: ${error?.message || error}`) }
}
async function denies(p: Promise<unknown>, pattern?: RegExp) {
  try { await p } catch (error: any) { if (pattern) assert.match(String(error?.message), pattern); return }
  assert.fail("expected the call to be denied")
}

// ---- pure helpers ----
await test("decideGuard: empty output allows, deny/ask/crash/timeout/garbage block", () => {
  assert.equal(core.decideGuard({ exitCode: 0, stdout: "", stderr: "" }).allow, true)
  const deny = JSON.stringify({ hookSpecificOutput: { permissionDecision: "deny", permissionDecisionReason: "no" } })
  assert.deepEqual(core.decideGuard({ exitCode: 0, stdout: deny, stderr: "" }), { allow: false, reason: "no" })
  const ask = JSON.stringify({ hookSpecificOutput: { permissionDecision: "ask" } })
  assert.equal(core.decideGuard({ exitCode: 0, stdout: ask, stderr: "" }).allow, false)
  assert.equal(core.decideGuard({ exitCode: 1, stdout: "", stderr: "boom" }).allow, false)
  assert.equal(core.decideGuard({ exitCode: 124, stdout: "", stderr: "", timedOut: true }).allow, false)
  assert.equal(core.decideGuard({ exitCode: 0, stdout: "not json", stderr: "" }).allow, false)
  assert.equal(core.decideGuard({ exitCode: 0, stdout: "{}", stderr: "" }).allow, false)
})
await test("matchesMatcher: anchored alternation", () => {
  assert.equal(core.matchesMatcher("Edit|Write", "Edit"), true)
  assert.equal(core.matchesMatcher("Edit|Write", "MultiEdit"), false)
  assert.equal(core.matchesMatcher("", "Bash"), true)
})
await test("matchesMatcher: an uncompilable matcher runs the hook (fail closed)", () => {
  assert.equal(core.matchesMatcher("Edit(", "Bash"), true)
})
await test("every path-scoped tool id has a matcher name", () => {
  for (const tool of core.PATH_SCOPED_TOOLS) assert.ok(core.MATCHER_TOOL_NAMES[tool], tool)
})
await test("hooks.json of the gates plugin is bridged (all four events)", () => {
  const config = core.loadHookConfig()
  for (const event of ["PreToolUse", "Stop", "SessionStart"]) assert.ok(config[event]?.length, event)
  assert.ok(config.PreToolUse.every(entry => entry.failClosed))
  assert.ok(config.PreToolUse.some(entry => core.hookBasename(entry.command) === "bash-destructive-guard.sh"))
})
await test("claudePayload: bash and edit shapes", () => {
  const bash = core.claudePayload("bash", { command: "ls" }, work, "s1")
  assert.deepEqual([bash.tool_name, (bash.tool_input as any).command], ["Bash", "ls"])
  const edit = core.claudePayload("edit", { filePath: "a.py", oldString: "x", newString: "y" }, work, "s1", "builder") as any
  assert.equal(edit.tool_input.file_path, path.join(work, "a.py"))
  assert.equal(edit.tool_input.new_string, "y")
  assert.equal(edit.agent_type, "builder")
})

// ---- structural bash classifier (pure, no parser needed) ----
await test("isReadOnly: allow-list and path-escape rejection", () => {
  assert.equal(structural.isReadOnly(["ls"]), true)
  assert.equal(structural.isReadOnly(["ls", "-la"]), true)
  assert.equal(structural.isReadOnly(["ls", "../secret"]), false)
  assert.equal(structural.isReadOnly(["ls", "/etc"]), false)
  assert.equal(structural.isReadOnly(["rm", "-rf", "x"]), false)
  assert.equal(structural.isReadOnly(["tail", "-f", "log"]), false)
})
await test("isReadOnly: no git diff/show (diff.external / textconv run code) and no file readers (symlink exfil)", () => {
  for (const t of [["git", "diff"], ["git", "diff", "--cached"], ["git", "diff", "--stat"], ["git", "show"], ["git", "show", "--stat"],
    ["cat", "link"], ["head", "link"], ["tail", "link"], ["grep", "x", "link"], ["wc", "link"], ["diff", "a", "b"], ["sha256sum", "link"]]) {
    assert.equal(structural.isReadOnly(t), false, t.join(" "))
  }
  // security review: git status/log run repo-local filter programs, so they are asked, not auto-approved
  for (const t of [["git", "status"], ["git", "status", "--short"], ["git", "log"], ["git", "log", "--oneline", "-5"]]) {
    assert.equal(structural.isReadOnly(t), false, t.join(" "))
  }
})
await test("isReadOnly: ls/du/stat/readlink are not auto-approved on a path operand (a symlink may point outside the project)", () => {
  for (const t of [["ls", "src"], ["ls", "link"], ["ls", "-la", "link"], ["du", "link"], ["du", "-L", "link"], ["stat", "link"], ["stat", "-L", "link"],
    ["readlink", "link"], ["readlink", "-f", "link"],
    ["ls", "-LR"], ["ls", "-lL"], ["ls", "-H"], ["ls", "-R"], ["realpath", "link"], ["df", "link"], ["df"], ["realpath"]]) {
    assert.equal(structural.isReadOnly(t), false, t.join(" "))
  }
})
await test("modelGatesConfigured: AIARCH_CONFIG inside the project is ignored, one outside is honoured", () => {
  const outside = path.join(tmpHome, "outside-gates.json")
  writeFileSync(outside, JSON.stringify({ no_code_models: ["planner"] }))
  const inProject = path.join(process.cwd(), `.gates-test-${process.pid}.json`)
  writeFileSync(inProject, JSON.stringify({ no_code_models: ["planner"] }))
  const saved = process.env.AIARCH_CONFIG
  try {
    process.env.AIARCH_CONFIG = inProject
    assert.equal(core.modelGatesConfigured(), false)
    process.env.AIARCH_CONFIG = outside
    assert.equal(core.modelGatesConfigured(), true)
  } finally {
    if (saved === undefined) delete process.env.AIARCH_CONFIG; else process.env.AIARCH_CONFIG = saved
    rmSync(inProject, { force: true })
  }
})
await test("gatesRoot: AIARCH_GATES_ROOT inside the project is ignored", () => {
  const dir = path.join(process.cwd(), `.gates-root-test-${process.pid}`)
  const saved = process.env.AIARCH_GATES_ROOT
  try {
    delete process.env.AIARCH_GATES_ROOT
    const def = core.gatesRoot()
    process.env.AIARCH_GATES_ROOT = dir
    assert.equal(core.gatesRoot(), def) // nonexistent path under cwd is still inside
    process.env.AIARCH_GATES_ROOT = path.join(tmpHome, "elsewhere-gates")
    assert.equal(core.gatesRoot(), path.join(tmpHome, "elsewhere-gates"))
  } finally { if (saved === undefined) delete process.env.AIARCH_GATES_ROOT; else process.env.AIARCH_GATES_ROOT = saved }
})
await test("gatesRoot is resolved once and re-resolved only when AIARCH_GATES_ROOT changes", () => {
  const first = core.gatesRoot()
  assert.equal(core.gatesRoot(), first)
  process.env.AIARCH_GATES_ROOT = "/x/gates"
  assert.equal(core.gatesRoot(), "/x/gates")
  delete process.env.AIARCH_GATES_ROOT
  assert.equal(core.gatesRoot(), first)
})
await test("parser failure means no auto-approve: classify errors leave the permission unanswered", async () => {
  let replied = 0
  const client = { postSessionIdPermissionsPermissionId: async () => { replied++; return {} } }
  const hook = createBashStructural({ client }, { loadParser: async () => { throw new Error("no parser") }, log: () => {} })
  await hook.event({ event: { type: "permission.asked", properties: { id: "p1", sessionID: "s", permission: "bash", metadata: { command: "git status" }, tool: { callID: "c1" } } } })
  assert.equal(replied, 0)
})
await test("bash structural hook: zero-pattern command denied, others pass (stub parser)", async () => {
  const hook = createBashStructural({}, {
    loadParser: async () => ({}),
    classifySync: (_p, c) => ({ approve: false, reason: "", zeroPattern: c === "", parseError: false }),
    log: () => {},
  })
  await denies(hook["tool.execute.before"]({ tool: "bash" }, { args: { command: "" } }), /no permission pattern/)
  await hook["tool.execute.before"]({ tool: "bash" }, { args: { command: "ls" } })
  await hook["tool.execute.before"]({ tool: "read" }, { args: {} })
})
await test("bash structural hook: parser load failure fails open, classify error denies", async () => {
  const open = createBashStructural({}, { loadParser: async () => { throw new Error("no parser") }, log: () => {} })
  await open["tool.execute.before"]({ tool: "bash" }, { args: { command: "ls" } })
  const closed = createBashStructural({}, { loadParser: async () => ({}), classifySync: () => { throw new Error("bad tree") }, log: () => {} })
  await denies(closed["tool.execute.before"]({ tool: "bash" }, { args: { command: "ls" } }))
})
await test("shell.env appends git hardening after existing entries", async () => {
  const hook = createBashStructural({}, { loadParser: async () => ({}), log: () => {} })
  const output: any = { env: { GIT_CONFIG_COUNT: "1", GIT_CONFIG_KEY_0: "a.b", GIT_CONFIG_VALUE_0: "c" } }
  await hook["shell.env"]({}, output)
  assert.equal(output.env.GIT_CONFIG_COUNT, "6")
  assert.equal(output.env.GIT_CONFIG_KEY_1, "core.fsmonitor")
  assert.equal(output.env.GIT_CONFIG_KEY_0, "a.b")
})

// ---- bridge end to end: real gates hooks, honours deny ----
const bridge = await core.createGatesBridge({ client: {}, directory: work })
// OpenCode 1.18 passes no `agent` in tool.execute.before input, so neither does this helper.
const pre = (tool: string, args: any) => bridge["tool.execute.before"]({ tool, sessionID: "t1" }, { args })

await test("bridge: destructive Bash command is denied by the gates hook", async () => {
  await denies(pre("bash", { command: "rm -rf /" }), /BLOCKED|denied|root|destructive/i)
})
await test("bridge: harmless Bash command passes", async () => { await pre("bash", { command: "echo hello" }) })
await test("bridge: edit inside the work tree passes", async () => { await pre("write", { filePath: path.join(work, "notes.md"), content: "x" }) })
await test("bridge: edit under the agent config directory is refused before any hook", async () => {
  await denies(pre("write", { filePath: path.join(tmpHome, ".claude/settings.json"), content: "{}" }), /protected/)
})
await test("bridge: edit through a symlink is refused", async () => {
  const { symlinkSync } = await import("node:fs")
  symlinkSync("/tmp", path.join(work, "link"))
  await denies(pre("write", { filePath: path.join(work, "link/x.md"), content: "x" }), /symlink/)
})
await test("bridge: mutation under .git is refused", async () => {
  await denies(pre("write", { filePath: path.join(work, ".git/hooks/pre-commit"), content: "x" }), /\.git/)
  await denies(pre("write", { filePath: path.join(work, ".GIT/config"), content: "x" }), /\.git/)
  await denies(pre("write", { filePath: path.join(path.dirname(work), ".git/info/attributes"), content: "x" }), /repository's \.git directory/)
})
await test("bridge: protected root is matched case-insensitively", async () => {
  await denies(pre("write", { filePath: path.join(tmpHome, ".Claude/settings.json"), content: "{}" }), /protected/)
})
await test("bridge: path with .. is refused", async () => {
  await denies(pre("write", { filePath: `${work}/a/../b.md`, content: "x" }), /noncanonical/)
})
await test("bridge: user-scope config makes the no-code gate deny a code write", async () => {
  const cfgDir = path.join(tmpHome, ".config/ai-architecture")
  mkdirSync(cfgDir, { recursive: true })
  writeFileSync(path.join(cfgDir, "gates.json"), JSON.stringify({ no_code_models: ["planner-model"] }))
  await denies(pre("write", { filePath: path.join(work, "app.py"), content: "print(1)\n".repeat(30) }))
  await pre("write", { filePath: path.join(work, "notes.md"), content: "docs stay allowed" })
})
await test("bridge: model gates configured => code edits denied even when HOME has a settings.json model", async () => {
  mkdirSync(path.join(tmpHome, ".claude"), { recursive: true })
  writeFileSync(path.join(tmpHome, ".claude/settings.json"), JSON.stringify({ model: "some-other-model" }))
  await denies(pre("write", { filePath: path.join(work, "app.py"), content: "print(1)\n" }), /cannot identify the model/)
  await denies(pre("edit", { filePath: path.join(work, "app.py"), oldString: "a", newString: "b" }), /cannot identify the model/)
  await pre("write", { filePath: path.join(work, "notes.md"), content: "docs stay allowed" })
})
await test("bridge: hooks.json that does not load denies every PreToolUse call", async () => {
  const saved = process.env.AIARCH_GATES_ROOT
  process.env.AIARCH_GATES_ROOT = path.join(tmpHome, "no-such-gates")
  try {
    const dead = await core.createGatesBridge({ client: {}, directory: work })
    await denies(dead["tool.execute.before"]({ tool: "read", sessionID: "t4" }, { args: { filePath: "x" } }), /AIARCH_GATES_ROOT/)
    await denies(dead["tool.execute.before"]({ tool: "bash", sessionID: "t4" }, { args: { command: "ls" } }), /AIARCH_GATES_ROOT/)
  } finally { if (saved === undefined) delete process.env.AIARCH_GATES_ROOT; else process.env.AIARCH_GATES_ROOT = saved }
  const empty = await core.createGatesBridge({ client: {}, directory: work }, {})
  await denies(empty["tool.execute.before"]({ tool: "bash", sessionID: "t4" }, { args: { command: "ls" } }), /AIARCH_GATES_ROOT/)
})
await test("bridge: a hook that crashes denies (fail closed)", async () => {
  const broken = await core.createGatesBridge({ client: {}, directory: work }, { PreToolUse: [{ matcher: "Bash", command: "exit 3", failClosed: true }] })
  await denies(broken["tool.execute.before"]({ tool: "bash", sessionID: "t2" }, { args: { command: "ls" } }), /exited 3/)
})
await test("bridge: SessionStart context is spliced into the first turn", async () => {
  const fake = await core.createGatesBridge({ client: {}, directory: work }, {
    PreToolUse: [{ matcher: "Bash", command: "true", failClosed: true }],
    SessionStart: [{ matcher: "", command: `printf '{"hookSpecificOutput":{"additionalContext":"CTX"}}'`, failClosed: true }],
  })
  const output: any = { message: { id: "m1" }, parts: [] }
  await fake["chat.message"]({ sessionID: "t3" }, output)
  assert.match(output.parts[0].text, /CTX/)
  const again: any = { message: { id: "m2" }, parts: [] }
  await fake["chat.message"]({ sessionID: "t3" }, again)
  assert.equal(again.parts.length, 0)
})

console.log(failed ? `${failed} FAILED` : "bridge tests passed")
process.exit(failed ? 1 : 0)
