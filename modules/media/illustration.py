"""Illustration provider interface (illustration/1). A provider is a command you configure; this collection ships none.

`providers.json` lists {name, argv, describe_argv}; anything else it claims is ignored. Only the provider's own `--describe`
output is trusted: {interface, kind, network, cost, licence, notice_required, output}. Every refusal happens before the
generate argv is spawned. The stub in the tests is a fixture: its result says so and can never count as a real generation."""
import importlib.util
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _sib(name):
    key = "aiarch_media_" + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


common = _sib("common")
Exit = common.Exit
DESCRIBE_KEYS = {"interface", "kind", "network", "cost", "licence", "notice_required", "output"}
ENTRY_KEYS = {"name", "argv", "describe_argv", "claims"}
ACTIVATION = "user-supplied provider; nothing installed by this collection"


def expand(argv):
    return [a.replace("{python}", sys.executable).replace("{root}", str(common.ROOT)) if isinstance(a, str) else a for a in argv]


def load_providers(path):
    doc = common.parse_json(common.read_capped(path, 1 << 16).decode("utf-8", "replace"), "providers")
    if not isinstance(doc, dict) or set(doc) != {"providers"} or not isinstance(doc["providers"], list):
        raise Exit(2, "invalid providers: expected {providers: [...]}")
    found = {}
    for entry in doc["providers"]:
        if not isinstance(entry, dict) or not {"name", "argv", "describe_argv"} <= set(entry) or set(entry) - ENTRY_KEYS:
            raise Exit(2, "invalid providers: entry keys")
        if not isinstance(entry["name"], str) or not common.ID.match(entry["name"]) or entry["name"] in found:
            raise Exit(2, "invalid providers: name")
        for key in ("argv", "describe_argv"):
            argv = entry[key]
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a and "\0" not in a for a in argv):
                raise Exit(2, f"invalid providers: {key}")
            if not os.path.isabs(expand(argv)[0]):
                raise Exit(2, f"invalid providers: {key} must start with an absolute executable")
        found[entry["name"]] = {"name": entry["name"], "argv": expand(entry["argv"]), "describe_argv": expand(entry["describe_argv"])}
    return found


def describe(entry):
    """The provider's own description, or Exit(4, 'provider not observed') for a missing, hung, failing or malformed one."""
    try:
        code, out, _ = common.run_tool(entry["describe_argv"], entry["name"], 10, limit=1 << 14)
    except Exit:
        raise Exit(4, "provider not observed")
    try:
        doc = common.parse_json(out.decode("utf-8", "replace"), "describe")
    except Exit:
        raise Exit(4, "provider not observed")
    ok = (code == 0 and isinstance(doc, dict) and set(doc) == DESCRIBE_KEYS and doc["interface"] == "illustration/1"
          and doc["kind"] in ("real", "fixture") and isinstance(doc["network"], bool) and doc["cost"] in ("none", "quota", "paid")
          and isinstance(doc["licence"], str) and 0 < len(doc["licence"]) <= 40 and isinstance(doc["notice_required"], bool)
          and doc["output"] == "png")
    if not ok:
        raise Exit(4, "provider not observed")
    return doc


def refuse_by_policy(desc, allow_network, allow_spend):
    if desc["network"] and not allow_network:
        raise Exit(4, "provider needs network")
    if desc["cost"] != "none" and not allow_spend:
        raise Exit(4, "provider costs money")


def backend_of(entry, desc):
    if desc["kind"] == "fixture":
        return "fixture"
    for item in entry["argv"]:
        if os.path.isabs(item) and os.path.exists(item) and common.backend_of(os.path.realpath(item)) == "fixture":
            return "fixture"
    return "real"


def generate(entry, desc, prompt, out):
    """Spawn the generate argv (after every refusal) and return the written png's bytes."""
    import tempfile
    target = os.path.join(out, "image.png")
    argv = [*entry["argv"], "--prompt-file", prompt, "--out", target]
    seconds = common.tool_seconds(120)
    with tempfile.TemporaryDirectory() as home:
        try:
            code, _, _ = common.documents.run_group(argv, timeout=seconds, env=common.child_env(home), limit=1 << 16)
        except common.subprocess.TimeoutExpired:
            raise Exit(1, "provider failed: timeout")
        except OSError:
            raise Exit(1, "provider failed: cannot start")
    data = common.read_artifact(out, "image.png", 16 << 20)
    head = common.checks.png_header(data) if data else None
    if code != 0 or head is None or not (0 < head[0] <= 4096 and 0 < head[1] <= 4096):
        raise Exit(1, "provider failed: no valid png")
    problem = common.checks.png_problem(data, head[0], head[1])
    if problem:
        raise Exit(1, f"png invalid: {problem}")
    return data


def read_prompt(path):
    if not path or not common.regular_file(path):
        raise Exit(2, "invalid prompt file")
    data = common.read_capped(path, common.LIM["prompt_bytes"], "invalid prompt file")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise Exit(2, "invalid prompt file")
    return os.path.abspath(path)


def licence_record():
    """The licence/activation record, derived at run time from the PROVENANCE units of the illustration skill's LICENSE and NOTICE."""
    text = (HERE / "PROVENANCE.md").read_text(encoding="utf-8")
    units = {m.group(1).strip(): m.group(2) for m in re.finditer(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", text, re.M | re.S)}
    lic, notice = units.get("illo LICENSE"), units.get("illo NOTICE")
    if not lic or not notice:
        raise Exit(2, "provenance unreadable")
    try:
        return {"licence": re.search(r"^License seen at the source: ([A-Za-z0-9+-]+)", lic, re.M).group(1),
                "notice_required": True,
                "licence_sha256": re.search(r"^Source-sha256: ([0-9a-f]{64})$", lic, re.M).group(1),
                "notice_sha256": re.search(r"^Source-sha256: ([0-9a-f]{64})$", notice, re.M).group(1),
                "activation": ACTIVATION, "licence_gate": "pending U02 row"}
    except AttributeError:
        raise Exit(2, "provenance unreadable")
