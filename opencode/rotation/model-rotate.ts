// Opt-in OpenCode plugin module: rotate the model of a `task` (sub-agent) call.
//
// OpenCode's task tool selects an agent, not a model, so rotation works through pre-defined
// variant agents: for a role listed in the pool, a drawn model that differs from the role's
// configured model becomes `output.args.subagent_type = "<role>--<slug(model)>"`, where
// slug replaces "/" with "-". You define those variant agents in your OpenCode config (same
// definition as the role, different `model`); see README.md. Nothing else in the call changes.
//
// Fail safe: every problem (no/invalid/oversized/wrong-version pool, mode other than "on",
// role not in the pool, a malformed entry in the role's list, no eligible model, variant agent
// not configured, unreadable config, MODEL_ROTATION=off) leaves the call unchanged. It never
// throws, so it cannot block a session, and it never picks a model that is not in the pool.
//
// Same pool file and brief lines as the Claude Code hook (plugins/gates/hooks/rotation):
// pin_arm and builder_arm, both read only from the brief header (roles in `independent_roles`
// for builder_arm). More than one `builder_arm:` line anywhere in the prompt: ignored and
// rotation is skipped for independent roles.
// Env: MODEL_ROTATION=off, ROTATION_POOL / ROTATION_LOG / ROTATION_OC_CONFIG (OpenCode config
// file, default ~/.config/opencode/opencode.json): must be absolute and outside
// CLAUDE_PROJECT_DIR and the cwd, else the default is used / no log is written.
// ROTATION_SEED=<text> (deterministic draws for tests/replays) is honoured only together
// with ROTATION_TEST=1; default is node:crypto randomInt.
// Routing only: mutate args, never grant permission or start workers. Existing harness
// policy remains authoritative; brief headers and caller identity are not permission grants.
import { createHash, randomInt } from "node:crypto"
import { closeSync, constants, fstatSync, mkdirSync, openSync, readFileSync, realpathSync, writeSync } from "node:fs"
import path from "node:path"

export const MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}$/
export const ROLE_RE = /^[a-z][a-z0-9-]{0,39}$/
const MAX_POOL_BYTES = 1_000_000
const MAX_WEIGHT = 1_000_000

export type Arm = { model: string; weight: number }
export type Pool = { independentRoles: string[]; roles: Record<string, unknown> }

const home = () => process.env.HOME || "/nonexistent"

// realpath of the deepest existing ancestor + the unresolved remainder (works for files not yet created).
function realish(p: string): string {
  const rest: string[] = []
  let cur = path.resolve(p)
  for (;;) {
    try { return path.join(realpathSync(cur), ...rest.reverse()) } catch {
      const up = path.dirname(cur)
      if (up === cur) return path.resolve(p)
      rest.push(path.basename(cur)); cur = up
    }
  }
}
function inside(p: string, root: string): boolean {
  const a = realish(p), b = realish(root)
  return a === b || a.startsWith(b.endsWith(path.sep) ? b : b + path.sep)
}
// Project directories OpenCode told the plugin about (the process cwd need not be the project).
const pluginDirs = new Set<string>()
// Env path that is absolute and not inside the project dir / cwd / a plugin `directory`, else undefined.
export function envPath(name: string): string | undefined {
  const v = process.env[name]
  if (!v || !path.isAbsolute(v)) return undefined
  const roots = [process.env.CLAUDE_PROJECT_DIR, process.cwd(), ...pluginDirs].filter((d): d is string => !!d)
  return roots.some((r) => inside(v, r)) ? undefined : v
}
export const poolPath = () => envPath("ROTATION_POOL") ?? path.join(home(), ".config", "ai-architecture", "model-pool.json")
export const agentConfigPath = () => envPath("ROTATION_OC_CONFIG") ?? path.join(home(), ".config", "opencode", "opencode.json")

