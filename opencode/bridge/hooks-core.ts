// Bridge core: runs the SAME guard hooks as plugins/gates from inside OpenCode.
//
// The hook list comes from the gates plugin's own hooks.json (not a second copy), each hook is
// spawned with a Claude-Code-shaped JSON payload on stdin, and its answer is mapped back:
// a "deny" (or "ask": there is no approver in a headless seat), a non-zero exit, a timeout or
// unparseable output blocks the tool call. Every PreToolUse hook fails closed.
// Configuration is read by the hooks themselves from the user-scope file (AIARCH_CONFIG, else
// ~/.config/ai-architecture/gates.json); this bridge never reads a project-level config.
import { spawn } from "node:child_process"
import { closeSync, existsSync, mkdtempSync, openSync, promises as fs, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"

const home = () => process.env.HOME || "/tmp"

// Directory of the gates plugin (the one holding hooks/hooks.json). Order: AIARCH_GATES_ROOT, a
// `gates/` copy next to this file (the documented install layout), then the repository layout
// (../../plugins/gates from opencode/bridge).
export function gatesRoot() {
  const fromEnv = process.env.AIARCH_GATES_ROOT
  if (rootCache && rootCache.env === fromEnv) return rootCache.value
  let value: string
  if (fromEnv && !inProject(fromEnv)) value = fromEnv
  else {
    if (fromEnv) warnOnce(`AIARCH_GATES_ROOT ignored: ${fromEnv} is inside the project; using the default gates layout`)
    const here = path.dirname(fileURLToPath(import.meta.url))
    const installed = path.join(here, "gates")
    value = existsSync(path.join(installed, "hooks/hooks.json")) ? installed : path.resolve(here, "../../plugins/gates")
  }
  rootCache = { env: fromEnv, value }
  return value
}
// Resolved once; recomputed only if AIARCH_GATES_ROOT itself (or the project root set) changes.
let rootCache: { env: string | undefined; value: string } | undefined

// Every bridged PreToolUse hook fails CLOSED on timeout/spawn error/unparseable output: a
// deny-capable guard that crashes must not silently wave a write through. Only hooks proven
// advisory-only (they never emit a deny) belong on this list and keep fail-open behaviour.
// None of the gates hooks is advisory, so it starts empty.
export const ADVISORY_HOOKS: string[] = []

const HOOK_TIMEOUT_MS = Number(process.env.AIARCH_HOOKS_TIMEOUT_MS) || 30_000

export type HookEntry = { matcher: string; command: string; failClosed: boolean }
export type HookConfig = Record<string, HookEntry[]>

// Name of the script a hook command runs, e.g. `bash "${CLAUDE_PLUGIN_ROOT}/hooks/x.sh"` -> x.sh
export function hookBasename(command: string): string {
  const match = /([.\w/-]+\.(?:sh|py))\b/.exec(command)
  return path.basename(match ? match[1] : command.trim().split(/\s+/).pop() || "")
}

// Parses a Claude-Code-style hooks.json. A missing, unreadable or malformed file yields an empty
// config and a warning; createGatesBridge then DENIES every PreToolUse call (see configProblem).
// Why the bridge cannot trust its hook list, or undefined when it can. A config without any
// PreToolUse entry means every gate would be silently off, so the caller must deny instead.
export function configProblem(config: HookConfig): string | undefined {
  if (config.PreToolUse?.length) return undefined
  return `gates hooks.json did not load or lists no PreToolUse hook (looked in ${gatesRoot()}); refusing every tool call. Set AIARCH_GATES_ROOT to the plugins/gates directory.`
}

export function loadHookConfig(hooksJson = path.join(gatesRoot(), "hooks/hooks.json")): HookConfig {
  const config: HookConfig = {}
  let raw: any
  try { raw = JSON.parse(readFileSync(hooksJson, "utf8")) } catch (error: any) {
    console.warn(`[gates-bridge] hooks file unreadable (${hooksJson}): ${error?.message || error}; no hooks bridged`)
    return config
  }
  const hooks = raw?.hooks
  if (!hooks || typeof hooks !== "object") return config
  for (const [event, groups] of Object.entries(hooks)) {
    if (!Array.isArray(groups)) continue
    const entries: HookEntry[] = []
    for (const group of groups as any[]) {
      const matcher = typeof group?.matcher === "string" ? group.matcher : ""
      for (const hook of group?.hooks || []) {
        if (hook?.type !== "command" || typeof hook?.command !== "string" || !hook.command.trim()) continue
        entries.push({ matcher, command: hook.command, failClosed: !ADVISORY_HOOKS.includes(hookBasename(hook.command)) })
      }
    }
    if (entries.length) config[event] = entries
  }
  return config
}

// Which Claude tool name a matcher regex is tested against. A write-capable OpenCode tool id
// missing here would silently skip every Edit|Write matcher, so every id in PATH_SCOPED_TOOLS
// must have an entry (the tests assert it).
export const MATCHER_TOOL_NAMES: Record<string, string> = {
  bash: "Bash", edit: "Edit", apply_patch: "Edit", patch: "Edit", write: "Write", read: "Read",
  glob: "Glob", grep: "Grep", task: "Task", webfetch: "WebFetch",
}

export function matchesMatcher(matcher: string, claudeToolName: string | undefined): boolean {
  const trimmed = (matcher || "").trim()
  if (!trimmed || trimmed === "*") return true
  if (!claudeToolName) return false
  try { return new RegExp(`^(?:${trimmed})$`).test(claudeToolName) } catch { return true } // uncompilable matcher: run the hook (fail closed)
}

export type GuardResult = { exitCode: number; stdout: string; stderr: string; timedOut?: boolean }
export type Decision = { allow: boolean; reason?: string }

export function decideGuard(result: GuardResult, failClosed = true, label = "guard"): Decision {
  if (result.timedOut) {
    if (!failClosed) { console.warn(`[gates-bridge] ${label} timed out; fail-open`); return { allow: true } }
    return { allow: false, reason: "guard timed out" }
  }
  if (result.exitCode !== 0) {
    if (!failClosed) { console.warn(`[gates-bridge] ${label} exited ${result.exitCode}; fail-open`); return { allow: true } }
    const detail = result.stderr.trim() ? `: ${result.stderr.trim()}` : ""
    return { allow: false, reason: `guard exited ${result.exitCode}${detail}` }
  }
  if (!result.stdout.trim()) return { allow: true }
  let parsed: any
  try { parsed = JSON.parse(result.stdout) } catch { return { allow: false, reason: "guard emitted malformed structured output" } }
  const hook = parsed?.hookSpecificOutput ?? parsed
  const decision = hook?.permissionDecision
  const reason = hook?.permissionDecisionReason || "guard denied request"
  if (decision === "allow") return { allow: true }
  if (decision === "ask") return { allow: false, reason: "ask->deny (headless seat, no approver)" }
  if (decision === "deny") return { allow: false, reason }
  return { allow: false, reason: "guard structured output lacks permissionDecision" }
}

export function extractAdditionalContext(result: GuardResult): string | undefined {
  if (result.stdout.trim()) {
    try {
      const parsed = JSON.parse(result.stdout)
      const hook = parsed?.hookSpecificOutput ?? parsed
      if (typeof hook?.additionalContext === "string" && hook.additionalContext.trim()) return hook.additionalContext.trim()
    } catch { /* fall through to stderr */ }
  }
  if (result.exitCode !== 0 && result.stderr.trim()) return result.stderr.trim()
  return undefined
}

// ---- mutation-path containment (checked before any hook runs) ----

function normalizedPath(value: unknown, cwd: string) {
  if (typeof value !== "string" || !value.trim() || value.includes("\0")) throw new Error("missing or malformed file path")
  return path.resolve(cwd, value)
}

function trustedHome() {
  const value = process.env.HOME
  if (typeof value !== "string" || !value || value.includes("\0") || !path.isAbsolute(value)) throw new Error("refusing mutation path: HOME must be absolute")
  return path.normalize(value)
}

// Real, case-folded form of a path: the deepest existing ancestor is resolved through realpath,
// the rest is appended, and the result is lower-cased so a case-insensitive filesystem cannot
// reach a protected root through different capitalisation.
function fold(value: string) {
  const rest: string[] = []
  let current = path.normalize(value)
  for (;;) {
    try { current = realpathSync(current); break } catch {
      const parent = path.dirname(current)
      if (parent === current) break
      rest.unshift(path.basename(current))
      current = parent
    }
  }
  return path.join(current, ...rest).toLowerCase()
}

// Agent-configuration directories a session must never write into (folded, see fold()).
function protectedRoots() {
  const h = trustedHome()
  return [path.join(h, ".claude"), path.join(h, ".config/opencode"), path.join(h, ".config/ai-architecture")].map(fold)
}

function isWithinPath(target: string, root: string) {
  const relative = path.relative(root, target)
  return !relative || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative))
}

