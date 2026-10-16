# Provenance

Source: ~/.claude/skills/tribunal/SKILL.md
Source-sha256: baa162c6910bd7dff7aa162ff6e9272180dc2e6df394e8ad3a32f7897d85d238
State: adapted

## Deltas

- Removed the rotating-model pin headers, rotation-log verification, external-model seat commands, the read-only guard hook (not included) and the charge-id ledger rules; replaced by 'verify what actually ran if your harness can' and a recorded-as-unverified fallback.
- Cross-family seat rule kept but generic: include a different model family if the harness can run one, else write CROSS-FAMILY: MISSING (the fail-closed disclosure is unchanged).
- Seat read-only enforcement is by the tribune tool list only; the stronger hook-based denial is not included and the skill says so.
- Inlined the lens list and stopping rules that lived in a private protocol file; all record writes still go through the shipped tribunal-scribe tool.
- Added the standard Steps, Output and done check, Failure handling and Worked example sections.
