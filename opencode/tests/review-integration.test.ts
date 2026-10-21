// Targeted edit -> emitted JSONL -> real Python Stop hook integration.
import assert from "node:assert/strict"
import { appendFileSync, chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs"
import { spawnSync } from "node:child_process"
import os from "node:os"
import path from "node:path"

const home = mkdtempSync(path.join(os.tmpdir(), "bridge-review-home-"))
process.env.HOME = home
delete process.env.AIARCH_CONFIG
delete process.env.AIARCH_GATES_ROOT
const work = path.join(home, "work")
mkdirSync(work)
const core = await import("../bridge/hooks-core")
const { createBashStructural } = await import("../bridge/bash-structural-hook")
const { isReadOnly } = await import("../bridge/bash-structural")
let failures = 0
let checks = 0
let sequence = 0
async function test(name: string, fn: () => Promise<void>) {
  checks++
  try { await fn(); console.log(`ok   ${name}`) }
  catch (e: any) { failures++; console.log(`FAIL ${name}: ${e.stack || e}`) }
}
async function harness(deny = false) {
  const sessionID = `bridge-review-${++sequence}`
  const prompts: any[] = []
  const client = { session: {
    get: async () => ({ data: { id: sessionID } }),
    messages: async () => ({ data: [{ info: { role: "user", agent: "orchestrator" } }] }),
    promptAsync: async (p: any) => { prompts.push(p); return {} },
  } }
  const bridge = await core.createGatesBridge({ client, directory: work }, {
    PreToolUse: [{ matcher: "*", command: deny ? "exit 3" : "true", failClosed: true }],
    Stop: [{ matcher: "", command: 'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/review-stop-gate.py"', failClosed: true }],
  })
  async function call(tool: string, args: any, result: any = {}, complete = true) {
    const input = { tool, args, sessionID, callID: `call-${++sequence}` }
    await bridge["tool.execute.before"](input, { args })
    if (complete) await bridge["tool.execute.after"](input, result)
    return input
  }
  async function idle(want: RegExp | null, absent?: RegExp) {
    const start = prompts.length
    await bridge.event({ event: { type: "session.idle", properties: { sessionID } } })
    if (process.env.AIARCH_BRIDGE_EVENTS_DIR) {
      mkdirSync(process.env.AIARCH_BRIDGE_EVENTS_DIR, { recursive: true })
      const prefix = path.join(process.env.AIARCH_BRIDGE_EVENTS_DIR, `${sessionID}-${++sequence}`)
      writeFileSync(`${prefix}.jsonl`, readFileSync(path.join(home, ".cache/aiarch-bridge", `${sessionID}.events.jsonl`)))
      writeFileSync(`${prefix}.expected.json`, JSON.stringify({ cwd: work, transcript: `${prefix}.jsonl`, want: want ? "block" : "allow" }))
    }
    assert.equal(prompts.length - start, want ? 1 : 0)
    if (want) assert.match(prompts.at(-1).body.parts[0].text, want)
    if (absent) assert.doesNotMatch(prompts.at(-1).body.parts[0].text, absent)
  }
  const finish = (input: any, result: any) => bridge["tool.execute.after"](input, result)
  const append = (record: any) => appendFileSync(path.join(home, ".cache/aiarch-bridge", `${sessionID}.events.jsonl`), `${JSON.stringify(record)}\n`)
  return { call, idle, sessionID, finish, append }
}
const edits: [string, any][] = [
  ["edit", { filePath: "view.tsx", oldString: "a", newString: "b" }],
  ["write", { filePath: "view.tsx", content: "b" }],
  ["patch", { filePath: "view.tsx" }],
  ["apply_patch", { patchText: "*** Begin Patch\n*** Add File: notes.md\n+note\n*** Add File: view.tsx\n+view\n*** End Patch" }],
]
for (const [tool, args] of edits) {
  await test(`${tool}: successful emitted edit blocks idle`, async () => {
    const h = await harness(); await h.call(tool, args); await h.idle(/design-critic/)
    const events = readFileSync(path.join(home, ".cache/aiarch-bridge", `${h.sessionID}.events.jsonl`), "utf8")
    assert.match(events, /view.tsx/)
  })
  await test(`${tool}: denied edit leaves no review pending`, async () => {
    const h = await harness(true)
    await assert.rejects(h.call(tool, args), /exited 3/)
    await h.idle(null)
  })
  await test(`${tool}: failed edit leaves no review pending`, async () => {
    const h = await harness(); await h.call(tool, args, { failed: true }); await h.idle(null)
  })
}
await test("unresolved edit remains pending; no-op does not", async () => {
  const pending = await harness(); await pending.call("edit", edits[0][1], {}, false); await pending.idle(/design-critic/)
  const noop = await harness(); await noop.call("edit", { filePath: "view.tsx", oldString: "a", newString: "a" }); await noop.idle(null)
})
for (const tool of ["write", "apply_patch"]) {
  await test(`${tool}: actual oversized emitted mutation cannot vanish`, async () => {
    const h = await harness()
    const content = "x".repeat(2_000_001)
    await h.call(tool, tool === "write" ? { filePath: "view.tsx", content } : {
      patchText: `*** Begin Patch\n*** Add File: view.tsx\n+${content}\n*** End Patch`,
    })
    const lines = readFileSync(path.join(home, ".cache/aiarch-bridge", `${h.sessionID}.events.jsonl`), "utf8").trim().split("\n")
    assert.ok(lines.every(line => line.length > 2_000_000))
    await h.idle(/review/i)
  })
}
await test("unknown oversized record fails closed even after reviewers succeed", async () => {
  const h = await harness(); await h.call("task", { subagent_type: "design-critic" })
  await h.call("task", { subagent_type: "security-reviewer" })
  h.append({ type: "unknown", content: "x".repeat(2_000_001) })
  await h.idle(/review/i)
})
for (const [subagent_type, filePath, reason] of [
  ["design-critic", "view.tsx", /design-critic/],
  ["security-reviewer", "auth.py", /security-reviewer/],
] as const) {
  for (const tool of ["write", "apply_patch"]) {
    for (const outcome of ["missing", "failed", "denied", "succeeded"]) {
      await test(`${tool}: ${subagent_type} ${outcome} terminal correlation`, async () => {
        const h = await harness()
        const content = subagent_type === "design-critic" ? "view" : "token"
        await h.call(tool, tool === "write" ? { filePath, content } : {
          patchText: `*** Begin Patch\n*** Add File: ${filePath}\n+${content}\n*** End Patch`,
        })
        await h.idle(reason)
        const input = await h.call("task", { subagent_type }, {}, false)
        if (outcome === "failed") await h.finish(input, { success: false })
        if (outcome === "succeeded") await h.finish(input, { success: true })
        if (outcome === "denied") h.append({ type: "tool_use", id: input.callID, name: "Task", input: { subagent_type }, denied: true })
        await h.idle(outcome === "succeeded" ? null : reason)
      })
    }
    await test(`${tool}: legacy ID-less ${subagent_type} keeps unresolved-dispatch semantics`, async () => {
      const h = await harness()
      const content = subagent_type === "design-critic" ? "view" : "token"
      await h.call(tool, tool === "write" ? { filePath, content } : {
        patchText: `*** Begin Patch\n*** Add File: ${filePath}\n+${content}\n*** End Patch`,
      })
      h.append({ type: "tool_use", name: "Task", input: { subagent_type } })
      await h.idle(null)
    })
  }
}
for (const subagent_type of ["design-critic", "ai-architecture:design-critic", "evil-plugin:design-critic", "security-reviewer"]) {
  await test(`review dispatch ${subagent_type} clears only its intended requirement`, async () => {
    const h = await harness()
    await h.call("write", { filePath: "auth.tsx", content: "token" })
    await h.call("task", { subagent_type })
    // Both review requirements start pending. A trusted dispatch clears only its role.
    const design = ["design-critic", "ai-architecture:design-critic"].includes(subagent_type)
    const want = design ? /security-reviewer/ : /design-critic/
    const absent = design ? /design-critic/ : subagent_type === "security-reviewer" ? /security-reviewer/ : undefined
    await h.idle(want, absent)
    await h.call("task", { subagent_type: "design-critic" })
    await h.call("task", { subagent_type: "security-reviewer" })
    await h.idle(null)
  })
}
await test("failed reviewer dispatch cannot clear pending review", async () => {
  const h = await harness(); await h.call("write", edits[1][1])
  await h.call("task", { subagent_type: "design-critic" }, { success: false })
  await h.idle(/design-critic/)
})
await test("denied reviewer dispatch cannot clear pending review", async () => {
  const h = await harness(); await h.call("write", edits[1][1])
  const denied = await core.createGatesBridge({ client: {}, directory: work }, {
    PreToolUse: [{ matcher: "Task", command: "exit 3", failClosed: true }],
  })
  await assert.rejects(denied["tool.execute.before"]({ tool: "task", sessionID: h.sessionID, callID: "denied-review" }, { args: { subagent_type: "design-critic" } }))
  await h.idle(/design-critic/)
})
await test("patch security content and all paths reach the Python gate", async () => {
  const h = await harness()
  await h.call("apply_patch", { patchText: "*** Begin Patch\n*** Add File: notes.md\n+note\n*** Add File: app.py\n+password = input()\n*** End Patch" })
  await h.idle(/security-reviewer/)
})
await test("a later failed edit cannot erase an earlier successful mutation", async () => {
  const h = await harness()
  await h.call("write", edits[1][1])
  await h.call("write", edits[1][1], { status: "error" })
  await h.idle(/design-critic/)
})
await test("mentioning a reviewer is not a reviewer dispatch", async () => {
  const h = await harness()
  await h.call("write", edits[1][1])
  await h.call("task", { subagent_type: "surveyor", prompt: "design-critic" })
  await h.idle(/design-critic/)
})
await test("authorised local commit runs repository hook; mutations never auto-approved", async () => {
  let replies = 0
  // Parser-independent wiring test uses the production token allow-list.
  const hook = createBashStructural({ client: { permission: { reply: async () => { replies++; return {} } } } }, {
    loadParser: async () => ({}),
    classifySync: (_p, c) => ({ approve: isReadOnly(c.split(" ")), zeroPattern: false, reason: "test tokens", parseError: false }),
  })
  const envOutput: any = { env: {} }
  await hook["shell.env"]({}, envOutput)
  const env = { ...process.env, ...envOutput.env, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null" }
  const repo = mkdtempSync(path.join(os.tmpdir(), "bridge-review-git-"))
  function git(...args: string[]) {
    const result = spawnSync("git", args, { cwd: repo, env, encoding: "utf8" })
    assert.equal(result.status, 0, result.stderr)
  }
  git("init", "-q")
  git("config", "user.name", "Fixture")
  git("config", "user.email", "fixture@example.invalid")
  const recordingHook = path.join(repo, ".git/hooks/pre-commit")
  writeFileSync(recordingHook, "#!/bin/sh\nprintf 'hook fired\\n' >> hook-record\n")
  chmodSync(recordingHook, 0o700)
  for (const command of ["git commit --allow-empty -m fixture", "git push", "git branch new", "git update-ref refs/heads/new HEAD"]) {
    await hook["tool.execute.before"]({ tool: "bash", callID: command }, { args: { command } })
    await hook.event({ event: { type: "permission.asked", properties: { id: command, sessionID: "git", permission: "bash", metadata: { command }, tool: { callID: command } } } })
  }
  assert.equal(replies, 0)
  git("commit", "--allow-empty", "-qm", "fixture") // deliberate test authorisation, no push
  assert.equal(existsSync(path.join(repo, "hook-record")), true, "commit hook was suppressed")
  for (const command of ["git branch", "git rev-parse HEAD", "git ls-files", "git merge-base HEAD HEAD"]) {
    await hook.event({ event: { type: "permission.asked", properties: { id: command, sessionID: "git", permission: "bash", metadata: { command }, tool: { callID: command } } } })
    git(...command.split(" ").slice(1))
  }
  assert.equal(replies, 4)
  assert.equal(readFileSync(path.join(repo, "hook-record"), "utf8"), "hook fired\n")
  for (const key of ["core.fsmonitor", "core.attributesFile", "core.pager", "log.showSignature"])
    assert.ok(Object.values(envOutput.env).includes(key), key)
})
console.log(`bridge review integration: ${checks - failures} passed, ${failures} failed`)
process.exitCode = failures ? 1 : 0
