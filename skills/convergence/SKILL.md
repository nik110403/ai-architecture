---
name: convergence
description: Run stakes-scaled independent review, refutation, and bounded repair convergence. Use for a frozen artifact with material risk.
---
# Convergence
## Objective
Separate maker, checker, and refuter decisions while preventing unbounded review loops.
## Steps
1. Freeze artifact identity, acceptance criteria, stakes, and evidence.
2. Select distinct lenses: correctness always; add security, domain, or user-impact only when relevant.
3. Each checker records evidence independently; unavailable seats remain missing, never silently removed.
4. Give one concrete finding to a refuter. The refuter tests it and does not arbitrate unrelated findings.
5. Allow at most two repair-review rounds; then PASS, park risk, descope, or stop.
## Output and done check
Use the bundled `templates/panel-review.md` record described in [DEPENDENCIES.md](DEPENDENCIES.md). Done is a traceable disposition with all absent lenses disclosed.
## Failure handling
Changed artifacts invalidate review. Severe or unresolved findings stop release; no role fixes its own review.
## Worked example
A data-export change gets correctness and privacy lenses; a missing privacy reviewer blocks the high-stakes release rather than reducing the panel.
