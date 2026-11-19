"""Budget arithmetic. Unknown usage or cost is never 0: it stops the run (`usage-unknown`).

Tokens come from the dispatcher's parsed usage (all four counts must be known). USD comes from the U12 `cost_of` with an
operator price table; no price ships. A budget argument must be a finite number > 0.
"""

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.observability import cost as _cost  # noqa: E402
from modules.observability import usage as _usage  # noqa: E402

DEFAULT_RESERVE_TOKENS = 1000


def parse_positive(text, integer=False):
    """float (or int) > 0 and finite, else None. Accepts only plain decimal text."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    if integer:
        if value != int(value):
            return None
        return int(value)
    return value


def charge(usage, model, prices):
    """(tokens|None, usd|None) for one round. None means unknown; it is never replaced by 0."""
    if not isinstance(usage, dict):
        return None, None
    row = _usage.row_from_envelope(usage, model=model)
    if row["usage_class"] != "usage_known":
        return None, None
    tokens = sum(row["tokens"][k] for k in _usage.PRICED_KEYS)
    usd = None
    if prices is not None:
        usd = _cost.cost_of(row, prices)["cost_usd"]
    return tokens, usd


def load_prices(path):
    return _cost.load_prices(path)


def refuse_start(limits, spent):
    """A reason string when a new round must not start, else None."""
    if limits["budget_tokens"] is not None and limits["budget_tokens"] - spent["tokens"] < limits["reserve_tokens"]:
        return "tokens left are below the per-round reserve"
    if limits["budget_usd"] is not None and limits["budget_usd"] - spent["usd"] <= 0:
        return "no USD left"
    return None


def exceeded(limits, spent):
    if limits["budget_tokens"] is not None and spent["tokens"] > limits["budget_tokens"]:
        return True
    return limits["budget_usd"] is not None and spent["usd"] > limits["budget_usd"] + 1e-12


def needs_cost(limits):
    return limits["budget_usd"] is not None
