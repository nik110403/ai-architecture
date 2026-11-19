"""Cost from known tokens and an operator price table. No price ships with this module.

Prices are USD per 1,000,000 tokens: {model: {"input": x, "output": y, "cache_read": z?, "cache_write": w?}}.
A cost is None unless every token count is known and every needed price is known; 0 appears only as known tokens
times known prices. Known `reasoning` tokens > 0 make the cost
None (reason `reasoning_unpriced`) unless the model's price entry declares `"reasoning": "in_output"` (they are then
already inside output).
"""

import json
import math
from fractions import Fraction
from pathlib import Path

from modules.observability.usage import PRICED_KEYS

CACHE_KEYS = ("cache_read", "cache_write")


def valid_rate(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def load_prices(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=lambda n: (_ for _ in ()).throw(ValueError(n)))
    models = doc.get("models") if isinstance(doc, dict) else None
    if not isinstance(models, dict) or doc.get("unit") != "usd_per_million_tokens":
        raise ValueError('price table must be {"unit": "usd_per_million_tokens", "models": {...}}')
    return models


def cost_of(row, prices):
    cls = row["usage_class"]
    if cls != "usage_known":
        return {"cost_usd": None, "reason": cls}
    price = prices.get(row["model"]) if isinstance(prices, dict) else None
    if not isinstance(price, dict) or not valid_rate(price.get("input")) or not valid_rate(price.get("output")):
        return {"cost_usd": None, "reason": "price_missing"}
    reasoning = row["tokens"].get("reasoning")
    if type(reasoning) is int and reasoning > 0 and price.get("reasoning") != "in_output":
        return {"cost_usd": None, "reason": "reasoning_unpriced"}  # declare "reasoning": "in_output" if output includes it
    total = Fraction(0)
    for key in PRICED_KEYS:
        count = row["tokens"][key]
        if count == 0:
            continue
        if not valid_rate(price.get(key)):
            return {"cost_usd": None, "reason": "cache_price_missing"}
        total += count * Fraction(str(price[key]))
    return {"cost_usd": float(total / 1_000_000), "reason": "ok"}
