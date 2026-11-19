// OpenCode runtime hooks over the continuity ledger CLI: usage records, resume injection, session freeze,
// the handoff gate for workers and the child budget. State lives in the Python module; this file keeps only
// caches. Everything is disabled when AI_ARCH_JOB_MARKER is set (this process is itself a dispatched worker).
import { createHash } from "node:crypto"
import { lstatSync, readFileSync, readdirSync, readlinkSync, realpathSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { REPO, ledgerActive, type Run } from "./bridge"

const MAX_RECORD = 16 * 1024
const RECORD_KEYS = ["version", "harness", "session_id", "root", "bash", "created_at"]
const FILE_TOOLS = ["write", "edit", "patch", "apply_patch"]
const PRUNE = [".git", "node_modules", "__pycache__", ".venv"]
const GUARD_FILE = path.join(REPO, "modules", "continuity", "hooks", "freeze_guard.py")
const ISO = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/

// --- session freeze: a port of modules/continuity/freeze.py (decide_tool); `freeze check` is the reference ---------

export function freezeDir(env: NodeJS.ProcessEnv): string {
  const set = env.AI_ARCH_FREEZE_DIR
  if (set) {
    if (!path.isAbsolute(set)) throw new Error("AI_ARCH_FREEZE_DIR must be an absolute path")
    return set
  }
  const xdg = env.XDG_STATE_HOME
  if (xdg && path.isAbsolute(xdg)) return path.join(xdg, "ai-architecture", "freeze")
  return path.join(env.HOME || os.homedir(), ".local", "state", "ai-architecture", "freeze")
}

export function freezeRecordPath(dir: string, sessionID: string): string {
  const digest = createHash("sha256").update(sessionID, "utf8").digest("hex").slice(0, 32)
  return path.join(dir, `opencode-${digest}.json`)
}

type FreezeRecord = { version: 1; harness: "opencode"; session_id: string; root: string; bash: "deny" | "unscoped"; created_at: string }

/** The record, null when there is none; throws when the file exists but cannot be trusted. */
export function loadFreeze(dir: string, sessionID: string): FreezeRecord | null {
  const file = freezeRecordPath(dir, sessionID)
  let text: string
  try {
    const stat = lstatSync(file)
    if (!stat.isFile()) throw new Error("not a regular file")
    if (stat.size > MAX_RECORD) throw new Error("too large")
    text = readFileSync(file, "utf8")
  } catch (error: any) {
    if (error?.code === "ENOENT") return null
    throw new Error(`freeze record unreadable: ${error?.message || error}`)
  }
  let rec: any
  try {
    rec = JSON.parse(text)
  } catch {
    throw new Error("freeze record is not JSON")
  }
  const keys = rec && typeof rec === "object" && !Array.isArray(rec) ? Object.keys(rec) : []
  if (keys.length !== RECORD_KEYS.length || !RECORD_KEYS.every((k) => keys.includes(k))) throw new Error("freeze record keys")
  if (rec.version !== 1 || rec.harness !== "opencode" || !["deny", "unscoped"].includes(rec.bash)) throw new Error("freeze record fields")
  if (rec.session_id !== sessionID) throw new Error("freeze record names another session")
  if (typeof rec.root !== "string" || !path.isAbsolute(rec.root) || rec.root.includes("\0")) throw new Error("freeze record root")
  if (typeof rec.created_at !== "string" || !ISO.test(rec.created_at)) throw new Error("freeze record created_at")
  return rec
}

/** Python os.path.realpath (non-strict): components resolved in order, symlinks followed, a missing tail kept. */
export function realpathLoose(input: string, depth = 0): string {
  if (depth > 40) throw new Error("too many symbolic links")
  let resolved = path.sep
  const parts = input.split(path.sep)
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i]
    if (part === "" || part === ".") continue
    if (part === "..") {
      resolved = path.dirname(resolved)
      continue
    }
    const candidate = path.join(resolved, part)
    let stat
    try {
      stat = lstatSync(candidate)
    } catch (error: any) {
      if (error?.code === "ENOENT" || error?.code === "ENOTDIR") {
        resolved = path.join(candidate, ...parts.slice(i + 1).filter((p) => p !== "" && p !== "."))
        return resolved
      }
      throw error
    }
    if (stat.isSymbolicLink()) {
      const target = readlinkSync(candidate)
      resolved = realpathLoose(path.isAbsolute(target) ? target : path.join(resolved, target), depth + 1)
    } else resolved = candidate
  }
  return resolved
}

