"""Spend and session reports. Unknown cost stays unknown: it is listed, never summed, never ordered."""

import json
import math
import os
import subprocess
import sys
from pathlib import Path

from modules.observability.cost import cost_of

CONTINUITY = Path(__file__).resolve().parents[1] / "continuity" / "continuity.py"


def _key(values):
    return tuple("" if v is None else "\x01" + v for v in values)


def spend_report(rows, prices):
    groups, unknown, known = {}, [], []
    for index, row in enumerate(rows):
        result = cost_of(row, prices)
        group = groups.setdefault((row["harness"], row["role"], row["model"]), {"rows": 0, "known": [], "unknown_rows": 0})
        group["rows"] += 1
        if result["cost_usd"] is None:
            group["unknown_rows"] += 1
            unknown.append({"index": index, "harness": row["harness"], "role": row["role"], "model": row["model"],
                            "source": row["source"], "usage_class": row["usage_class"], "reason": result["reason"],
                            "reported_cost_usd": row["reported_cost_usd"]})
        else:
            group["known"].append(result["cost_usd"])
            known.append(result["cost_usd"])
    ordered = sorted(groups, key=_key)  # by key, never by cost
    return {
        "rows": len(rows), "known_total": math.fsum(known) if known else None, "complete": not unknown,
        "unknown": unknown,
        "groups": [{"harness": k[0], "role": k[1], "model": k[2], "rows": groups[k]["rows"],
                    "known_cost_usd": math.fsum(groups[k]["known"]) if groups[k]["known"] else None,
                    "unknown_rows": groups[k]["unknown_rows"]} for k in ordered],
        "note": "known_total sums only rows with a computed cost; reported_cost_usd is shown, never summed",
    }


def _unknown_session(session, error):
    return {"session": session, "usage_tokens": None, "limit_tokens": None, "phase": "UNKNOWN", "error": error}


def session_report(project, sessions):
    """Per session usage and phase, read through `continuity.py status` only (never a second store, never a write)."""
    out = []
    for session in sessions:
        argv = [sys.executable, "-B", str(CONTINUITY), "status", "--project", str(project), "--session", session]
        try:
            res = subprocess.run(argv, capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL, env=dict(os.environ))
            doc = json.loads(res.stdout)
        except subprocess.TimeoutExpired:
            out.append(_unknown_session(session, "continuity status timed out"))
            continue
        except (OSError, ValueError):
            out.append(_unknown_session(session, "continuity status gave no readable answer"))
            continue
        if res.returncode != 0 or not isinstance(doc, dict):
            error = doc.get("error") if isinstance(doc, dict) else None
            out.append(_unknown_session(session, str(error or "continuity status failed")[:200]))
            continue
        out.append({"session": session, "usage_tokens": doc.get("usage_tokens"), "limit_tokens": doc.get("limit_tokens"),
                    "phase": doc.get("phase", "UNKNOWN")})
    return {"sessions": out}
