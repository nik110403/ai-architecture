#!/usr/bin/env python3
"""Report a conservative route; it never dispatches, authorizes, or performs work."""

import argparse
import json
import re

POLICY = {
    "coding": {
        "words": ("code", "bug", "test", "debug", "refactor"),
        "roles": ("builder", "verifier"),
        "caps": ("code.builder", "code.verifier"),
        "context": ("decided scope", "acceptance criteria", "named inputs"),
    },
    "research": {
        "words": ("research", "citation", "literature", "source"),
        "roles": ("researcher", "verifier"),
        "caps": ("research.sources", "research.verifier"),
        "context": ("question", "source scope", "claim boundary"),
    },
    "design": {
        "words": ("ui", "design", "frontend", "visual"),
        "roles": ("builder", "design-critic"),
        "caps": ("design.builder", "design.render", "design.critic"),
        "context": ("product outcome", "design constraints", "render target"),
    },
    "external-action": {
        "words": ("submit", "send", "purchase", "browser"),
        "roles": ("operator", "checker"),
        "caps": ("external.survey", "external.approval", "external.act"),
        "context": (
            "observed state",
            "proposed action",
            "state-bound approval metadata",
        ),
    },
    "maintenance": {
        "words": ("drift", "loop", "memory", "instruction", "retro", "promote"),
        "roles": ("maintainer", "checker"),
        "caps": ("maintenance.inventory", "maintenance.checker"),
        "context": ("declared scope", "baseline", "cadence or trigger"),
    },
}
NO_PORT = (
    "system atlas",
    "system-atlas",
    "ui-ux-design",
    "impeccable",
    "design-builder",
    "web-design-guidelines",
    "troubleshoot",
    "security audit",
    "security-audit",
    "project setup",
    "project-setup",
    "skill-scout",
    "retro",
    "instinct-promote",
)
ALLOWED = {"capabilities", "models", "approval", "review_budget"}


def _words(text, phrase):
    return (
        re.search(r"(?<![a-z0-9-])" + re.escape(phrase) + r"(?![a-z0-9-])", text)
        is not None
    )


def _classification(intent):
    if not isinstance(intent, str) or not intent.strip():
        return None, "invalid intent"
    lowered = intent.lower()
    # No-port entrypoints refuse first, so an explicit route cannot bypass them.
    if any(_words(lowered, phrase) for phrase in NO_PORT):
        return None, "no-port"
    if re.search(r"(?<![a-z0-9-])route:", lowered):
        explicit = re.findall(r"(?<![a-z0-9-])route:([a-z0-9-]+)", lowered)
        if any(name not in POLICY for name in explicit):
            return None, "unknown"
        if len(set(explicit)) == 1:
            return explicit[0], None
        return None, "ambiguous"
    matches = [
        name
        for name, rule in POLICY.items()
        if any(_words(lowered, word) for word in rule["words"])
    ]
    return (
        (matches[0], None)
        if len(matches) == 1
        else (None, "ambiguous" if matches else "unknown")
    )


def _strings(value):
    return isinstance(value, list) and all(
        isinstance(item, str) and item for item in value
    )


def _validated(available, pending_jobs, unknown_arrivals):
    if not isinstance(available, dict) or set(available) - ALLOWED:
        return None, "invalid availability shape"
    caps, models = available.get("capabilities"), available.get("models")
    if not _strings(caps) or not _strings(models):
        return None, "capabilities and models must be lists of nonempty strings"
    budget = available.get("review_budget", 2)
    if type(budget) is not int or not 0 <= budget <= 2:
        return None, "review_budget must be an integer from 0 to 2"
    if not isinstance(pending_jobs, (list, tuple)) or not all(
        isinstance(job, str) and job for job in pending_jobs
    ):
        return None, "pending_jobs must be a list of nonempty strings"
    if type(unknown_arrivals) is not bool:
        return None, "unknown_arrivals must be boolean"
    approval = available.get("approval")
    if approval is not None and (
        not isinstance(approval, dict)
        or set(approval) != {"state_hash", "approved_action", "authorized"}
        or not isinstance(approval["state_hash"], str)
        or not approval["state_hash"]
        or not isinstance(approval["approved_action"], str)
        or not approval["approved_action"]
        or type(approval["authorized"]) is not bool
    ):
        return None, "approval must be typed state-bound metadata"
    return (set(caps), set(models), budget, approval), None