const inside = (root: string, target: string) => target === root || target.startsWith(root.replace(/\/+$/, "") + path.sep)
const isSettingsName = (name: string) => /^settings.*\.json$/s.test(name.toLowerCase())
const isSettings = (p: string) => path.basename(path.dirname(p)).toLowerCase() === ".claude" && isSettingsName(path.basename(p))

function claudeDirs(root: string): Set<string> {
  const found = new Set<string>()
  const walk = (here: string) => {
    let entries
    try {
      entries = readdirSync(here, { withFileTypes: true })
    } catch {
      return
    }
    for (const e of entries) {
      const full = path.join(here, e.name)
      const lower = e.name.toLowerCase()
      if (lower === ".claude" && (e.isDirectory() || e.isSymbolicLink())) found.add(realpathLoose(full))
      if (e.isDirectory() && !PRUNE.includes(lower)) walk(full)
    }
  }
  walk(root)
  return found
}

function inSettingsDir(dirs: Set<string>, target: string): boolean {
  if (isSettingsName(path.basename(target)) && dirs.has(path.dirname(target))) return true
  for (const d of dirs) {
    let names: string[]
    try {
      names = readdirSync(d)
    } catch {
      continue
    }
    for (const n of names) if (isSettingsName(n) && realpathLoose(path.join(d, n)) === target) return true
  }
  return false
}

export function decideFreeze(record: FreezeRecord, tool: string, target: string | undefined, cwd: string | undefined, dir: string): { allowed: boolean; reason: string } {
  const deny = (reason: string) => ({ allowed: false, reason })
  if (tool === "bash") return record.bash === "deny" ? deny("frozen session: bash is denied (it cannot be scoped to the root)") : { allowed: true, reason: "bash unscoped: not enforced" }
  if (!FILE_TOOLS.includes(tool)) return { allowed: true, reason: "not a frozen tool" }
  if (typeof target !== "string" || !target || target.includes("\0")) return deny("frozen session: the call names no usable target path")
  if (target.startsWith("~") || target.includes("$")) return deny("frozen session: a target path starting with ~ or containing $ is denied")
  let targets: string[], root: string, protectedPaths: string[], dirs: Set<string>, lexical: string
  try {
    let p = target
    if (!path.isAbsolute(p)) {
      if (typeof cwd !== "string" || !path.isAbsolute(cwd)) return deny("frozen session: relative target path without a working directory")
      p = path.join(cwd, p)
    }
    lexical = path.normalize(p).replace(/(.)\/+$/, "$1")
    targets = [...new Set([realpathLoose(p), realpathLoose(lexical)])]
    root = realpathLoose(record.root)
    protectedPaths = [realpathLoose(dir), realpathLoose(GUARD_FILE)]
    dirs = claudeDirs(root)
  } catch {
    return deny("frozen session: the target path does not resolve")
  }
  if (protectedPaths.some((p) => targets.some((t) => inside(p, t)))) return deny("frozen session: the freeze record, its directory and the guard are protected")
  if (isSettings(lexical) || targets.some((t) => isSettings(t) || inSettingsDir(dirs, t))) return deny("frozen session: .claude/settings*.json under the root is protected")
  if (targets.every((t) => inside(root, t))) return { allowed: true, reason: "inside the frozen root" }
  return deny(`frozen session: ${tool} outside ${root} is denied`)
}

