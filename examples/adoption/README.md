# Selective adoption fixture

This authored fixture copies exactly the declared discovery, maintenance,
evaluation, and continuity assets into a temporary recipient. It then runs each
copied CLI against recipient-owned, authored artifacts: memory health, a loop
snapshot, receipt accounting, and a persisted fixed Next. The persisted Next is
matched literally and implemented by this fixture; no arbitrary checkpoint text
is executed.

Run from the collection root: `python3 -B examples/adoption/run.py`.

The output is adoption evidence for copied offline tools, not real-agent,
provider, plugin, or native-harness evidence. The fixture neither installs nor
discovers a harness, and it leaves user projects untouched.
