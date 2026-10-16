# Provenance

Source: ~/.claude/skills/exec-pack/SKILL.md
Source-sha256: 765ed3d18f9f3d3fac9211fcd33a4fe41d432078371ca12e2e09a63369cf2e85
State: adapted

## Deltas

- Removed the autopilot-ownership guard, the Autopilot mode section, dispatch-record files and every run-ledger / human-time branch.
- Workflow-tool waves kept where the harness has one; added a documented sequential fallback, both marked UNVERIFIED outside the author's setup.
- Removed private agent names and model-specific cap wording; the UI-specialist builder is mentioned as optional (not shipped).
- Approval-scope wording made generic (the human owner); `Gate: nik` renamed `Gate: human`; merge still happens behind a fresh-context reviewer.
- Added the standard Steps, Output and done check, Failure handling and Worked example sections.
