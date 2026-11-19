"""Offline validation for the portable module contract descriptors."""

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = Path(__file__).resolve().parent
IDS = {f"{number:02d}" for number in range(1, 73)}
SUPPORT = {"planned", "partial", "available", "unavailable", "unsupported"}
DELIVERY = {
    "core",
    "optional",
    "optional-interface",
    "companion",
    "assess",
    "core-protocol",
    "core-upstream",
}
VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z")
SOURCE_VERSION = re.compile(
    r"(?:\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.+-]+)?|[0-9a-f]{12,40})\Z"
)
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
LICENSE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*\Z")
ORIGIN_SCHEME = re.compile(r"(?:https://|npm:)[A-Za-z0-9@][A-Za-z0-9@._/~+-]*\Z")
ORIGIN_RELATIVE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*\Z")
HARNESSES = {"claude-code", "opencode"}
RECEIPT_FIELDS = {
    "schema",
    "class",
    "module",
    "claim",
    "event_id",
    "harness",
    "harness_version",
    "command",
    "exit_code",
    "started_at",
    "finished_at",
    "stdout_sha256",
    "dependencies",
}
SOURCE_IDS = [
    "opencode-plugin",
    "superpowers",
    "document-skills",
    "epic-infographics",
    "local-adapters",
    "research-project",
    "research-synthesis",
]
STAGED_SOURCES = {"research-project", "research-synthesis"}
SOURCE_DISPOSITIONS = {
    "external-untested",
    "external-only",
    "license-limited",
    "excluded",
    "staged",
}
REDISTRIBUTION = {"blocked", "not-vendored"}
BLOCKING_LICENSES = {"UNKNOWN", "Proprietary", "MIT-package-only"}
EVIDENCE_CLASSES = {"fixture", "native"}
FIELD_TYPES = {"string", "evidence-list"}
EVIDENCE_SCHEMAS = {
    "event": {
        "required": ["event_id", "kind", "occurred_at", "subject"],
        "types": {
            key: "string" for key in ("event_id", "kind", "occurred_at", "subject")
        },
        "values": {},
    },
    "result": {
        "required": ["event_id", "outcome", "evidence", "finished_at"],
        "types": {
            "event_id": "string",
            "outcome": "string",
            "evidence": "evidence-list",
            "finished_at": "string",
        },
        "values": {"outcome": ["succeeded", "failed", "unsupported"]},
    },
    "status": {
        "required": ["subject", "state", "observed_at", "reason"],
        "types": {
            key: "string" for key in ("subject", "state", "observed_at", "reason")
        },
        "values": {"state": sorted(SUPPORT)},
    },
}


def fail(message):
    raise ValueError(message)


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"invalid contract JSON: {path.name}: {error}")


def parse_timestamp(value):
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        fail("invalid receipt timestamp")
    if moment.tzinfo is None:
        fail("invalid receipt timestamp")
    return moment


def check_origin(origin):
    if origin == "none":
        return
    if ".." in origin.split("/") or not (
        ORIGIN_SCHEME.fullmatch(origin) or ORIGIN_RELATIVE.fullmatch(origin)
    ):
        fail("invalid source origin")


def safe_dependency(item):
    if not isinstance(item, dict) or set(item) != {"identifier", "version", "status"}:
        fail("dependency fields mismatch")
    identifier = item["identifier"]
    version = item["version"]
    status = item["status"]
    if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
        fail("unsafe dependency identifier")
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        fail("unsafe dependency version")
    if status not in {"tested", "declared-untested"}:
        fail("invalid dependency status")