export function assertMutationRoot(cwd: unknown) {
  if (typeof cwd !== "string" || !cwd || cwd.includes("\0") || !path.isAbsolute(cwd)) throw new Error("refusing mutation path: session directory must be absolute")
  const root = path.normalize(cwd)
  const foldedRoot = fold(root)
  if (protectedRoots().some(protectedRoot => isWithinPath(foldedRoot, protectedRoot) || isWithinPath(protectedRoot, foldedRoot))) {
    throw new Error(`refusing mutation from protected writer root: ${root}`)
  }
  return root
}

export function assertProtectedMutationTarget(target: string) {
  const foldedTarget = fold(target)
  if (protectedRoots().some(protectedRoot => isWithinPath(foldedTarget, protectedRoot))) {
    throw new Error(`refusing mutation path under protected target: ${target}`)
  }
}

function mutationAbsolutePath(value: unknown, cwd: string) {
  if (typeof value !== "string" || !value.trim() || value.includes("\0")) throw new Error("missing or malformed file path")
  const raw = path.isAbsolute(value) ? value : `${cwd}${path.sep}${value}`
  if (raw.split(path.sep).some(component => component === "." || component === "..")) throw new Error(`refusing noncanonical mutation path: ${value}`)
  return path.normalize(raw)
}

function mutationPaths(tool: string, args: any) {
  if (tool === "apply_patch") {
    const names = ["patchText", "patch_text"].filter(name => Object.hasOwn(args || {}, name))
    if (!names.length) throw new Error("missing or malformed apply_patch text")
    const values = names.map(name => args[name])
    if (values.some(value => typeof value !== "string") || new Set(values).size !== 1) throw new Error("conflicting or malformed apply_patch text")
    return patchPaths(values[0])
  }
  const names = ["filePath", "file_path", "path"].filter(name => Object.hasOwn(args || {}, name))
  if (!names.length) throw new Error("missing or malformed file path")
  const values = names.map(name => args[name])
  if (values.some(value => typeof value !== "string") || new Set(values).size !== 1) throw new Error("conflicting or malformed file path")
  return [values[0]]
}

