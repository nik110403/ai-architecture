#!/usr/bin/env python3
"""Stop hook: nudge the agent to leave a clean, resumable state at session end.

OPT-IN per project: fires ONLY when the cwd has a plan-pack tracker (TRACKER.md at the root and/or
plans/*/TRACKER.md; worktree duplicates and trackers marked done/abandoned/promoted/parked are
ignored). Silent everywhere else.

Session-scoped detector: walks THIS session's transcript for tool_use nodes. Fires when the
session wrote a non-tracker file (or dispatched a configured builder role, `builder_roles`) and
never wrote a tracker itself, or when a written tracker lacks a "## Resume here" heading. The
moment the tracker is updated the nag clears. Records the last tracker worked on for
resume-inject.py. Contract: {"decision":"block","reason":...} feeds the reason back and the turn
continues; stop_hook_active nudges once. Never exits nonzero. The transcript JSONL format can
change between Claude Code versions.
"""
import sys, json, os, re, glob, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import gates_config
d = gates_config.init("stop")  # payload first; unparseable -> block
CFG = gates_config.load()
gates_config.warn_if_invalid()  # invalid user config: defaults stay in force, say so
BUILDER_ROLES = {gates_config.role(r) for r in (CFG.get("builder_roles") or []) if isinstance(r, str)}


# stop_hook_active no longer skips the walk -- only the nag emission below. The walk +
# write_topic () must run every Stop cycle so the topic-state stays fresh even on a cycle
# that already nudged once (measured walk cost 52ms on the largest transcript on disk).
stop_hook_active = bool(d.get("stop_hook_active"))

cwd = os.path.realpath(d.get("cwd") or os.getcwd())  # canonicalize before file/subprocess use

def _repo_root(cwd):
    # bullet 4: same repo-root realpath containment lib/trackers.py has (defense-in-depth)
    # -- duplicated rather than imported so this file keeps working even if the lib dir is gone.
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return os.path.realpath(r.stdout.strip())
    except Exception:
        pass
    return os.path.realpath(cwd)

def tracker_status(path):
    # first ~4KB is plenty for a header field; matches the template's `**Status: active**`
    # and bare `Status: done` alike. Anchored to a real header field (line-start, case-sensitive
    # `Status:`) so prose mentioning "status: done" mid-line can never match. No match (old
    # trackers, migration pending) -> None -> active.
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            head = fh.read(4000)
    except Exception:
        return None
    m = re.search(r"(?m)^\*{0,2}Status:\s*\**\s*((?i:active|done|abandoned|promoted|parked))", head)
    return m.group(1).lower() if m else None

def has_resume_block(path):
    if not os.path.isfile(path):   # rejects symlinks to FIFO/socket/dir (would hang on read)
        return False
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return re.search(r"(?m)^## Resume here", fh.read(65536)) is not None
    except Exception:
        return False

def has_real_questions(path):
    # An empty/hollow QUESTIONS.md must not silently satisfy the gate -- require at least one
    # real Q-heading or a Parked section. Same regular-file + bounded-read guard as above.
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            head = fh.read(65536)
    except Exception:
        return False
    return re.search(r"(?m)^## (Q\d+|Parked)\b", head) is not None

# opt-in: TRACKER.md at cwd root and/or plans/*/TRACKER.md, excluding worktree duplicates and
# trackers whose header marks the pack done/abandoned/promoted/parked (closed packs never nag).
candidates = []
root_tracker = os.path.join(cwd, "TRACKER.md")
if os.path.isfile(root_tracker):
    candidates.append(root_tracker)
try:
    candidates.extend(sorted(glob.glob(os.path.join(cwd, "plans", "*", "TRACKER.md"))))
except Exception:
    pass
candidates = [c for c in candidates if "/.claude/worktrees/" not in os.path.realpath(c)]
_root = _repo_root(cwd)
candidates = [c for c in candidates
              if os.path.realpath(c) == _root or os.path.realpath(c).startswith(_root + os.sep)]
# Closed packs are not candidates, but WRITING one (closing a pack is the final state update)
# still satisfies the gate -- otherwise a session that closes its pack can never clear the nag.
closed_reals = set(os.path.realpath(c) for c in candidates
                   if tracker_status(c) in ("done", "abandoned", "promoted", "parked"))
candidates = [c for c in candidates if os.path.realpath(c) not in closed_reals]

if not candidates:                      # opt-in: no active tracker -> not a plan-pack project
    sys.exit(0)

