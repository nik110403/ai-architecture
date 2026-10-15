---
name: design
description: Capture interface intent, states, and accessibility constraints before frontend implementation. Use for UI work where visual choices are not already decided.
---
# Design
## Objective
Create a compact design brief that lets an implementer build intentional UI rather than generic screens.
## Steps
1. Identify users, primary task, content priority, and product register.
2. Specify states: loading, empty, error, success, keyboard, and narrow viewport where relevant.
3. Name visual tokens or existing system constraints; do not invent a brand from placeholders.
4. Define accessibility expectations and acceptance screenshots or interactions.
5. Save the brief using [design-brief.md](design-brief.md).
## Output and done check
Done means a UI builder can implement the stated states and a critic can test the experience.
## Failure handling
If taste, branding, or architecture is undecided, record a decision request rather than pretending a brief is final.
## Worked example
For a search panel: focus lands in the query field, empty results explain the filter, and Escape clears only an open suggestion list.
