---
name: routing
description: Report a conservative route from an explicit intent and explicit available capabilities. Use before delegated or specialist work.
---
# Routing
## Objective
Choose a bounded process without pretending to discover providers, permissions, or models.
## Steps
1. Classify one intent; unknown, mixed, and inventory-needed families stop for clarification.
2. List stable capability IDs, maker/checker roles, configured model order, and required adapters.
3. Optionally run the exact bundle in [DEPENDENCIES.md](DEPENDENCIES.md) with only caller-supplied availability. It performs no dispatch, installation, network access, or discovery.
4. Refuse a COMPLETE route when a critical capability, checker model role, unread family, pending job, or unknown arrival is present.
5. In manual mode, provide instructions but label them manual, not native or enforced.
## Output and done check
Save the bundled `templates/route-report.md` record. Done is COMPLETE or an explicit REFUSE/clarifying question, never a guessed fallback.
## Failure handling
Keep missing checker lenses visible; configured replacement is allowed only when supplied by the recipient. Otherwise stop.
## Worked example
Run from the collection root, where the router script lives (it is not inside this skill folder): `python3 scripts/route.py --intent 'fix code test' --capabilities '{"capabilities":["code.builder","code.verifier"],"models":["builder","verifier"]}'` reports a coding route.