def clean(s):                           # neutralize newline/control-char injection into model-facing text
    return "".join(c for c in str(s) if c.isprintable())[:80]

# Bare `except Exception` below is INTENTIONAL and matches review-stop-gate.sh: a Stop hook must
# never crash or exit nonzero. Any failure to read state -> stay silent, let the stop proceed.

tpath = d.get("transcript_path", "")
if not tpath or not os.path.exists(tpath):
    sys.exit(0)
tpath = os.path.realpath(tpath)   # resolve symlinks before opening

wrote_tracker = False
wrote_other = False
written = []
written_trackers = set()
last_tracker = None   # LAST tracker (in transcript walk order) this session wrote --
                       # a QUESTIONS.md write maps to its pack-sibling TRACKER.md
# Only THIS repo-s real trackers (and their pack-sibling QUESTIONS.md) may satisfy the gate. A
# basename match would let a write to any file called TRACKER.md anywhere on disk (scratchpad,
# unrelated repo) silence the hook. QUESTIONS.md counts as a pack-state write alongside the
# tracker itself -- same set, so the existing membership check below covers both for free.
tracker_reals = set(os.path.realpath(c) for c in candidates)
questions_to_tracker = {
    os.path.realpath(os.path.join(os.path.dirname(c), "QUESTIONS.md")): os.path.realpath(c)
    for c in candidates
}
questions_reals = set(questions_to_tracker.keys())
candidate_reals = tracker_reals | questions_reals

def record(p):
    if len(written) < 50:
        written.append(p)

# bullet 2: a Bash command is a TOPIC signal only -- it never sets wrote_tracker/
# wrote_other (those stay Edit/Write/MultiEdit/Task-only, byte-identical nag semantics).
# Matches when the command string names the abspath or cwd-relative path of exactly ONE
# candidate tracker AND carries a write indicator; two-or-more named trackers -> ambiguous,
# no signal.
_BASH_WRITE_INDICATORS = (">>", "> ", "tee ", "sed -i", "open(")
_tracker_path_variants = {}
for _c in candidates:
    _real = os.path.realpath(_c)
    _variants = {_real}
    try:
        _rel = os.path.relpath(_real, cwd)
        if _rel:
            _variants.add(_rel)
    except Exception:
        pass
    _tracker_path_variants[_real] = _variants

def bash_topic_signal(cmd):
    if not cmd:
        return None
    hits = set()
    for real, variants in _tracker_path_variants.items():
        if any(v in cmd for v in variants):
            hits.add(real)
    if len(hits) != 1:
        return None
    if not any(ind in cmd for ind in _BASH_WRITE_INDICATORS):
        return None
    return next(iter(hits))

# bullet 3: Task/Agent dispatch naming exactly one plans/<topic>/ among candidates is a
# WEAK topic signal -- set alongside the existing wrote_other=True, never overriding a later
# direct write (walk order already guarantees "later wins": last_tracker is just overwritten).
_plan_topic_to_tracker = {}
for _c in candidates:
    _real = os.path.realpath(_c)
    _parent = os.path.basename(os.path.dirname(_real))
    _grandparent = os.path.basename(os.path.dirname(os.path.dirname(_real)))
    if _grandparent == "plans":
        _plan_topic_to_tracker[_parent] = _real