async function assertExistingPathComponents(target: string, terminalMustBeDirectory: boolean) {
  const parsed = path.parse(target)
  const components = target.slice(parsed.root.length).split(path.sep).filter(Boolean)
  let current = parsed.root
  for (let index = 0; index < components.length; index++) {
    current = path.join(current, components[index])
    let stat: import("node:fs").Stats
    try { stat = await fs.lstat(current) } catch (error: any) {
      if (error?.code === "ENOENT") return
      throw error
    }
    if (stat.isSymbolicLink()) throw new Error(`refusing mutation path through symlink: ${target}`)
    if (index < components.length - 1 && !stat.isDirectory()) throw new Error(`refusing mutation path through non-directory: ${target}`)
    if (index === components.length - 1 && (terminalMustBeDirectory ? !stat.isDirectory() : !stat.isFile())) {
      throw new Error(`refusing mutation path to non-regular target: ${target}`)
    }
  }
}

// OpenCode mutation tools do their own filesystem write after permission matching. Reject
// protected targets and any existing symlink component. Outside-worktree paths inside a git
// repository are left to OpenCode's own edit permission (which can ask); outside a git
// repository there is no worktree to ask against, so they are refused. The check is not atomic:
// a concurrent filesystem change between lstat and the write is a documented limitation.
async function assertContainedMutationPath(target: string, cwd: string, isMoveTarget = false) {
  const lexicalRoot = assertMutationRoot(cwd)
  assertProtectedMutationTarget(target)
  const relative = path.relative(lexicalRoot, target)
  if (!relative) throw new Error(`refusing mutation of the worktree root itself: ${target}`)
  const outside = relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)
  if (path.resolve(target).split(path.sep).some(component => component.toLowerCase() === ".git")) {
    throw new Error(`refusing mutation under the repository's .git directory: ${target}`)
  }
  if (outside && (isMoveTarget || !insideGitRepo(lexicalRoot))) {
    throw new Error(`refusing mutation path outside worktree: ${target}`)
  }
  await assertExistingPathComponents(lexicalRoot, true)
  await assertExistingPathComponents(target, false)
}

function insideGitRepo(dir: string) {
  for (let current = dir; ; current = path.dirname(current)) {
    if (existsSync(path.join(current, ".git"))) return true
    if (path.dirname(current) === current) return false
  }
}

function patchMoveTargets(args: any) {
  const text = args?.patchText ?? args?.patch_text
  if (typeof text !== "string") return []
  return text.split(/\r?\n/).filter(line => line.startsWith("*** Move to:")).map(line => line.slice("*** Move to:".length).trim())
}

function patchPaths(patchText: unknown) {
  if (typeof patchText !== "string" || !patchText.trim()) throw new Error("missing or malformed apply_patch text")
  const paths: string[] = []
  const prefixes = ["*** Add File:", "*** Update File:", "*** Delete File:", "*** Move to:"]
  for (const line of patchText.split(/\r?\n/)) {
    const prefix = prefixes.find(prefix => line.startsWith(prefix))
    if (!prefix) continue
    const patchPath = line.slice(prefix.length).trim()
    if (!patchPath) throw new Error("apply_patch text contains an empty file marker")
    paths.push(patchPath)
  }
  if (!paths.length) throw new Error("apply_patch text contains no supported file markers")
  return paths
}

