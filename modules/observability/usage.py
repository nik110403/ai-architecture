"""Normalised usage rows. A token count that is unknown stays None: it is never 0 and never summed into a total.

Row shape: {harness, role, model, source, tokens: {input, output, cache_read, cache_write, reasoning}: int|None,
usage_class, reported_cost_usd: number|None}. `reported_cost_usd` is what a worker said it cost; it is shown next to
the row and never enters a computed cost, a total or an order.
"""

import importlib.util
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")
PRICED_KEYS = ("input", "output", "cache_read", "cache_write")
MAX_INPUT_BYTES = 8_000_000


def _receipt_report():
    mod = sys.modules.get("receipt_report")
    if mod is None:
        path = ROOT / "skills" / "evaluation" / "scripts" / "receipt_report.py"
        spec = importlib.util.spec_from_file_location("receipt_report", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["receipt_report"] = mod
        spec.loader.exec_module(mod)
    return mod


_RR = _receipt_report()
receipt_usage_class = _RR.usage_class  # the offline reporter's classification, reused


def known_int(value):
    return value if type(value) is int and value >= 0 else None


def known_number(value):
    ok = type(value) in (int, float) and math.isfinite(value) and value >= 0
    return value if ok else None


def envelope_class(tokens):
    known = sum(tokens[k] is not None for k in PRICED_KEYS)
    return "usage_missing" if known == 0 else "usage_known" if known == len(PRICED_KEYS) else "usage_partial"


def row_from_envelope(envelope, harness=None, role=None, model=None, source="dispatch"):
    """A dispatch usage envelope {input, output, cache_read, cache_write, cost_usd}: an absent key is unknown."""
    env = envelope if isinstance(envelope, dict) else {}
    tokens = {k: known_int(env.get(k)) for k in TOKEN_KEYS}
    return {"harness": harness, "role": role, "model": model, "source": source, "tokens": tokens,
            "usage_class": envelope_class(tokens), "reported_cost_usd": known_number(env.get("cost_usd"))}


def row_from_receipt(receipt):
    """A normalised strict-v1 receipt row (receipt_report.normalize); its usage_class is the offline one."""
    tokens = {"input": receipt["in_tok"], "output": receipt["out_tok"], "cache_read": receipt["cache_read_tok"],
              "cache_write": receipt["cache_write_tok"], "reasoning": receipt["reasoning_tok"]}
    return {"harness": receipt["harness"], "role": receipt["role"], "model": receipt["model"],
            "source": receipt["source"] or "receipt", "tokens": tokens, "usage_class": receipt_usage_class(receipt),
            "reported_cost_usd": known_number(receipt["cost_usd"])}


def load_receipts(paths):
    rows, _blank = _RR.load([str(p) for p in paths])
    return [row_from_receipt(r) for r in rows]


def _no_constant(name):
    raise ValueError(f"non-finite number {name}")


def load_envelopes(path):
    """JSONL: {"harness", "role", "model", "source"?, "usage": {...}} per line."""
    data = Path(path).read_bytes()
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("envelope file too large")
    rows = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        doc = json.loads(line, parse_constant=_no_constant)
        if not isinstance(doc, dict) or not isinstance(doc.get("usage", {}), dict):
            raise ValueError(f"line {number}: expected an object with a usage object")
        rows.append(row_from_envelope(doc.get("usage"), doc.get("harness"), doc.get("role"), doc.get("model"),
                                      doc.get("source") or "dispatch"))
    return rows