def walk(node):
    global wrote_tracker, wrote_other, last_tracker
    if isinstance(node, list):
        for x in node: walk(x)
        return
    if not isinstance(node, dict):
        return
    if node.get("type") == "tool_use":
        name = node.get("name", "")
        inp  = node.get("input", {}) or {}
        if name in ("Edit", "Write", "MultiEdit"):
            fp = inp.get("file_path", "") or ""
            if fp:
                if not os.path.isabs(fp):
                    fp = os.path.join(cwd, fp)
                fp = os.path.realpath(fp)   # collapse .. before any containment test
                if fp in tracker_reals:
                    wrote_tracker = True
                    written_trackers.add(fp)
                    last_tracker = fp
                elif fp in closed_reals:
                    wrote_tracker = True
                elif fp in questions_reals:
                    # A trivial/empty QUESTIONS.md write does not satisfy the gate -- fall
                    # through to "wrote_other" so the nag still fires (and still helps the
                    # topic guess below) instead of silently disappearing.
                    if has_real_questions(fp):
                        wrote_tracker = True
                        last_tracker = questions_to_tracker.get(fp, last_tracker)
                    else:
                        wrote_other = True
                        record(fp)
                elif fp.startswith(cwd + os.sep):
                    wrote_other = True
                    record(fp)
        elif name == "Bash":
            sig = bash_topic_signal(inp.get("command", "") or "")
            if sig:
                last_tracker = sig
        elif name in ("Task", "Agent"):
            # Exact subagent_type match only -- a mere mention of the agent name in a
            # description/prompt must NOT satisfy the gate (anti-spoof, OWASP ASI01).
            sub = gates_config.trusted_role(inp.get("subagent_type", ""))
            if sub in BUILDER_ROLES:
                # Subagent edits are written to a SEPARATE transcript that this hook never
                # reads (same blind spot as review-stop-gate.sh), so treat dispatching one
                # of these as write intent even though we cannot see the resulting paths.
                wrote_other = True
                record("(delegated)")
                task_prompt = inp.get("prompt", "")
                task_desc = inp.get("description", "")
                text = f"{task_prompt} {task_desc}"
                mentioned = set(re.findall(r"plans/([^/\s]+)/", text))
                topic_hits = mentioned & set(_plan_topic_to_tracker.keys())
                if len(topic_hits) == 1:
                    last_tracker = _plan_topic_to_tracker[next(iter(topic_hits))]
    for v in node.values():
        walk(v)

try:
    with open(tpath, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if len(line) > 2_000_000: continue   # guard against a single bloated line
            line = line.strip()
            if not line: continue
            try: walk(json.loads(line))
            except Exception: continue
except Exception:
    sys.exit(0)

# record the last tracker THIS session actually worked on, so resume-inject.py can follow it instead of blind mtime-newest. Every outcome path
# below writes it (or not, if none was seen) -- never before the walk above has completed.
# Import/write failure is silent; this hook-s never-crash contract is untouched.
if last_tracker:
    try:
        from resume_state import write_topic
        write_topic(cwd, last_tracker)
    except Exception:
        pass

# bullet 1: stop_hook_active skips ONLY the nag emission below (already nudged this
# stop-cycle -> let it stop) -- the walk and the write_topic call above already ran.
if stop_hook_active:
    sys.exit(0)

# A written tracker must carry a "## Resume here" heading on disk right now -- checked first,
# independent of wrote_other, so a session that ONLY touched the tracker (no other files) still
# gets caught if it left the block out.
missing_resume = sorted(t for t in written_trackers if not has_resume_block(t))
if missing_resume:
    if len(missing_resume) == 1:
        rel = clean(os.path.relpath(missing_resume[0], cwd))
        msg = (f"{rel} is missing a \"## Resume here\" heading -- add it (state + next "
               f"action) before ending.")
    else:
        names = [clean(os.path.relpath(t, cwd)) for t in missing_resume]
        msg = ("These trackers are missing a \"## Resume here\" heading -- add it (state + "
               "next action) before ending: " + ", ".join(names))
    print(json.dumps({"decision": "block", "reason": msg}))
    sys.exit(0)

if not (wrote_other and not wrote_tracker):
    sys.exit(0)

# Narrow to a single tracker by topic when the written paths point at exactly one plans/<topic>/
plans_prefix = os.path.join(cwd, "plans") + os.sep
topics = set()
for p in written:
    if p.startswith(plans_prefix):
        rest = p[len(plans_prefix):]
        topic = rest.split(os.sep)[0]
        if topic:
            topics.add(topic)

named = None
if len(topics) == 1:
    topic = next(iter(topics))
    guess = os.path.realpath(os.path.join(cwd, "plans", topic, "TRACKER.md"))
    for c in candidates:
        if os.path.realpath(c) == guess:
            named = c
            break

if named:
    rel = clean(os.path.relpath(named, cwd))
    msg = (f"Update the ## Resume block in {rel} (state + next action, overwrite do not "
           f"append) before ending.")
else:
    names = [clean(os.path.relpath(c, cwd)) for c in candidates[:6]]
    extra = len(candidates) - len(names)
    namestr = ", ".join(names) + (f", +{extra} more" if extra > 0 else "")
    msg = (f"Update the ## Resume block (state + next action, overwrite do not append) in "
           f"the tracker matching the topic this session worked on before ending -- "
           f"candidates: {namestr}. The hook cannot guess which topic.")

print(json.dumps({"decision": "block", "reason": msg}))
sys.exit(0)
