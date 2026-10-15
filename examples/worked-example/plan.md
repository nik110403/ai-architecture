# Plan: parser input validation — fixture

Outcome: a tiny comma-separated signed-integer parser with clear input errors.
Non-goal: a general parser or integration into a recipient project.
Acceptance: empty/whitespace raises ValueError; nontext raises TypeError;
`2, 3,5` yields `[2, 3, 5]`; malformed tokens are rejected.
Item: implement [parser.py](parser.py) and check it with [test_parser.py](tests/test_parser.py).
Check from collection root: `python3 -B -m unittest discover -s examples/worked-example/tests -v`.
Next: a human runs the fixture and inspects its boundary cases before adoption.
