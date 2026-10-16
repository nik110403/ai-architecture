# Provenance

Source: ~/.claude/skills/plan-pack/SKILL.md
Source-sha256: 9391467701e6f775195ca36bab983255caacc06718f0faaf5298112cfc01cb51
State: adapted

## Deltas

- Removed the unattended-runner section, the autopilot header fields and the kickoff-line guidance (not included).
- Removed references to morning reports, the owner queue, memory promotion, the Stop-hook gate and other private tooling; plan-pack now points at the shipped plancheck and gate-check tools.
- Approval-scope wording made generic (the human owner); `Gate: nik` renamed `Gate: human`.
- Template and linter paths now resolve inside this collection (`templates/planning/`, `tools/plancheck/`); added the standard Steps, Output and done check, Failure handling and Worked example sections.
- Hand-off into exec-pack kept but marked UNVERIFIED outside the author's setup.
