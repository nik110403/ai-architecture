"""OpenCode harness. The role is defined inline through OPENCODE_CONFIG_CONTENT as a primary agent
with an allowlist permission set, so no install is needed and nothing outside the allowlist runs.

The prompt travels on stdin, never in argv. No shell. `--auto` is never passed: an `ask` is rejected.
"""

import json

HARNESS = "opencode"
PAID = True
SUPPORTS_BUDGET = False
EXECUTABLE = "opencode"
# WebSearch maps to OpenCode's `websearch` permission id (OpenCode 1.18.31).
PERMISSION_IDS = {
    "Read": "read", "Grep": "grep", "Glob": "glob", "Bash": "bash", "Write": "edit", "Edit": "edit",
    "WebFetch": "webfetch", "WebSearch": "websearch",
}


def argv(request):
    out = [EXECUTABLE, "run", "--agent", request["role"], "--model", request["model"], "--format", "json"]
    if request.get("effort"):
        out += ["--variant", request["effort"]]
    return out


def permission(tools):
    unknown = [t for t in tools if t not in PERMISSION_IDS]
    if unknown:
        raise ValueError(f"no OpenCode permission id for tool(s): {', '.join(unknown)}")
    rules = {"*": "deny", "external_directory": "deny"}
    for tool in tools:
        rules[PERMISSION_IDS[tool]] = "allow"
    return rules


def env(request):
    agent = {
        "description": request["description"], "mode": "primary", "model": request["model"],
        "prompt": request["prompt"], "permission": permission(request["tools"]),
    }
    # `title` off and `small_model` = the pin: the harness's auxiliary title call cannot pick another model.
    config = {"agent": {request["role"]: agent, "title": {"disable": True}}, "small_model": request["model"]}
    return {
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_CONFIG_CONTENT": json.dumps(config, allow_nan=False),
    }


def harness_files(request):
    return {}
