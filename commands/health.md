---
description: Run the collection's offline health checks without generation.
---
With ordinary execution approval, run `python3 -B "${CLAUDE_PLUGIN_ROOT}/scripts/check_collection.py"` and `python3 -B -m unittest discover -s "${CLAUDE_PLUGIN_ROOT}/tests" -v`; report exact results. These paths target the collection, never the recipient project's tests. These checks do not establish live harness discovery. Invocation: `/ai-architecture:health`. This command grants no permissions.
