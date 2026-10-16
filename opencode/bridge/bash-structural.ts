// Structural read-only bash classifier (pure logic; wiring lives in bash-structural-hook.ts).
// Parser = web-tree-sitter 0.25.10 + tree-sitter-bash 0.25.0, the exact pins OpenCode 1.18.x ships
// (packages/opencode/src/tool/shell.ts). Fail closed: anything not positively recognised => approve:false
// (the user is asked exactly as today).
//
// approve  : every node is in a tiny whitelist AND every command is in the read-only set below.
// zeroPattern: B-deny trigger. Ports shell.ts collect() (pattern added per `command` node whose first token is
//   not in CWD below) -- OC skips the ask entirely at zero patterns (shell.ts:282). Deny when there are
//   no patterns (or a degenerate command name: empty/MISSING or an arithmetic expansion, whose ask pattern
//   would not show what runs) and the command is not empty / a plain cd|pushd|popd.
import { createRequire } from "node:module"

export type Verdict = { approve: boolean; reason: string; zeroPattern: boolean; parseError: boolean }

// ---- read-only set (ported from the author's bash allow-list `allow` entries) ----
// DASH: exact tokens, optionally followed by `-- <plain words>`.  EXACT: exact tokens only.
// ANY: tokens followed by any plain args (OC wildcard `x *` also matches bare `x`).
// No `git status` / `git log` here: they run repo-local filter.<x>.clean/process programs (via .git/info/attributes,
// and .gitattributes on git < 2.43), so they fall through to the normal permission ask.
const DASH: string[][] = []
const EXACT: string[][] = [
  ...[[], ["--show-current"], ["-a"], ["-r"], ["-v"], ["-vv"], ["--list"]].map(a => ["git", "branch", ...a]),
  ...["date", "pwd", "whoami", "hostname", "uptime", "free", "nproc", "true", "false"].map(c => [c]),
]
const ANY: string[][] = [
  ["git", "rev-parse"], ["git", "ls-files"], ["git", "merge-base"], ["command", "-v"],
  // No file-content readers (cat head tail grep diff cut wc sha256sum md5sum): on a relative path they follow symlinks
  // out of the worktree. No `git diff` / `git show` either: repo-local diff.external / textconv run code.
  // ls, stat, readlink and du are not here: a relative operand may be a symlink pointing outside the project
  // (ls lists the target, du -L / stat -L / readlink -f follow it) and the classifier has no working directory to check.
  // realpath and df are not here either: a relative operand may be a symlink and they print its external target / filesystem.
  // `ls` is allowed only without operands and with a letter-allowlisted flag set (no L, H, R: follow/recurse); see isReadOnly.
  ...["tr", "basename", "dirname", "echo", "exit", "which"].map(c => [c]),
]
// bash-allow.json's `git * --output*` / `git * --ext-diff*` ask rules, applied to every wildcard entry.
const BAD_ARG = /^--(output|ext-diff|textconv)/
// M1: args that could reach outside the cwd / secrets, or expand: applied to every ANY entry.
const PATHY = (a: string) => a.includes("~") || a.includes("..") || /[*?[]/.test(a) || a.startsWith("/") || a.includes("=/") || /^-[A-Za-z]+\//.test(a)
// L1: per-command options that follow/page/exec.
const EXTRA_BAD: Record<string, RegExp[]> = {}
const eq = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i])
const starts = (t: string[], p: string[]) => t.length >= p.length && p.every((x, i) => x === t[i])

export function isReadOnly(t: string[]): boolean {
  if (EXACT.some(e => eq(t, e))) return true
  // `ls` with flags only: any operand could be a symlink to a directory outside the project.
  if (t[0] === "ls") return t.slice(1).every(a => /^-[aAlh1FtrSdcuUvXGgonpqmxCs]+$/.test(a)) && !t.slice(1).some(PATHY)
  for (const e of DASH) {
    if (eq(t, e)) return true
    // rest are plain words (node walk guarantees) but must still be in-repo paths: `git diff -- /dev/null /etc/x`
    // is an implicit --no-index and prints out-of-repo files.
    if (starts(t, e) && t[e.length] === "--") return !t.slice(e.length + 1).some(PATHY)
  }
  return ANY.some(e => starts(t, e) && !t.slice(e.length).some(a => BAD_ARG.test(a) || PATHY(a) || (EXTRA_BAD[t[0]] ?? []).some(r => r.test(a))))
}
export const APPROVE_TABLE = { DASH, EXACT, ANY }

// ---- parser (lazy; load failure surfaces as a throw so the caller can fail open for B) ----
let parserP: Promise<any> | undefined
export const PARSER_LOAD_MS = 10_000 // parser load gets its own, longer budget than the 2 s classify budget
export function loadParser(): Promise<any> {
  parserP ??= (async () => {
    const req = createRequire(import.meta.url)
    const { Parser, Language } = await import("web-tree-sitter")
    await Parser.init({ locateFile: () => req.resolve("web-tree-sitter/tree-sitter.wasm") })
    const lang = await Language.load(req.resolve("tree-sitter-bash/tree-sitter-bash.wasm"))
    const p = new Parser()
    p.setLanguage(lang)
    return p
  })()
  parserP.catch(() => { parserP = undefined })
  return parserP
}