const PAYLOAD_TOOL_NAMES: Record<string, string> = {
  bash: "Bash", write: "Write", edit: "Edit", patch: "Patch", apply_patch: "Patch",
  read: "Read", glob: "Glob", grep: "Grep", task: "Task", webfetch: "WebFetch",
}

// Only a real non-orchestrator agent name is ever sent as agent_type; the main session's own
// calls leave the field absent, matching Claude Code's top-level-call shape.
// KNOWN LIMITATION: OpenCode 1.18's tool.execute.before input carries no `agent`, so in production
// this is always undefined and subagent calls are gated as the main session (stricter, never looser).
function guardAgentType(agent: unknown): string | undefined {
  return typeof agent === "string" && agent && agent !== "orchestrator" ? agent : undefined
}

// The payload a Claude Code hook would receive. transcript_path points at the bridge's own
// event log so hooks that look for an override phrase or a prior agent call have a file to read.
export function claudePayload(tool: string, args: any, cwd: string, sessionID: string, agentType?: string) {
  const tool_name = PAYLOAD_TOOL_NAMES[tool] || tool
  const base = { cwd, session_id: sessionID, transcript_path: cacheFile(sessionID, "events.jsonl"), ...(agentType ? { agent_type: agentType } : {}) }
  if (tool === "bash") return { tool_name, tool_input: { command: String(args?.command ?? "") }, ...base }
  if (tool === "edit" || tool === "write" || tool === "patch" || tool === "apply_patch") {
    const sourcePaths = tool === "apply_patch" ? patchPaths(args?.patchText ?? args?.patch_text) : [args?.filePath ?? args?.file_path ?? args?.path]
    const guardedPaths = sourcePaths.map(source => normalizedPath(source, cwd))
    const extra: Record<string, unknown> = {}
    if (tool === "apply_patch") extra.content = args?.patchText ?? args?.patch_text
    for (const key of ["content", "old_string", "new_string"]) {
      const value = args?.[key] ?? args?.[{ old_string: "oldString", new_string: "newString" }[key] ?? key]
      if (typeof value === "string") extra[key] = value
    }
    return { tool_name, tool_input: { file_path: guardedPaths[0], patch_paths: guardedPaths, ...extra }, ...base }
  }
  if (tool === "read") return { tool_name, tool_input: { file_path: normalizedPath(args?.filePath ?? args?.file_path ?? args?.path, cwd) }, ...base }
  if (tool === "webfetch") return { tool_name, tool_input: { url: String(args?.url ?? "") }, ...base }
  return { tool_name, tool_input: args || {}, ...base }
}

function readStream(stream: NodeJS.ReadableStream) {
  return new Promise<string>((resolve, reject) => {
    const chunks: Buffer[] = []
    stream.on("data", chunk => chunks.push(Buffer.from(chunk)))
    stream.once("error", reject)
    stream.once("end", () => resolve(Buffer.concat(chunks).toString("utf8")))
  })
}

function stopProcessGroup(proc: ReturnType<typeof spawn>) {
  try { process.kill(-proc.pid!, "SIGKILL") } catch { try { proc.kill("SIGKILL") } catch {} }
  for (const stream of [proc.stdin, proc.stdout, proc.stderr]) {
    try { stream?.destroy() } catch {}
  }
}

// The hook gets a minimal environment: PATH, HOME, the plugin root its command refers to and,
// when set, the user-scope config pointer. Nothing else from the host process leaks in.
// CLAUDE_PROJECT_DIR is the session directory (so the hooks' own "inside the project" checks work even when
// this process's cwd is elsewhere); AIARCH_CONFIG is forwarded only when userConfigFile() accepted it.
function hookEnv(projectDir?: string) {
  const env: Record<string, string> = { PATH: process.env.PATH || "", HOME: home(), CLAUDE_PLUGIN_ROOT: gatesRoot() }
  for (const key of ["CLAUDE_HOOK_STATE_DIR", "CLAUDE_PLUGIN_DATA", "PYTHONDONTWRITEBYTECODE"]) {
    if (process.env[key]) env[key] = process.env[key]!
  }
  const project = projectDir || process.env.CLAUDE_PROJECT_DIR
  if (project) env.CLAUDE_PROJECT_DIR = project
  const cfg = process.env.AIARCH_CONFIG
  if (cfg && userConfigFile() === cfg) env.AIARCH_CONFIG = cfg
  return env
}

