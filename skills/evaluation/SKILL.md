---
name: evaluation
description: Define a small, reproducible measurement for a claim before optimizing or comparing approaches. Use when success needs evidence beyond a passing build.
---
# Evaluation
## Objective
Convert a claim into a falsifiable measurement with a fixed input and interpretation rule.
## Steps
1. State the decision the measurement will inform.
2. Choose a representative fixture, metric, baseline, and pass/fail interpretation.
3. Keep data provenance and command line with the result.
4. Run the measurement deterministically where possible.
5. Record limitations and follow-up using [measurement.md](measurement.md).
## Output and done check
Done means a different reader can reproduce the result from named inputs and command.
## Failure handling
If the fixture is unrepresentative or metric is ambiguous, label the result exploratory and do not use it as a release verdict.
## Worked example
Compare parser output against three named fixtures and require zero unexpected fields, rather than reporting “looks correct.”
