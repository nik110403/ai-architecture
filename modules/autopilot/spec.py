"""SPEC / TRACKER / QUESTIONS parser and the freeze hashes. Pure functions over text; reads nothing, runs nothing.

Vocabulary (plan-pack): items are `### ID — title` headings with `- Key: value` lines. `Needs:` ids, `Touches:` literal path
prefixes (no globs), `Blocked-by: Qn`, `Gate: auto|human` (absent = auto), `Lineage:`, `Stakes:`. Done state lives in the
TRACKER status table (`| ID | lane | State | evidence |`).
"""

import hashlib
import re

HEADING = re.compile(r"^###\s+([A-Za-z0-9][A-Za-z0-9._-]*)\b[^\n]*$", re.M)
FIELD = re.compile(r"^-\s+([A-Za-z][A-Za-z-]*):[ \t]*(.*)$")
ACC_KEYS = ("Acceptance", "Verify", "Touches")
GATE_KEYS = ("Needs", "Gate", "Blocked-by")
QHEAD = re.compile(r"^##\s+(Q[A-Za-z0-9._-]*)\b.*$", re.M)


def norm(text):
    return re.sub(r"\s+", " ", text.strip())


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_spec(text):
    """Ordered list of items: {"id", "fields": {key: value}}. A field line may continue on indented lines."""
    heads = list(HEADING.finditer(text))
    items = []
    for i, m in enumerate(heads):
        body = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        fields, last = {}, None
        for line in body.splitlines():
            fm = FIELD.match(line)
            if fm:
                last = fm.group(1)
                fields.setdefault(last, fm.group(2))
            elif last and line.startswith("  ") and line.strip():
                fields[last] += " " + line.strip()
            elif not line.strip():
                last = None
        items.append({"id": m.group(1), "fields": fields})
    return items


def verify_command(fields):
    raw = fields.get("Verify", "").strip()
    m = re.fullmatch(r"`(.+)`", raw, re.S)
    return (m.group(1) if m else raw) or None


def touches(fields):
    return [p.strip() for p in re.split(r"[,\n]", fields.get("Touches", "")) if p.strip()]


def needs(fields):
    raw = fields.get("Needs", "")
    if re.match(r"\s*(none|-)?\s*$", raw, re.I):
        return []
    raw = re.sub(r"^soft\b", "", raw.strip(), flags=re.I)
    return [x for x in re.split(r"[,\s]+", raw) if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", x)]


def gate(fields):
    """'auto' or 'human'. Anything unrecognised is human: fail closed."""
    raw = fields.get("Gate", "auto").strip().lower()
    word = re.split(r"[\s—:-]", raw, 1)[0] if raw else "auto"
    return "auto" if word in ("auto", "") else "human"


def blocked_by(fields):
    raw = fields.get("Blocked-by", "")
    return [x for x in re.split(r"[,\s]+", raw) if re.fullmatch(r"Q[A-Za-z0-9._-]*", x)]


def lineage_root(item):
    return item["fields"].get("Lineage", "").strip().split()[0] if item["fields"].get("Lineage", "").strip() else item["id"]


def stakes(fields):
    return fields.get("Stakes", "STANDARD").strip().split()[0].upper() if fields.get("Stakes", "").strip() else "STANDARD"


def fix_cap(items, item):
    """Fix rounds allowed for the lineage the item belongs to: the root item's Stakes (HIGH 2, else 1)."""
    root = lineage_root(item)
    by_id = {i["id"]: i for i in items}
    src = by_id.get(root, item)
    return 2 if stakes(src["fields"]) == "HIGH" else 1


def parse_tracker_done(text):
    """Set of item ids whose State cell in the tracker table is `done`."""
    done, col = set(), None
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if any(c.lower() == "state" for c in cells):
            col = next(i for i, c in enumerate(cells) if c.lower() == "state")
            continue
        if col is not None and len(cells) > col and cells[col].lower().startswith("done"):
            done.add(cells[0])
    return done


def tracker_rows(text):
    out = set()
    for line in text.splitlines():
        if line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and not set(cells[0]) <= set("-: "):
                out.add(cells[0])
    return out


def mark_done(text, item_id):
    lines, col, out, hit = text.splitlines(), None, [], False
    for line in lines:
        if line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if any(c.lower() == "state" for c in cells):
                col = next(i for i, c in enumerate(cells) if c.lower() == "state")
            elif col is not None and cells and cells[0] == item_id and len(cells) > col:
                cells[col] = "done"
                line = "| " + " | ".join(cells) + " |"
                hit = True
        out.append(line)
    if not hit:
        raise ValueError(f"tracker has no table row for {item_id}")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def open_questions(text):
    """Question ids that are not explicitly closed. An id with no section is open (fail closed) via `is_open`."""
    state = {}
    heads = list(QHEAD.finditer(text or ""))
    for i, m in enumerate(heads):
        body = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        st = re.search(r"^Status:\s*(\w+)", body, re.M | re.I)
        state[m.group(1)] = (st.group(1).lower() if st else "open")
    return state


def is_open(questions, qid):
    return questions.get(qid, "open") not in ("closed", "answered", "resolved")


def freeze(spec_text, questions_text):
    """{"ids": [...], "items": {id: {"acc": sha, "gate": sha}}, "questions": sha|None}"""
    items = parse_spec(spec_text)
    out = {"ids": [i["id"] for i in items], "items": {}, "questions": sha(norm(questions_text)) if questions_text is not None else None}
    for it in items:
        f = it["fields"]
        out["items"][it["id"]] = {
            "acc": sha("\n".join(f"{k}: {norm(f.get(k, ''))}" for k in ACC_KEYS)),
            "gate": sha("\n".join(f"{k}: {norm(f.get(k, ''))}" for k in GATE_KEYS)),
        }
    return out


def drift(frozen, spec_text, questions_text):
    """None, 'verify-drift' or 'gate-changed' for the current plan text against the frozen hashes."""
    now = freeze(spec_text, questions_text)
    if now["ids"] != frozen["ids"]:
        return "verify-drift"
    for i in frozen["ids"]:
        if now["items"][i]["acc"] != frozen["items"][i]["acc"]:
            return "verify-drift"
    for i in frozen["ids"]:
        if now["items"][i]["gate"] != frozen["items"][i]["gate"]:
            return "gate-changed"
    if now["questions"] != frozen["questions"]:
        return "gate-changed"
    return None


def path_matches(path, prefixes):
    for p in prefixes:
        p = p.strip()
        if not p:
            continue
        if p.endswith("/"):
            if path.startswith(p):
                return True
        elif path == p or path.startswith(p + "/"):
            return True
    return False


def disjoint(a, b):
    """Two touches lists overlap when a prefix of one is a prefix of a path of the other."""
    for x in a:
        for y in b:
            xs, ys = x.rstrip("/"), y.rstrip("/")
            if xs == ys or xs.startswith(ys + "/") or ys.startswith(xs + "/"):
                return False
    return True