// Read a regular file without following symlinks or blocking on a FIFO; undefined when not a small regular file.
function readRegular(file: string): string | undefined {
  const fd = openSync(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  try {
    const st = fstatSync(fd)
    if (!st.isFile() || st.size > MAX_POOL_BYTES) return undefined
    return readFileSync(fd, "utf8")
  } finally {
    closeSync(fd)
  }
}

export const slug = (model: string) => model.replace(/\//g, "-")

function normalizeNewlines(text: string): string {
  return text.replace(/\r\n/g, "\n").replace(/\r/g, "\n")
}

export function briefHeader(prompt: string): string {
  const lines = normalizeNewlines(prompt).split("\n")
  const blank = lines.findIndex((l) => l.trim() === "")
  return lines.slice(0, Math.min(blank === -1 ? lines.length : blank, 15)).join("\n")
}

export function extractPinArm(prompt: unknown): string | null {
  if (typeof prompt !== "string") return null
  const m = /^pin_arm:[ \t]*(\S+)[ \t]*$/m.exec(briefHeader(prompt))
  return m ? m[1] : null
}

// Builder arm from the brief header only. "ambiguous" when a `builder_arm:` line appears more than once anywhere.
export function extractBuilderArm(prompt: unknown): string | null | "ambiguous" {
  if (typeof prompt !== "string") return null
  if ((normalizeNewlines(prompt).match(/^builder_arm:/gm) ?? []).length > 1) return "ambiguous"
  const m = /^builder_arm:[ \t]*(\S+)[ \t]*$/m.exec(briefHeader(prompt))
  return m ? m[1] : null
}

export function loadPool(file: string): Pool | undefined {
  try {
    const text = readRegular(file)
    if (text === undefined) return undefined
    const raw = JSON.parse(text)
    if (!raw || typeof raw !== "object" || Array.isArray(raw) || raw.mode !== "on") return undefined
    if ("version" in raw && raw.version !== 1) return undefined
    if (!raw.roles || typeof raw.roles !== "object" || Array.isArray(raw.roles)) return undefined
    const ind = Array.isArray(raw.independent_roles) ? raw.independent_roles.filter((r: unknown) => typeof r === "string") : []
    return { independentRoles: ind, roles: raw.roles }
  } catch {
    return undefined
  }
}

// Enabled [model, weight] entries, or undefined when ANY entry of the list is malformed.
export function roleArms(entries: unknown): Arm[] | undefined {
  if (!Array.isArray(entries) || !entries.length) return undefined
  const out: Arm[] = []
  for (const e of entries) {
    if (!e || typeof e !== "object") return undefined
    const { model, weight, enabled = true } = e as Record<string, unknown>
    if (typeof model !== "string" || !MODEL_RE.test(model)) return undefined
    if (typeof weight !== "number" || !Number.isInteger(weight) || weight < 0 || weight > MAX_WEIGHT) return undefined
    if (typeof enabled !== "boolean") return undefined
    if (enabled && weight > 0) out.push({ model, weight })
  }
  return out
}

// mulberry32 over a sha256-derived 32-bit seed: deterministic, no dependency.
export function seededRng(seed: string): (n: number) => number {
  let a = createHash("sha256").update("rotation:" + seed).digest().readUInt32BE(0)
  return (n) => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return Math.floor((((t ^ (t >>> 14)) >>> 0) / 4294967296) * n)
  }
}

export function pickModel(arms: Arm[], rng?: (n: number) => number): string | undefined {
  if (!arms.length) return undefined
  const pick = rng ?? (process.env.ROTATION_SEED && process.env.ROTATION_TEST === "1" ? seededRng(process.env.ROTATION_SEED) : (n: number) => randomInt(n))
  let n = pick(arms.reduce((s, a) => s + a.weight, 0))
  for (const a of arms) {
    if (n < a.weight) return a.model
    n -= a.weight
  }
  return arms[arms.length - 1].model
}

function readConfig(): any {
  try {
    const text = readRegular(agentConfigPath())
    return text === undefined ? undefined : JSON.parse(text)
  } catch {
    return undefined
  }
}

function logDecision(entry: Record<string, unknown>) {
  const file = envPath("ROTATION_LOG")
  if (!file) return
  try {
    mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 })
    const fd = openSync(file, constants.O_WRONLY | constants.O_APPEND | constants.O_CREAT | constants.O_NOFOLLOW, 0o600)
    try { writeSync(fd, `${JSON.stringify(entry)}\n`) } finally { closeSync(fd) }
  } catch {
    // advisory only
  }
}