async function spawnHook(command: string, args: string[], payload: unknown, timeoutMs: number): Promise<GuardResult> {
  let proc: ReturnType<typeof spawn> | undefined
  let inputDir: string | undefined
  try {
    inputDir = mkdtempSync(path.join(os.tmpdir(), "aiarch-guard-"))
    const inputPath = path.join(inputDir, "payload.json")
    writeFileSync(inputPath, `${JSON.stringify(payload)}\n`, { mode: 0o600 })
    const inputFd = openSync(inputPath, "r")
    const cwd = typeof (payload as any)?.cwd === "string" && (payload as any).cwd ? (payload as any).cwd as string : undefined
    proc = spawn(command, args, { detached: true, shell: false, stdio: [inputFd, "pipe", "pipe"], env: hookEnv(cwd), ...(cwd ? { cwd } : {}) })
    closeSync(inputFd)
    rmSync(inputDir, { recursive: true, force: true })
    inputDir = undefined
    const stdout = readStream(proc.stdout!)
    const stderr = readStream(proc.stderr!)
    const exited = new Promise<number>((resolve, reject) => {
      proc!.once("error", reject)
      proc!.once("exit", code => resolve(code ?? 1))
    })
    const completed = Promise.all([exited, stdout, stderr])
    let deadlineTimer: ReturnType<typeof setTimeout> | undefined
    const deadline = new Promise<never>((_, reject) => { deadlineTimer = setTimeout(() => reject(new Error("guard deadline exceeded")), timeoutMs) })
    try {
      const [exitCode, stdoutText, stderrText] = await Promise.race([completed, deadline])
      clearTimeout(deadlineTimer)
      return { exitCode, stdout: stdoutText, stderr: stderrText, timedOut: false }
    } catch (error: any) {
      clearTimeout(deadlineTimer)
      const timedOut = error?.message === "guard deadline exceeded"
      stopProcessGroup(proc)
      await Promise.race([completed.catch(() => undefined), new Promise(resolve => setTimeout(resolve, 100))])
      return { exitCode: timedOut ? 124 : 127, stdout: "", stderr: error?.message || "guard execution failed", timedOut }
    }
  } catch (error: any) {
    if (proc) stopProcessGroup(proc)
    if (inputDir) rmSync(inputDir, { recursive: true, force: true })
    return { exitCode: 127, stdout: "", stderr: error?.message || "guard execution failed" }
  }
}

// Runs a full hooks.json command string through a shell, so quoting and ${CLAUDE_PLUGIN_ROOT}
// expand exactly as the Claude Code harness would run them.
export async function runHookCommand(command: string, payload: unknown, timeoutMs = HOOK_TIMEOUT_MS): Promise<GuardResult> {
  return spawnHook("bash", ["-c", command], payload, timeoutMs)
}

// A Stop hook's block reason, or undefined when it does not block.
export function stopAdvisory(result: GuardResult): string | undefined {
  if (result.exitCode !== 0 || !result.stdout.trim()) return
  try {
    const parsed = JSON.parse(result.stdout)
    const hook = parsed?.hookSpecificOutput ?? parsed
    if (hook?.decision === "block" || parsed?.decision === "block") return hook?.reason || parsed?.reason || "Stop guard advisory: review required"
  } catch {}
}

function cacheFile(sessionID: string, suffix: string) {
  return `${home()}/.cache/aiarch-bridge/${String(sessionID || "unknown").replace(/[^A-Za-z0-9_.-]/g, "_")}.${suffix}`
}

async function appendEvent(sessionID: string, value: unknown) {
  const eventPath = cacheFile(sessionID, "events.jsonl")
  await fs.mkdir(path.dirname(eventPath), { recursive: true, mode: 0o700 })
  try { if ((await fs.lstat(eventPath)).isSymbolicLink()) throw new Error(`refusing symlinked hook event file: ${eventPath}`) } catch (error: any) { if (error?.code !== "ENOENT") throw error }
  await fs.appendFile(eventPath, `${JSON.stringify(value)}\n`, { encoding: "utf8", mode: 0o600 })
  await fs.chmod(eventPath, 0o600)
}