def route(intent, available, mode="enforced", pending_jobs=(), unknown_arrivals=False):
    """Return JSON-safe reporting data. Availability is explicit caller input only."""
    base = {
        "intent": intent,
        "mode": mode,
        "native_enforced": False,
        "tool_executes_input": False,
        "metadata_status": "UNKNOWN",
        "configured_model_order": ["configured-primary", "configured-replacement"],
        "pending_jobs": list(pending_jobs)
        if isinstance(pending_jobs, (list, tuple))
        else None,
        "unknown_arrivals": unknown_arrivals,
    }
    if mode not in ("enforced", "manual"):
        base.update(status="REFUSE", reason="invalid mode", route=None)
        return base
    validated, error = _validated(available, pending_jobs, unknown_arrivals)
    kind, problem = _classification(intent)
    if error or problem:
        base.update(
            status="REFUSE",
            reason=error or problem,
            clarify="Provide one bounded intent and typed explicit availability.",
            route=None,
        )
        return base
    caps, models, budget, approval = validated
    rule = POLICY[kind]
    missing, missing_models = (
        [cap for cap in rule["caps"] if cap not in caps],
        [role for role in rule["roles"] if role not in models],
    )
    external_checks = (
        [
            "survey observed state",
            "write proposal",
            "independently review proposal against state",
            "obtain state-bound approval",
            "authorized harness act and preserve receipt",
        ]
        if kind == "external-action"
        else [
            "freeze inputs and acceptance criteria",
            "maker records evidence",
            "independent checker reviews frozen artifact",
            "persist exact Next only after arrivals are known",
        ]
    )
    base.update(
        metadata_status="EXPLICIT",
        route=kind,
        roles=list(rule["roles"]),
        required_context=list(rule["context"]),
        required_capabilities=list(rule["caps"]),
        available_adapters=sorted(caps),
        missing_adapters=missing,
        missing_model_roles=missing_models,
        ordered_checks=external_checks,
        stop_conditions=[
            "ambiguous or no-port intent",
            "critical adapter missing",
            "checker role/model unavailable",
            "pending or unknown arrival",
            "review budget exhausted",
        ],
        approval_conditions=(
            [
                "approval metadata is state-bound",
                "approval is reported, never a permission grant",
            ]
            if kind == "external-action"
            else []
        ),
        review={
            "budget": budget,
            "remaining": budget,
            "disposition": "REVIEW_BUDGET_EXHAUSTED" if budget == 0 else "AVAILABLE",
        },
        fallback="Ask for the missing adapter or use documented manual workflow; never call it native or enforced.",
    )
    blocked = bool(
        missing or missing_models or pending_jobs or unknown_arrivals or budget == 0
    )
    if kind == "external-action":
        if approval is None or not approval["authorized"]:
            blocked = True
            base["reason"] = "state-bound approval metadata missing or not authorized"
        base["status"] = (
            "MANUAL_INSTRUCTIONS"
            if mode == "manual" and not (pending_jobs or unknown_arrivals)
            else "CONDITIONAL"
            if not blocked
            else "REFUSE"
        )
    else:
        base["status"] = (
            "MANUAL_INSTRUCTIONS"
            if mode == "manual" and not (pending_jobs or unknown_arrivals)
            else "COMPLETE"
            if not blocked
            else "REFUSE"
        )
    if blocked and "reason" not in base:
        base["reason"] = (
            "critical capability, model role, review budget, or lifecycle state missing"
        )
    return base


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent", required=True)
    parser.add_argument(
        "--capabilities",
        default="{}",
        help="JSON object: capabilities/models lists, optional review_budget and approval",
    )
    parser.add_argument("--mode", choices=("enforced", "manual"), default="enforced")
    parser.add_argument("--pending-job", action="append", default=[])
    parser.add_argument("--unknown-arrivals", action="store_true")
    args = parser.parse_args()
    try:
        available = json.loads(args.capabilities)
    except json.JSONDecodeError as exc:
        parser.error(f"--capabilities {exc}")
    print(
        json.dumps(
            route(
                args.intent,
                available,
                args.mode,
                args.pending_job,
                args.unknown_arrivals,
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
