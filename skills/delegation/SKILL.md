---
name: delegation
description: Create a bounded worker brief and assess its returned evidence. Use when a task can be handed to a separate worker without shared design decisions.
---
# Delegation
## Objective
Give one worker a small, independently verifiable assignment without granting implied authority.
## Steps
1. State the objective, acceptance criteria, allowed files, and prohibited scope.
2. Include only the necessary artifact excerpts and exact verification command.
3. Ask for a concise result with paths, commands, outputs, and unresolved issues.
4. Treat the result as a claim; assign or perform a fresh check before relying on it.
5. Store the brief and result using [brief.md](brief.md).
## Output and done check
The brief is done when another reader can identify the exact next edit and its boundary.
## Failure handling
If the assignment needs architecture, conflicting authority, or private context, return it to the decision owner rather than splitting it further.
## Worked example
Delegate “add one parser fixture and run parser tests,” not “make parsing robust.”
