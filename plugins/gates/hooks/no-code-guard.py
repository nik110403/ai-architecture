#!/usr/bin/env python3
"""PreToolUse (Edit|Write|MultiEdit|NotebookEdit): deny code-file writes from models that must not code.

Config key `no_code_models` lists model-name substrings (e.g. "planner-model") whose sessions
(main loop, or a subagent running that model) may not write code files; docs/config stay allowed.
Tiny single edits pass unless the file is a build script/manifest. FAILS CLOSED when the model
cannot be determined (transcript unreadable and no `model` in the user settings file) -- but only
when `no_code_models` is non-empty. Bash writes are a known uncovered channel.
The model is read from the session transcript JSONL, a format that can change between
Claude Code versions (see README.md).
"""
import sys, json, os, re, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import gates_config
d = gates_config.init("pretool")  # payload first; unparseable -> deny
CFG = gates_config.load()
gates_config.warn_if_invalid()  # invalid user config: defaults stay in force, say so

def _emit(*a, **k):
    pass  # telemetry is not part of this plugin

CODE_EXTS = {".py",".js",".ts",".tsx",".jsx",".mjs",".cjs",".sh",".bash",".zsh",".r",
             ".go",".rs",".c",".cpp",".cc",".h",".hpp",".java",".kt",".rb",".php",
             ".sql",".css",".scss",".html",".svelte",".vue",".astro",".pl",".lua",".ipynb",
             ".tf",".tfvars",".rmd",".qmd",
             # build scripts + Android sources are executable logic; .yaml/.json/.toml stay
             # allowed as orchestration output.
             ".kts",".gradle",".xml"}
CODE_BASENAMES = {"dockerfile", "makefile"}

def is_code(p, ti, tool_name):
    raw_base = os.path.basename(p)
    base = raw_base.strip().rstrip(".")  # evasion: "evil.py. " / "evil.py." -> evil.py
    if os.path.splitext(base)[1].lower() in CODE_EXTS:
        return True
    if base.lower() in CODE_BASENAMES:
        return True
    if "/.github/workflows/" in p:
        return True
    if raw_base and "." not in raw_base:  # extensionless — sniff a shebang
        if tool_name == "Write":
            return ti.get("content", "").startswith("#!")
        try:
            with open(p, "rb") as f:
                return f.read(2) == b"#!"
        except Exception:
            return False
    return False

def last_model(path, sidechain=False):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 200_000))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return ""
    for line in reversed(tail):
        if "\"type\":\"assistant\"" not in line and "\"type\": \"assistant\"" not in line:
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        if sidechain:
            if not j.get("isSidechain"):
                continue
        else:
            if j.get("isSidechain"):
                continue
        m = (j.get("message") or {}).get("model")
        if m:
            return m
    return ""

NO_CODE = [m.lower() for m in (CFG.get("no_code_models") or []) if isinstance(m, str) and m]
if not NO_CODE:
    sys.exit(0)  # nothing configured: the guard is inert

tool_name = d.get("tool_name") or ""
ti = d.get("tool_input") or {}
p = ti.get("file_path") or ti.get("notebook_path") or ""
_sid = d.get("session_id")

if d.get("agent_type") or d.get("agent_id"):
    # delegated subagent/fork call — sniff the last SIDECHAIN model instead of the
    # main-loop model (transcript_path points at the MAIN transcript for these calls).
    side_model = last_model(d.get("transcript_path") or "", sidechain=True)
    _agent = d.get("agent_type") or d.get("agent_id")
    if not side_model:
        # subagent model cannot be determined (assumes sidechain lines appear in the main
        # transcript): fail closed, as the main-loop branch does.
        _emit("deny", "subagent_model_undetermined_fail_closed", session=_sid, agent=_agent)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Subagent model could not be determined (no sidechain model in the transcript) -- "
                "failing closed. Dispatch the " + str(CFG.get("delegate_role") or "builder") + " agent."
            )}}))
    elif any(m in side_model.lower() for m in NO_CODE):
        _emit("deny", "no_code_subagent_write_denied", session=_sid, agent=_agent)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "This model is configured as no-code (no_code_models): subagents on it must not "
                "write code -- dispatch the " + str(CFG.get("delegate_role") or "builder") + " agent instead."
            )}}))
    else:
        _emit("allow", "allowed_subagent", session=_sid, agent=_agent)
    sys.exit(0)

if not is_code(p, ti, tool_name):
    _emit("allow", "non_code_file", session=_sid)
    sys.exit(0)

old_string = ti.get("old_string", "")
new_string = ti.get("new_string", "")
# The tiny-edit carve-out does NOT extend to build scripts and manifests. A one-LINE
# change there is disproportionately consequential — a <uses-permission>, an exported
# component, or a single dependency line (supply chain) all fit well under 400 chars.
# Without this, the carve-out reopens exactly the gap .kts/.gradle/.xml were added to
# close.
NO_CARVEOUT_EXTS = {".kts", ".gradle", ".xml"}
_ext = os.path.splitext(os.path.basename(p).strip().rstrip("."))[1].lower()
if (tool_name == "Edit" and _ext not in NO_CARVEOUT_EXTS
        and len(old_string) + len(new_string) <= 400
        and old_string.count("\n") <= 4 and new_string.count("\n") <= 4):
    _emit("allow", "tiny_edit_carveout", session=_sid)
    sys.exit(0)  # tiny-edit carve-out — delegation round-trip costs more than it saves

model = last_model(d.get("transcript_path") or "")
if not model:
    try:
        with open(os.path.join(gates_config.home(), ".claude", "settings.json")) as f:
            model = (json.load(f) or {}).get("model", "")
    except Exception:
        model = ""

if not model:
    _emit("deny", "model_undetermined_fail_closed", session=_sid)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "Model could not be determined (transcript+settings unreadable) -- "
            "failing closed. Retry, or dispatch the " + str(CFG.get("delegate_role") or "builder") + " agent."
        )}}))
    sys.exit(0)

if not any(m in model.lower() for m in NO_CODE):
    _emit("allow", "model_allowed", session=_sid)
    sys.exit(0)

_emit("deny", "no_code_model_write_denied", session=_sid)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": (
        f"This model is configured as an orchestrator that does not write code (no_code_models). "
        f"Dispatch the {CFG.get('delegate_role') or 'builder'} agent with a written spec for this "
        f"edit to {p}. Docs/config (.md/.json/.yaml/...) edits remain allowed."
    )}}))
