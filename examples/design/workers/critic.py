"""Stand-in design-critic (role: design-critic). Authored and deterministic; no model judgement is exercised.

Runs with the bundle as its working directory and reads only what the bundle names: bundle.json comes in on stdin
inside the prompt, inspection.json is read from the bundle. It turns every non-passing check into one blocking finding
that cites an evidence id from the bundle, and claims browser validation only when the bundle holds a screenshot.
"""
import hashlib
import json
import sys


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def main():
    prompt = json.loads(sys.stdin.read())
    bundle = prompt["bundle"]
    bundle_sha = hashlib.sha256(canon(bundle).encode()).hexdigest()
    with open("inspection.json", encoding="utf-8") as handle:
        inspection = json.load(handle)
    findings = []
    for check in inspection["checks"]:
        if check["result"] == "pass":
            continue
        bad = [e for e in check["evidence"] if e["result"] != "pass"]
        findings.append({"id": f"F{len(findings) + 1}", "check": check["check"], "evidence_id": bad[0]["id"],
                         "priority": "blocking", "bundle_sha": bundle_sha})
    shots = [name for name in bundle["files"] if name.startswith("screenshots/")]
    print(json.dumps({"claims_browser_validation": bool(shots), "approved": not findings, "findings": findings},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
