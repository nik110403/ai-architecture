"""Brain adapter: metadata by default, content only with an exact source/provider/purpose permission record.

`status` passes only typed metadata keys (id, kind, state, counts, updated_at). `query` fails closed (exit 3, no process
spawned) unless the permission file holds a record whose source_id, provider and purpose all match the request.
"""

import datetime
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(key, path):
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


graph = _load("kad_graph", HERE / "graph.py")
recall = graph.recall
Refuse = graph.Refuse

TOKEN = r"[A-Za-z0-9_.:-]{1,64}"
ISO = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})"
STATUS_KEYS = ("id", "kind", "state", "counts", "updated_at")
RECORD_KEYS = {"source_id", "provider", "purpose"}


def load_config(path):
    cfg = graph.load_config(path, "brain")
    if not isinstance(cfg.get("provider"), str) or not re.fullmatch(TOKEN, cfg["provider"]):
        raise Refuse("config needs a short provider token")
    return cfg


def request(cfg, payload):
    state, out = graph.run_child(cfg["argv"], [], cfg["timeout_s"], stdin=json.dumps(payload).encode())
    if state != "ok":
        raise Refuse(f"backend {state}", 1)
    try:
        data = json.loads(out)
    except ValueError:
        raise Refuse("backend returned something that is not JSON", 1) from None
    if not isinstance(data, dict):
        raise Refuse("backend returned something that is not a JSON object", 1)
    return data


def token(value, what):
    if not isinstance(value, str) or not re.fullmatch(TOKEN, value):
        raise Refuse(f"backend {what} is not a short token; metadata only", 3)
    return value


def handshake(cfg):
    data = request(cfg, {"op": "handshake"})
    caps = data.get("capabilities", [])
    if not isinstance(caps, list) or len(caps) > 16:
        raise Refuse("backend capabilities are not a short list", 3)
    return {"backend": token(data.get("backend"), "backend"), "version": token(data.get("version"), "version"),
            "capabilities": [token(c, "capability") for c in caps]}


def status(cfg):
    data = request(cfg, {"op": "status"})
    out = {}
    for key in STATUS_KEYS:  # everything else the backend sent is dropped here
        if key not in data:
            continue
        value = data[key]
        if key in ("id", "kind", "state"):
            out[key] = token(value, key)
        elif key == "counts":
            if (not isinstance(value, dict) or len(value) > 32
                    or any(not isinstance(k, str) or not re.fullmatch(TOKEN, k) or isinstance(v, bool) or not isinstance(v, int)
                           or not 0 <= v < 1 << 53 for k, v in value.items())):
                raise Refuse("backend counts are not a short token-to-integer map; metadata only", 3)
            out[key] = value
        else:
            try:
                if not isinstance(value, str) or not re.fullmatch(ISO, value):
                    raise ValueError
                datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise Refuse("backend updated_at is not an ISO 8601 timestamp; metadata only", 3) from None
            out[key] = value
    return out


def check_permission(path, source_ids, provider, purpose):
    """Fail closed (exit 3) on a missing, unreadable, malformed, mismatching or mixed-source permission."""
    if not path:
        raise Refuse("content access needs --permissions", 3)
    try:
        records = json.loads(recall.read_regular(path, 64 << 10))["records"]
        valid = isinstance(records, list) and all(
            isinstance(r, dict) and set(r) == RECORD_KEYS and all(isinstance(v, str) and re.fullmatch(TOKEN, v) for v in r.values())
            for r in records)
    except (ValueError, KeyError, TypeError, recall.Refuse):
        valid = False
    if not valid:
        raise Refuse("permission file is missing or malformed", 3)
    if len(set(source_ids)) != 1 or not isinstance(purpose, str):
        raise Refuse("exactly one --source-id and a --purpose are required", 3)
    sid = source_ids[0]
    if not any(r["source_id"] == sid and r["provider"] == provider and r["purpose"] == purpose for r in records):
        raise Refuse("no permission record matches this source, provider and purpose", 3)
    return sid


def query(cfg, q, source_ids, purpose, permissions):
    sid = check_permission(permissions, source_ids, cfg["provider"], purpose)  # before any spawn
    data = request(cfg, {"op": "query", "q": q[:2000], "source_id": sid, "provider": cfg["provider"], "purpose": purpose})
    items = data.get("results")
    if not isinstance(items, list):
        raise Refuse("backend results are not a list", 1)
    results = [{"id": recall.sanitize(str(i.get("id", "")), 64), "excerpt": recall.sanitize(str(i.get("excerpt", "")), 400),
                "source_id": sid} for i in items[:20] if isinstance(i, dict)]
    return {"results": results}