export function createModelRotate(pluginInput: any = {}) {
  const dir = pluginInput?.directory
  if (typeof dir === "string" && dir) pluginDirs.add(dir)
  return {
    "tool.execute.before": async (input: any, output: any) => {
      let role: unknown = null
      const bail = (reason: string, extra: Record<string, unknown> = {}) =>
        logDecision({ t: new Date().toISOString(), session_id: input?.sessionID ?? null, role, model: null, pinned: false, action: "unchanged", reason, ...extra })
      try {
        if (process.env.MODEL_ROTATION === "off") return bail("MODEL_ROTATION=off")
        if (input?.tool !== "task") return
        role = output?.args?.subagent_type
        if (typeof role !== "string" || !ROLE_RE.test(role)) return bail("invalid subagent_type")
        const pool = loadPool(poolPath())
        if (!pool) return bail("pool missing, unreadable or invalid")
        if (!Object.prototype.hasOwnProperty.call(pool.roles, role)) return bail("role not in pool")
        let eligible = roleArms(pool.roles[role])
        if (!eligible || !eligible.length) return bail("no eligible model for role")
        const prompt = output.args.prompt
        const reasons: string[] = []
        let builder: string | null = null
        if (pool.independentRoles.includes(role)) {
          const b = extractBuilderArm(prompt)
          if (b === "ambiguous") return bail("builder_arm appears more than once; rotation skipped")
          builder = b
          if (builder) {
            const rest = eligible.filter((a) => a.model !== builder)
            if (rest.length) eligible = rest
          }
        }
        let chosen: string | undefined
        let pinned = false
        const pin = extractPinArm(prompt)
        if (pin) {
          // an independent role must not be pinned onto the builder's model by omitting builder_arm
          if (pool.independentRoles.includes(role) && !builder) reasons.push("pin_arm ignored: independent role without builder_arm")
          else if (pin === builder) reasons.push("pin_arm matches builder_arm; ignored")
          else if (eligible.some((a) => a.model === pin)) { chosen = pin; pinned = true }
          else reasons.push("pin_arm is not an enabled model of this role; ignored")
        }
        chosen ??= pickModel(eligible)
        if (!chosen) return bail("no model drawn")
        const config = readConfig()
        const baseModel = config?.agent?.[role]?.model
        const row = { t: new Date().toISOString(), session_id: input?.sessionID ?? null, role, model: chosen, pinned }
        if (chosen === baseModel) {
          logDecision({ ...row, action: "unchanged", reason: reasons.join("; ") || null })
          return
        }
        const variant = `${role}--${slug(chosen)}`
        const agents = config?.agent
        if (!agents || typeof agents !== "object" || !Object.prototype.hasOwnProperty.call(agents, variant)) {
          logDecision({ ...row, action: "unchanged", reason: [...reasons, `variant agent "${variant}" is not configured`].join("; ") })
          return
        }
        if (agents[variant]?.model !== chosen) {
          logDecision({ ...row, action: "unchanged", reason: [...reasons, `variant agent "${variant}" does not use model ${chosen}`].join("; ") })
          return
        }
        output.args.subagent_type = variant
        logDecision({ ...row, action: "model", reason: reasons.join("; ") || null })
      } catch {
        try { bail("internal error") } catch { /* fail safe */ }
      }
    },
  }
}