def check_receipt(directory, member, module, claim, event_id):
    """Validate one execution receipt; return its (identifier, version) pairs."""
    reference = member["reference"]
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts or "\\" in reference:
        fail("unsafe receipt reference")
    base = directory.resolve()
    path = directory / relative
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        fail("missing receipt file")
    if base not in resolved.parents or not resolved.is_file():
        fail("unsafe receipt reference")
    data = resolved.read_bytes()
    if hashlib.sha256(data).hexdigest() != member["sha256"]:
        fail("receipt sha256 mismatch")
    try:
        receipt = json.loads(data.decode("utf-8"), object_pairs_hook=unique_keys)
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail("invalid receipt JSON")
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS:
        fail("receipt fields mismatch")
    exit_code = receipt["exit_code"]
    if type(exit_code) is not int or exit_code != 0:
        fail("receipt exit_code must be 0")
    schema = receipt["schema"]
    if type(schema) is not int or schema != 1 or receipt["class"] != "native":
        fail("invalid receipt identity")
    for key in (
        "module",
        "claim",
        "event_id",
        "harness",
        "harness_version",
        "command",
        "started_at",
        "finished_at",
    ):
        if not isinstance(receipt[key], str) or not receipt[key].strip():
            fail(f"invalid receipt {key}")
    if parse_timestamp(receipt["finished_at"]) < parse_timestamp(receipt["started_at"]):
        fail("receipt timestamps out of order")
    if receipt["harness"] not in HARNESSES:
        fail("invalid receipt harness")
    if not isinstance(receipt["stdout_sha256"], str) or not HEX64.fullmatch(
        receipt["stdout_sha256"]
    ):
        fail("invalid receipt stdout_sha256")
    if (
        receipt["module"] != module
        or receipt["claim"] != claim
        or receipt["event_id"] != event_id
    ):
        fail("receipt disagrees with evidence record")
    dependencies = receipt["dependencies"]
    if not isinstance(dependencies, list):
        fail("invalid receipt dependencies")
    pairs = set()
    for item in dependencies:
        if (
            not isinstance(item, dict)
            or set(item) != {"identifier", "version"}
            or not isinstance(item["identifier"], str)
            or not IDENTIFIER.fullmatch(item["identifier"])
            or not isinstance(item["version"], str)
            or not VERSION.fullmatch(item["version"])
        ):
            fail("invalid receipt dependencies")
        pairs.add((item["identifier"], item["version"]))
    return pairs


def check_evidence(directory, evidence, support, module="core-contracts"):
    """Validate evidence for `module`; return the dependency pairs named by valid receipts."""
    if not isinstance(evidence, list):
        fail("invalid descriptor evidence")
    native_claims = set()
    receipt_dependencies = set()
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {
            "class",
            "claim",
            "event",
            "result",
            "status",
        }:
            fail("invalid evidence reference")
        if item["class"] not in EVIDENCE_CLASSES:
            fail("unsupported evidence class")
        if item["claim"] != support:
            fail("evidence claim does not match support")
        check_record("evidence event", item["event"], EVIDENCE_SCHEMAS["event"])
        check_record("evidence result", item["result"], EVIDENCE_SCHEMAS["result"])
        check_record("evidence status", item["status"], EVIDENCE_SCHEMAS["status"])
        if (
            item["event"]["kind"] != "support-claim"
            or item["event"]["subject"] != module
            or item["result"]["event_id"] != item["event"]["event_id"]
            or item["result"]["outcome"] != "succeeded"
            or item["status"]["subject"] != module
            or item["status"]["state"] != support
        ):
            fail("unlinked or fabricated support evidence")
        if item["class"] == "native":
            receipts = [
                member
                for member in item["result"]["evidence"]
                if member["kind"] == "execution-receipt"
            ]
            if not receipts:
                fail("native evidence requires an execution receipt")
            for member in receipts:
                receipt_dependencies |= check_receipt(
                    directory,
                    member,
                    module,
                    item["claim"],
                    item["event"]["event_id"],
                )
            native_claims.add(item["claim"])
    if support in {"available", "partial"} and support not in native_claims:
        fail("overclaimed support without native evidence")
    return receipt_dependencies


