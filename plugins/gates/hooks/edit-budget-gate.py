#!/usr/bin/env python3
"""PreToolUse (Edit|Write|MultiEdit|NotebookEdit): per-model edit budget.

Config key `edit_budget_models` maps a model-name substring (e.g. "large-model") to the maximum
number of direct code-file edits the MAIN session may make; the next edit is denied with a
request to delegate the rest to a builder role. Subagents are never gated (they are the
reroute destination). Tiny single edits (<=400 chars, <=4 lines) and non-code files pass.
The user lifts the gate by putting the configured `override_phrase` in their next real message.
Fails CLOSED on an unparseable payload. The model is read from the session transcript JSONL, a format
that can change between Claude Code versions (see README.md).
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

def last_model(path):
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
        if j.get("isSidechain"):
            continue
        m = (j.get("message") or {}).get("model")
        if m:
            return m
    return ""

def user_overrode(path):
    # Most recent real human turn: type=user, content is a plain string or a list
    # containing a {"type":"text"} block. tool_result-only lines are NOT user turns —
    # the model cannot self-override via tool output, so keep scanning past those.
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 2_000_000))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return False
    for line in reversed(tail):
        if "\"type\":\"user\"" not in line and "\"type\": \"user\"" not in line:
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        # model-generated lines masquerade as type:user — never accept them as the human (security review 2026-07-22)
        if j.get("isCompactSummary") or j.get("isMeta") or j.get("isSidechain"):
            continue
        content = (j.get("message") or {}).get("content")
        text = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            if texts:
                text = " ".join(texts)
            else:
                continue  # tool_result-only content — not a real user turn, keep looking
        if text is None:
            continue
        return str(CFG.get("override_phrase") or "override: inline").lower() in text.lower()
    return False

_sid = d.get("session_id")

if d.get("agent_type") or d.get("agent_id"):
    _emit("allow", "subagent_unrestricted", session=_sid,
          agent=(d.get("agent_type") or d.get("agent_id")))
    sys.exit(0)  # subagent path — subagents are the reroute destination, never gated

tool_name = d.get("tool_name") or ""
ti = d.get("tool_input") or {}
p = ti.get("file_path") or ti.get("notebook_path") or ""

if not is_code(p, ti, tool_name):
    _emit("allow", "non_code_file", session=_sid)
    sys.exit(0)

old_string = ti.get("old_string", "")
new_string = ti.get("new_string", "")
NO_CARVEOUT_EXTS = {".kts", ".gradle", ".xml"}
_ext = os.path.splitext(os.path.basename(p).strip().rstrip("."))[1].lower()
if (tool_name == "Edit" and _ext not in NO_CARVEOUT_EXTS
        and len(old_string) + len(new_string) <= 400
        and old_string.count("\n") <= 4 and new_string.count("\n") <= 4):
    _emit("allow", "tiny_edit_carveout", session=_sid)
    sys.exit(0)  # tiny-edit carve-out — delegation round-trip costs more than it saves

transcript_path = d.get("transcript_path") or ""
model = last_model(transcript_path)
budget, budget_key = None, ""
for _k, _v in (CFG.get("edit_budget_models") or {}).items():
    if (isinstance(_k, str) and _k and isinstance(_v, int) and not isinstance(_v, bool) and _v >= 0
            and _k.lower() in model.lower() and len(_k) > len(budget_key)):
        budget, budget_key = _v, _k
if budget is None:
    _emit("allow", "model_without_budget", session=_sid)
    sys.exit(0)  # models without a configured budget are unrestricted

if user_overrode(transcript_path):
    _emit("allow", "user_override_inline", session=_sid)
    sys.exit(0)

sid = d.get("session_id") or ""
if not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
    sid = "unknown"
cdir = gates_config.state_dir()
os.makedirs(cdir, exist_ok=True)
state = os.path.join(cdir, f"edit-budget-{sid}")

n = 0
try:
    fd = os.open(state, os.O_RDONLY | os.O_NOFOLLOW)
    raw = os.read(fd, 256)
    os.close(fd)
    n = int(raw.decode().strip() or "0")
except Exception:
    n = 0
n += 1
try:
    fd = os.open(state, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.write(fd, str(n).encode())
    os.close(fd)
except Exception:
    _emit("error", "state_write_failed", session=_sid)
    sys.exit(0)

if n <= budget:
    _emit("allow", f"under_edit_threshold_{n}", session=_sid)
    sys.exit(0)

# Route UI files to a UI-specific builder role when one is configured.
UI_EXTS = {".css", ".scss", ".sass", ".less", ".tsx", ".jsx", ".vue", ".svelte",
           ".html", ".htm", ".astro"}
_is_ui = _ext in UI_EXTS   # _ext computed above at module scope
_ui_role = str(CFG.get("ui_delegate_role") or "")
_role = _ui_role if (_is_ui and _ui_role) else str(CFG.get("delegate_role") or "builder")
_phrase = str(CFG.get("override_phrase") or "override: inline")

_emit("deny", f"edit_budget_exceeded_{n}", session=_sid)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": (
        f"Edit budget reached: more than {budget} direct code edits this session on a "
        f"'{budget_key}' model. The WHAT is decided -- dispatch the {_role} agent with a spec "
        f"for the remaining edits. Tiny edits still pass automatically; for design-coupled code or "
        f"interleaved debugging the user can put '{_phrase}' in their next message."
    )}}))
