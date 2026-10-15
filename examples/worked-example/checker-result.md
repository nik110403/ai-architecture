# Checker record — deterministic fixture

Artifact: [parser.py](parser.py). Checker: [test_parser.py](tests/test_parser.py),
with explicit expected values and rejection cases, not a copy of parser logic.
Reproduce from collection root: `python3 -B -m unittest discover -s examples/worked-example/tests -v`.
Expected output: five tests, OK. Actual candidate execution evidence belongs in
[COVERAGE](../../COVERAGE.md); no historical “Ran 2 tests” claim is made.
Scope: the fixture criteria only. This is not an independent AI judge verdict.
