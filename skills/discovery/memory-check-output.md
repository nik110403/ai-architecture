# Memory health report template

```text
memory-check: complete
root: <resolved-root>
files: <count>
drift: <count>
info: <count>
unsupported: <count>
inaccessible: <count>
```

Findings are emitted in stable lexical order as `severity | check | relative-path | detail`.
`--strict` changes only a report with drift into exit status 1. Unsupported and inaccessible
entries make the scan `incomplete`; they are not drift and never certify health.