def check_record(name, record, schema):
    if not isinstance(record, dict) or set(record) != set(schema["required"]):
        fail(f"invalid {name} record shape")
    for field, kind in schema["types"].items():
        value = record[field]
        if kind == "string" and (not isinstance(value, str) or not value.strip()):
            fail(f"invalid {name} {field}")
        if kind == "evidence-list" and (
            not isinstance(value, list)
            or any(
                not isinstance(member, dict)
                or set(member) != {"kind", "reference", "sha256"}
                or not all(
                    isinstance(member[key], str) and member[key] for key in member
                )
                for member in value
            )
        ):
            fail(f"invalid {name} {field}")
    for field, choices in schema.get("values", {}).items():
        if record[field] not in choices:
            fail(f"invalid {name} {field}")


def execute_adapter(adapter, operation, schema_version, dependencies, dependency=None):
    """Run one generic adapter operation after compatibility checks."""
    if schema_version != 1:
        fail("unknown schema")
    for declared in dependencies:
        safe_dependency(declared)
    if dependency is not None and dependency not in {
        item["identifier"] for item in dependencies
    }:
        fail("undeclared dependency")
    if operation not in {"describe", "handle"}:
        fail("unsupported operation")
    method = getattr(adapter, operation, None)
    if not callable(method):
        fail("adapter operation unavailable")
    return method()


def check_protocol(protocol, dependencies):
    if (
        set(protocol) != {"version", "adapter", "compatibility"}
        or protocol["version"] != 1
    ):
        fail("protocol fields mismatch")
    adapter = protocol["adapter"]
    if (
        set(adapter) != {"protocol_version", "required"}
        or adapter["protocol_version"] != 1
    ):
        fail("invalid adapter protocol")
    if adapter["required"] != ["describe", "handle"]:
        fail("invalid adapter operations")
    compatibility = protocol["compatibility"]
    if set(compatibility) != {"requires_schema_version", "rejects"}:
        fail("invalid compatibility fields")
    if compatibility["requires_schema_version"] != 1 or not isinstance(
        compatibility["rejects"], list
    ):
        fail("invalid compatibility requirements")
    if set(compatibility["rejects"]) != {
        "unknown-schema",
        "undeclared-dependency",
        "unsupported-operation",
    }:
        fail("incomplete compatibility refusals")
    for dependency in dependencies:
        safe_dependency(dependency)


def check_module_descriptor(directory, *, module):
    """Validate a non-core module descriptor: identity, support, evidence, dependencies, schemas."""
    try:
        return _check_module_descriptor(Path(directory), module)
    except (TypeError, KeyError, AttributeError, RuntimeError, OSError) as error:
        fail(f"malformed module descriptor shape: {type(error).__name__}")


def _check_module_descriptor(directory, module):
    if not isinstance(module, str) or not IDENTIFIER.fullmatch(module) or module == "core-contracts":
        fail("invalid module name")
    descriptor = load(directory / "descriptor.json")
    if not isinstance(descriptor, dict) or set(descriptor) != {
        "version",
        "module",
        "claimed_support",
        "evidence",
        "dependencies",
        "schemas",
    }:
        fail("descriptor fields mismatch")
    if descriptor["version"] != 1 or descriptor["module"] != module:
        fail("invalid descriptor identity")
    if descriptor["claimed_support"] not in SUPPORT:
        fail("invalid descriptor support")
    receipt_dependencies = check_evidence(
        directory, descriptor["evidence"], descriptor["claimed_support"], module
    )
    if not isinstance(descriptor["dependencies"], list):
        fail("invalid descriptor dependencies")
    for dependency in descriptor["dependencies"]:
        safe_dependency(dependency)
        if (
            dependency["status"] == "tested"
            and (dependency["identifier"], dependency["version"])
            not in receipt_dependencies
        ):
            fail("overclaimed tested dependency without receipt")
    declared = descriptor["schemas"]
    if (
        not isinstance(declared, list)
        or len(set(map(str, declared))) != len(declared)
        or any(name not in EVIDENCE_SCHEMAS for name in declared)
    ):
        fail("module schema inventory mismatch")
    return descriptor