/** Every path a frozen file tool names: path-like args and the file headers of a patch body. */
export function targetPaths(tool: string, args: any): (string | undefined)[] {
  const found: (string | undefined)[] = []
  if (args && typeof args === "object") {
    for (const [k, v] of Object.entries(args)) {
      if (/path|file/i.test(k)) found.push(typeof v === "string" ? v : undefined)
    }
    for (const v of Object.values(args)) {
      if (typeof v !== "string") continue
      for (const m of v.matchAll(/^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$/gm)) found.push((m[1] ?? m[2]).trim())
    }
  }
  return found.length ? found : [undefined]
}

// --- runtime ------------------------------------------------------------------------------------------------------

const ID = /^[A-Za-z0-9_-]{1,128}$/
export function tokensOf(info: any): string {
  const t = info?.tokens
  const parts = [t?.input, t?.output, t?.cache?.read, t?.cache?.write]
  if (!parts.every((n) => Number.isInteger(n) && n >= 0)) return "unknown"
  return String(parts.reduce((a: number, b: number) => a + b, 0))
}

export function createContinuityRuntime(opts: {
  client: any
  directory: string
  run: Run
  env?: NodeJS.ProcessEnv
  now?: () => number
  log?: (message: string) => void
}) {
  const env = opts.env ?? process.env
  const log = opts.log ?? ((m: string) => console.error(`[continuity] ${m}`))
  const childUsage = new Map<string, number>() // child session -> last observed total tokens
  const childBudget = new Map<string, number>() // child session -> its job's token budget (set by the hub)
  const runtime = { hooks: {} as Record<string, any>, childUsage, childBudget, active: () => ledgerActive(opts.directory) }
  if (env.AI_ARCH_JOB_MARKER) return runtime

  const rootCache = new Map<string, boolean>()
  const injections = new Map<string, string | null>()
  const recorded = new Set<string>()
  const compacted = new Set<string>()
  let limits: Map<string, number> | null = null
  let warnedInactive = false
  const project = () => ["--project", opts.directory]
  const sessionKey = (id: string) => `opencode:${id}`

  function active(): boolean {
    if (ledgerActive(opts.directory)) return true
    if (!warnedInactive) {
      warnedInactive = true
      log("no ledger in this directory (run continuity.py init): ledger features are inactive here")
    }
    return false
  }

  async function isRoot(id: string): Promise<boolean | null> {
    if (rootCache.has(id)) return rootCache.get(id)!
    try {
      const got = await opts.client.session.get({ path: { id } })
      const info = got?.data ?? got
      if (!info || typeof info !== "object") return null
      const root = !info.parentID
      rootCache.set(id, root)
      return root
    } catch {
      return null
    }
  }

  async function contextLimit(info: any): Promise<number | undefined> {
    try {
      if (!limits) {
        limits = new Map()
        const got = await opts.client.config.providers()
        const data = got?.data ?? got
        for (const p of data?.providers ?? []) {
          for (const [mid, m] of Object.entries<any>(p?.models ?? {})) {
            const n = m?.limit?.context
            if (Number.isInteger(n) && n >= 1) limits.set(`${p.id}/${mid}`, n)
          }
        }
      }
      return limits.get(`${info.providerID}/${info.modelID}`)
    } catch {
      return undefined
    }
  }

  async function onMessage(info: any) {
    if (!info || info.role !== "assistant" || !info.time?.completed || typeof info.sessionID !== "string" || !ID.test(info.sessionID)) return
    const mark = `${info.sessionID}/${info.id}`
    if (recorded.has(mark)) return
    if (recorded.size > 2000) recorded.clear()
    recorded.add(mark)
    const tokens = tokensOf(info)
    const root = await isRoot(info.sessionID)
    if (root === null) return log(`session ${info.sessionID}: parent unknown, usage not recorded`)
    if (!root) {
      if (tokens !== "unknown") childUsage.set(info.sessionID, Number(tokens))
      return
    }
    if (!active()) return
    const limit = await contextLimit(info)
    const args = ["usage", "record", ...project(), "--session", sessionKey(info.sessionID), "--tokens", tokens]
    if (limit !== undefined) args.push("--limit", String(limit))
    args.push("--source", "opencode-event")
    const out = await opts.run(args)
    if (out.code !== 0) log(`usage record failed (exit ${out.code}); the session's usage stays as it was`)
  }

  runtime.hooks = {
    event: async ({ event }: any) => {
      try {
        if (event?.type === "message.updated") await onMessage(event.properties?.info)
        else if (event?.type === "session.compacted") {
          const id = event.properties?.sessionID
          if (typeof id === "string") {
            injections.delete(id)
            compacted.add(id)
          }
        }
      } catch (error: any) {
        log(`event hook failed: ${error?.message || error}`)
      }
    },

    "experimental.chat.system.transform": async (input: any, output: any) => {
      try {
        const id = input?.sessionID
        if (typeof id !== "string" || !ID.test(id) || !Array.isArray(output?.system)) return
        if (!injections.has(id)) {
          if ((await isRoot(id)) !== true || !active()) return
          const source = compacted.has(id) ? "compact" : "resume"
          const out = await opts.run(["resume", "consume", ...project(), "--session", sessionKey(id), "--cwd", opts.directory, "--source", source])
          if (out.code !== 0 || !out.json) return log(`resume consume failed (exit ${out.code}); nothing injected`)
          compacted.delete(id)
          injections.set(id, typeof out.json.injection === "string" ? out.json.injection : null)
        }
        const text = injections.get(id)
        if (text) output.system.push(text)
      } catch (error: any) {
        log(`resume injection failed: ${error?.message || error}`)
      }
    },

    "tool.execute.before": async (input: any, output: any) => {
      const tool = String(input?.tool ?? "")
      const id = input?.sessionID
      // 1. freeze: keyed by the session id, no session-less fallback
      if (typeof id === "string" && id) {
        let dir: string | null = null
        let record: FreezeRecord | null = null
        let unreadable: string | null = null
        try {
          dir = freezeDir(env)
          record = loadFreeze(dir, id)
        } catch (error: any) {
          unreadable = error?.message || String(error)
        }
        if (unreadable && (FILE_TOOLS.includes(tool) || tool === "bash")) throw new Error(`frozen: ${unreadable}`)
        if (record && dir) {
          const targets = FILE_TOOLS.includes(tool) ? targetPaths(tool, output?.args) : [undefined]
          for (const t of targets) {
            const d = decideFreeze(record, tool, t, opts.directory, dir)
            if (!d.allowed) throw new Error(d.reason)
          }
        }
      }
      // 2. child budget: a hub child at or over its job budget stops and returns what it has
      if (typeof id === "string" && childBudget.has(id)) {
        const used = childUsage.get(id)
        if (used !== undefined && used >= childBudget.get(id)!) {
          throw new Error(`child token budget reached (${used} of ${childBudget.get(id)}): stop now and return a partial result`)
        }
      }
      // 3. worker gate: no new worker once the session is at HANDOFF
      const isHubStart = tool === "hub" && output?.args?.action === "start"
      if ((tool === "task" || isHubStart) && typeof id === "string") {
        if (!active()) {
          if (isHubStart) throw new Error("the hub needs a continuity ledger: run continuity.py init in this project")
          return
        }
        const out = await opts.run(["status", ...project(), "--session", sessionKey(id)])
        if (out.code !== 0 || !out.json) {
          if (isHubStart) throw new Error("the continuity ledger could not be read: hub start refused")
          return log(`handoff gate: ledger unreadable (exit ${out.code}); the task call goes through`)
        }
        if (out.json.phase === "HANDOFF") {
          throw new Error(`session is at HANDOFF (${out.json.usage_tokens} of ${out.json.limit_tokens} tokens): bank results and checkpoint before new workers`)
        }
      }
    },
  }
  return runtime
}
