"""Guarded outcome-driven reweighting of a rotation pool: a plan, an apply that recomputes it, and a rollback.

Only `matched` evidence moves weights (see evidence.py); outcome evidence never does. Held arms keep their integer
weight verbatim: `pinned: true`, `enabled: false`, weight 0, and arms with fewer than `min_samples` paired tasks. The
movable arms share the movable mass (the sum of their old weights) in proportion to their mean score, clamped to
[floor, cap] by water-filling and rounded by largest remainder. The result must pass both rotation hooks' validation.
Quota is gate-time state: nothing here writes a quota cut into the pool.
"""

import hashlib
import json
import math
import os
import re
import stat

import evidence
from evidence import Refused

MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}\Z")
ROLE_RE = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
MAX_WEIGHT = 1_000_000
DEFAULTS = {"min_samples": 5, "floor": 0.05, "cap": 0.6}
EPS = 1e-9


class Conflict(Exception):
    """Plan, pool or evidence no longer agree (exit 4)."""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(doc):
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


def plan_hash(plan):
    return sha(canonical({k: v for k, v in plan.items() if k != "plan_sha256"}))


def check_params(params):
    ms, floor, cap = params["min_samples"], params["floor"], params["cap"]
    if type(ms) is not int or ms < 1:
        raise Refused("--min-samples must be an integer >= 1")
    for name, value in (("floor", floor), ("cap", cap)):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise Refused(f"--{name} must be a finite number")
    if not 0 <= floor < 1 or not 0 < cap <= 1 or floor > cap:
        raise Refused("need 0 <= floor < 1, 0 < cap <= 1 and floor <= cap")


def waterfill(raw, floor, cap):
    """Shares proportional to `raw`, summing to 1, each within [floor, cap]. Raises ValueError when infeasible."""
    n = len(raw)
    if n == 0 or any(r < 0 or not math.isfinite(r) for r in raw):
        raise ValueError("raw values must be finite and >= 0")
    if floor > cap or cap * n < 1 - EPS:
        raise ValueError("infeasible: need floor <= cap and cap * n >= 1")
    if floor * n > 1 + EPS or sum(raw) <= 0:
        return [1.0 / n] * n
    fixed, free = {}, list(range(n))
    while free:
        mass = 1.0 - sum(fixed.values())
        total = sum(raw[i] for i in free)
        prop = {i: mass * raw[i] / total for i in free} if total > 0 else {i: mass / len(free) for i in free}
        over = [i for i in free if prop[i] > cap + EPS]
        under = [i for i in free if prop[i] < floor - EPS]
        if not over and not under:
            fixed.update(prop)
            break
        excess = sum(prop[i] - cap for i in over)
        deficit = sum(floor - prop[i] for i in under)
        if excess >= deficit:
            fixed.update({i: cap for i in over})
            free = [i for i in free if i not in over]
        if deficit >= excess:
            fixed.update({i: floor for i in under})
            free = [i for i in free if i not in under]
    return [fixed[i] for i in range(n)]


def largest_remainder(shares, mass):
    targets = [s * mass for s in shares]
    base = [math.floor(t + 1e-12) for t in targets]
    left = mass - sum(base)
    if left < 0 or left > len(shares):
        raise Refused("rounding failed")
    order = sorted(range(len(shares)), key=lambda i: (-(targets[i] - base[i]), i))
    for i in order[:left]:
        base[i] += 1
    return base


def valid_entries(entries):
    """True when the hooks' role validation accepts the list (any malformed entry disables the role there)."""
    if not isinstance(entries, list) or not entries:
        return False
    for e in entries:
        if not isinstance(e, dict) or not isinstance(e.get("model"), str) or not MODEL_RE.fullmatch(e["model"]):
            return False
        w = e.get("weight")
        if type(w) is not int or not 0 <= w <= MAX_WEIGHT or not isinstance(e.get("enabled", True), bool):
            return False
    return True


def load_pool(path):
    data, mode = evidence.read_regular(path)
    doc = evidence.strict_json(data, "pool")
    if not isinstance(doc, dict) or not isinstance(doc.get("roles"), dict) or doc.get("version", 1) != 1:
        raise Refused("pool must be an object with version 1 and roles")
    return data, mode, doc


def compute_plan(pool_bytes, pool, evidence_bytes, ev_doc, params):
    check_params(params)
    role = ev_doc.get("role") if isinstance(ev_doc, dict) else None
    if not isinstance(role, str) or not ROLE_RE.fullmatch(role) or role not in pool["roles"]:
        raise Refused("evidence names no role of the pool")
    entries = pool["roles"][role]
    if not valid_entries(entries) or len({e["model"] for e in entries}) != len(entries):
        raise Refused("the role's entries are malformed or repeat a model")
    verdict = evidence.evidence_verdict(ev_doc)
    stats = {r["arm"]: r for r in verdict["ranking"]} if verdict["status"] == "matched" else {}
    before = {e["model"]: e["weight"] for e in entries}
    held, movable = {}, []
    for e in entries:
        m = e["model"]
        if e.get("pinned") is True:
            held[m] = "pinned"
        elif e.get("enabled", True) is False:
            held[m] = "disabled"
        elif e["weight"] == 0:
            held[m] = "weight 0"
        elif m not in stats:
            held[m] = "no matched evidence for this arm"
        elif stats[m]["n"] < params["min_samples"]:
            held[m] = f"fewer than min_samples={params['min_samples']} paired tasks"
        else:
            movable.append(m)
    reasons, after, shares = [], dict(before), {}
    if verdict["status"] == "outcome":
        reasons.append(evidence.OUTCOME_REASON)
    elif verdict["status"] != "matched":
        reasons += ["evidence insufficient: " + r for r in verdict["reasons"]]
    elif len(movable) < 2:
        reasons.append("fewer than 2 movable arms (min_samples, pins and disabled arms hold the rest)")
        if any("min_samples" in why for why in held.values()):
            reasons.append("arms under min_samples keep their weight")
    else:
        raw = [stats[m]["mean"] for m in movable]
        if sum(raw) <= 0:
            reasons.append("all movable arms scored 0: nothing to rank")
        else:
            try:
                fills = waterfill(raw, params["floor"], params["cap"])
            except ValueError as error:
                raise Refused(str(error))
            mass = sum(before[m] for m in movable)
            weights = largest_remainder(fills, mass)
            for m, share, w in zip(movable, fills, weights):
                if abs(w / mass - share) > 1 / mass + EPS or (params["floor"] > 0 and w < 1) or w > MAX_WEIGHT:
                    raise Refused("integer weights cannot meet the floor/cap limits for this pool")
                shares[m], after[m] = share, w
            reasons.append(f"matched evidence: {len(movable)} arms moved within [{params['floor']}, {params['cap']}]")
    plan = {"version": 1, "role": role, "pool_sha256": sha(pool_bytes), "evidence_sha256": sha(evidence_bytes),
            "params": dict(params), "before": before, "after": after, "shares": shares, "held": held,
            "reasons": reasons, "changed": after != before}
    plan["plan_sha256"] = plan_hash(plan)
    return plan


