# Continuity

`journal.py` is a standalone Python 3.11+ stdlib CLI. It stores a project-local journal under `.ai-continuity/`; it does not invoke workers, models, shells, or networks.

```text
python3 journal.py start --target PROJECT --job worker-1
python3 journal.py finish --target PROJECT --job worker-1 --result artifact-reference
python3 journal.py checkpoint --target PROJECT --state "artifact checked" --evidence artifact-reference --next "write checked summary"
python3 journal.py resume --target PROJECT --json
```

Result and evidence values are opaque references. `ready` certifies journal structure only, not artifact truth. The journal never executes `Next`; a caller must use a task-specific, fixed dispatch and independently verify its output. See the deterministic fixture in `examples/continuity/`.