def check_contracts(
    directory=CONTRACTS, registry_path=ROOT / "capabilities" / "registry.json"
):
    """Validate every contract file; any wrong-shape input is a ValueError."""
    try:
        return _check_contracts(directory, registry_path)
    except (TypeError, KeyError, AttributeError, RuntimeError, OSError) as error:
        fail(f"malformed contract shape: {type(error).__name__}")


def _check_contracts(directory, registry_path):
    descriptor = load(directory / "descriptor.json")
    if set(descriptor) != {
        "version",
        "module",
        "claimed_support",
        "evidence",
        "dependencies",
        "schemas",
    }:
        fail("descriptor fields mismatch")
    if descriptor["version"] != 1 or descriptor["module"] != "core-contracts":
        fail("invalid descriptor identity")
    if descriptor["claimed_support"] not in SUPPORT:
        fail("invalid descriptor support")
    receipt_dependencies = check_evidence(
        directory, descriptor["evidence"], descriptor["claimed_support"]
    )
    if not isinstance(descriptor["dependencies"], list):
        fail("invalid descriptor dependencies")
    for dependency in descriptor["dependencies"]:
        safe_dependency(dependency)
        if (
            dependency["status"] == "tested"
            and (dependency["identifier"], dependency["version"])
            not in receipt_dependencies
        ):
            fail("overclaimed tested dependency without receipt")
    if descriptor["schemas"] != ["event", "result", "status"]:
        fail("core schema inventory mismatch")

    schemas = load(directory / "schemas.json")
    if (
        set(schemas) != {"version", "event", "result", "status"}
        or schemas["version"] != 1
    ):
        fail("schema fields mismatch")
    for name in ("event", "result", "status"):
        schema = schemas[name]
        if set(schema) - {"required", "types", "values"} or set(schema) != {
            "required",
            "types",
            "values",
        }:
            fail(f"invalid {name} schema")
        if not isinstance(schema["required"], list) or set(schema["required"]) != set(
            schema["types"]
        ):
            fail(f"invalid {name} schema fields")
        if not all(kind in FIELD_TYPES for kind in schema["types"].values()):
            fail(f"unsupported {name} schema type")

    protocol = load(directory / "adapter_protocol.json")
    check_protocol(protocol, descriptor["dependencies"])
    example = load(directory / "example.json")
    if set(example) != {"event", "result", "status"}:
        fail("example record inventory mismatch")
    for name in ("event", "result", "status"):
        check_record(name, example[name], schemas[name])

    sources = load(directory / "sources.json")
    if set(sources) != {"version", "sources"} or sources["version"] != 1:
        fail("source report fields mismatch")
    if not isinstance(sources["sources"], list):
        fail("source report fields mismatch")
    required = {
        "identifier",
        "origin",
        "version",
        "revision",
        "license",
        "redistribution",
        "delta",
        "disposition",
    }
    for source in sources["sources"]:
        if not isinstance(source, dict) or set(source) != required:
            fail("source fields mismatch")
        revision = source["revision"]
        if revision is not None and not (
            isinstance(revision, str) and HEX40.fullmatch(revision)
        ):
            fail("invalid source revision")
        if not all(
            isinstance(source[key], str) and source[key]
            for key in required - {"revision"}
        ):
            fail("invalid source disposition")
        license_value = source["license"]
        if not LICENSE.fullmatch(license_value) or (
            license_value not in BLOCKING_LICENSES
            and license_value.casefold() in {x.casefold() for x in BLOCKING_LICENSES}
        ):
            fail("invalid source license")
        check_origin(source["origin"])
        if source["disposition"] not in SOURCE_DISPOSITIONS:
            fail("invalid source disposition state")
        if source["redistribution"] not in REDISTRIBUTION:
            fail("invalid source redistribution")
        if source["license"] == "UNKNOWN" and source["redistribution"] != "blocked":
            fail("unknown license must block redistribution")
        if (
            source["license"] in BLOCKING_LICENSES
            and source["redistribution"] != "blocked"
        ):
            fail("restricted license must block redistribution")
        version = source["version"]
        if (version == "STAGED") != (source["disposition"] == "staged"):
            fail("STAGED version requires staged disposition")
        if (version == "NONE") != (source["disposition"] == "excluded"):
            fail("NONE version requires excluded disposition")
        if version not in {"STAGED", "NONE"} and not SOURCE_VERSION.fullmatch(version):
            fail("source version must be a single pin")
    if sorted(source.get("identifier") for source in sources["sources"]) != sorted(
        SOURCE_IDS
    ):
        fail("sources must list each required identifier exactly once")
    dispositions = {
        source["identifier"]: source["disposition"] for source in sources["sources"]
    }

    registry = load(registry_path)
    for item in registry["capabilities"]:
        if item.get("support") in {"available", "partial"} and (
            item.get("evidence_class") == "none"
        ):
            fail(f"registry support without evidence: {item.get('id')}")
    registry_modules = {item["id"]: item["module"] for item in registry["capabilities"]}
    mapping = load(directory / "mapping.json")
    if (
        set(mapping)
        != {"version", "descriptor", "adapter", "handshakes", "staged", "capabilities"}
        or mapping["version"] != 1
    ):
        fail("mapping fields mismatch")
    if mapping["descriptor"] != "descriptor.json":
        fail("unresolved contract descriptor")
    if mapping["adapter"] != {
        "protocol_version": 1,
        "operations": ["describe", "handle"],
    }:
        fail("invalid mapping adapter")
    companion_ids = set()
    for group in mapping["handshakes"]:
        if (
            set(group) != {"delivery_type", "capabilities", "state", "blocked_until"}
            or group["delivery_type"] != "companion"
            or group["state"] != "proposed"
            or group["blocked_until"] != "live evidence"
        ):
            fail("invalid companion handshake")
        if not group["capabilities"] or not set(group["capabilities"]).issubset(IDS):
            fail("invalid companion capability")
        companion_ids.update(group["capabilities"])
    source_ids = {source["identifier"] for source in sources["sources"]}
    staged_ids = set()
    for staged in mapping["staged"]:
        if (
            set(staged) != {"source", "capabilities", "adapter", "blocked_until"}
            or staged["source"] not in source_ids
            or dispositions.get(staged["source"]) != "staged"
            or staged["adapter"] != "proposed-v1"
            or staged["blocked_until"] != "live evidence"
        ):
            fail("invalid staged association")
        if not staged["capabilities"] or not set(staged["capabilities"]).issubset(IDS):
            fail("invalid staged capability")
        staged_ids.update(staged["capabilities"])
    if {staged["source"] for staged in mapping["staged"]} != STAGED_SOURCES:
        fail("staged sources need a nonempty association")
    entries = mapping["capabilities"]
    if not isinstance(entries, list):
        fail("mapping must cover IDs 01 through 72")
    entry_ids = [entry.get("id") for entry in entries]
    if len(entry_ids) != len(set(entry_ids)):
        fail("duplicate mapping ID")
    if set(entry_ids) != IDS:
        fail("mapping must cover IDs 01 through 72")
    for entry in entries:
        if set(entry) != {"id", "registry_module", "delivery_type"}:
            fail("mapping entry fields mismatch")
        delivery = entry["delivery_type"]
        if isinstance(delivery, str):
            delivery = [delivery]
        if (
            not isinstance(delivery, list)
            or not delivery
            or len(delivery) != len(set(delivery))
            or not set(delivery).issubset(DELIVERY)
        ):
            fail("invalid delivery type")
        if "companion" in delivery and entry["id"] not in companion_ids:
            fail("companion capability missing handshake")
        if "companion" not in delivery and entry["id"] in companion_ids:
            fail("handshake capability lacks companion delivery")
        if entry["id"] in staged_ids and "companion" not in delivery:
            fail("staged capability is not companion delivery")
        if registry_modules.get(entry["id"]) != entry["registry_module"]:
            fail(f"unresolved registry link: {entry['id']}")
    return entries
