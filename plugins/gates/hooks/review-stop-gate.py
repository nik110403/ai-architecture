#!/usr/bin/env python3
"""Stop hook: require that review agents ran before the turn ends.

If a UI file was edited this session but the design-review role never ran, or a security-
sensitive code file was edited but the security-review role never ran, block the stop and tell
the model to dispatch it. Roles come from config (`design_review_role`, `security_review_role`,
`ui_builder_roles`) and match after the last ':' (plugin namespacing).
Contract: {"decision":"block","reason":...} feeds the reason back and the turn CONTINUES;
stop_hook_active makes it nudge once per cycle. Only edits that actually succeeded count
(tool_use correlated with its tool_result); unresolved calls count as mutated (fail toward review).
Reads the session transcript JSONL, whose format can change between Claude Code versions.
"""
import sys, json, os, re, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import gates_config
d = gates_config.init("stop")  # payload first; unparseable -> block
CFG = gates_config.load()
gates_config.warn_if_invalid()  # invalid user config: defaults stay in force, say so

def _emit(*a, **k):
    pass  # telemetry is not part of this plugin


_sid = d.get("session_id")

if d.get("stop_hook_active"):          # already nudged this stop-cycle -> let it stop
    _emit("allow", "already_nudged_this_cycle", session=_sid)
    sys.exit(0)

tpath = d.get("transcript_path", "")
if not tpath or not os.path.exists(tpath):
    _emit("allow", "no_transcript_available", session=_sid)
    sys.exit(0)
tpath = os.path.realpath(tpath)   # resolve symlinks before opening

def clean(s):                     # neutralize newline/CR injection into model-facing text
    return str(s).replace("\n", " ").replace("\r", " ")[:200]

UI_EXTS   = {".css",".scss",".tsx",".jsx",".vue",".svelte",".html",".astro"}
CODE_EXTS = {".php",".js",".jsx",".ts",".tsx",".mjs",".cjs",".py",".rb",".go",".java",
             ".cs",".sql",".sh",".bash",".vue",".svelte",".html",".htm",".env",".conf"}
SEC_PAT = re.compile(r"authenticat|authoriz|\blogin\b|\bsession\b|password|\btoken\b|\bsql\b|"
                     r"sanitiz|csrf|\bjwt\b|\bupload\b|\bquery\b|secret|credential", re.I)

# tool_use_id -> (name, input); populated from "assistant" lines only.
tool_uses = {}
# tool_use_id -> True once a paired tool_result (from a "user" line) is seen with
# is_error, OR the line itself carries toolDenialKind (Claude Code's own signal for a
# PreToolUse-hook-denied call, e.g. {"content":[...],"is_error":true}} + top-level
# "toolDenialKind":"permission-rule" on the enclosing line).
tool_denied_or_errored = set()

def collect_uses(node):
    if isinstance(node, list):
        for x in node: collect_uses(x)
        return
    if not isinstance(node, dict):
        return
    if node.get("type") == "tool_use":
        tid = node.get("id")
        if tid:
            tool_uses[tid] = (node.get("name", ""), node.get("input", {}) or {})
    for v in node.values():
        collect_uses(v)

def collect_results(node, line_denied):
    if isinstance(node, list):
        for x in node: collect_results(x, line_denied)
        return
    if not isinstance(node, dict):
        return
    if node.get("type") == "tool_result":
        tid = node.get("tool_use_id")
        if tid and (line_denied or node.get("is_error")):
            tool_denied_or_errored.add(tid)
    for v in node.values():
        collect_results(v, line_denied)

with open(tpath, encoding="utf-8", errors="ignore") as fh:
    for line in fh:
        if len(line) > 2_000_000: continue   # guard against a single bloated line
        line = line.strip()
        if not line: continue
        try: j = json.loads(line)
        except Exception: continue
        t = j.get("type")
        if t == "assistant":
            collect_uses(j)
        elif t == "user":
            collect_results(j, "toolDenialKind" in j)

ui_edited = design_ran = sec_edited = sec_ran = False
ui_files, sec_files = set(), set()

for tid, (name, inp) in tool_uses.items():
    denied = tid in tool_denied_or_errored
    if name in ("Edit", "Write", "MultiEdit"):
        if denied:
            continue  # attempted but never actually mutated the file
        old_s, new_s = inp.get("old_string"), inp.get("new_string")
        if name == "Edit" and old_s is not None and old_s == new_s:
            continue  # true no-op edit -- succeeded, but nothing was mutated
        fp = inp.get("file_path", "") or ""
        ext = os.path.splitext(fp)[1].lower()
        if ext in UI_EXTS:
            ui_edited = True; ui_files.add(fp)
        if ext in CODE_EXTS:
            payload = " ".join(str(inp.get(k, "")) for k in ("new_string","content","command"))
            if SEC_PAT.search(fp + " " + payload):
                sec_edited = True; sec_files.add(fp)
    elif name in ("Task", "Agent"):
        if denied:
            continue  # dispatch attempted but never actually ran
        # Exact subagent_type match only — a mere mention of the agent name in a
        # description/prompt must NOT satisfy the gate (anti-spoof, OWASP ASI01).
        sub = gates_config.trusted_role(inp.get("subagent_type", ""))
        if sub == gates_config.role(CFG.get("design_review_role") or "design-critic"): design_ran = True
        if sub == gates_config.role(CFG.get("security_review_role") or "security-reviewer"): sec_ran = True
        # Subagent edits are written to a SEPARATE transcript that this hook never
        # reads, so delegated UI work is invisible to the ui_edited check above.
        # Treat dispatching the design builder as UI-editing intent, else the whole
        # delegated path (the default per CLAUDE.md) escapes review entirely.
        if sub in [gates_config.role(r) for r in (CFG.get("ui_builder_roles") or [])]:
            ui_edited = True; ui_files.add("(delegated to a UI builder)")

reasons = []
if ui_edited and not design_ran:
    fl = ", ".join(sorted(clean(x) for x in ui_files)) or "UI file(s)"
    reasons.append(f"UI file(s) were edited ({fl}) but the {gates_config.role(CFG.get('design_review_role') or 'design-critic')} agent never ran. "
                   f"Dispatch that read-only agent on the affected page(s) before finishing.")
if sec_edited and not sec_ran:
    fl = ", ".join(sorted(clean(x) for x in sec_files)) or "security-sensitive file(s)"
    reasons.append(f"Security-sensitive code was edited ({fl}) but the {gates_config.role(CFG.get('security_review_role') or 'security-reviewer')} agent "
                   f"never ran. Dispatch that read-only agent on the diff before finishing.")

if reasons:
    # Ledger gets counts only — no persistent index of credential-keyword file
    # paths in the cross-project ledger.
    _emit("deny", f"missing_review ui_files={len(ui_files)} sec_files={len(sec_files)}", session=_sid)
    print(json.dumps({"decision":"block","reason":" ".join(reasons)}))
else:
    _emit("allow", "no_missing_review", session=_sid)
sys.exit(0)
