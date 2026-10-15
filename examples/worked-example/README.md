# Parser worked example — deterministic fixture

The [plan](plan.md), [brief](builder-brief.md), [builder record](builder-result.md),
[checker record](checker-result.md), and [Resume](Resume.md) show a small handoff
around the real [parser](parser.py) and [checker tests](tests/test_parser.py).
These records are authored fixture documents, not a historical AI-worker transcript
or an independent AI verdict.

Run from the collection root:

```sh
python3 -B -m unittest discover -s examples/worked-example/tests -v
```

Expected result: five tests, OK. They check empty/whitespace input, valid signed
integers, malformed nonempty input, nontext errors, and separate returned lists.
The accepted nonempty example `2, 3,5` produces `[2, 3, 5]`. The fixture does not
cover a general-purpose parser or imply a change was merged into a real project.
