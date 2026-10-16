#!/usr/bin/env python3
"""PreToolUse (Bash): hard floor against irreversible destruction and secret reads.

Reads the hook payload on stdin; prints a permissionDecision deny|ask JSON on a match and
stays silent otherwise. This is a tripwire over the literal command string, not a sandbox.

Design: DENY set (catastrophic/unrecoverable commands, protected-data deletion, secret
reads), ASK set (recoverable-but-costly: force-push, reset --hard, DROP TABLE, ...).
Where the guard cannot resolve what a destructive or secret-reading command touches, it
fails CLOSED. Any exception inside the checks denies ("guard error"), and so does a payload
that does not parse as a JSON object.

Config: protected roots and extra patterns come from user-scope config only (see
lib/gates_config.py). A project may ADD deny/ask regexes via
<cwd>/.claude/destructive-patterns.json but can never suppress a built-in rule.
"""
import sys, json, re, os, stat, signal, threading, shlex, posixpath, fnmatch, time, bisect
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import gates_config
d = gates_config.init("pretool")  # payload first; unparseable -> deny

# --- 1. Heredoc bodies are stripped (so docs/commit messages that merely MENTION dangerous
#        commands are not flagged) ONLY when the heredoc is a pure data sink; everything else --
#        interpreters, pipes, unterminated heredocs, unknown owners -- keeps its body scanned.
#        Failing to strip risks a false positive; failing to scan risks a missed command. ---
HD_RE = re.compile(r"<<(-?)\s*([\"\x27]?)\\?([A-Za-z_][A-Za-z0-9_]*)\2")
FILE_ARG = r"[^\s|;&`$()<>]+"
SINK_PREFIX_RE = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:cat|tee(?:\s+-a)?)\b[^|;&`$()<>]*?(?:>>?\s*" + FILE_ARG + r")?\s*$")
SINK_SUFFIX_RE = re.compile(r"^\s*(?:>>?\s*" + FILE_ARG + r")?\s*$")
MSG_PREFIX_RE = re.compile(r"^\s*(?:git|gh|echo|printf)\b[^`|;&]*?(?:\$\(|`)\s*cat\s*$")

def find_heredoc(ln):
    "(start, end, delim) of the first real << operator outside quotes (never <<<), else None."
    stack = [["n", 0]]
    i, n = 0, len(ln)
    while i < n:
        c, mode = ln[i], stack[-1][0]
        if mode == "s":
            if c == "\x27": stack.pop()
        elif mode == "d":
            if c == "\\": i += 1
            elif c == "\"": stack.pop()
            elif ln.startswith("$(", i):
                stack.append(["n", 0]); i += 1
        else:
            if c == "\\": i += 1
            elif c == "\x27": stack.append(["s", 0])
            elif c == "\"": stack.append(["d", 0])
            elif c == "(": stack[-1][1] += 1
            elif c == ")":
                if stack[-1][1] > 0: stack[-1][1] -= 1
                elif len(stack) > 1: stack.pop()
            elif ln.startswith("<<", i):
                if ln.startswith("<<<", i):
                    i += 3; continue
                m = HD_RE.match(ln, i)
                if m: return (m.start(), m.end(), m.group(3))
                i += 2; continue
        i += 1
    return None

def heredoc_is_sink(prefix, suffix, nxt):
    "True only for cat/tee to a plain file, or a git/gh/echo/printf message via $(cat <<X), with no pipe."
    if "|" in prefix or "|" in suffix or "|" in nxt: return False
    if SINK_PREFIX_RE.match(prefix) and SINK_SUFFIX_RE.match(suffix): return True
    if MSG_PREFIX_RE.match(prefix) and not suffix.strip(): return True
    return False

def strip_heredocs(s):
    # Linear: one pass indexes every line that could terminate a heredoc (by stripped text), then
    # each opener bisects for its first terminator at/after the body start -- no per-opener rescan.
    lines, out, i = s.split("\n"), [], 0
    ends = {}
    for k, l in enumerate(lines): ends.setdefault(l.strip(), []).append(k)
    while i < len(lines):
        ln = lines[i]
        out.append(ln)
        i += 1
        hd = find_heredoc(ln)
        if not hd: continue
        idx = ends.get(hd[2], ())
        p = bisect.bisect_left(idx, i)
        if p >= len(idx): continue            # no terminator: keep the body for scanning
        j = idx[p]
        nxt = lines[j + 1] if j + 1 < len(lines) else ""
        if heredoc_is_sink(ln[:hd[0]], ln[hd[1]:], nxt):
            out.append(lines[j])
            i = j + 1
    return "\n".join(out)

# --- 1b. Undo backslash-escapes on unquoted words (r\m -rf /, g\it reset --hard) that would
#         otherwise defeat the literal command-name matches below. Single-quoted regions are
#         left alone (bash never treats \ as an escape inside \x27...\x27). ---
def deescape(s):
    out, in_single, i, n = [], False, 0, len(s)
    while i < n:
        c = s[i]
        if in_single:
            out.append(c)
            if c == "\x27": in_single = False
            i += 1; continue
        if c == "\x27":
            in_single = True; out.append(c); i += 1; continue
        if c == "\\" and i + 1 < n and s[i + 1].isalnum():
            out.append(s[i + 1]); i += 2; continue
        out.append(c); i += 1
    return "".join(out)

# --- 1c. Cheap textual normalisations that would otherwise defeat command-name matching:
#         $IFS / ${IFS} used as a fake space, $\x27...\x27 ANSI-C escapes, empty quote pairs,
#         and a quoted fragment glued to a bareword whose content is plain command-name
#         characters ("r"m, \x27g\x27it -> rm, git). Quotes containing anything else are left
#         alone. ---
def expand_ifs(s):
    return re.sub(r"\$\{IFS\}|\$IFS\b", " ", s)

def decode_ansi_c_quotes(s):
    def esc(mm):
        e = mm.group(0)
        if e[:2].lower() == "\\x" and len(e) > 2:
            try: return chr(int(e[2:], 16))
            except ValueError: return e
        if len(e) > 1 and e[1] in "01234567":
            try: return chr(int(e[1:], 8))
            except ValueError: return e
        named = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "\x27": "\x27"}
        return named.get(e[1:2], e)
    def repl(m):
        return re.sub(r"\\x[0-9A-Fa-f]{1,2}|\\[0-7]{1,3}|\\.", esc, m.group(1))
    return re.sub(r"\$\x27((?:[^\x27\\]|\\.)*)\x27", repl, s)

def strip_empty_quotes(s):
    return re.sub(r"\"\"|\x27\x27", "", s)

def unwrap_glued_quotes(s):
    return re.sub(r"([\"\x27])([A-Za-z0-9_./-]+)\1", lambda m: m.group(2), s)

def pre_normalize(s):
    return unwrap_glued_quotes(strip_empty_quotes(decode_ansi_c_quotes(expand_ifs(s))))

# Command position: start of string, or right after a shell separator / subshell / brace-group
# opener, optionally preceded by sudo / doas / env / command / builtin / exec / nice / nohup /
# time / timeout prefixes (which may chain: `sudo nice timeout 5 rm -rf /`).
CMDPOS_SEP = r"(?:^|[;&|`(\n{])\s*"
CMDPOS_PREFIX = (
    r"(?:sudo\s+|doas\s+|command\s+|builtin\s+|exec\s+|nice\s+|nohup\s+|time\s+|timeout\s+\S+\s+"
    r"|env\s+(?:[a-z_][a-z0-9_]*=\S*\s+)*)*"
)
CMDPOS = r"(?:" + CMDPOS_SEP + CMDPOS_PREFIX + r")"

def strip_cmdpos_paths(s):
    "An absolute/relative path in command position (/bin/rm, /usr/bin/git, ./rm) is reduced to\n    its basename so the literal command-name regexes below (which match bare names) still fire."
    pat = r"(" + CMDPOS_SEP + CMDPOS_PREFIX + r")((?:\.{1,2}/|/)(?:[^\s/]+/)*)([A-Za-z0-9_.+-]+)"
    return re.sub(pat, lambda m: m.group(1) + m.group(3), s)

SEP_CLASS = r"[;&|)}]"

RULES = [
    (r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (CMDPOS + r"mkfs(\.[a-z0-9]+)?\b", "filesystem format (mkfs)"),
    (CMDPOS + r"wipefs\b", "wipefs — erases filesystem signatures"),
    (CMDPOS + r"dd\b[^|;&]*\bof=/dev/(sd|nvme|vd|hd|mmcblk|disk)", "dd writing to a raw block device"),
    (r">\s*/dev/(sd|nvme|vd|hd|mmcblk)\b", "redirect overwriting a raw block device"),
    (CMDPOS + r"chmod\b[^|;&]*\s-(?=[a-z]*r)[a-z]+\s+0?7?77?\s+/(\s|$)", "recursive chmod on /"),
    (CMDPOS + r"chmod\b[^|;&]*\s-(?=[a-z]*r)[a-z]+\s+[0-7]{3,4}\s+/(\s|$)", "recursive chmod on /"),
    (CMDPOS + r"shred\b[^|;&]*\s/dev/", "shred on a block device"),
]

def rm_is_catastrophic(c):
    if not re.search(CMDPOS + r"rm\b", c): return False
    flags = re.findall(r"-[a-zA-Z]+", c)
    joined = "".join(flags)
    if not ("r" in joined.lower() and "f" in joined.lower()): return False
    bad = [
        r"\s/(\s|$|\*|" + SEP_CLASS + r")",
        r"\s/\*",
        r"\s~(/\s|/\*|/$|\s|$|/" + SEP_CLASS + r"|" + SEP_CLASS + r")",
        r"\s\$home(/|\s|$|" + SEP_CLASS + r")",
        r"\s\.\.?(\s|$|" + SEP_CLASS + r")\s*\*?$",
        r"\s\*\s*$",
        r"\s/(etc|usr|bin|boot|var|lib|home|root|dev|sys|proc)(\s|/\s|/\*|$|" + SEP_CLASS + r")",
    ]
    return any(re.search(b, c) for b in bad)

# --- 2. ASK tier: medium-risk, recoverable-but-costly ops (pattern list derived from the
#        gstack project's safety hooks; see LICENSES.md in the plugin root). ---
def force_flagged(c, cmd_pat):
    "True if CMDPOS + cmd_pat is found and its trailing args carry -f (short) or --force(-with-lease)."
    m = re.search(CMDPOS + cmd_pat + r"\b([^;&|`]*)", c)
    if not m: return False
    args = m.group(1)
    shorts = "".join(re.findall(r"(?<!\S)-[a-zA-Z]+", args))
    return "f" in shorts or bool(re.search(r"--force(-with-lease)?\b", args))

ASK_RULES = [
    (lambda c: force_flagged(c, r"git\s+push"), "git push --force / -f — rewrites remote history"),
    (CMDPOS + r"git\s+reset\b[^;&|`]*--hard\b", "git reset --hard — discards uncommitted work"),
    (CMDPOS + r"git\s+checkout\s+(--\s+)?\.\s*($|[;&|])", "git checkout . — discards all uncommitted changes"),
    (CMDPOS + r"git\s+restore\s+\.\s*($|[;&|])", "git restore . — discards all uncommitted changes"),
    (r"\bdrop\s+table\b", "DROP TABLE — irreversible schema/data loss"),
    (r"\btruncate\b", "TRUNCATE — irreversible data loss"),
    (CMDPOS + r"kubectl\s+delete\b", "kubectl delete — removes a live cluster resource"),
    (lambda c: force_flagged(c, r"docker\s+rm"), "docker rm -f — force-removes a container"),
    (CMDPOS + r"docker\s+system\s+prune\b", "docker system prune — bulk-deletes docker resources"),
]

# --- 3. Additive patterns (user config + <cwd>/.claude/destructive-patterns.json) may ADD deny/ask rules,
#        never suppress a baseline one (it is only ever consulted after the built-ins above).
#        Untrusted regexes (project-authored) get a length/nested-quantifier/alternation reject
#        up front AND a per-pattern time budget at match time — a hung/slow pattern is skipped,
#        not allowed to stall every Bash call (baseline rules above are unaffected either way). ---
MAX_PROJECT_PATTERN_LEN = 200
MAX_COMMAND_BYTES = 8192      # longer commands are denied before any scanning (quadratic tokenising)
MAX_PROJECT_PATTERNS = 32     # per-file cap on project deny+ask regexes; more -> deny (config error)
PROJECT_REGEX_BUDGET = 1.0    # total seconds for ALL project/configured regex matching; exhausted -> deny
NESTED_QUANTIFIER = re.compile(r"\([^()]*[+*][^()]*\)[+*{]")
ALT_QUANTIFIER = re.compile(r"\([^()]*\|[^()]*\)[+*{]")

def pattern_is_safe(pat):
    if len(pat) > MAX_PROJECT_PATTERN_LEN: return False
    if NESTED_QUANTIFIER.search(pat): return False
    if ALT_QUANTIFIER.search(pat): return False
    return True

MAX_PROJECT_FILE_BYTES = 65536

def _inside_dir(path, root):
    try:
        path, root = os.path.realpath(path), os.path.realpath(root)
        return os.path.commonpath([path, root]) == root
    except Exception:
        return False

def load_project_rules():
    cwd = d.get("cwd") or os.getcwd()
    path = os.path.join(cwd, ".claude", "destructive-patterns.json")
    deny, ask = [], []
    n_project = 0
    for pat in _CFG.get("deny_patterns", []) or []:
        if isinstance(pat, str) and pattern_is_safe(pat):
            try: deny.append((re.compile(pat, re.IGNORECASE), f"configured rule (deny): {pat}"))
            except re.error: pass
    for pat in _CFG.get("ask_patterns", []) or []:
        if isinstance(pat, str) and pattern_is_safe(pat):
            try: ask.append((re.compile(pat, re.IGNORECASE), f"configured rule (ask): {pat}"))
            except re.error: pass
    err = None
    try:
        try:
            if not _inside_dir(path, cwd):
                raise OSError("rule file resolves outside the project directory")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return deny, ask, 0, None
        except OSError as e:
            return deny, ask, 0, f"cannot open .claude/destructive-patterns.json safely ({e.__class__.__name__}: symlink or unreadable)"
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return deny, ask, 0, ".claude/destructive-patterns.json is not a regular file"
            raw = os.read(fd, MAX_PROJECT_FILE_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > MAX_PROJECT_FILE_BYTES:
            return deny, ask, 0, f".claude/destructive-patterns.json exceeds {MAX_PROJECT_FILE_BYTES} bytes"
        spec = json.loads(raw.decode("utf-8"))
        n_project = len(spec.get("deny", []) or []) + len(spec.get("ask", []) or [])
        if n_project > MAX_PROJECT_PATTERNS:
            return deny, ask, n_project, None  # caller denies; compile nothing from an oversized file
        for pat in spec.get("deny", []) or []:
            if not isinstance(pat, str) or not pattern_is_safe(pat): continue
            try: deny.append((re.compile(pat, re.IGNORECASE), f"project rule (deny): {pat}"))
            except re.error: continue
        for pat in spec.get("ask", []) or []:
            if not isinstance(pat, str) or not pattern_is_safe(pat): continue
            try: ask.append((re.compile(pat, re.IGNORECASE), f"project rule (ask): {pat}"))
            except re.error: continue
    except Exception:
        pass
    return deny, ask, n_project, err

def first_match(text, rules):
    for pat, reason in rules:
        if hasattr(pat, "search"):
            if pat.search(text): return reason
        elif callable(pat):
            if pat(text): return reason
        elif re.search(pat, text):
            return reason
    return None

class _PatternTimeout(Exception): pass
def _alarm(signum, frame): raise _PatternTimeout()

class ScanTimeout(BaseException): pass   # BaseException: internal `except Exception` blocks must not swallow it
MAX_SCAN_SECONDS = 3.0        # wall-clock cap on protected_hit (tokenising is superlinear in command length)

def scan_protected(text, cwd0):
    "protected_hit under a hard wall-clock cap; running out of time is itself a deny."
    if not (hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()):
        return protected_hit(text, cwd0)
    def _scan_alarm(signum, frame): raise ScanTimeout()
    old = signal.signal(signal.SIGALRM, _scan_alarm)
    signal.setitimer(signal.ITIMER_REAL, MAX_SCAN_SECONDS)
    try:
        return protected_hit(text, cwd0)
    except ScanTimeout:
        return f"command too complex to scan within {MAX_SCAN_SECONDS:g}s (split it or run it manually)"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)

class BudgetExhausted(Exception): pass
_regex_left = [PROJECT_REGEX_BUDGET]

def first_match_budgeted(text, rules, budget=0.2):
    # Like first_match, but each compiled pattern gets a hard time budget (a slow pattern is
    # skipped) AND all project/configured matching shares one total budget; when that is used
    # up BudgetExhausted is raised and the caller denies.
    if not rules:
        return None
    can_alarm = hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
    if not can_alarm:
        return first_match(text, rules)
    old = signal.signal(signal.SIGALRM, _alarm)
    try:
        for pat, reason in rules:
            if _regex_left[0] <= 0: raise BudgetExhausted()
            slice_ = min(budget, _regex_left[0])
            t0 = time.monotonic()
            signal.setitimer(signal.ITIMER_REAL, slice_)
            try:
                hit = pat.search(text)
            except _PatternTimeout:
                hit = None
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                _regex_left[0] -= time.monotonic() - t0
            if hit: return reason
        if _regex_left[0] <= 0: raise BudgetExhausted()
    finally:
        signal.signal(signal.SIGALRM, old)
    return None

# --- 4b. Protected-data deletion + secret reads.
#         Runs on the heredoc-stripped text but is NOT limited to command position: command words
#         are matched after start/separator/whitespace/QUOTE, so bash -c "...", sh -c \x27...\x27,
#         eval "...", xargs rm, ssh host \x27...\x27 bodies are seen. ---
Q = "\x27"
HOME = gates_config.home()
_CFG = gates_config.load()
# Built-in floor: standard secret stores. Configured roots/containers are added on top.
_FLOOR_ROOTS = [HOME + "/" + p for p in (".ssh", ".gnupg", ".password-store")]
_cfg_roots = [gates_config.expand(x) for x in _CFG.get("protected_roots", []) if isinstance(x, str)]
_cfg_conts = [gates_config.expand(x) for x in _CFG.get("protected_containers", []) if isinstance(x, str)]
PROT_ROOTS = list(dict.fromkeys(_FLOOR_ROOTS + _cfg_roots))
CONTAINERS = list(dict.fromkeys(_cfg_conts))
# Directory NAMES that are protected wherever they appear (a recursive delete of any path with
# this final component is denied). Floor: secret stores. protected_names adds user-chosen names.
PROT_BASENAMES = {".ssh", ".gnupg", ".password-store"} | {n for n in _CFG.get("protected_names", []) if isinstance(n, str) and n}
PROT_WORD_RE = re.compile("|".join(re.escape(b) for b in sorted(
    PROT_BASENAMES | {posixpath.basename(x) for x in PROT_ROOTS + CONTAINERS if posixpath.basename(x)})))
BROAD = {"/", HOME, "/home"}
BROAD_SRC = BROAD | {HOME + "/" + p for p in (
    ".ssh", ".gnupg", ".password-store", ".claude", ".codex", ".config", ".local/share/opencode",
    ".docker", ".aws")}
SECRET_DIRS = [HOME + "/.ssh", HOME + "/.gnupg", HOME + "/.password-store"]
SECRET_FILES = [HOME + p for p in (
    "/.claude/.credentials.json", "/.codex/auth.json", "/.local/share/opencode/auth.json",
    "/.config/gh/hosts.yml", "/.git-credentials", "/.netrc", "/.aws/credentials",
    "/.docker/config.json", "/.config/rclone/rclone.conf")]
SSH_SAMPLES = [HOME + "/.ssh/" + n for n in ("id_ed25519", "id_rsa", "id_ecdsa", "id_dsa")]
SSH_OK = re.compile(r"^(?:(?:config|known_hosts[^/]*|authorized_keys[^/]*)(?:\.bak[^/]*)?|.*\.pub)$")
KEY_TYPES = "rsa|dsa|ecdsa|ed25519|xmss"
WORD_RE = re.compile(r"(?:^|[\s;&|(`\"\x27{])((?:[^\s;&|\"\x27`(){}]*/)?"
                     r"(?:rm|find|rsync|shred|grep|egrep|fgrep|rg|tar|zip|cp|mv|python[0-9.]*|perl|node|ruby))(?=\s|$)")
SEG_END_RE = re.compile(r"[;&|\n]")
CD_RE = re.compile(r"(?:^|[;&|\n(){`]|\$\()\s*(?:cd|pushd)(?:\s+(?:--\s+)?(\"[^\"]*\"|\x27[^\x27]*\x27|[^\s;&|)}`]+))?(?=\s|$|[;&|)}])")
SKIP_TOK = {"{}", "+", ";", "\\", "\\;"}
FIND_FILTERS = {"-name", "-iname", "-path", "-ipath", "-regex", "-iregex", "-newer", "-mtime", "-mmin",
                "-atime", "-ctime", "-size", "-user", "-perm", "-empty", "-lname", "-wholename"}
EXEC_OPS = ("-exec", "-execdir", "-ok", "-okdir")
NONREAD = {"echo", "printf", "ls", "stat", "test", "[", "touch", "git", "mkdir", "basename", "dirname",
           "realpath", "readlink", "file", "which", "type", "cd", "true", "chmod", "chown"}
MUTATION_RE = re.compile(r"rmtree|remove|unlink|rmdir|rename|replace|system|popen|subprocess|exec|spawn|"
                         r"truncate|shutil|rimraf|\brm\b|fs\.|FileUtils|Dir\.|unlink|write|open\(")
ALWAYS_DENY = [
    (re.compile(r"(?:^|[\s;&|(`\"\x27])gh\s+auth\s+token\b"), "gh auth token prints a credential"),
    (re.compile(r"(?:^|[\s;&|(`\"\x27])git\b[^;&|\n]*\bcredential\s+fill\b"), "git credential fill prints a credential"),
    (re.compile(r"(?:^|[\s;&|(`\"\x27])pass\s+show\b"), "pass show prints a secret"),
]
GLOBCH = re.compile(r"[*?\[]")

def tokenize_seg(seg):
    seg = seg.strip()
    for cand in (seg, seg.rstrip(")}\"" + Q + " ")):
        try: return shlex.split(cand)
        except ValueError: pass
    return seg.split()

def clean_tok(t):
    return t if t == "{}" else t.rstrip(")}")

def resolve(t, cwdv):
    """Classify a path token. Returns None if unresolvable (variable, command substitution, brace
    list, ~user, relative with unknown cwd) else (P, glob_in_dir_component, glob_in_last_component)."""
    t = t.strip()
    if not t or "$(" in t or "`" in t: return None
    if t.startswith("~") and not re.match(r"~(/|$)", t): return None
    t = re.sub(r"^(?:~|\$HOME|\$\{HOME\})(?=/|$)", HOME, t)
    if "$" in t or "{" in t or "}" in t: return None
    t = re.sub(r"(?:/\.?\*)+$", "", t) or "/"
    if not t.startswith("/"):
        if not cwdv: return None
        t = cwdv + "/" + t
    comps = t.rstrip("/").split("/")
    dirglob = any(GLOBCH.search(c) for c in comps[:-1])
    lastglob = bool(GLOBCH.search(comps[-1]))
    return posixpath.normpath(t), dirglob, lastglob

def strict_reason(P):
    if P == "/" or P == HOME or HOME.startswith(P + "/"): return "the home directory or an ancestor of it"
    for r in PROT_ROOTS:
        if P == r: return "protected root " + r
        if r.startswith(P + "/"): return "an ancestor of protected root " + r
    if posixpath.basename(P) in PROT_BASENAMES: return "a path named " + posixpath.basename(P)
    return None

def del_path_reason(P, lastglob=False):
    r = strict_reason(P)
    if r: return r
    if posixpath.dirname(P) == HOME: return "a top-level home entry (never scratch)"
    if posixpath.dirname(P) in CONTAINERS: return "a whole project/history directly under " + posixpath.dirname(P)
    if lastglob:
        for root in PROT_ROOTS:
            if fnmatch.fnmatchcase(root, P): return "a glob matching protected root " + root
    return None

def del_tok_reason(t, cwdv, strict=False):
    "Reason string when deleting token t is forbidden, UNRES when it cannot be resolved, else None."
    raw = re.sub(r"(?:/\.?\*)+$", "", t.rstrip("/")) or t
    if posixpath.basename(raw) in PROT_BASENAMES: return "a path named " + posixpath.basename(raw)
    r = resolve(t, cwdv)
    if r is None or r[1]: return "UNRES"
    return strict_reason(r[0]) if strict else del_path_reason(r[0], r[2])

def short_flags(toks):
    out = ""
    for t in toks:
        if t == "--": break
        if t.startswith("-") and not t.startswith("--"): out += t[1:]
    return out

def flagless(toks):
    out, dd = [], False
    for t in toks:
        if dd: out.append(t)
        elif t == "--": dd = True
        elif not t.startswith("-"): out.append(t)
    return out

def find_starts(toks):
    out = []
    for t in toks:
        if t in ("-H", "-L", "-P", "-D", "-O"): continue
        if t.startswith("-") or t in ("(", "!", "\\("): break
        out.append(t)
    return out

def long_opt(t, name, minlen=3):
    return len(t) >= minlen and t.startswith("--") and name.startswith(t)

def comp_match(name, pat):
    "bash-style single path component glob: leading dot must be matched explicitly."
    if name.startswith(".") and not pat.startswith("."): return False
    return fnmatch.fnmatchcase(name, pat)

def path_match(P, target):
    pc, tc = P.split("/"), target.split("/")
    return len(pc) == len(tc) and all(comp_match(t, p) for t, p in zip(tc, pc))

def secret_path_reason(P, dirglob, lastglob, nonread):
    glob = dirglob or lastglob
    cands = SECRET_FILES + SSH_SAMPLES + [HOME + "/.gnupg/x", HOME + "/.password-store/x"]
    if glob:
        for c in cands:
            if path_match(P, c): return "glob matching secret material " + c
        bn = posixpath.basename(P)
        if not nonread and len(GLOBCH.sub("", bn)) >= 3:
            for sample in (".env", ".env.local", "k.pem", "id_ed25519", ".npmrc"):
                if comp_match(sample, bn): return "glob matching secret file pattern " + bn
        return None
    if P in SECRET_FILES: return "secret file " + P
    if P.startswith(HOME + "/.ssh/"):
        if not SSH_OK.match(P[len(HOME + "/.ssh/"):]): return "private key material " + P
    for sd in SECRET_DIRS[1:]:
        if P.startswith(sd + "/"): return "secret store " + sd
    bn = posixpath.basename(P)
    if not nonread:
        if bn == ".env" or (bn.startswith(".env.") and bn not in (".env.example", ".env.sample")):
            return "env file " + bn
        if bn == ".npmrc" or bn.endswith(".pem"): return "credential file " + bn
        if re.match(r"^id_(?:" + KEY_TYPES + r")[A-Za-z0-9_-]*$", bn) and not bn.endswith(".pub"):
            return "private key " + bn
    return None

def secret_word_reason(w, cwdv, nonread):
    subs = {w} | set(re.split(r"[\s=:,]+", w))
    for sub in subs:
        sub = sub.strip("\"" + Q + "<>)}(")
        if not sub or sub in (".", ".."): continue
        if sub.startswith("-") and "/" not in sub: continue
        if sub == "*" and not cwdv: continue
        r = resolve(sub, cwdv)
        if r is None: continue
        if re.search(r"/\.?\*$", sub) and r[0] in SECRET_DIRS:
            return "glob over secret dir " + r[0]
        why = secret_path_reason(r[0], r[1], r[2], nonread)
        if why: return why
    return None

def exempt_args(argv0, args, segtext):
    "Indices of args exempt from the secret check (per-tool key use), or None for ALL."
    if argv0 in ("ssh", "autossh", "ssh-copy-id"):
        return {i + 1 for i, a in enumerate(args) if a == "-i"}
    if argv0 == "ssh-keygen":
        for a in args:
            if re.match(r"^-[a-zA-Z]*[ly][a-zA-Z]*$", a): return None
        return set()
    if argv0 == "ssh-add": return None
    if argv0 == "git" and "core.sshcommand" in segtext.lower(): return None
    return set()

def cd_events(text, cwd0):
    ev, cur = [], cwd0
    for m in CD_RE.finditer(text):
        arg = m.group(1)
        if arg is None: cur = HOME
        else:
            arg = arg.strip("\"" + Q)
            if arg == "-": cur = None
            else:
                r = resolve(arg, cur)
                cur = r[0] if (r and not r[1] and not r[2]) else None
        ev.append((m.end(), cur))
    return ev

def protected_hit(text, cwd0):
    events = cd_events(text, cwd0)
    def cwd_at(pos):
        cur = cwd0
        for end, c in events:
            if end <= pos: cur = c
            else: break
        return cur
    UNRES = "unresolvable target; spell the literal path"
    # ---- (a) deletion of protected data + (b2) recursive readers over broad roots ----
    for m in WORD_RE.finditer(text):
        cmd = posixpath.basename(m.group(1))
        rest = text[m.end():]
        e = SEG_END_RE.search(rest)
        seg = rest[:e.start()] if e else rest
        toks = [clean_tok(x) for x in tokenize_seg(seg)]
        sf = short_flags(toks)
        cw = cwd_at(m.start())
        targets, why, strict = [], None, False
        if cmd == "rm":
            rec = bool(re.search(r"[rR]", sf)) or any(long_opt(t, "--recursive") for t in toks)
            if not rec: continue
            targets = [t for t in flagless(toks) if t not in SKIP_TOK]
            why = "recursive rm"
            before = text[:m.start()]
            ps = max(before.rfind(";"), before.rfind("\n"), before.rfind("&&"))
            pipe = before[ps + 1:]
            if re.search(r"\bxargs\b", pipe):
                extra = [clean_tok(x) for x in tokenize_seg(pipe + " " + seg)]
                targets += [t for t in extra if re.match(r"^[/~$]|/|\$", t) and t not in SKIP_TOK]
                why = "xargs rm -r"
        elif cmd == "find":
            starts = find_starts(toks) or ["."]
            dele = "-delete" in toks
            for i, t in enumerate(toks[:-1]):
                if t in EXEC_OPS and posixpath.basename(toks[i + 1]) in ("rm", "shred", "unlink", "truncate", "mv"):
                    dele = True
            strict = any(t in FIND_FILTERS for t in toks)
            if dele: targets, why = starts, "find -delete/-exec rm|shred|unlink|truncate|mv"
            if any(t in EXEC_OPS for t in toks):
                for s in starts:
                    r = resolve(s, cw)
                    if r is None: continue
                    P = r[0]
                    if P in BROAD_SRC:
                        return "find -exec/-ok over broad root " + s + " (can read every secret)"
                    for sd in SECRET_DIRS:
                        if P == sd or P.startswith(sd + "/"):
                            return "find -exec/-ok over secret dir " + s
        elif cmd == "rsync":
            dl = any(t.startswith("--del") for t in toks)
            rs = any(t.startswith("--remove") for t in toks)
            fl = [t for t in flagless(toks) if not re.match(r"^[^/]*:", t)]
            if dl: targets, why = fl[-1:], "rsync --delete destination"
            if rs: targets, why = fl, "rsync --remove-source-files"
        elif cmd == "shred":
            targets, why = flagless(toks), "shred"
        elif cmd == "mv":
            fl = flagless(toks)
            if len(fl) >= 2:
                dest = resolve(fl[-1], cw)
                for s in fl[:-1]:
                    r = resolve(s, cw)
                    if r is None or r[1]: continue
                    P = r[0]
                    if strict_reason(P): return "mv moves away " + s + " -> " + strict_reason(P)
                    par = posixpath.dirname(P)
                    if par in PROT_ROOTS or par in CONTAINERS:
                        if not (dest and (dest[0] + "/").startswith(par + "/")):
                            return "mv moves a direct child of " + par + " away (" + s + ")"
            continue
        elif cmd.startswith(("python", "perl", "node", "ruby")):
            if not any(re.match(r"^-[A-Za-z]*[ce]$", t) for t in toks[:6]): continue
            body = rest[:4000]
            if re.search(r"\.ssh|\.gnupg|\.password-store|\.credentials|auth\.json|\.netrc|\.git-credentials", body):
                return cmd + " one-liner naming a secret store"
            if PROT_WORD_RE.search(body) and MUTATION_RE.search(body):
                return cmd + " one-liner mutating a protected root"
            continue
        elif cmd in ("grep", "egrep", "fgrep", "rg", "zip", "cp", "tar"):
            if cmd in ("grep", "egrep", "fgrep"):
                if not (re.search(r"[rR]", sf) or any(long_opt(t, "--recursive") for t in toks) or "recurse" in toks): continue
                srcs = flagless(toks)
            elif cmd == "rg": srcs = flagless(toks)
            elif cmd == "zip":
                if not re.search(r"r", sf): continue
                srcs = flagless(toks)
            elif cmd == "cp":
                if not (re.search(r"[rRa]", sf) or any(long_opt(t, "--recursive") for t in toks)): continue
                srcs = flagless(toks)[:-1]
            else:
                first = toks[0] if toks else ""
                if not (re.match(r"^-?[a-zA-Z]*c[a-zA-Z]*$", first) or "--create" in toks): continue
                srcs = flagless(toks[1:] if re.match(r"^[a-zA-Z]+$", first) else toks)
            for s in srcs:
                r = resolve(s, cw)
                if r and r[0] in BROAD_SRC:
                    return cmd + " reading broad/secret root " + s + " (recursive read of secrets)"
            continue
        else:
            continue
        for t in targets:
            r = del_tok_reason(t, cw, strict)
            if r == "UNRES": return why + " target " + t + ": " + UNRES
            if r: return why + " targeting " + t + " -> " + r
    # ---- (b) secret reads ----
    for rx, nm in ALWAYS_DENY:
        if rx.search(text): return nm
    for sm in re.finditer(r"[^;&|\n`()]+", text):
        seg = sm.group(0)
        toks = tokenize_seg(seg)
        k = 0
        while k < len(toks) and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[k]) or toks[k] in
                                 ("sudo", "env", "command", "nohup", "time", "nice", "exec")):
            k += 1
        if k >= len(toks): continue
        argv0 = posixpath.basename(toks[k].strip("\"" + Q))
        if argv0 in ("cd", "pushd"): continue
        args = toks[k + 1:]
        ex = exempt_args(argv0, args, seg)
        if ex is None: continue
        cw = cwd_at(sm.start())
        for i, w in enumerate(args):
            if i in ex: continue
            why = secret_word_reason(w, cw, argv0 in NONREAD)
            if why: return "reference to " + why
    return None

def emit(decision, hit, cmd_text):
    safe = cmd_text.replace("\n"," ").replace("\r"," ")[:300]
    verb = "BLOCKED" if decision == "deny" else "FLAGGED"
    reason = (f"{verb} by bash-destructive-guard: {hit}. Command: `{safe}`. "
              + ("This is an irreversible/destructive operation. If you truly intend it, run it "
                 "manually in a terminal outside Claude Code." if decision == "deny" else
                 "This is a recoverable-but-costly operation — confirm you intend it."))
    print(json.dumps({"hookSpecificOutput":{
        "hookEventName":"PreToolUse",
        "permissionDecision":decision,
        "permissionDecisionReason":reason}}))

# --- 5. Everything below inspects a live, possibly-adversarial command string; ANY exception
#        here fails CLOSED (deny). A payload that does not parse is denied by gates_config.init() above. ---
TOTAL_DEADLINE = 6.0  # seconds of process time over the whole of run(); expiry denies

class _Deadline(BaseException): pass
def _deadline_handler(signum, frame): raise _Deadline()

def run():
    cfg_err = gates_config.config_error()
    if cfg_err:
        print(json.dumps({"hookSpecificOutput":{
            "hookEventName":"PreToolUse",
            "permissionDecision":"deny",
            "permissionDecisionReason":cfg_err}}))
        return
    cmd = str(d.get("tool_input", {}).get("command", ""))
    if len(cmd.encode("utf-8", "replace")) > MAX_COMMAND_BYTES:
        emit("deny", f"command longer than the {MAX_COMMAND_BYTES}-byte scan cap (split it or run it manually)", cmd)
        return
    # Best-effort tripwire: continuation emulation is not attempted. A heredoc opener line ending in a
    # backslash makes bash join the next line before locating the body, which this scanner cannot follow -> deny.
    for _ln in cmd.split("\n"):
        if _ln.endswith("\\") and "<<" in _ln and find_heredoc(_ln):
            emit("deny", "heredoc opener line ends in a backslash (line continuation); the guard cannot parse it -- put the heredoc on one line", cmd)
            return
    stripped = re.sub(r"\n\s*", "\n", strip_heredocs(cmd))  # collapse newline runs: CMDPOS regexes are quadratic on them
    # Line continuations (\<newline>) join into the same command; any OTHER newline is a real
    # command separator (same as ";") and is kept so CMDPOS below still recognizes it.
    joined = re.sub(r"\\\n", " ", deescape(pre_normalize(stripped)))
    norm = re.sub(r"[ \t]+", " ", joined).strip()
    norm = strip_cmdpos_paths(norm)
    low = norm.lower()

    # Order matters: built-ins, protected-path and secret checks run BEFORE any project/configured
    # regex, so a slow project pattern can never starve them of the hook timeout.
    deny_hit = "recursive force-delete of a root, home, system or wildcard path" if rm_is_catastrophic(low) else None
    deny_hit = deny_hit or first_match(low, RULES)
    if not deny_hit:
        scan = re.sub(r"\\\n", " ", deescape(pre_normalize(stripped)))
        scan = re.sub(r"[ \t]+", " ", scan)
        deny_hit = scan_protected(scan, d.get("cwd") or os.getcwd())
    if deny_hit:
        emit("deny", deny_hit, norm)
        return
    proj_deny, proj_ask, n_project, cfg_err = load_project_rules()
    if cfg_err:
        emit("deny", f"config error: {cfg_err}", norm)
        return
    if n_project > MAX_PROJECT_PATTERNS:
        emit("deny", f"config error: {n_project} patterns in .claude/destructive-patterns.json (cap {MAX_PROJECT_PATTERNS})", norm)
        return
    try:
        deny_hit = deny_hit or first_match_budgeted(norm, proj_deny)
        ask_hit = None if deny_hit else (first_match(low, ASK_RULES) or first_match_budgeted(norm, proj_ask))
    except BudgetExhausted:
        emit("deny", f"project/configured regex time budget ({PROJECT_REGEX_BUDGET}s) exhausted", norm)
        return
    if deny_hit:
        emit("deny", deny_hit, norm)
        return
    if ask_hit:
        emit("ask", ask_hit, norm)
        return

try:
    if hasattr(signal, "SIGVTALRM"):
        signal.signal(signal.SIGVTALRM, _deadline_handler)
        signal.setitimer(signal.ITIMER_VIRTUAL, TOTAL_DEADLINE)
    run()
    if hasattr(signal, "SIGVTALRM"): signal.setitimer(signal.ITIMER_VIRTUAL, 0)
except _Deadline:
    print(json.dumps({"hookSpecificOutput":{
        "hookEventName":"PreToolUse",
        "permissionDecision":"deny",
        "permissionDecisionReason":f"guard error: total scan deadline ({TOTAL_DEADLINE}s) exceeded -- failing closed."}}))
except Exception as e:
    print(json.dumps({"hookSpecificOutput":{
        "hookEventName":"PreToolUse",
        "permissionDecision":"deny",
        "permissionDecisionReason":f"guard error: {type(e).__name__} — failing closed (see hook logs)."}}))