def apply_to_pool(pool, plan):
    new = json.loads(json.dumps(pool))
    entries = new["roles"][plan["role"]]
    for e in entries:
        e["weight"] = plan["after"][e["model"]]
    if not valid_entries(entries):
        raise Refused("reweighted role would not pass the hooks' validation")
    return new


def _atomic_replace(path, data, mode):
    tmp = f"{path}.reweight-{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def dry_run(pool_path, ev_path, out, params):
    pool_bytes, _mode, pool = load_pool(pool_path)
    ev_bytes, _ = evidence.read_regular(ev_path)
    plan = compute_plan(pool_bytes, pool, ev_bytes, evidence.strict_json(ev_bytes, "evidence"), params)
    rel = f"plans/{plan['plan_sha256'][:16]}.json"
    evidence.write_private(out / rel, json.dumps(plan, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    return plan, rel


def apply(pool_path, ev_path, out, plan_path):
    pool_bytes, mode, pool = load_pool(pool_path)
    plan = evidence.load_json_file(plan_path, "plan")
    if not isinstance(plan, dict) or plan.get("plan_sha256") != plan_hash(plan):
        raise Conflict("plan hash does not match its content")
    if plan.get("pool_sha256") != sha(pool_bytes):
        raise Conflict("the pool changed since the plan was made")
    ev_bytes, _ = evidence.read_regular(ev_path)
    if plan.get("evidence_sha256") != sha(ev_bytes):
        raise Conflict("the evidence changed since the plan was made")
    try:
        fresh = compute_plan(pool_bytes, pool, ev_bytes, evidence.strict_json(ev_bytes, "evidence"), plan["params"])
    except (Refused, KeyError, TypeError) as error:
        raise Conflict(f"plan cannot be recomputed: {error}")
    if fresh != plan:
        raise Conflict("plan differs from the one recomputed from pool, evidence and params")
    if not plan["changed"]:
        raise Refused("the plan changes nothing")
    new_bytes = json.dumps(apply_to_pool(pool, plan), indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    ident = plan["plan_sha256"][:16]
    backup, record = out / "backups" / f"{ident}.json", out / "applied" / f"{ident}.json"
    if backup.exists() or record.exists():
        raise Conflict("this plan was already applied; roll it back first")
    written = []
    try:
        evidence.write_private(backup, pool_bytes, exclusive=True)
        written.append(backup)
        evidence.write_private(record, json.dumps({
            "id": ident, "pool_path_sha256": sha(os.path.realpath(pool_path).encode()), "pool_sha256_before": sha(pool_bytes),
            "pool_sha256_after": sha(new_bytes), "mode": mode}, indent=2, sort_keys=True).encode() + b"\n", exclusive=True)
        written.append(record)
        _atomic_replace(os.path.realpath(pool_path), new_bytes, mode)
    except FileExistsError:
        raise Conflict("this plan was already applied; roll it back first")
    except BaseException:
        for path in written:  # nothing was replaced: do not leave a record that blocks a retry
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    return {"applied": ident, "role": plan["role"], "after": plan["after"]}


def rollback(pool_path, out, ident):
    if not re.fullmatch(r"[0-9a-f]{16}", ident):
        raise Refused("rollback id must be 16 hex characters")
    pool_bytes, mode, _pool = load_pool(pool_path)
    record = evidence.load_json_file(out / "applied" / f"{ident}.json", "apply record") if (out / "applied" / f"{ident}.json").exists() else None
    if record is None:
        raise Conflict("no such applied plan")
    if record["pool_path_sha256"] != sha(os.path.realpath(pool_path).encode()):
        raise Conflict("this apply was made on a different pool")
    if sha(pool_bytes) != record["pool_sha256_after"]:
        raise Conflict("the pool changed since the apply; not rolling back over it")
    backup_bytes, _ = evidence.read_regular(out / "backups" / f"{ident}.json")
    if sha(backup_bytes) != record["pool_sha256_before"]:
        raise Conflict("the backup does not match its record")
    _atomic_replace(os.path.realpath(pool_path), backup_bytes, record["mode"])
    done = out / "rolled-back"
    done.mkdir(mode=0o700, exist_ok=True)
    os.replace(out / "backups" / f"{ident}.json", done / f"{ident}.backup.json")
    os.replace(out / "applied" / f"{ident}.json", done / f"{ident}.record.json")
    return {"rolled_back": ident}
