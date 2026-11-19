"""Usage records, context phases and child budgets (stdlib only).

Phase thresholds and budgets are recipient settings from config.json, not a fixed policy; nothing here counts
root sessions. Usage that is unknown stays unknown: it never becomes zero and never makes a session READY.
"""

import math

from modules.continuity import ledger as L

TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")
NORMAL, PREPARE, HANDOFF, UNKNOWN = "NORMAL", "PREPARE", "HANDOFF", "UNKNOWN"


def usage_total(usage):
    """Sum of the known token counts of a result's usage object; None when it is null or has no token count."""
    if not isinstance(usage, dict):
        return None
    known = [usage[k] for k in TOKEN_KEYS if k in usage and L.is_int(usage[k])]
    return int(sum(known)) if known else None


def result_total(result):
    """`usage_total` of a banked result; records written before U04b carry none, so it is derived."""
    if "usage_total" in result:
        return result["usage_total"]
    return usage_total(result.get("usage"))


def budget_state(budget_tokens, total):
    if budget_tokens is None:
        return "unbounded"
    if total is None:
        return "unknown"
    return "exceeded" if total > budget_tokens else "within"


def thresholds(cfg, limit):
    """(prepare, handoff) token counts for a context limit."""
    handoff = limit - cfg["handoff_reserve_tokens"]
    if cfg["handoff_cap_tokens"] is not None:
        handoff = min(handoff, cfg["handoff_cap_tokens"])
    return math.floor(cfg["prepare_fraction"] * handoff), handoff


def limit_of(cfg, rec):
    if rec is None:
        return None
    if rec["limit_tokens"] is not None:
        return rec["limit_tokens"]
    return cfg["context_limits"].get(rec["model"]) if rec["model"] else None


def phase(cfg, rec):
    """NORMAL, PREPARE, HANDOFF, or UNKNOWN when the usage or the limit is not known."""
    limit = limit_of(cfg, rec)
    if rec is None or rec["usage_tokens"] is None or limit is None:
        return UNKNOWN
    prepare, handoff = thresholds(cfg, limit)
    if rec["usage_tokens"] >= handoff:
        return HANDOFF
    return PREPARE if rec["usage_tokens"] >= prepare else NORMAL


def session_view(doc, cfg, key):
    rec = doc["sessions"].get(key)
    return {"session": key, "phase": phase(cfg, rec), "usage_tokens": rec["usage_tokens"] if rec else None,
            "limit_tokens": limit_of(cfg, rec)}


def parse_tokens(value):
    if value == "unknown":
        return None
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        raise L.Refused("--tokens must be an integer >= 0 or 'unknown'")
    if tokens < 0:
        raise L.Refused("--tokens must be an integer >= 0 or 'unknown'")
    return tokens


def record_in(doc, key, tokens, limit=None, model=None, source="cli"):
    """Write the session's usage record into `doc`. A later record without a limit or model keeps the earlier one."""
    owner = L.parse_session(key)
    if source not in L.SOURCES:
        raise L.Refused(f"--source must be one of {list(L.SOURCES)}")
    if tokens is not None and (not L.is_int(tokens) or tokens < 0):
        raise L.Refused("tokens must be an integer >= 0 or unknown")
    if limit is not None and (not L.is_int(limit) or limit < 1):
        raise L.Refused("--limit must be an integer >= 1")
    if model is not None and (not isinstance(model, str) or not model or len(model) > L.MAX_TEXT):
        raise L.Refused("--model must be a short non-empty string")
    old = doc["sessions"].get(key) or {}
    doc["sessions"][key] = {
        "harness": owner["harness"], "session_id": owner["session_id"], "usage_tokens": tokens,
        "limit_tokens": limit if limit is not None else old.get("limit_tokens"),
        "model": model if model is not None else old.get("model"), "observed_at": L.now(), "source": source,
    }
    while len(doc["sessions"]) > L.MAX_SESSIONS:
        oldest = min((k for k in doc["sessions"] if k != key), key=lambda k: (doc["sessions"][k]["observed_at"], k))
        del doc["sessions"][oldest]


def record(ctx, key, tokens, limit=None, model=None, source="cli"):
    with ctx.ledger.transaction() as doc:
        record_in(doc, key, tokens, limit, model, source)
        return 0, dict(session_view(doc, ctx.cfg, key), recorded=True)


# --- admission (start) -----------------------------------------------------------------------------------

def last_checkpoint_revision(doc):
    return doc["checkpoints"][-1]["revision"] if doc["checkpoints"] else 0


def admit_check(doc, cfg, session_key, new_budget):
    """Refuse a `start` at HANDOFF or past the child pool. Returns a note when the session's usage is unknown."""
    note = None
    if session_key is not None:
        view = session_view(doc, cfg, session_key)
        if view["phase"] == HANDOFF:
            raise L.Refused(f"session {session_key} is at HANDOFF ({view['usage_tokens']} of {view['limit_tokens']} "
                            "tokens): bank results and checkpoint before new workers")
        if view["phase"] == UNKNOWN:
            note = f"session {session_key}: context usage or limit unknown, no phase gate applied"
    pool = cfg["child_budget_pool_tokens"]
    if pool is not None:
        since = last_checkpoint_revision(doc)
        if new_budget is None:  # an unbudgeted job would reserve 0 and slip past the pool: the pool needs a budget
            raise L.Refused("child_budget_pool_tokens is set: give --budget-tokens (or set child_budget_tokens)")
        reserved, spent = new_budget, 0
        for job in doc["jobs"].values():
            if job["state"] != "banked":
                reserved += job["budget"]["tokens"] or 0
            elif job["result"]["bank_revision"] > since:
                total = result_total(job["result"])
                if total is None:
                    raise L.Refused("child usage unknown since the last checkpoint: checkpoint before starting more workers")
                spent += total
        if reserved + spent > pool:
            raise L.Refused(f"child budget pool {pool} exceeded: {reserved} reserved + {spent} used since the last checkpoint")
    return note
