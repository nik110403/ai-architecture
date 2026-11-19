// Native background hub over OpenCode child sessions: READ roles only. Writers use dispatch.py worktrees.
// Every job is registered in the continuity ledger before its prompt is sent and banked exactly once when it ends.
import { mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { REPO, ledgerActive, ledgerFile, type Run } from "./bridge"

export const SOFT_CAP = 4 // child jobs of one coordinator session; over it a start still works but says so
export const HARD_CAP = 15 // child jobs of one coordinator session; over it a start is refused
export const RESULT_CAP = 8000
const NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
const WRITER_REFUSAL = "use dispatch.py: write roles run in an isolated worktree"

type Job = {
  job_id: string
  child: string
  agent: string
  parent: string
  instance: string
  pid?: number // owner process of this entry; a foreign entry is swept only when that process is gone
  ticks?: number // its start time (clock ticks since boot) when /proc was readable, so a recycled pid is not mistaken for it
  banked: boolean
  state: "running" | "finished" | "failed" | "cancelled"
  bank_error?: string
  cause?: string // ledger cause to bank with once the job has ended; set for an orphan so a retried bank keeps it
  resultText?: string
}

export function createHub(opts: {
  client: any
  directory: string
  run: Run
  env?: NodeJS.ProcessEnv
  rolesPath?: string
  budgets?: Map<string, number>
  pid?: number
  sleep?: (ms: number) => Promise<void>
  pollMs?: number // background poll of running jobs for a ledger cancel request; 0 or absent = only on status/wait/cancel
  log?: (message: string) => void
}) {
  const env = opts.env ?? process.env
  if (env.AI_ARCH_JOB_MARKER) return null
  const log = opts.log ?? ((m: string) => console.error(`[hub] ${m}`))
  const rolesPath = opts.rolesPath ?? path.join(REPO, "modules", "orchestration", "roles.json")
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)))
  const pid = opts.pid ?? process.pid
  const instance = `${pid}-${Math.random().toString(36).slice(2, 10)}`
  const stateFile = path.join(opts.directory, ".ai-continuity", "hub-jobs.json")
  const project = () => ["--project", opts.directory]
  const jobs = new Map<string, Job>()
  const inflight = new Map<string, Promise<void>>()
  let swept = false
  const dropped = new Set<string>() // orphans whose ledger row is already banked or absent: removed from the file

  const data = (got: any) => got?.data ?? got

  /** Start time of a process in clock ticks since boot, or undefined when /proc has no readable answer. */
  function startTicks(p: number): number | undefined {
    try {
      const stat = readFileSync(`/proc/${p}/stat`, "utf8")
      const ticks = Number(stat.slice(stat.lastIndexOf(")") + 2).split(" ")[19])
      return Number.isInteger(ticks) ? ticks : undefined
    } catch {
      return undefined
    }
  }
  const myTicks = startTicks(pid)

  /** True only for a live process that is not this one. kill(pid, 0) failing with ESRCH, or a changed start time, means gone. */
  function foreignLive(old: Job): boolean {
    if (!Number.isInteger(old.pid) || old.pid! <= 0 || old.pid === pid) return false
    try {
      process.kill(old.pid!, 0)
    } catch (error: any) {
      if (error?.code !== "EPERM") return false // ESRCH (or anything else): not running
    }
    if (old.ticks !== undefined) {
      const now = startTicks(old.pid!)
      if (now !== undefined && now !== old.ticks) return false // the pid was recycled
    }
    return true
  }

  /** The ledger's cancel request for a job, read from the ledger file (no CLI read command exposes the flag). */
  function cancelRequested(job: Job): boolean {
    try {
      return !!JSON.parse(readFileSync(ledgerFile(opts.directory), "utf8"))?.jobs?.[job.job_id]?.cancel
    } catch {
      return false
    }
  }

  /** The ledger row of a job: "banked", "missing" (readable ledger, no such row), "open", or "unreadable". */
  function ledgerRow(id: string): "banked" | "missing" | "open" | "unreadable" {
    try {
      const row = JSON.parse(readFileSync(ledgerFile(opts.directory), "utf8"))?.jobs?.[id]
      return !row ? "missing" : row.state === "banked" ? "banked" : "open"
    } catch {
      return "unreadable"
    }
  }

  // --- persistence: lets a restarted process find the jobs its predecessor left open -------------------------------
  function persisted(): Record<string, Job> {
    try {
      const doc = JSON.parse(readFileSync(stateFile, "utf8"))
      return doc && doc.version === 1 && doc.jobs && typeof doc.jobs === "object" ? doc.jobs : {}
    } catch {
      return {}
    }
  }
  function persist(extra: Record<string, Job>) {
    const open: Record<string, Job> = { ...extra }
    // read-modify-write: keep every foreign entry this instance has not banked (a dead peer's entry waits for a sweep);
    // one that is banked here is deleted by job_id, whoever wrote it, so it is never swept a second time
    for (const [id, j] of Object.entries(persisted())) if (j.instance !== instance && !j.banked && !jobs.get(id)?.banked && !dropped.has(id)) open[id] = j
    for (const j of jobs.values()) if (!j.banked) open[j.job_id] = j
    for (const [id, j] of Object.entries(open)) if (j.banked) delete open[id]
    const tmp = `${stateFile}.${process.pid}.tmp`
    try {
      writeFileSync(tmp, JSON.stringify({ version: 1, jobs: open }), { mode: 0o600 })
      renameSync(tmp, stateFile)
    } catch (error: any) {
      log(`could not persist hub jobs: ${error?.message || error}`)
    }
  }

  // --- ledger ------------------------------------------------------------------------------------------------------
  async function bankLedger(job: Job, outcome: "succeeded" | "failed", cause: string, text: string): Promise<boolean> {
    const dir = mkdtempSync(path.join(os.tmpdir(), "hub-result-"))
    try {
      const file = path.join(dir, "result.txt")
      writeFileSync(file, text.slice(0, RESULT_CAP), { mode: 0o600 })
      const out = await opts.run(["external", "bank", ...project(), "--job-id", job.job_id, "--outcome", outcome, "--cause", cause, "--result-file", file])
      if (out.code !== 0) {
        job.bank_error = out.json?.error ?? `exit ${out.code}`
        log(`bank of ${job.job_id} failed (${job.bank_error}); it stays open and is retried`)
        return false
      }
      return true
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  }

  /** Bank once: concurrent callers share one attempt, and a banked job is never banked again. */
  function bank(job: Job, outcome: "succeeded" | "failed", cause: string, text: string): Promise<void> {
    if (job.banked) return Promise.resolve()
    const running = inflight.get(job.job_id)
    if (running) return running
    const attempt = (async () => {
      if (await bankLedger(job, outcome, cause, text)) {
        job.banked = true
        delete job.bank_error
        persist({})
      }
    })().finally(() => inflight.delete(job.job_id))
    inflight.set(job.job_id, attempt)
    return attempt
  }

  async function abort(child: string) {
    try {
      await opts.client.session.abort({ path: { id: child } })
    } catch {
      /* already gone */
    }
  }

  /** Once per process: jobs left open by an earlier process are aborted if still running and banked as orphaned. */
  async function sweep() {
    if (swept) return
    swept = true
    if (!ledgerActive(opts.directory)) return
    const left = persisted()
    for (const old of Object.values(left)) {
      if (old.instance === instance || old.banked) continue
      if (foreignLive(old)) continue // a live peer process owns this job: not ours to abort or bank
      const row = ledgerRow(old.job_id)
      if (row === "unreadable") {
        log(`ledger unreadable: orphan ${old.job_id} deferred (kept in the hub jobs file)`)
        continue
      }
      if (row !== "open") { // already banked, or never registered: nothing to abort or bank
        dropped.add(old.job_id)
        continue
      }
      await abort(old.child)
      const job: Job = { ...old, instance, pid, ticks: myTicks, state: "failed", cause: "orphaned_external", banked: false }
      jobs.set(job.job_id, job)
      await bank(job, "failed", "orphaned_external", "orphaned: the process that started this job is gone")
    }
    persist({})
  }

  // --- policy ------------------------------------------------------------------------------------------------------
  // OpenCode falls back to allow for an unmatched command, so only a deny/ask default ("*") is safe.
  const safe = (v: any) => v === "deny" || v === "ask"
  const bashOk = (b: any) => (typeof b === "string" ? safe(b) : !!b && typeof b === "object" && safe((b as any)["*"]))

  /** null when the agent may be a hub child, else the refusal text. Unreadable files or config refuse (fail closed). */
  async function refusal(agent: string): Promise<string | null> {
    let access: string | undefined
    try {
      access = JSON.parse(readFileSync(rolesPath, "utf8"))?.roles?.[agent]?.access
    } catch {
      return `roles file unreadable: ${agent} refused`
    }
    if (access === "write") return WRITER_REFUSAL
    if (access !== "read") return `unknown role ${agent}: only read roles listed in modules/orchestration/roles.json may run in the hub`
    try {
      const perm = data(await opts.client.config.get())?.agent?.[agent]?.permission
      if (perm?.edit !== "deny") return `agent ${agent} is not read-only in the OpenCode config (permission.edit must be deny)`
      if (!bashOk(perm?.bash)) return `agent ${agent} is not read-only in the OpenCode config (permission.bash must be set and not allow)`
    } catch {
      return `OpenCode agent config unreadable: ${agent} refused`
    }
    return null
  }

  // --- observation -------------------------------------------------------------------------------------------------
  async function observe(job: Job): Promise<{ done: boolean; failed: boolean; text: string }> {
    const busy = data(await opts.client.session.status())?.[job.child]
    if (busy && (busy.type === "busy" || busy.type === "retry")) return { done: false, failed: false, text: "" }
    const messages: any[] = data(await opts.client.session.messages({ path: { id: job.child } })) ?? []
    const last = [...messages].reverse().find((m) => m?.info?.role === "assistant")
    if (!last || !last.info?.time?.completed) return { done: false, failed: false, text: "" }
    const text = (last.parts ?? []).filter((p: any) => p?.type === "text" && typeof p.text === "string").map((p: any) => p.text).join("\n")
    return { done: true, failed: !!last.info.error, text }
  }

  /** Move a job to its end state and bank it. Safe to call from status and wait at once. */
  async function settle(job: Job) {
    if (job.banked) return
    if (job.state === "running") {
      const seen = await observe(job)
      if (job.state === "running") { // a concurrent settle or cancel may have moved it while we were observing
        if (seen.done) {
          job.state = seen.failed ? "failed" : "finished"
          job.resultText = seen.failed ? `error: the child session ended with an error\n${seen.text}` : seen.text
        } else if (cancelRequested(job)) {
          job.state = "cancelled"
          job.resultText = "cancelled"
          await abort(job.child)
        } else return
      }
    }
    if (job.state === "finished") await bank(job, "succeeded", "completed", job.resultText ?? "")
    else await bank(job, "failed", job.cause ?? "cancelled", job.resultText ?? "")
  }

  const view = (job: Job) => ({
    job_id: job.job_id, session_id: job.child, agent: job.agent, state: job.state, banked: job.banked,
    ...(job.bank_error ? { bank_error: job.bank_error } : {}),
  })

  // --- actions -----------------------------------------------------------------------------------------------------
  async function start(args: any, callerID: string) {
    await sweep()
    if (!ledgerActive(opts.directory)) throw new Error("the hub needs a continuity ledger: run continuity.py init in this project")
    if (typeof args?.agent !== "string" || typeof args?.prompt !== "string" || !args.prompt.trim()) throw new Error("start needs agent and prompt")
    const caller = data(await opts.client.session.get({ path: { id: callerID } }))
    if (!caller || caller.parentID) throw new Error("only a root session may start hub jobs")
    const refused = await refusal(args.agent)
    if (refused) throw new Error(refused)
    const mine = [...jobs.values()].filter((j) => j.parent === callerID && !j.banked).length
    if (mine >= HARD_CAP) throw new Error(`hub limit reached: ${mine} open jobs for this session (hard cap ${HARD_CAP})`)
    const child = data(await opts.client.session.create({ body: { parentID: callerID, title: String(args.title || `hub: ${args.agent}`).slice(0, 120) } }))
    const childId = child?.id
    if (typeof childId !== "string" || !childId) throw new Error("could not create the child session")
    const job: Job = {
      job_id: `oc-${childId.replace(/[^A-Za-z0-9._-]/g, "_")}`.slice(0, 64), child: childId, agent: args.agent,
      parent: callerID, instance, pid, ticks: myTicks, banked: false, state: "running",
    }
    if (!NAME.test(job.job_id)) {
      await abort(childId)
      throw new Error("child session id cannot be used as a job id")
    }
    const reg = await opts.run(["external", "register", ...project(), "--job-id", job.job_id, "--role", args.agent, "--access", "read", "--session", `opencode:${callerID}`, "--owner-pid", String(pid)])
    if (reg.code !== 0) {
      await abort(childId)
      throw new Error(`ledger register failed (${reg.json?.error ?? `exit ${reg.code}`}): job not started`)
    }
    jobs.set(job.job_id, job)
    persist({})
    if (Number.isInteger(args.budget_tokens) && args.budget_tokens > 0) opts.budgets?.set(childId, args.budget_tokens)
    try {
      await opts.client.session.promptAsync({ path: { id: childId }, body: { agent: args.agent, parts: [{ type: "text", text: args.prompt }] } })
    } catch (error: any) {
      job.state = "cancelled"
      job.resultText = `the prompt could not be sent: ${error?.message || error}`
      await abort(childId)
      await settle(job)
      throw new Error(`could not send the prompt (${error?.message || error}); the job was banked as cancelled`)
    }
    return { ...view(job), ...(mine >= SOFT_CAP ? { over_soft_cap: true } : {}) }
  }

  const find = (id: any) => {
    const job = typeof id === "string" ? jobs.get(id) : undefined
    if (!job) throw new Error(`unknown job ${id}`)
    return job
  }

  async function status(args: any) {
    await sweep()
    const list = args?.job_id ? [find(args.job_id)] : [...jobs.values()]
    for (const job of list) await settle(job)
    return { jobs: list.map((j) => ({ ...view(j), ...(j.state !== "running" && j.resultText ? { result: j.resultText.slice(0, RESULT_CAP) } : {}) })) }
  }

  async function wait(args: any) {
    await sweep()
    const job = find(args?.job_id)
    const limit = Math.min(Number.isFinite(args?.timeout_ms) ? Number(args.timeout_ms) : 60_000, 600_000)
    let waited = 0
    for (;;) {
      await settle(job)
      if (job.state !== "running" || waited >= limit) break
      await sleep(250)
      waited += 250
    }
    return { ...view(job), timed_out: job.state === "running", ...(job.resultText ? { result: job.resultText.slice(0, RESULT_CAP) } : {}) }
  }

  async function cancel(args: any) {
    await sweep()
    const job = find(args?.job_id)
    if (job.banked || job.state !== "running") return view(job)
    job.state = "cancelled"
    job.resultText = "cancelled"
    await abort(job.child)
    await settle(job)
    return view(job)
  }

  async function execute(args: any, callerID: string) {
    switch (args?.action) {
      case "start": return start(args, callerID)
      case "status": return status(args)
      case "wait": return wait(args)
      case "cancel": return cancel(args)
      default: throw new Error("action must be start, status, wait or cancel")
    }
  }

  const poll = opts.pollMs && opts.pollMs > 0
    ? setInterval(() => {
        for (const j of jobs.values()) if (j.state === "running" && !j.banked) void settle(j).catch(() => {})
      }, opts.pollMs)
    : null
  poll?.unref?.()

  return { start, status, wait, cancel, execute, sweep, jobs, stop: () => poll && clearInterval(poll) }
}
