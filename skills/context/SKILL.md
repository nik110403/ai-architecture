---
name: context
description: Create a compact, source-linked context card for a bounded task. Use before handoff, review, or when stale context may cause scope drift.
---
# Context
## Objective
Preserve only facts needed to act, distinguish them from assumptions, and point to source artifacts.
## Steps
1. Name the objective and current artifact version or hash.
2. List facts with source locations; list assumptions separately.
3. Record active constraints, excluded scope, and the smallest next action.
4. Remove conversation history that is not needed to perform that action.
5. Use [context-card.md](context-card.md) as the portable record.

For a deterministic manual manifest from an explicit project's routing table, use
[context-packet.md](context-packet.md) and its standalone script bundle. It reports
admitted paths and size estimates; it does not load task bodies, dispatch agents,
enforce a budget, or provide a verified native packet consumer.
## Output and done check
Done means a new reader can start safely without asking what artifact is current.
## Failure handling
If a source cannot be located or conflicts with the card, mark it unknown and re-read the authoritative artifact before action.
## Worked example
“API returns UTC timestamps (api.md §3)” is a fact; “clients accept RFC3339” is an assumption pending a test.
