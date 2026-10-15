# Builder record — authored fixture

Artifacts: [parser.py](parser.py) implements the shared validation;
[test_parser.py](tests/test_parser.py) supplies five deterministic checks.
Reproduce from collection root: `python3 -B -m unittest discover -s examples/worked-example/tests -v`.
Expected output: five tests, OK; this expectation is not a past execution receipt.
Observed candidate runs are recorded in [COVERAGE](../../COVERAGE.md).
No AI builder dispatch or production integration is claimed.
