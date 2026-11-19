"""Claude Code harness. The role is defined inline (--agents FILE), so no install is needed.

The prompt travels on stdin, never in argv. No shell. Every permitted tool is pre-allowed and
anything else is denied (dontAsk), so nothing is asked about. Claude Code does not always report a refused
act in permission_denials (a tool outside the list can end succeeded with none), unlike OpenCode: see README.
"""

import json

HARNESS = "claude-code"
PAID = True
SUPPORTS_BUDGET = True
EXECUTABLE = "claude"
AGENT_FILE = "agents.json"


def argv(request):
    tools = ",".join(request["tools"])
    out = [
        EXECUTABLE, "-p", "--agents", request["agent_file"], "--agent", request["role"],
        "--tools", tools, "--permission-mode", "dontAsk", "--allowedTools", tools,
        "--setting-sources", "user", "--strict-mcp-config",
        "--output-format", "json", "--no-session-persistence",
    ]
    if request.get("model"):
        out += ["--model", request["model"]]
    if request.get("effort"):
        out += ["--effort", request["effort"]]
    if request.get("max_budget_usd") is not None:
        out += ["--max-budget-usd", str(request["max_budget_usd"])]
    return out


def env(request):
    return {}


def harness_files(request):
    agent = {"description": request["description"], "prompt": request["prompt"], "tools": list(request["tools"])}
    return {AGENT_FILE: json.dumps({request["role"]: agent}, allow_nan=False)}
