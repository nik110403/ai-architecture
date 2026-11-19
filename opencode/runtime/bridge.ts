// Bridge to the continuity CLI (modules/continuity/continuity.py). Plain execFile, no shell.
import { execFile } from "node:child_process"
import { existsSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

export const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..")
export const CONTINUITY = path.join(REPO, "modules", "continuity", "continuity.py")
export const TIMEOUT_MS = 5000
export const MAX_OUTPUT = 64 * 1024

export type RunResult = { code: number; json: any }
export type Run = (args: string[], input?: string) => Promise<RunResult>

export function ledgerFile(directory: string): string {
  return path.join(directory, ".ai-continuity", "ledger", "ledger.json")
}

/** True when `directory` holds a ledger; the only case in which `--project` is passed. */
export function ledgerActive(directory: string): boolean {
  return existsSync(ledgerFile(directory))
}

/** Run the CLI. Any failure (no python, timeout, too much output, not JSON) is {code: -1, json: null}. */
export function runContinuity(
  args: string[],
  input?: string,
  options: { env?: NodeJS.ProcessEnv; timeoutMs?: number } = {},
): Promise<RunResult> {
  return new Promise((resolve) => {
    const fail = { code: -1, json: null }
    try {
      const child = execFile(
        "python3",
        ["-B", CONTINUITY, ...args],
        { timeout: options.timeoutMs ?? TIMEOUT_MS, maxBuffer: MAX_OUTPUT, env: options.env ?? process.env, encoding: "utf8" },
        (error: any, stdout: string) => {
          let code = 0
          if (error) {
            if (typeof error.code !== "number" || error.killed) return resolve(fail)
            code = error.code
          }
          try {
            resolve({ code, json: JSON.parse(stdout) })
          } catch {
            resolve(fail)
          }
        },
      )
      child.on("error", () => resolve(fail))
      child.stdin?.on("error", () => {})
      child.stdin?.end(input ?? "")
    } catch {
      resolve(fail)
    }
  })
}