// Delivers a Stop-hook reason as a new turn in the SAME session identity (agent and model of
// its latest user message); never guesses an identity. Returns false on any failure so the caller
// neither burns its re-prompt cap nor marks the snapshot delivered.
async function dispatchStopAdvisory(client: any, sessionID: string, text: string, identity: { agent: string; model?: { providerID: string; modelID: string } }): Promise<boolean> {
  if (typeof client?.session?.promptAsync !== "function") {
    console.warn(`[gates-bridge] Stop advisory not dispatched: promptAsync unavailable for ${sessionID}`)
    return false
  }
  let response: any
  try {
    response = await client.session.promptAsync({
      path: { id: sessionID },
      body: {
        agent: identity.agent,
        ...(identity.model ? { model: identity.model } : {}),
        parts: [{ type: "text", text: `<system-reminder>\n${text}\n</system-reminder>`, synthetic: true }],
      },
    })
  } catch (error: any) {
    console.warn(`[gates-bridge] Stop advisory not dispatched: ${sessionID}: ${error?.message || error}`)
    return false
  }
  if (response?.error) {
    console.warn(`[gates-bridge] Stop advisory not dispatched: ${sessionID}: ${response.error?.message || response.error}`)
    return false
  }
  return true
}

async function resolveSessionIdentity(client: any, sessionID: string): Promise<{ agent: string; model?: { providerID: string; modelID: string } } | undefined> {
  let response: any
  try { response = await client.session.messages({ path: { id: sessionID } }) } catch { return undefined }
  if (response?.error || !Array.isArray(response?.data)) return undefined
  for (let index = response.data.length - 1; index >= 0; index--) {
    const info = response.data[index]?.info
    if (info?.role === "user" && typeof info.agent === "string" && info.agent) {
      return { agent: info.agent, model: info.model ? { providerID: info.model.providerID, modelID: info.model.modelID } : undefined }
    }
  }
  return undefined
}

export const PATH_SCOPED_TOOLS = ["edit", "write", "patch", "apply_patch"]

// ---- model-based gates: fail closed under OpenCode ----
// no-code-guard and edit-budget-gate read the current model from a Claude Code transcript, which
// OpenCode never writes (and fall back to ~/.claude/settings.json, which proves nothing about the
// OpenCode session). So when either list is configured the bridge denies code Edit/Write itself,
// without detecting any model.
const CODE_EXTS = new Set([".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".sh", ".bash", ".zsh", ".r", ".go", ".rs", ".c",
  ".cpp", ".cc", ".h", ".hpp", ".java", ".kt", ".rb", ".php", ".sql", ".css", ".scss", ".html", ".svelte", ".vue", ".astro",
  ".pl", ".lua", ".ipynb", ".tf", ".tfvars", ".rmd", ".qmd", ".kts", ".gradle", ".xml"])

function jsonObject(file: string): any {
  try { const value = JSON.parse(readFileSync(file, "utf8")); return value && typeof value === "object" ? value : {} } catch { return {} }
}

// Same rule as plugins/gates/hooks/lib/gates_config.py: AIARCH_CONFIG is ignored (default file used)
// when it points inside the project (CLAUDE_PROJECT_DIR or the working directory), because a
// project can set environment variables. Only the user-scope file may configure the gates.
// Project roots: CLAUDE_PROJECT_DIR, the working directory, and every plugin `directory` seen.
const pluginDirs = new Set<string>()
function projectRoots(): string[] {
  return [process.env.CLAUDE_PROJECT_DIR, process.cwd(), ...pluginDirs].filter((dir): dir is string => !!dir)
}
// realpath of the nearest existing ancestor plus the not-yet-existing remainder.
function realish(value: string): string {
  let cur = path.resolve(value)
  const rest: string[] = []
  for (;;) {
    try { return path.join(realpathSync(cur), ...rest) } catch {}
    const parent = path.dirname(cur)
    if (parent === cur) return path.resolve(value)
    rest.unshift(path.basename(cur))
    cur = parent
  }
}
function inside(file: string, root: string): boolean {
  try {
    const rel = path.relative(realish(root), realish(file))
    return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel))
  } catch { return true } // fail closed: unresolvable => treat as inside the project
}
function inProject(file: string): boolean { return projectRoots().some(dir => inside(file, dir)) }
const warned = new Set<string>()
function warnOnce(msg: string) {
  if (warned.has(msg)) return
  warned.add(msg)
  try { process.stderr.write(`ai-architecture gates: ${msg}\n`) } catch {}
}
export function userConfigFile(): string {
  const fallback = path.join(home(), ".config/ai-architecture/gates.json")
  const env = process.env.AIARCH_CONFIG
  if (!env) return fallback
  if (inProject(env)) { warnOnce(`AIARCH_CONFIG ignored: ${env} is inside the project; using ${fallback}`); return fallback }
  return env
}

// True when defaults.json or the user-scope file sets no_code_models / edit_budget_models.
export function modelGatesConfigured(): boolean {
  return [path.join(gatesRoot(), "defaults.json"), userConfigFile()].some(file => {
    const cfg = jsonObject(file)
    return (Array.isArray(cfg.no_code_models) && cfg.no_code_models.length > 0) ||
      (cfg.edit_budget_models && typeof cfg.edit_budget_models === "object" && Object.keys(cfg.edit_budget_models).length > 0)
  })
}