// ---- structural walk ----
const NAMED = new Set(["program", "command", "command_name", "word", "number", "pipeline", "list", "raw_string", "string", "string_content"])
const ANON = new Set(["&&", "||", ";", "|", '"', "'"])
const WORD_BAD = /[$`\\(){}<>|&;!]/
const CONTENT_BAD = /[$`\\]/
// Exact port of OpenCode 1.18.x packages/opencode/src/tool/shell.ts:28 (CWD set; ps/cmd lower-casing is moot: we
// only compare case-sensitively, and a differently-cased name only ever ADDS an OC pattern, i.e. is not zero-pattern).
export const CWD = new Set(["cd", "chdir", "popd", "pushd", "push-location", "set-location"])
const MAX_LEN = 8192

// iterative (explicit stack): a deep tree cannot overflow the JS stack
function walk(root: any): string | null {
  const stack: any[] = [root]
  while (stack.length) {
    const n = stack.pop()
    const r = visit(n)
    if (r) return r
    if (r === null && n.isNamed && n.type !== "word" && n.type !== "number" && n.type !== "string_content" && n.type !== "raw_string")
      for (let i = n.childCount - 1; i >= 0; i--) stack.push(n.child(i))
  }
  return null
}
function visit(n: any): string | null {
  if (n.isMissing) return "missing node"
  if (!n.isNamed) return ANON.has(n.type) ? null : `token ${JSON.stringify(n.type)}`
  if (!NAMED.has(n.type)) return `node ${n.type}`
  const t = n.type
  if (t === "word" || t === "number") {
    if (n.childCount) return `${t} with children`
    if (WORD_BAD.test(n.text)) return "unsafe word char"
    return null
  }
  if (t === "string_content") return n.childCount || CONTENT_BAD.test(n.text) ? "unsafe string content" : null
  if (t === "raw_string") return n.childCount ? "raw_string children" : null
  if (t === "command_name" && !(n.childCount === 1 && n.child(0).type === "word")) return "command name not plain word"
  return null
}

// tokens of a `command` node (all children already vetted by walk): command_name, then args
function tokensOf(cmd: any): string[] {
  const out: string[] = []
  for (let i = 0; i < cmd.childCount; i++) {
    const c = cmd.child(i)
    if (c.type === "command_name" || c.type === "word" || c.type === "number") out.push(c.text)
    else if (c.type === "string" || c.type === "raw_string") out.push(c.text.slice(1, -1))
  }
  return out
}

// shell.ts parts(): tokens OC derives for a command node (bash path)
function ocTokens(cmd: any): string[] {
  const out: string[] = []
  for (let i = 0; i < cmd.childCount; i++) {
    const c = cmd.child(i)
    if (["command_name", "command_name_expr", "word", "string", "raw_string", "concatenation"].includes(c.type)) out.push(c.text)
  }
  return out
}

export function classifySync(parser: any, command: string): Verdict {
  const v = (approve: boolean, reason: string, zeroPattern = false, parseError = false): Verdict => ({ approve, reason, zeroPattern, parseError })
  if (typeof command !== "string") return v(false, "non-string command", false, true)
  if (command.trim() === "") return v(false, "empty command")
  const tree = parser.parse(command)
  if (!tree) return v(false, "parse returned null", true, true)
  try {
    const root = tree.rootNode
    const hasError = root.hasError
    const cmds: any[] = root.descendantsOfType("command").filter(Boolean)
    // B: faithful port of the OC scan
    let patterns = 0
    let degenerate = false
    for (const c of cmds) {
      const tokens = ocTokens(c)
      if (tokens.length && !CWD.has(tokens[0])) patterns++
      const name = c.childForFieldName?.("name") ?? null
      if (name && (name.text === "" || name.child(0)?.type === "arithmetic_expansion" || name.child(0)?.isMissing)) degenerate = true
    }
    // Oversize: still parse and count patterns (B must not deny legit long commands) but never walk/approve.
    const oversize = command.length > MAX_LEN
    const structOk = !oversize && !hasError && !/[\u0000-\u0008\u000b-\u001f\u007f]/.test(command) && walk(root) === null
    const plainCwd = structOk && cmds.length > 0 && cmds.every(c => CWD.has(tokensOf(c)[0]))
    const zeroPattern = !plainCwd && (patterns === 0 || degenerate)
    // A: approval
    if (oversize) return v(false, "command too long", zeroPattern)
    if (hasError) return v(false, "parse error", zeroPattern, true)
    if (!structOk) return v(false, "non-whitelisted construct", zeroPattern)
    if (!cmds.length) return v(false, "no command", zeroPattern)
    const bad = cmds.find(c => !isReadOnly(tokensOf(c)))
    if (bad) return v(false, `not read-only: ${tokensOf(bad)[0]}`, zeroPattern)
    return v(true, "read-only", zeroPattern)
  } finally {
    tree.delete?.()
  }
}

export async function classify(command: string): Promise<Verdict> {
  return classifySync(await loadParser(), command)
}
