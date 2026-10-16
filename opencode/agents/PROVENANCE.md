# Provenance

Sources named `claude_improvement/bin/...` are files from the author's operator repository; they are not shipped.
`scripts/check_collection.py` validates this file.

## opencode-agents-sync

Kit file: agents/opencode-agents-sync
Source: claude_improvement/bin/opencode-agents-sync
Source-sha256: 98d389cc96014fb34e9eb451853f56c597cec6048768804b36789016b32d15dc
State: re-authored

Deltas:
- the source inlines a role contract and canonical bodies into per-agent stub files; this generator instead reads the collection's role files and prints one `agent` block for the OpenCode config file
- models are the placeholder `provider/model-name`; no accounts, pools or quotas
- bash script embedding a Python program replaced by a stand-alone Python program (standard library only)
