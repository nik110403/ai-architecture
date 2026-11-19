"""Stand-in tribune: ranks incident candidates by risk (low first), then id. A fixed rule, not a judgement."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as c  # noqa: E402

candidates = c.prompt()["candidates"]
order = {"low": 0, "medium": 1, "high": 2}
ranking = [x["id"] for x in sorted(candidates, key=lambda x: (order.get(x["risk"], 3), x["id"]))]
c.emit({"ranking": ranking, "candidates_sha256": c.sha(c.canon(candidates))})