function isCodePath(target: string, content: unknown) {
  const base = path.basename(target).trim().replace(/\.+$/, "")
  if (CODE_EXTS.has(path.extname(base).toLowerCase()) || ["dockerfile", "makefile"].includes(base.toLowerCase())) return true
  if (target.includes("/.github/workflows/")) return true
  if (base && !base.includes(".")) { // extensionless: a shebang makes it a script
    if (typeof content === "string" && content.startsWith("#!")) return true
    try { return readFileSync(target).subarray(0, 2).toString() === "#!" } catch { return false }
  }
  return false
}

export const createGatesBridge = async ({ client, directory }: any, configOverride?: HookConfig) => {
  if (typeof directory === "string" && directory && !pluginDirs.has(directory)) { pluginDirs.add(directory); rootCache = undefined }
  const idleMarkers = new Set<string>()
  const idleInFlight = new Set<string>()
  const deliveredStops = new Set<string>()
  const repromptCounts = new Map<string, number>()
  const config = configOverride ?? loadHookConfig()
  const problem = configProblem(config)

  // Child sessions (sub-agents) never get the Stop nudge: it exists for the main session's
  // own bookkeeping. "unknown" (lookup failed) is never cached and never treated as a child.
  const sessionKinds = new Map<string, "root" | "child">()
  async function sessionKind(id: string): Promise<"root" | "child" | "unknown"> {
    if (sessionKinds.has(id)) return sessionKinds.get(id)!
    try {
      const response = await client.session.get({ path: { id } })
      if (response?.error || response?.data?.id !== id) return "unknown"
      const value: "root" | "child" = response.data.parentID ? "child" : "root"
      sessionKinds.set(id, value)
      return value
    } catch { return "unknown" }
  }
  const sessionStartSeen = new Set<string>()

  return {
    "tool.execute.before": async (input: any, output: any) => {
      if (problem) throw new Error(problem)
      const entries = config.PreToolUse || []
      const claudeToolName = MATCHER_TOOL_NAMES[input.tool]
      const matched = entries.filter(entry => matchesMatcher(entry.matcher, claudeToolName))
      if (PATH_SCOPED_TOOLS.includes(input.tool)) {
        const args = output?.args ?? input?.args
        const paths = mutationPaths(input.tool, args).map(source => mutationAbsolutePath(source, directory))
        const moveTargets = input.tool === "apply_patch" ? patchMoveTargets(args).map(source => mutationAbsolutePath(source, directory)) : []
        for (const guardedPath of paths) await assertContainedMutationPath(guardedPath, directory, moveTargets.includes(guardedPath))
        if (modelGatesConfigured()) {
          const content = args?.content ?? args?.newString ?? args?.new_string
          const code = input.tool === "apply_patch" || paths.some(target => isCodePath(target, content))
          if (code) throw new Error("model-based gates (no_code_models / edit_budget_models) are configured but cannot identify the model under OpenCode; code edits are denied. Edit docs/config files, delegate, or empty those lists in your gates.json.")
        }
      }
      if (!matched.length) return
      const payload = claudePayload(input.tool, output?.args ?? input?.args, directory, input.sessionID, guardAgentType(input.agent))
      const paths = PATH_SCOPED_TOOLS.includes(input.tool) ? (payload.tool_input as any).patch_paths : [undefined]
      for (const guardedPath of paths) {
        const guardedPayload = guardedPath ? { ...payload, tool_input: { ...payload.tool_input, file_path: guardedPath } } : payload
        // Independent hooks for one matched list run concurrently; results are walked in
        // hooks.json order, so the first denial and its reason match a serial run.
        const results = await Promise.all(matched.map(entry => runHookCommand(entry.command, guardedPayload, HOOK_TIMEOUT_MS)))
        for (let index = 0; index < matched.length; index++) {
          const entry = matched[index]
          const decision = decideGuard(results[index], entry.failClosed, hookBasename(entry.command))
          if (!decision.allow) {
            await appendEvent(input.sessionID, { type: "tool_use", id: input.callID, name: payload.tool_name, input: guardedPayload.tool_input, denied: true, reason: decision.reason })
            throw new Error(decision.reason)
          }
        }
      }
      await appendEvent(input.sessionID, { type: "tool_use", id: input.callID, name: payload.tool_name, input: payload.tool_input, attempted: true })
    },
    "tool.execute.after": async (input: any, output: any) => {
      if (PATH_SCOPED_TOOLS.includes(input.tool) || input.tool === "task") {
        const payload = claudePayload(input.tool, input.args, directory, input.sessionID, guardAgentType(input.agent))
        const failed = output?.error || output?.failed === true || output?.success === false || output?.status === "error"
        await appendEvent(input.sessionID, { type: "tool_use", id: input.callID, name: payload.tool_name, input: payload.tool_input, succeeded: !failed, ...(failed ? { failure: "tool result reported failure" } : {}) })
      }
      // PostToolUse hooks (none ship in gates today) may add context; they never block.
      const entries = config.PostToolUse || []
      const matched = entries.filter(entry => matchesMatcher(entry.matcher, MATCHER_TOOL_NAMES[input.tool]))
      if (!matched.length) return
      const payload = claudePayload(input.tool, input.args, directory, input.sessionID, guardAgentType(input.agent))
      const settled = await Promise.allSettled(matched.map(entry => runHookCommand(entry.command, payload, HOOK_TIMEOUT_MS)))
      for (let index = 0; index < matched.length; index++) {
        const outcome = settled[index]
        try {
          if (outcome.status !== "fulfilled") continue
          const context = extractAdditionalContext(outcome.value)
          if (!context) continue
          output.metadata = { ...(output.metadata || {}), [hookBasename(matched[index].command)]: context }
          if (typeof output.output === "string") output.output += `\n${context}`
        } catch { /* PostToolUse never blocks */ }
      }
    },
    "chat.message": async (input: any, output: any) => {
      // SessionStart hooks run once per session, on its first real turn, and their context is
      // spliced into that turn's own parts (so it inherits the turn's agent and model).
      if (!sessionStartSeen.has(input.sessionID)) {
        sessionStartSeen.add(input.sessionID)
        const starters = config.SessionStart || []
        if (starters.length) {
          const contexts: string[] = []
          for (const entry of starters) {
            const context = extractAdditionalContext(await runHookCommand(entry.command, { session_id: input.sessionID, cwd: directory, source: "startup" }, HOOK_TIMEOUT_MS))
            if (context) contexts.push(context)
          }
          if (contexts.length && output?.message?.id) {
            output.parts.push({
              id: `prt_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`,
              messageID: output.message.id,
              sessionID: input.sessionID,
              type: "text",
              synthetic: true,
              text: `\n\n<system-reminder>\n${contexts.join("\n\n")}\n</system-reminder>`,
            })
          }
        }
      }
      // Record the user's own message (never tool or model text) so same-turn hooks that look
      // for an override phrase in the transcript can find it.
      const text = (output?.parts || []).filter((part: any) => part?.type === "text" && part?.synthetic === false).map((part: any) => part.text).join("\n")
      if (text.trim()) await appendEvent(input.sessionID, { type: "user", message: { content: [{ type: "text", text }] } })
    },
    event: async ({ event }: any) => {
      if (event.type !== "session.idle") return
      const sessionID = event.properties.sessionID
      if (idleInFlight.has(sessionID)) return
      idleInFlight.add(sessionID)
      try {
        if ((await sessionKind(sessionID)) === "child") return
        const transcript_path = cacheFile(sessionID, "events.jsonl")
        const marker = `${sessionID}:${await fs.readFile(transcript_path, "utf8").catch(() => "")}`
        if (idleMarkers.has(marker)) return
        const entries = [...(config.Stop || []), ...(config.SubagentStop || [])]
        let blocked = false
        let pending = false
        for (let index = 0; index < entries.length; index++) {
          const entry = entries[index]
          const advisory = stopAdvisory(await runHookCommand(entry.command, { session_id: sessionID, cwd: directory, transcript_path }, HOOK_TIMEOUT_MS))
          if (!advisory) continue
          blocked = true
          const deliveryKey = `${marker}\0${index}`
          if (deliveredStops.has(deliveryKey)) continue
          const count = repromptCounts.get(sessionID) || 0
          if (count >= 3) {
            console.warn(`[gates-bridge] Stop advisory suppressed after 3 consecutive re-prompts: ${sessionID}`)
            deliveredStops.add(deliveryKey)
            continue
          }
          const identity = await resolveSessionIdentity(client, sessionID)
          if (!identity) {
            console.warn(`[gates-bridge] Stop advisory skipped: no resolvable session identity for ${sessionID}`)
            deliveredStops.add(deliveryKey)
            continue
          }
          if (!await dispatchStopAdvisory(client, sessionID, advisory, identity)) {
            pending = true
            continue
          }
          deliveredStops.add(deliveryKey)
          repromptCounts.set(sessionID, count + 1)
        }
        if (!pending) {
          idleMarkers.add(marker)
          for (const key of deliveredStops) if (key.startsWith(`${marker}\0`)) deliveredStops.delete(key)
        }
        if (!blocked) repromptCounts.delete(sessionID)
      } finally {
        idleInFlight.delete(sessionID)
      }
    },
  }
}
