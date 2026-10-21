// Wiring: (A) `event` hook auto-replies "once" to a bash `permission.asked` whose command is
// structurally read-only; (B) `tool.execute.before` denies bash commands OC would run with zero ask patterns.
// Config stays `bash: {"*":"ask"}`; any doubt = no reply = the user is asked as before.
import { classifySync as realClassifySync, loadParser as realLoadParser, PARSER_LOAD_MS, type Verdict } from "./bash-structural"

type Deps = {
  loadParser?: () => Promise<any>
  classifySync?: (parser: any, c: string) => Verdict
  timeoutMs?: number
  parserTimeoutMs?: number
  log?: (m: string) => void
}

const DENY = "no permission pattern; run as a simple command"

// classifySync is synchronous and cannot be pre-empted; enforce its budget by elapsed time (over budget => throw => DENY / no-approve).
function budgeted<T>(f: () => T, ms: number): T {
  const t0 = Date.now()
  const r = f()
  if (Date.now() - t0 > ms) throw new Error("timeout")
  return r
}

function timed<T>(p: Promise<T>, ms: number): Promise<T> {
  let t: any
  return Promise.race([p, new Promise<T>((_, rej) => { t = setTimeout(() => rej(new Error("timeout")), ms) })]).finally(() => clearTimeout(t)) as Promise<T>
}

export function createBashStructural(input: any, deps: Deps = {}) {
  const loadParser = deps.loadParser ?? realLoadParser
  const classifySync = deps.classifySync ?? realClassifySync
  const ms = deps.timeoutMs ?? 2000
  const pms = deps.parserTimeoutMs ?? PARSER_LOAD_MS
  const log = deps.log ?? ((m: string) => console.error(`[bash-structural] ${m}`))
  // Prewarm; a failure is logged, never thrown (the next call retries via loadParser's own reset).
  try { loadParser().catch((e: any) => log(`parser prewarm failed: ${e?.message ?? e}`)) } catch (e: any) { log(`parser prewarm failed: ${e?.message ?? e}`) }
  // Parser load has its own budget (pms); classification has `ms`.
  const answered = new Set<string>()
  // callIDs of bash calls carrying a `workdir` arg: A reads only metadata.command, so those are never auto-approved.
  // Link verified in OC source: tool.execute.before gets {tool, sessionID, callID} (session/tools.ts) BEFORE the ask,
  // and the permission request carries tool: {messageID, callID} (session/tools.ts ctx.ask).
  const workdirCalls = new Set<string>()

  // v1 SDK client (what PluginInput.client is in 1.18.31): postSessionIdPermissionsPermissionId
  // => POST /session/{id}/permissions/{permissionID} {response}. v2 shape kept as a fallback.
  async function reply(req: any) {
    const c = input?.client
    if (typeof c?.postSessionIdPermissionsPermissionId === "function") {
      const r = await c.postSessionIdPermissionsPermissionId({ path: { id: req.sessionID, permissionID: req.id }, body: { response: "once" } })
      if (r?.error) throw new Error("reply error")
      return
    }
    if (typeof c?.permission?.reply === "function") {
      const r = await c.permission.reply({ requestID: req.id, reply: "once" })
      if (r?.error) throw new Error("reply error")
      return
    }
    throw new Error("no permission reply method on host client")
  }

  return {
    event: async ({ event }: any) => {
      try {
        if (event?.type !== "permission.asked") return
        const req = event.properties
        if (!req || req.permission !== "bash" || typeof req.id !== "string" || typeof req.sessionID !== "string") return
        const command = req.metadata?.command
        if (typeof command !== "string" || answered.has(req.id)) return
        // no verifiable "default cwd" signal => never approve
        const callID = req.tool?.callID
        if (typeof callID !== "string" || workdirCalls.has(callID)) return
        const parser = await timed(loadParser(), pms)
        const verdict = budgeted(() => classifySync(parser, command), ms)
        if (!verdict.approve) return
        answered.add(req.id)
        if (answered.size > 500) answered.delete(answered.values().next().value as string)
        await timed(reply(req), ms)
      } catch (e: any) {
        log(`no auto-approve: ${e?.message ?? e}`) // fail closed: the user is asked
      }
    },
    // Git read hardening for every bash command: GIT_CONFIG_COUNT/KEY_n/VALUE_n (highest precedence after -c) neutralise
    // repo-local fsmonitor / attributes / external-diff / pager code execution on auto-approved read-only git commands.
    // Appends after any pre-existing GIT_CONFIG_COUNT entries instead of clobbering them.
    "shell.env": async (_input: any, output: any) => {
      try {
        const env = output?.env
        if (!env) return
        const raw = env.GIT_CONFIG_COUNT ?? process.env.GIT_CONFIG_COUNT
        const base = /^\d+$/.test(String(raw ?? "")) ? parseInt(String(raw), 10) : 0
        // NOT diff.external="" (git then fails "cannot run :"). GIT_ATTR_SOURCE=<empty tree> (git>=2.43) makes git read
        // attributes from an empty tree, so in-tree .gitattributes (textconv / diff driver / filter) are ignored.
        // Side effect: eol/binary/etc. attributes are ignored for OC bash git too. Residual: a repo-local
        // diff.external in .git/config and .git/info/attributes are not neutralised here.
        // The auto-approved Git read set does not invoke hooks. Do not disable repository
        // hooks here: this environment also reaches deliberately authorised commits/pushes.
        const pairs: [string, string][] = [["core.fsmonitor", "false"], ["core.attributesFile", "/dev/null"], ["core.pager", "cat"], ["log.showSignature", "false"]]
        if (env.GIT_ATTR_SOURCE === undefined && process.env.GIT_ATTR_SOURCE === undefined) env.GIT_ATTR_SOURCE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        // keys/values of the pre-existing entries come from process.env unless this plugin chain already set them
        for (let i = 0; i < base; i++) {
          for (const k of [`GIT_CONFIG_KEY_${i}`, `GIT_CONFIG_VALUE_${i}`]) if (env[k] === undefined && process.env[k] !== undefined) env[k] = process.env[k]!
        }
        pairs.forEach(([k, v], j) => { env[`GIT_CONFIG_KEY_${base + j}`] = k; env[`GIT_CONFIG_VALUE_${base + j}`] = v })
        env.GIT_CONFIG_COUNT = String(base + pairs.length)
      } catch (e: any) { log(`shell.env failed: ${e?.message ?? e}`) }
    },
    "tool.execute.before": async (event: any, output: any) => {
      if (event?.tool !== "bash") return
      const wd = output?.args?.workdir ?? event?.args?.workdir
      if (typeof wd === "string" && wd !== "" && typeof event?.callID === "string") {
        workdirCalls.add(event.callID)
        if (workdirCalls.size > 500) workdirCalls.delete(workdirCalls.values().next().value as string)
      }
      const command = output?.args?.command ?? event?.args?.command
      if (typeof command !== "string") return
      // Only a parser LOAD failure (reject or load timeout) fails open; once a parser exists, ANY error
      // (RangeError, timeout, bad tree) is DENY.
      let parser: any
      try {
        parser = await timed(loadParser(), pms)
      } catch (e: any) {
        log(`parser unavailable, B fails open: ${e?.message ?? e}`) // never brick bash on a load failure
        return
      }
      let verdict: Verdict
      try {
        verdict = budgeted(() => classifySync(parser, command), ms)
      } catch (e: any) {
        log(`classify failed, B denies: ${e?.message ?? e}`)
        throw new Error(DENY)
      }
      if (verdict.zeroPattern) throw new Error(DENY)
    },
  }
}
