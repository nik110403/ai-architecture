# Continuity fixture

Run `python3 run.py`. The fixture creates a temporary project, registers a worker, creates and separately checks an artifact, records its result reference, checkpoints nonempty state/evidence/Next, then starts fresh processes to read the checkpoint, dispatch one fixed supported Next, and independently verify the output. It never executes arbitrary `Next` text.
