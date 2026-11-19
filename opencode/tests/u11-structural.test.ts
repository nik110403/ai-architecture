// A2: the shared bypass corpus through the REAL tree-sitter classifier (bash-structural.ts).
// Only this component may auto-approve a shell command. No corpus entry may be approved; every input the
// parser flags as an error is approve:false; the corpus `allow` list is approved (so the test is not vacuous).
// AIARCH_STRUCTURAL_MODULE points at a (mutated) copy for the mutation check; copies must live in bridge/.
// The pinned parser packages live OUTSIDE the source tree: AIARCH_PARSER_DIR names a dir whose node_modules holds
// web-tree-sitter 0.25.10 + tree-sitter-bash 0.25.0 (see opencode/tests/parser-env.sh for the install line).
// AIARCH_PARSER_DIR set => a load failure is a FAIL. Unset and nothing resolves => SKIPPED, which run.sh reports loudly.
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

const here = path.dirname(fileURLToPath(import.meta.url))
const corpus = JSON.parse(readFileSync(path.join(here, "../../plugins/gates/tests/fixtures/bypass-corpus.json"), "utf8"))
const modPath = process.env.AIARCH_STRUCTURAL_MODULE || path.join(here, "../bridge/bash-structural")
const structural: any = await import(modPath)

let parser: any
try { parser = await structural.loadParser(process.env.AIARCH_PARSER_DIR) } catch (error: any) {
  const msg = `parser packages not loadable (${error?.message || error}); set AIARCH_PARSER_DIR to a dir installed with: bun add --cwd "$AIARCH_PARSER_DIR" web-tree-sitter@0.25.10 tree-sitter-bash@0.25.0`
  if (process.env.AIARCH_REQUIRE_PARSER === "1" || process.env.AIARCH_PARSER_DIR) { console.log(`FAIL ${msg}`); process.exit(1) }
  console.log(`SKIPPED structural corpus: ${msg}`)
  process.exit(0)
}

let failed = 0
function test(name: string, fn: () => void) {
  try { fn(); console.log(`ok   ${name}`) } catch (error: any) { failed++; console.log(`FAIL ${name}: ${error?.message || error}`) }
}
const expand = (e: any) => e.cmd + (e.gen === "megabyte" ? "A".repeat(1 << 20) : "")

test("corpus is large enough and has parse-error inputs built from allowlisted tokens", () => {
  assert.ok(corpus.entries.length >= 20)
  assert.ok(corpus.entries.filter((e: any) => e.parse_error).length >= 2)
  assert.ok(corpus.allow.length >= 5)
})
for (const e of corpus.entries) {
  test(`not approved: ${(e.note || e.cmd).slice(0, 50)}`, () => {
    const v = structural.classifySync(parser, expand(e))
    assert.equal(v.approve, false, `approved: ${JSON.stringify(e.cmd).slice(0, 60)} (${v.reason})`)
    if (e.parse_error) assert.equal(v.parseError, true, "expected the parser to flag an error")
    if (v.parseError) assert.equal(v.approve, false)
  })
}
for (const cmd of corpus.allow) {
  test(`approved read-only command: ${cmd}`, () => {
    const v = structural.classifySync(parser, cmd)
    assert.equal(v.approve, true, v.reason)
  })
}
console.log(failed ? `${failed} FAILED` : "structural corpus tests passed")
process.exit(failed ? 1 : 0)
